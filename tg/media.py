"""Media helpers: attachment metadata, download paths, opening files, clipboard.

Nothing here talks to Telegram directly; uploads/downloads go through
``GuardedClient.send_file`` / ``GuardedClient.download``.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telethon.tl import types

# --------------------------------------------------------------------------- #
# Attachment metadata (works for raw types.Message and Telethon's Message)
# --------------------------------------------------------------------------- #


@dataclass
class Attachment:
    kind: str  # photo | video | voice | audio | sticker | gif | file | location | contact | poll | media
    name: str | None = None
    size: int | None = None
    duration: int | None = None
    extra: str | None = None  # sticker emoji, poll question, contact name, audio title
    openable: bool = False
    ext: str = ""


def human_size(n: int | None) -> str:
    if not n:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit in ("B", "KB") else f"{size:.1f}{unit}"
        size /= 1024
    return ""


def human_duration(sec: int | float | None) -> str:
    if not sec:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _photo_size(photo: Any) -> int | None:
    best = 0
    for s in getattr(photo, "sizes", None) or []:
        if isinstance(s, types.PhotoSizeProgressive):
            best = max(best, max(s.sizes or [0]))
        else:
            best = max(best, getattr(s, "size", 0) or 0)
    return best or None


def attachment(msg: Any) -> Attachment | None:
    media = getattr(msg, "media", None)
    if media is None or isinstance(media, (types.MessageMediaEmpty, types.MessageMediaWebPage)):
        return None
    if isinstance(media, types.MessageMediaPhoto):
        return Attachment("photo", size=_photo_size(media.photo), openable=media.photo is not None, ext=".jpg")
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        if not isinstance(doc, types.Document):
            return Attachment("file")
        att = Attachment("file", size=doc.size, openable=True)
        att.ext = mimetypes.guess_extension(doc.mime_type or "") or ""
        for a in doc.attributes:
            if isinstance(a, types.DocumentAttributeFilename):
                att.name = a.file_name
                att.ext = Path(a.file_name).suffix or att.ext
            elif isinstance(a, types.DocumentAttributeVideo):
                att.kind = "video"
                att.duration = a.duration
            elif isinstance(a, types.DocumentAttributeAudio):
                att.kind = "voice" if a.voice else "audio"
                att.duration = a.duration
                if a.title:
                    att.extra = f"{a.performer} – {a.title}" if a.performer else a.title
            elif isinstance(a, types.DocumentAttributeSticker):
                att.kind = "sticker"
                att.extra = a.alt
            elif isinstance(a, types.DocumentAttributeAnimated):
                att.kind = "gif"
        if att.kind == "voice" and not att.ext:
            att.ext = ".ogg"
        return att
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        return Attachment("location", extra=getattr(media, "title", None))
    if isinstance(media, types.MessageMediaContact):
        return Attachment("contact", extra=f"{media.first_name} {media.last_name}".strip())
    if isinstance(media, types.MessageMediaPoll):
        q = media.poll.question
        return Attachment("poll", extra=getattr(q, "text", q))
    return Attachment("media")


def attachment_label(msg: Any) -> str | None:
    """``[photo 1.2MB]``, ``[file report.pdf 2.3MB]``, ``[voice 0:12]`` …"""
    att = attachment(msg)
    if att is None:
        return None
    parts = [att.kind]
    if att.kind == "file" and att.name:
        parts.append(att.name)
    if att.extra:
        parts.append(att.extra)
    if att.duration:
        parts.append(human_duration(att.duration))
    if att.size and att.kind not in ("sticker", "voice"):
        parts.append(human_size(att.size))
    return "[" + " ".join(parts) + "]"


# --------------------------------------------------------------------------- #
# Downloads and opening
# --------------------------------------------------------------------------- #

_UNSAFE = re.compile(r"[^\w.\- ]+")


def download_path(download_dir: Path, chat_id: int, msg: Any) -> Path | None:
    """Deterministic location so a second open doesn't re-download."""
    att = attachment(msg)
    if att is None or not att.openable:
        return None
    if att.name:
        name = _UNSAFE.sub("_", att.name).strip() or "file"
        fname = f"{msg.id}_{name}"
    else:
        fname = f"{msg.id}_{att.kind}{att.ext}"
    return Path(download_dir).expanduser() / str(chat_id) / fname


def open_file(path: Path) -> None:
    """Open with the desktop's default app, detached from the terminal."""
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    else:
        cmd = ["xdg-open", str(path)]
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #


@dataclass
class ClipboardFiles:
    paths: list[Path] = field(default_factory=list)
    is_temp: bool = False  # image saved from clipboard bytes

    def describe(self) -> str:
        if len(self.paths) == 1:
            p = self.paths[0]
            label = "image" if self.is_temp else p.name
            try:
                return f"{label} ({human_size(p.stat().st_size)})"
            except OSError:
                return label
        return f"{len(self.paths)} files"


