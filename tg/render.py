"""Message formatting for terminal output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich.markup import escape
from telethon import utils

from .media import attachment, attachment_label

MEDIA_STYLE = {
    "photo": "bold magenta",
    "video": "bold blue",
    "gif": "bold blue",
    "voice": "bold yellow",
    "audio": "bold yellow",
    "file": "bold green",
    "sticker": "dim",
    "location": "bold cyan",
    "contact": "bold cyan",
    "poll": "bold cyan",
}
MEDIA_ICON = {
    "photo": "🖼 ", "video": "🎬", "gif": "🎞 ", "voice": "🎤", "audio": "🎵",
    "file": "📄", "sticker": "🙂", "location": "📍", "contact": "👤", "poll": "📊",
}

SNIPPET_LEN = 60
INDENT = "  "


def sender_name(msg: Any) -> str:
    if getattr(msg, "out", False):
        return "You"
    sender = getattr(msg, "sender", None)
    if sender is not None:
        return utils.get_display_name(sender) or str(getattr(sender, "id", "?"))
    post_author = getattr(msg, "post_author", None)
    return post_author or "?"


def local_time(dt: datetime | None) -> str:
    if dt is None:
        return "--:--"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%Y-%m-%d %H:%M")


def short_time(dt: datetime | None) -> str:
    """Compact timestamp for the chat list: 14:05 · Mon · 03.09.25"""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    now = datetime.now().astimezone()
    days = (now.date() - local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if 0 < days < 7:
        return local.strftime("%a")
    return local.strftime("%d.%m.%y")


def body_text(msg: Any) -> str:
    parts = []
    label = attachment_label(msg)
    if label:
        parts.append(label)
    text = getattr(msg, "message", "") or ""
    if text:
        parts.append(text)
    if not parts and getattr(msg, "action", None) is not None:
        parts.append(f"<{type(msg.action).__name__.removeprefix('MessageAction')}>")
    return " ".join(parts)


def snippet(msg: Any) -> str:
    s = body_text(msg).replace("\n", " ")
    return s if len(s) <= SNIPPET_LEN else s[: SNIPPET_LEN - 1] + "…"


def reply_to_id(msg: Any) -> int | None:
    r = getattr(msg, "reply_to", None)
    return getattr(r, "reply_to_msg_id", None) if r else None


def group_albums(msgs: list[Any]) -> list[list[Any]]:
    """Consecutive messages sharing a ``grouped_id`` (an album) become one item."""
    items: list[list[Any]] = []
    for m in msgs:
        gid = getattr(m, "grouped_id", None)
        if gid and items and getattr(items[-1][0], "grouped_id", None) == gid:
            items[-1].append(m)
        else:
            items.append([m])
    return items


def is_edited(msg: Any) -> bool:
    return bool(getattr(msg, "edit_date", None)) and not getattr(msg, "edit_hide", False)


def read_ticks(msgs: list[Any], read_max: int | None) -> str:
    """✓ sent · ✓✓ read (by the other person, or by someone in a group)."""
    if read_max is None or not getattr(msgs[0], "out", False):
        return ""
    if max(m.id for m in msgs) <= read_max:
        return "[bold green]✓✓[/bold green]"
    return "[dim]✓[/dim]"


def media_line(msg: Any) -> str | None:
    label = attachment_label(msg)
    if label is None:
        return None
    att = attachment(msg)
    icon = MEDIA_ICON.get(att.kind, "📎")
    style = MEDIA_STYLE.get(att.kind, "bold")
    return f"{icon} [{style}]{escape(label)}[/{style}]"


def format_message(
    msg: Any,
    replies: dict[int, Any] | None = None,
    chat_name: str | None = None,
    read_max: int | None = None,
    album: list[Any] | None = None,
) -> str:
    """Rich-markup block for one message (or album), then a blank separator line:

        10:23  Alice  #99  (edited)  ✓✓
          ↳ reply to #98 Bob: can someone review?
          🖼 [photo 1.2MB]
          🎬 [video 0:32 4.1MB]
          standup at 10
    """
    items = album or [msg]
    first = items[0]
    out = getattr(first, "out", False)
    who = "bold green" if out else "bold cyan"
    head = f"[dim]{local_time(first.date)}[/dim]  "
    if chat_name:
        head += f"[magenta]{escape(chat_name)}[/magenta] › "
    ids = f"#{first.id}" if len(items) == 1 else f"#{first.id}–{items[-1].id}"
    head += f"[{who}]{escape(sender_name(first))}[/{who}]  [dim]{ids}[/dim]"
    if any(is_edited(m) for m in items):
        head += "  [dim italic](edited)[/dim italic]"
    ticks = read_ticks(items, read_max)
    if ticks:
        head += f"  {ticks}"
    if not out and any(getattr(m, "mentioned", False) for m in items):
        head += "  [bold white on red] @you [/bold white on red]"
    lines = [head]

    rid = next((r for m in items if (r := reply_to_id(m))), None)
    if rid:
        target = (replies or {}).get(rid)
        ctx = f"{escape(sender_name(target))}: {escape(snippet(target))}" if target else "(message not loaded)"
        lines.append(f"{INDENT}[dim]↳ reply to #{rid} {ctx}[/dim]")

    for m in items:
        ml = media_line(m)
        if ml:
            lines.append(INDENT + ml)
    texts = [t for m in items if (t := getattr(m, "message", "") or "")]
    if texts:
        for t in texts:
            lines.extend(INDENT + escape(line) for line in t.split("\n"))
    elif len(lines) == 1:
        action = getattr(first, "action", None)
        lines.append(INDENT + escape(f"<{type(action).__name__.removeprefix('MessageAction')}>" if action else "(empty)"))
    lines.append("")
    return "\n".join(lines)
