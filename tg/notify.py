"""Desktop notifications for new messages in allowed chats.

Modes: ``sound`` (popup + sound), ``silent`` (popup only), ``off`` (nothing).
Linux uses ``notify-send`` plus a freedesktop sound; macOS uses ``osascript``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

MODES = ("sound", "silent", "off")
MODE_LABEL = {"sound": "🔔 sound", "silent": "🔕 silent", "off": "🚫 off"}
SOUND_FILES = (
    Path("/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"),
    Path("/usr/share/sounds/freedesktop/stereo/message.oga"),
)


def check_mode(mode: str) -> str:
    mode = mode.strip().lower()
    if mode not in MODES:
        raise ValueError(f"notifications mode must be one of {', '.join(MODES)}, not {mode!r}")
    return mode


def _spawn(cmd: list[str]) -> None:
    try:
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def _play_sound() -> None:
    sound = next((p for p in SOUND_FILES if p.is_file()), None)
    if sound is None:
        return
    for player in ("pw-play", "paplay"):
        if shutil.which(player):
            _spawn([player, str(sound)])
            return


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Notifier:
    def __init__(self, mode: str = "silent"):
        self.mode = check_mode(mode)

    def cycle(self) -> str:
        """sound → silent → off → sound; returns the new mode."""
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        return self.mode

    @property
    def label(self) -> str:
        return MODE_LABEL[self.mode]

    def send(self, title: str, body: str) -> None:
        if self.mode == "off":
            return
        sound = self.mode == "sound"
        if sys.platform == "darwin":
            script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
            if sound:
                script += ' sound name "Glass"'
            _spawn(["osascript", "-e", script])
            return
        if not shutil.which("notify-send"):
            return
        body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # suppress-sound asks the notification daemon not to play its own sound.
        _spawn(["notify-send", "-a", "telegram-vim", "-h", "boolean:suppress-sound:true", "--", title, body])
        if sound:
            _play_sound()