def parse_uri_list(text: str) -> list[Path]:
    """Paths from ``text/uri-list`` (what file managers put on the clipboard)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        u = urlparse(line)
        if u.scheme == "file":
            out.append(Path(unquote(u.path)))
    return [p for p in out if p.is_file()]


def parse_path_text(text: str) -> list[Path]:
    """Plain text that is one or more existing file paths (one per line)."""
    lines = [ln.strip().strip("'\"") for ln in text.strip().splitlines() if ln.strip()]
    if not lines or len(lines) > 10:
        return []
    paths = [Path(ln).expanduser() for ln in lines]
    return paths if all(p.is_file() for p in paths) else []


def _run(cmd: list[str], binary: bool = False) -> bytes | str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode(errors="replace")


def _backend() -> str | None:
    if sys.platform == "darwin":
        return "mac"
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return "wayland"
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return "x11"
    return None


def clipboard_help() -> str:
    return "clipboard needs wl-clipboard (Wayland) or xclip (X11)" if sys.platform != "darwin" else ""


def read_clipboard_files(tmp_dir: Path) -> ClipboardFiles | None:
    """Return files to send from the clipboard: copied files, an image, or a path."""
    backend = _backend()
    if backend is None:
        return None

    if backend == "wayland":
        list_types = ["wl-paste", "--list-types"]
        get = lambda t: ["wl-paste", "--no-newline", "--type", t]  # noqa: E731
    elif backend == "x11":
        list_types = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
        get = lambda t: ["xclip", "-selection", "clipboard", "-t", t, "-o"]  # noqa: E731
    else:  # macOS: file paths via pbpaste, images via pngpaste if installed
        text = _run(["pbpaste"]) or ""
        paths = parse_path_text(text)
        if paths:
            return ClipboardFiles(paths)
        if shutil.which("pngpaste"):
            return _save_image(tmp_dir, ".png", lambda dest: _run(["pngpaste", str(dest)]) is not None)
        return None

    mime_types = (_run(list_types) or "").split()
    if "text/uri-list" in mime_types:
        paths = parse_uri_list(_run(get("text/uri-list")) or "")
        if paths:
            return ClipboardFiles(paths)
    images = [t for t in mime_types if t.startswith("image/")]
    if images:
        mime = "image/png" if "image/png" in images else images[0]
        data = _run(get(mime), binary=True)
        if data:
            ext = mimetypes.guess_extension(mime) or ".png"
            return _save_image(tmp_dir, ext, lambda dest: dest.write_bytes(data) is not None)
    for t in ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING"):
        if t in mime_types:
            paths = parse_path_text(_run(get(t)) or "")
            if paths:
                return ClipboardFiles(paths)
            break
    return None


def _save_image(tmp_dir: Path, ext: str, writer) -> ClipboardFiles | None:
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = tmp_dir / f"clipboard-{int(time.time() * 1000)}{ext}"
    if not writer(dest) or not dest.exists() or dest.stat().st_size == 0:
        return None
    return ClipboardFiles([dest], is_temp=True)


def copy_text(text: str) -> bool:
    backend = _backend()
    cmd = {
        "wayland": ["wl-copy"],
        "x11": ["xclip", "-selection", "clipboard"],
        "mac": ["pbcopy"],
    }.get(backend or "")
    if not cmd:
        return False
    try:
        subprocess.run(cmd, input=text.encode(), timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- #
# Voice recording (ffmpeg → OGG/Opus, the format Telegram uses for voice notes)
# --------------------------------------------------------------------------- #


class VoiceRecorder:
    """Records the default microphone with ffmpeg until ``stop()`` or ``cancel()``."""

    def __init__(self, dest: Path, source: str = "default"):
        self.dest = Path(dest)
        self.source = source
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0

    @staticmethod
    def available() -> bool:
        return shutil.which("ffmpeg") is not None

    @staticmethod
    def help() -> str:
        return "voice recording needs ffmpeg with libopus and PipeWire/PulseAudio"

    def start(self) -> None:
        self.dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backend = ["-f", "avfoundation", "-i", f":{self.source if self.source != 'default' else 0}"] \
            if sys.platform == "darwin" else ["-f", "pulse", "-i", self.source]
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *backend,
             "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k",
             "-application", "voip", "-y", str(self.dest)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.started_at = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.proc else 0.0

    def stop(self) -> float:
        """Finish the file cleanly; returns the recorded duration in seconds."""
        duration = self.elapsed
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")  # ffmpeg's graceful quit: finalizes the ogg
                self.proc.stdin.flush()
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.proc = None
        return duration

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc = None
        self.dest.unlink(missing_ok=True)
