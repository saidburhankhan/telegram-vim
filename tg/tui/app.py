"""Interactive Textual app with vim-style modal keys.

Modes are expressed through focus: when a list is focused you're in NORMAL
mode and letter keys are commands; when the input box is focused you're typing
(INSERT / REPLY / EDIT / SEARCH / ATTACH caption). Esc always steps back one level.

All Telegram access goes through ``GuardedClient``; the app never sees the raw
Telethon client, so it can only reach chats in the configured folder.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.suggester import Suggester
from textual.widgets import Footer, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from telethon import utils

from .. import media, render
from ..guard import ChatInfo, GuardedClient, GuardError
from ..notify import Notifier
from .search import filter_chats

HISTORY_PAGE = 50
HISTORY_KEEP = 1000  # G trims history loaded beyond this back down
MAX_JUMP_GAP = 1000  # how far back we'll load to reach a replied/searched message
MEMBERS_LIMIT = 200
MENTION_RE = re.compile(r"(?:^|\s)@(\w*)$")
MIN_VOICE_SECONDS = 0.7


# --------------------------------------------------------------------------- #
# Chat list
# --------------------------------------------------------------------------- #


def chat_row(c: ChatInfo) -> Table:
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)

    style = "bold" if c.unread else ""
    icon = {"user": "👤", "bot": "🤖", "channel": "📢", "self": "🔖"}.get(c.kind, "👥")
    right = Text(render.short_time(c.last_date), style="dim")
    if c.unread_mentions:
        right.append("  ")
        right.append(f" @{c.unread_mentions} ", style="bold white on red")
    if c.unread:
        right.append("  ")
        right.append(f" {c.unread} ", style="bold black on yellow")
    grid.add_row(Text(f"{icon} {c.name}", style=style), right)

    m = c.last_message
    if m is not None:
        line = Text("   ")
        if getattr(m, "out", False):
            ticks = render.read_ticks([m], c.read_outbox_max_id if c.kind not in ("channel", "self") else None)
            if ticks:
                line.append_text(Text.from_markup(ticks + " "))
            line.append("You: ", style="dim")
        elif getattr(m, "sender", None) is not None and c.kind == "group":
            line.append(f"{render.sender_name(m)}: ", style="dim")
        line.append(render.snippet(m), style="dim")
        grid.add_row(line, "")
    else:
        grid.add_row(Text("   no messages", style="dim italic"), "")
    return grid


class ChatOptionList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "Top", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("s,slash", "screen.search", "Search"),
        Binding("n", "app.cycle_notifications", "Notifications"),
        Binding("escape", "screen.clear_search", "Clear filter", show=False),
        Binding("question_mark", "app.help", "Help"),
        Binding("q", "app.quit", "Quit"),
    ]


class SearchInput(Input):
    BINDINGS = [Binding("escape", "screen.leave_search", "Done")]


class ChatListScreen(Screen):
    BINDINGS = [
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("ctrl+r", "reload", "Reload"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        yield SearchInput(placeholder="🔍 s or / to search chats  (fuzzy, or /regex)", id="search")
        yield Static("", id="status")
        yield ChatOptionList(id="chats")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_list()
        self.query_one("#chats").focus()

    def on_screen_resume(self) -> None:
        self.refresh_list()
        self.query_one("#chats").focus()

    @property
    def app_(self) -> "TelegramVim":
        return self.app  # type: ignore[return-value]

    def refresh_list(self) -> None:
        ol = self.query_one("#chats", OptionList)
        keep = ol.highlighted_option.id if ol.highlighted_option else None

        query = self.query_one("#search", Input).value
        chats, error = filter_chats(query, self.app_.chats.values())
        ol.clear_options()
        ol.add_options([Option(chat_row(c), id=str(c.peer_id)) for c in chats])

        total = len(self.app_.chats)
        unread = sum(1 for c in self.app_.chats.values() if c.unread)
        mentions = sum(c.unread_mentions for c in self.app_.chats.values())
        top = (f"[b]telegram-vim[/b] · folder [b]{escape(self.app_.folder)}[/b] · "
               f"{total} chats · {unread} unread · {self.app_.notifier.label}")
        if mentions:
            top += f" · [bold white on red] @{mentions} [/bold white on red]"
        self.query_one("#topbar", Static).update(top)
        status = self.query_one("#status", Static)
        if error:
            status.update(f"[red]{escape(error)}[/red]")
        elif query.strip() and not chats:
            status.update("[dim]no matching chats in this folder[/dim]")
        elif query.strip():
            status.update(f"[dim]{len(chats)} match(es) · Esc clears[/dim]")
        else:
            status.update("[dim]j/k move · Enter open · s search · ? help[/dim]")

        if chats:
            idx = next((i for i, c in enumerate(chats) if str(c.peer_id) == keep), 0) if keep else 0
            ol.highlighted = idx

    @on(Input.Changed, "#search")
    def _search_changed(self) -> None:
        self.query_one("#chats", OptionList).highlighted = None  # jump to best match
        self.refresh_list()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.action_open()

    @on(OptionList.OptionSelected, "#chats")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.app_.open_chat(int(event.option.id))

    def action_move(self, delta: int) -> None:
        ol = self.query_one("#chats", OptionList)
        if not ol.option_count:
            return
        cur = ol.highlighted if ol.highlighted is not None else -1
        ol.highlighted = max(0, min(ol.option_count - 1, cur + delta))

    def action_open(self) -> None:
        opt = self.query_one("#chats", OptionList).highlighted_option
        if opt is not None:
            self.app_.open_chat(int(opt.id))

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_leave_search(self) -> None:
        self.query_one("#chats").focus()

    def action_clear_search(self) -> None:
        self.query_one("#search", Input).value = ""

    async def action_reload(self) -> None:
        await self.app_.reload_chats()
        self.notify("Chat list reloaded")


# --------------------------------------------------------------------------- #
# Mentions
# --------------------------------------------------------------------------- #


@dataclass
class Member:
    user_id: int
    name: str
    username: str | None

    @property
    def token(self) -> str:
        """What gets typed into the box: @username, or @FirstName for users without one."""
        if self.username:
            return "@" + self.username
        first = re.sub(r"\W+", "", self.name.split(" ")[0]) or f"user{self.user_id}"
        return "@" + first

    def matches(self, prefix: str) -> bool:
        p = prefix.casefold()
        if not p:
            return True
        if self.username and self.username.casefold().startswith(p):
            return True
        return any(w.casefold().startswith(p) for w in self.name.split())


def mention_prefix(value: str) -> str | None:
    m = MENTION_RE.search(value)
    return m.group(1) if m else None


class MentionSuggester(Suggester):
    """Shows the best @mention completion as ghost text (Tab accepts)."""

    def __init__(self, screen: "ChatScreen"):
        super().__init__(use_cache=False, case_sensitive=True)
        self._screen = screen

    async def get_suggestion(self, value: str) -> str | None:
        return self._screen.mention_completion(value)


# --------------------------------------------------------------------------- #
# Chat view
# --------------------------------------------------------------------------- #


class MessageList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("G", "screen.bottom", "Latest", show=False),
        Binding("i,a", "screen.insert", "Write"),
        Binding("r", "screen.reply", "Reply"),
        Binding("e", "screen.edit", "Edit"),
        Binding("d", "screen.delete", "Delete"),
        Binding("u", "screen.up_reply", "Replied msg", show=False),
        Binding("ctrl+o", "screen.jump_back", "Jump back", show=False),
        Binding("o", "screen.open_media", "Open", show=False),
        Binding("space", "screen.voice_key", "Hold: voice"),
        Binding("p", "screen.paste", "Paste file"),
        Binding("y", "screen.yank", "Copy"),
        Binding("v", "screen.view", "Select text", show=False),
        Binding("w", "screen.who_read", "Who read", show=False),
        Binding("s,slash", "screen.search", "Search"),
        Binding("n", "screen.next_match(1)", "Next match", show=False),
        Binding("N", "screen.next_match(-1)", "Prev match", show=False),
        Binding("question_mark", "app.help", "Help"),
        Binding("escape,q", "screen.back", "Back"),
    ]

    def action_cursor_up(self) -> None:
        if self.highlighted == 0:
            self.screen.run_worker(self.screen.action_older(), exclusive=True, group="older")  # type: ignore[attr-defined]
            return
        super().action_cursor_up()


class ComposeInput(Input):
    BINDINGS = [
        Binding("escape", "screen.normal", "Normal mode"),
        Binding("tab", "screen.complete_mention", "Complete @", show=False),
    ]


MODE_PLACEHOLDER = {
    "insert": "Message…  Enter send · @ to mention · Esc normal mode",
    "reply": "Reply…  Enter send · Esc cancel",
    "edit": "Edit message…  Enter save · Esc cancel",
    "search": "Search this chat…  Enter search · Esc cancel",
    "attach": "Caption (optional)…  Enter send · Esc cancel",
    "normal": "i write · r reply · e edit · d delete · p paste · y copy · ? help",
}
MODE_STYLE = {
    "normal": ("NORMAL", "reverse"), "insert": ("INSERT", "reverse green"),
    "reply": ("REPLY", "reverse cyan"), "edit": ("EDIT", "reverse blue"),
    "search": ("SEARCH", "reverse yellow"), "attach": ("ATTACH", "reverse magenta"),
}
TYPING_MODES = ("insert", "reply", "edit", "attach")


class ChatScreen(Screen):
    def __init__(self, chat: ChatInfo):
        super().__init__()
        self.chat = chat
        self.messages: list[Any] = []  # oldest first, contiguous
        self.items: list[list[Any]] = []  # messages grouped into albums; one option each
        self.by_id: dict[int, Any] = {}  # includes reply targets outside the list
        self._index: dict[int, int] = {}  # message id → index in self.items
        self.mode = "normal"
        self.reply_target: Any = None
        self.edit_target: Any = None
        self.attach: media.ClipboardFiles | None = None
        self.jumps: list[int] = []
        self.search_query = ""
        self.search_hits: list[int] = []  # newest first
        self.search_idx = -1
        self.new_below = 0
        self.mention_tokens: dict[str, Member] = {}
        self._members_loading = False
        self.rec: media.VoiceRecorder | None = None
        self.rec_state: str | None = None  # pending → hold (autorepeat seen) | toggle (it was a tap)
        self._last_space = 0.0
        self._rec_timer = None

    @property
    def app_(self) -> "TelegramVim":
        return self.app  # type: ignore[return-value]

    @property
    def read_max(self) -> int | None:
        return None if self.chat.kind in ("channel", "self") else self.chat.read_outbox_max_id

    # ---- layout ----------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        uname = f" [dim]@{self.chat.username}[/dim]" if self.chat.username else ""
        yield Static(f"[b]{escape(self.chat.name)}[/b]{uname}  [dim]({self.chat.kind})[/dim]", id="topbar")
        yield MessageList(id="msgs")
        yield Static("", id="hint")
        yield Static("", id="mode")
        yield ComposeInput(placeholder=MODE_PLACEHOLDER["normal"], id="compose",
                           suggester=MentionSuggester(self), select_on_focus=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hint").display = False
        self.set_mode("normal")
        self._status("[dim]loading…[/dim]")
        self.run_worker(self._load(), exclusive=True)

    @property
    def msglist(self) -> MessageList:
        return self.query_one("#msgs", MessageList)

    @property
    def input(self) -> ComposeInput:
        return self.query_one("#compose", ComposeInput)

    def _status(self, text: str) -> None:
        self.query_one("#mode", Static).update(text)

    def _hint(self, text: str) -> None:
        hint = self.query_one("#hint", Static)
        hint.update(text)
        hint.display = bool(text)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.input.placeholder = MODE_PLACEHOLDER[mode]
        label, style = MODE_STYLE[mode]
        extra = ""
        if mode == "reply" and self.reply_target is not None:
            t = self.reply_target
            extra = f" ↳ {escape(render.sender_name(t))}: {escape(render.snippet(t))}"
        elif mode == "edit" and self.edit_target is not None:
            extra = f" ✎ #{self.edit_target.id}"
        elif mode == "attach" and self.attach is not None:
            extra = f" 📎 {escape(self.attach.describe())}"
            if self.reply_target is not None:
                extra += f" ↳ reply to #{self.reply_target.id}"
        elif mode == "normal" and self.search_hits:
            extra = (f" match {self.search_idx + 1}/{len(self.search_hits)} for "
                     f"'{escape(self.search_query)}' · n/N next/prev")
        elif mode == "normal" and self.new_below:
            extra = f" ↓ {self.new_below} new message(s) · G to jump"
        self._status(f"[{style}] {label} [/{style}]{extra}")
        if mode not in TYPING_MODES:
            self._hint("")
        if mode == "normal":
            self.msglist.focus()
        else:
            self.input.focus()

    # ---- rendering -------------------------------------------------------- #

    def _option(self, item: list[Any]) -> Option:
        markup = render.format_message(item[0], self.by_id, read_max=self.read_max, album=item)
        return Option(Text.from_markup(markup), id=f"m{item[0].id}")

    def _item_key(self, item: list[Any]) -> tuple:
        """Identifies a rendered item; a reply whose target has since loaded re-renders."""
        rid = next((r for m in item if (r := render.reply_to_id(m))), None)
        return tuple(m.id for m in item), rid is None or rid in self.by_id

    def _rebuild(self, select_id: int | None = None) -> None:
        ol = self.msglist
        # Reuse options for unchanged items: each caches its rendered visual, so
        # loading an older page only renders the new messages.
        old = {self._item_key(it): ol.get_option_at_index(i)
               for i, it in enumerate(self.items) if i < ol.option_count}
        self.items = render.group_albums(self.messages)
        ol.clear_options()
        ol.add_options([old.get(self._item_key(it)) or self._option(it) for it in self.items])
        self._index = {m.id: i for i, it in enumerate(self.items) for m in it}
        if not self.items:
            return
        if select_id is None or not self.select(select_id):
            ol.highlighted = len(self.items) - 1

    def _item_index(self, msg_id: int) -> int | None:
        return self._index.get(msg_id)

    def _rerender(self, idx: int) -> None:
        item = self.items[idx]
        markup = render.format_message(item[0], self.by_id, read_max=self.read_max, album=item)
        self.msglist.replace_option_prompt_at_index(idx, Text.from_markup(markup))

    def selected_item(self) -> list[Any] | None:
        idx = self.msglist.highlighted
        return self.items[idx] if idx is not None and 0 <= idx < len(self.items) else None

    def selected(self) -> Any:
        """The selected message; for an album, the one carrying the caption."""
        item = self.selected_item()
        if not item:
            return None
        return next((m for m in item if getattr(m, "message", "")), item[0])

    def select(self, msg_id: int) -> bool:
        idx = self._item_index(msg_id)
        if idx is None:
            return False
        self.msglist.highlighted = idx
        self.msglist.scroll_to_highlight()
        if idx == len(self.items) - 1 and self.new_below:
            self.new_below = 0
            if self.mode == "normal":
                self.set_mode("normal")
        return True

    # ---- loading ---------------------------------------------------------- #

    async def _load(self) -> None:
        try:
            msgs, by_id = await self.app_.gc.get_history(self.chat.peer_id, limit=HISTORY_PAGE)
        except GuardError as e:
            self.notify(str(e), severity="error")
            self.app.pop_screen()
            return
        except Exception as e:
            self.notify(f"could not load messages: {e}", severity="error")
            return
        self.messages, self.by_id = msgs, by_id
        self._rebuild()
        self.set_mode(self.mode)
        if not msgs:
            self._status("[dim]no messages yet — press i to write[/dim]")
        await self.app_.mark_read(self.chat, msgs[-1].id if msgs else None)

    async def action_older(self) -> None:
        if not self.messages:
            return
        first = self.messages[0].id
        try:
            older = await self.app_.gc.get_messages(self.chat.peer_id, limit=HISTORY_PAGE, offset_id=first)
        except Exception as e:
            self.notify(f"could not load older messages: {e}", severity="error")
            return
        if not older:
            self.notify("beginning of chat", timeout=2)
            return
        self._prepend(older)
        self._rebuild(select_id=first)
        self.msglist.action_cursor_up()

    def _prepend(self, newest_first: list[Any]) -> None:
        have = {x.id for x in self.messages}
        fresh = [m for m in newest_first if m.id not in have]
        fresh.reverse()
        for m in fresh:
            self.by_id[m.id] = m
        self.messages = fresh + self.messages

    async def ensure_loaded(self, msg_id: int) -> bool:
        """Load back far enough that ``msg_id`` is in the list."""
        if any(m.id == msg_id for m in self.messages):
            return True
        if not self.messages or msg_id > self.messages[-1].id:
            return False
        self._status("[dim]loading earlier messages…[/dim]")
        try:
            gap = await self.app_.gc.get_messages(
                self.chat.peer_id, limit=MAX_JUMP_GAP, offset_id=self.messages[0].id, min_id=msg_id - 1
            )
        except Exception as e:
            self.notify(f"could not load messages: {e}", severity="error")
            return False
        if not any(m.id == msg_id for m in gap):
            self.set_mode(self.mode)
            self.notify("that message is too far back or was deleted", severity="warning")
            return False
        self._prepend(gap)
        self._rebuild()
        self.set_mode(self.mode)
        return True

    async def jump_to(self, msg_id: int, record: bool = True) -> None:
        cur = self.selected()
        if await self.ensure_loaded(msg_id):
            if record and cur is not None and cur.id != msg_id:
                self.jumps.append(cur.id)
            self.select(msg_id)

    # ---- live updates ----------------------------------------------------- #

    async def add_message(self, msg: Any) -> None:
        if any(m.id == msg.id for m in self.messages[-20:]):
            return
        rid = render.reply_to_id(msg)
        if rid and rid not in self.by_id:
            try:
                for m in await self.app_.gc.get_messages_by_id(self.chat.peer_id, [rid]):
                    self.by_id[m.id] = m
            except Exception:
                pass
        ol = self.msglist
        following = ol.highlighted is None or ol.highlighted >= ol.option_count - 1
        self.messages.append(msg)
        self.by_id[msg.id] = msg
        gid = getattr(msg, "grouped_id", None)
        if gid and self.items and getattr(self.items[-1][0], "grouped_id", None) == gid:
            self.items[-1].append(msg)  # next photo of an album that's arriving
            self._index[msg.id] = len(self.items) - 1
            self._rerender(len(self.items) - 1)
        else:
            self.items.append([msg])
            self._index[msg.id] = len(self.items) - 1
            ol.add_option(self._option([msg]))
        if following or msg.out:
            ol.highlighted = ol.option_count - 1
            ol.scroll_to_highlight()
        else:
            self.new_below += 1
            if self.mode == "normal":
                self.set_mode("normal")

    def update_message(self, msg: Any) -> None:
        """An edit (ours or someone else's)."""
        for i, m in enumerate(self.messages):
            if m.id == msg.id:
                self.messages[i] = msg
                break
        else:
            return
        self.by_id[msg.id] = msg
        idx = self._item_index(msg.id)
        if idx is not None:
            self.items[idx] = [msg if m.id == msg.id else m for m in self.items[idx]]
            self._rerender(idx)

    def remove_messages(self, ids: list[int]) -> None:
        gone = set(ids)
        if not any(m.id in gone for m in self.messages):
            return
        idx = self.msglist.highlighted or 0
        self.messages = [m for m in self.messages if m.id not in gone]
        for i in gone:
            self.by_id.pop(i, None)
        self._rebuild()
        if self.items:
            self.msglist.highlighted = min(idx, len(self.items) - 1)

    def refresh_read(self, old_max: int) -> None:
        new_max = self.chat.read_outbox_max_id
        for i, it in enumerate(self.items):
            if it[0].out and any(old_max < m.id <= new_max for m in it):
                self._rerender(i)

    # ---- NORMAL-mode actions ---------------------------------------------- #

    def action_insert(self) -> None:
        self.reply_target = None
        self.set_mode("insert")

    def action_reply(self) -> None:
        sel = self.selected()
        if sel is None:
            return
        self.reply_target = sel
        self.set_mode("reply")

    def action_edit(self) -> None:
        sel = self.selected()
        if sel is None:
            return
        if not sel.out:
            self.notify("you can only edit your own messages", timeout=2)
            return
        self.edit_target = sel
        self.input.value = sel.message or ""
        self.input.cursor_position = len(self.input.value)
        self.set_mode("edit")

    def action_delete(self) -> None:
        item = self.selected_item()
        if not item:
            return
        ids = [m.id for m in item]
        what = "this message" if len(ids) == 1 else f"these {len(ids)} messages"
        who = "for everyone" if item[0].out or self.chat.kind == "user" else "(needs admin rights)"
        preview = render.snippet(self.selected())

        async def done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await self.app_.gc.delete_messages(self.chat.peer_id, ids, revoke=True)
            except Exception as e:
                self.notify(f"delete failed: {e}", severity="error")
                return
            self.remove_messages(ids)
            self.notify("deleted", timeout=1.5)

        self.app.push_screen(ConfirmScreen(f"Delete {what} {who}?\n\n[dim]{escape(preview)}[/dim]"), done)

    def action_search(self) -> None:
        self.input.value = self.search_query
        self.set_mode("search")

    def action_bottom(self) -> None:
        if not self.messages:
            return
        if len(self.messages) > HISTORY_KEEP:
            self._trim_history()
        self.select(self.messages[-1].id)

    def _trim_history(self) -> None:
        """Drop old pages after scrolling far back; k at the top reloads them."""
        start = len(self.messages) - HISTORY_KEEP
        gid = getattr(self.messages[start - 1], "grouped_id", None)
        while gid and getattr(self.messages[start], "grouped_id", None) == gid:
            start += 1  # don't split an album across the cut
        keep = self.messages[start:]
        needed = {m.id for m in keep} | {r for m in keep if (r := render.reply_to_id(m))}
        self.messages = keep
        self.by_id = {i: m for i, m in self.by_id.items() if i in needed}
        self._rebuild()

    def action_back(self) -> None:
        if self.rec is not None:
            self.cancel_recording()
            return
        self.app.pop_screen()

    def on_unmount(self) -> None:
        if self.rec is not None:
            self.rec.cancel()

    # ---- voice messages --------------------------------------------------- #
    #
    # Terminals don't report key *release*, only autorepeat while a key is held.
    # First Space starts recording. If autorepeat follows, we're in push-to-talk
    # and a gap in repeats means "released" → send. If no repeat arrives it was a
    # tap → hands-free mode until the next Space. Esc cancels either way.

    def action_voice_key(self) -> None:
        now = time.monotonic()
        if self.rec_state is None:
            if not media.VoiceRecorder.available():
                self.notify(media.VoiceRecorder.help(), severity="error")
                return
            dest = self.app_.tmp_dir / f"voice-{int(time.time() * 1000)}.ogg"
            self.rec = self.app_.make_recorder(dest)
            try:
                self.rec.start()
            except Exception as e:
                self.rec = None
                self.notify(f"could not start recording: {e}", severity="error")
                return
            self.rec_state = "pending"
            self._last_space = now
            self._rec_timer = self.set_interval(0.05, self._rec_tick)
            self._rec_status()
        elif self.rec_state in ("pending", "hold"):
            self.rec_state = "hold"  # autorepeat → key is being held
            self._last_space = now
        elif self.rec_state == "toggle":
            self.run_worker(self._finish_recording(), group="voice")

    def _rec_tick(self) -> None:
        if self.rec is None:
            return
        gap = time.monotonic() - self._last_space
        if self.rec_state == "hold" and gap > self.app_.voice_release_ms / 1000:
            self.run_worker(self._finish_recording(), group="voice")
            return
        if self.rec_state == "pending" and gap > self.app_.voice_tap_ms / 1000:
            self.rec_state = "toggle"
        self._rec_status()

    def _rec_status(self) -> None:
        if self.rec is None:
            return
        secs = self.rec.elapsed
        how = ("release Space to send" if self.rec_state in ("pending", "hold")
               else "tap Space to send")
        dot = "●" if int(secs * 2) % 2 == 0 else "○"
        self._status(f"[reverse red] {dot} REC {int(secs) // 60}:{int(secs) % 60:02d} [/reverse red]"
                     f" {how} · Esc cancel")

    def _stop_timer(self) -> None:
        if self._rec_timer is not None:
            self._rec_timer.stop()
            self._rec_timer = None

    def cancel_recording(self) -> None:
        self._stop_timer()
        if self.rec is not None:
            self.rec.cancel()
        self.rec, self.rec_state = None, None
        self.set_mode("normal")
        self.notify("recording cancelled", timeout=1.5)

    async def _finish_recording(self) -> None:
        rec = self.rec
        if rec is None or self.rec_state == "sending":
            return
        self.rec_state = "sending"
        self._stop_timer()
        duration = await asyncio.to_thread(rec.stop)
        self.rec = None
        self.rec_state = None
        if duration < MIN_VOICE_SECONDS or not rec.dest.exists() or rec.dest.stat().st_size == 0:
            rec.dest.unlink(missing_ok=True)
            self.set_mode("normal")
            self.notify("too short — hold Space while talking", timeout=2)
            return
        self._status(f"[dim]sending voice {media.human_duration(duration) or '0:01'}…[/dim]")
        try:
            sent = await self.app_.gc.send_voice(self.chat.peer_id, str(rec.dest), duration)
        except Exception as e:
            self.notify(f"voice send failed: {e} (kept at {rec.dest})", severity="error")
            self.set_mode("normal")
            return
        rec.dest.unlink(missing_ok=True)
        self.set_mode("normal")
        for m in sent if isinstance(sent, list) else [sent]:
            await self.add_message(m)
            self.app_.note_message(self.chat.peer_id, m)

    @on(OptionList.OptionSelected, "#msgs")
    async def _enter(self, event: OptionList.OptionSelected) -> None:
        item = self.selected_item()
        if not item:
            return
        if any((a := media.attachment(m)) is not None and a.openable for m in item):
            await self.action_open_media()
        elif render.reply_to_id(item[0]):
            await self.action_up_reply()
        else:
            self.notify("r reply · e edit · y copy · v select text", timeout=2)

    async def action_up_reply(self) -> None:
        sel = self.selected()
        rid = render.reply_to_id(sel) if sel is not None else None
        if not rid:
            self.notify("this message isn't a reply", timeout=2)
            return
        await self.jump_to(rid)

    async def action_jump_back(self) -> None:
        if not self.jumps:
            self.notify("no earlier position", timeout=2)
            return
        await self.jump_to(self.jumps.pop(), record=False)

    async def action_open_media(self) -> None:
        item = self.selected_item() or []
        targets = [(m, d) for m in item
                   if (d := media.download_path(self.app_.download_dir, self.chat.peer_id, m)) is not None]
        if not targets:
            self.notify("nothing to open in this message", timeout=2)
            return
        for m, dest in targets:  # albums open every item
            self.run_worker(self._download_and_open(m, dest), group="download")

    async def _download_and_open(self, msg: Any, dest: Path) -> None:
        path: Path | None = dest if dest.exists() and dest.stat().st_size > 0 else None
        if path is None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.notify(f"downloading {escape(media.attachment_label(msg) or '')}…", timeout=3)
            try:
                got = await self.app_.gc.download(self.chat.peer_id, msg.id, str(dest))
            except Exception as e:
                self.notify(f"download failed: {e}", severity="error")
                return
            if not got:
                self.notify("nothing to download", severity="warning")
                return
            path = Path(got)
        try:
            media.open_file(path)
        except Exception as e:
            self.notify(f"saved to {path} but could not open it: {e}", severity="warning")
            return
        self.notify(f"opened {path.name}", timeout=2)

    async def action_paste(self) -> None:
        files = await asyncio.to_thread(media.read_clipboard_files, self.app_.tmp_dir)
        if files is None:
            hint = media.clipboard_help()
            self.notify("clipboard has no image or file" + (f" ({hint})" if hint else ""), severity="warning")
            return
        self.attach = files
        self.input.value = ""
        self.set_mode("attach")

    def action_yank(self) -> None:
        item = self.selected_item()
        if not item:
            return
        text = "\n".join(render.body_text(m) for m in item if render.body_text(m))
        self.app_.copy(text)

    def action_view(self) -> None:
        item = self.selected_item()
        if not item:
            return
        head = f"{render.sender_name(item[0])} · {render.local_time(item[0].date)} · #{item[0].id}"
        body = "\n".join(render.body_text(m) for m in item if render.body_text(m))
        self.app.push_screen(MessageViewScreen(head, body))

    async def action_who_read(self) -> None:
        sel = self.selected()
        if sel is None:
            return
        if not sel.out:
            self.notify("read receipts are only for your own messages", timeout=2)
            return
        if self.chat.kind in ("channel", "self"):
            self.notify("no read receipts here", timeout=2)
            return
        read = self.read_max is not None and sel.id <= self.read_max
        gc = self.app_.gc
        try:
            if self.chat.kind in ("user", "bot"):
                date = await gc.outbox_read_date(self.chat.peer_id, sel.id)
                body = (f"Read at [b]{render.local_time(date)}[/b]" if date
                        else ("Read (time hidden)" if read else "Not read yet"))
            else:
                readers = await gc.read_participants(self.chat.peer_id, sel.id)
                if not readers:
                    body = "Nobody has read it yet" if not read else "Read, but Telegram didn't say by whom"
                else:
                    names = {mb.user_id: mb.name for mb in await self.members()}
                    readers = sorted(readers, key=lambda r: getattr(r, "date", None) or 0, reverse=True)
                    rows = [f"  {escape(names.get(r.user_id, str(r.user_id)))}  "
                            f"[dim]{render.local_time(getattr(r, 'date', None))}[/dim]" for r in readers]
                    body = f"Read by {len(readers)}:\n\n" + "\n".join(rows)
        except Exception as e:
            reason = str(e).split("(")[0].strip() or type(e).__name__
            body = f"{'✓✓ Read' if read else '✓ Not read yet'}\n\n[dim]Telegram won't give details: {escape(reason)}[/dim]"
        self.app.push_screen(InfoScreen(f"Message #{sel.id}", body))

    async def action_next_match(self, step: int) -> None:
        if not self.search_hits:
            self.notify("no search — press s", timeout=2)
            return
        self.search_idx = (self.search_idx + step) % len(self.search_hits)
        await self.jump_to(self.search_hits[self.search_idx])
        self.set_mode("normal")

    # ---- mentions --------------------------------------------------------- #

    async def members(self) -> list[Member]:
        cache = self.app_.members
        if self.chat.peer_id not in cache:
            try:
                users = await self.app_.gc.get_participants(self.chat.peer_id, limit=MEMBERS_LIMIT)
            except Exception:
                users = []
            cache[self.chat.peer_id] = [
                Member(u.id, utils.get_display_name(u) or str(u.id), getattr(u, "username", None))
                for u in users if not getattr(u, "is_self", False) and not getattr(u, "deleted", False)
            ]
        return cache[self.chat.peer_id]

    def mention_candidates(self, prefix: str) -> list[Member]:
        members = self.app_.members.get(self.chat.peer_id, [])
        hits = [m for m in members if m.matches(prefix)]
        return sorted(hits, key=lambda m: (not (m.username or "").casefold().startswith(prefix.casefold()),
                                           m.name.casefold()))[:6]

    def mention_completion(self, value: str) -> str | None:
        prefix = mention_prefix(value)
        if prefix is None:
            return None
        cands = self.mention_candidates(prefix)
        if not cands:
            return None
        return value[: len(value) - len(prefix) - 1] + cands[0].token + " "

    def action_complete_mention(self) -> None:
        value = self.input.value
        prefix = mention_prefix(value)
        comp = self.mention_completion(value)
        if comp is None or prefix is None:
            return
        best = self.mention_candidates(prefix)[0]
        if not best.username:
            self.mention_tokens[best.token] = best
        self.input.value = comp
        self.input.cursor_position = len(comp)

    @on(Input.Changed, "#compose")
    def _compose_changed(self, event: Input.Changed) -> None:
        if self.mode not in TYPING_MODES or self.chat.kind in ("user", "bot", "channel", "self"):
            self._hint("")
            return
        prefix = mention_prefix(event.value)
        if prefix is None:
            self._hint("")
            return
        if self.chat.peer_id not in self.app_.members:
            self._hint("[dim]loading members…[/dim]")
            if not self._members_loading:
                self._members_loading = True
                self.run_worker(self._load_members_then_hint(), group="members")
            return
        cands = self.mention_candidates(prefix)
        if not cands:
            self._hint("[dim]no matching members[/dim]")
            return
        parts = []
        for i, m in enumerate(cands):
            style = "bold cyan" if i == 0 else "cyan"
            extra = f" [dim]{escape(m.name)}[/dim]" if m.username else ""
            parts.append(f"[{style}]{escape(m.token)}[/{style}]{extra}")
        self._hint("  ".join(parts) + "   [dim]Tab to complete[/dim]")

    async def _load_members_then_hint(self) -> None:
        await self.members()
        self._members_loading = False
        self._compose_changed(Input.Changed(self.input, self.input.value))

    def _expand_mentions(self, text: str) -> str:
        """Turn @FirstName tokens for users without a username into real mentions."""
        for token, m in self.mention_tokens.items():
            if token in text:
                label = m.name.replace("[", "(").replace("]", ")")
                text = text.replace(token, f"[{label}](tg://user?id={m.user_id})", 1)
        self.mention_tokens.clear()
        return text

    # ---- input-mode handling ---------------------------------------------- #

    def action_normal(self) -> None:
        """Esc from the input box."""
        if self.mode in ("search", "edit"):
            self.input.value = ""
            self.edit_target = None
        elif self.mode == "attach":
            self._discard_attach()
            self.input.value = ""
        elif self.mode == "reply":
            self.reply_target = None
        self.set_mode("normal")

    def _discard_attach(self) -> None:
        if self.attach and self.attach.is_temp:
            for p in self.attach.paths:
                p.unlink(missing_ok=True)
        self.attach = None

    @on(Input.Submitted, "#compose")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if self.mode == "search":
            await self._run_search(text)
        elif self.mode == "attach":
            await self._send_attach(text)
        elif self.mode == "edit":
            await self._save_edit(text)
        elif text:
            if self.mode == "normal":
                self.set_mode("insert")
            await self._send_text(text)

    async def _send_text(self, text: str) -> None:
        reply_to = self.reply_target.id if self.mode == "reply" and self.reply_target else None
        self.input.value = ""
        self._hint("")
        try:
            sent = await self.app_.gc.send_message(self.chat.peer_id, self._expand_mentions(text), reply_to=reply_to)
        except Exception as e:
            self.input.value = text  # don't lose what they typed
            self.notify(f"send failed: {e}", severity="error")
            return
        await self.add_message(sent)
        self.app_.note_message(self.chat.peer_id, sent)
        if self.mode == "reply":
            self.reply_target = None
            self.set_mode("insert")

    async def _save_edit(self, text: str) -> None:
        target = self.edit_target
        if target is None:
            self.set_mode("normal")
            return
        if not text or text == (target.message or ""):
            self.action_normal()
            return
        try:
            edited = await self.app_.gc.edit_message(self.chat.peer_id, target.id, self._expand_mentions(text))
        except Exception as e:
            self.notify(f"edit failed: {e}", severity="error")
            return
        self.input.value = ""
        self.edit_target = None
        if edited is not None:
            self.update_message(edited)
        self.set_mode("normal")

    async def _send_attach(self, caption: str) -> None:
        files = self.attach
        if files is None:
            self.set_mode("normal")
            return
        reply_to = self.reply_target.id if self.reply_target else None
        self._status(f"[dim]uploading {escape(files.describe())}…[/dim]")
        paths = [str(p) for p in files.paths]
        try:
            sent = await self.app_.gc.send_file(
                self.chat.peer_id, paths if len(paths) > 1 else paths[0],
                caption=self._expand_mentions(caption) or None, reply_to=reply_to,
            )
        except Exception as e:
            self.notify(f"upload failed: {e}", severity="error")
            self.set_mode("attach")
            return
        self.input.value = ""
        self._discard_attach()
        self.reply_target = None
        for m in sent if isinstance(sent, list) else [sent]:
            await self.add_message(m)
            self.app_.note_message(self.chat.peer_id, m)
        self.set_mode("normal")

    async def _run_search(self, query: str) -> None:
        self.input.value = ""
        self.search_query = query
        self.search_hits, self.search_idx = [], -1
        if not query:
            self.set_mode("normal")
            return
        self._status(f"[dim]searching '{escape(query)}'…[/dim]")
        try:
            hits = await self.app_.gc.search_messages(self.chat.peer_id, query)
        except Exception as e:
            self.notify(f"search failed: {e}", severity="error")
            self.set_mode("normal")
            return
        if not hits:
            self.set_mode("normal")
            self.notify(f"no messages match '{query}'", timeout=2)
            return
        self.search_hits = [m.id for m in hits]
        self.set_mode("normal")
        await self.action_next_match(1)


# --------------------------------------------------------------------------- #
# Modals: confirm, info, message view (text selection), help
# --------------------------------------------------------------------------- #


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape,q", "answer(False)", "No"),
    ]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static("\n[b]y[/b] yes    [b]n[/b] / Esc no", classes="dim")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class InfoScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,enter,w", "app.pop_screen", "Close")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self.title_text, self.body = title, body

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/b]\n\n{self.body}\n\n[dim]Esc to close[/dim]")


class MessageViewScreen(ModalScreen):
    """Read-only text box: select with mouse drag or Shift+arrows, then copy."""

    BINDINGS = [
        Binding("y,ctrl+c", "copy", "Copy selection (or all)", priority=True),
        Binding("escape,q", "app.pop_screen", "Close"),
    ]

    def __init__(self, head: str, body: str):
        super().__init__()
        self.head, self.body = head, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Static(f"[b]{escape(self.head)}[/b]   [dim]select with mouse or Shift+arrows · "
                         f"y / Ctrl+C copy · Esc close[/dim]")
            yield TextArea(self.body, read_only=True, soft_wrap=True, id="viewer")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#viewer").focus()

    def action_copy(self) -> None:
        ta = self.query_one("#viewer", TextArea)
        text = ta.selected_text or ta.text
        self.app.copy(text)  # type: ignore[attr-defined]


HELP = """\
[b]Chat list[/b]
  j / k  ↓ / ↑     move
  Enter            open chat
  s  /             search chats (fuzzy; start with / for regex)
  Esc              leave search / clear filter
  Ctrl+R           reload
  n                notifications: sound → silent → off (Ctrl+N anywhere)
  q                quit
  [white on red] @2 [/] = unread mentions of you   [black on yellow] 5 [/] = unread

[b]Chat — NORMAL mode[/b]
  j / k  ↓ / ↑     move between messages (k at top loads older)
  G                jump to latest
  i  a             write a message (INSERT mode) — type @ to mention, Tab completes
  r                reply to selected message
  e                edit your message
  d                delete selected message (asks first)
  Enter            open photo/video/file · or jump to the message it replies to
  o                open photo/video/file (albums open every item)
  u                go to the replied-to message (repeat to climb)
  Ctrl+O           jump back to where you were
  s  /             search this chat · n / N next / previous match
  Space (hold)     record a voice message; release to send (tap = hands-free, tap again to send)
  p                paste from clipboard: copied image or file(s) → send
  y                copy message text
  v                open message to select part of the text (mouse or Shift+arrows)
  w                who read your message, and when
  Esc  q           back to chat list (while recording: cancel)

[b]Marks[/b]
  ✓ sent   [green]✓✓[/] read   [white on red] @you [/] you were mentioned   (edited)
  🖼 [magenta]photo[/]  🎬 [blue]video[/]  🎤 [yellow]voice[/]  📄 [green]file[/]

[b]Typing (INSERT / REPLY / EDIT / SEARCH / ATTACH)[/b]
  Enter            send / save / search
  Tab              complete @mention
  Esc              back to NORMAL mode (cancels reply, edit, search, attachment)

Mouse: hold Shift while dragging for normal terminal selection.
Ctrl+Q quits from anywhere.   Press Esc, q, or ? to close this help.\
"""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(HELP)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


class TelegramVim(App):
    TITLE = "telegram-vim"
    BINDINGS = [Binding("ctrl+n", "cycle_notifications", "Notifications", show=False, priority=True)]
    CSS = """
    #topbar { height: 1; padding: 0 1; background: $primary-background; }
    #search { margin: 0 1; }
    #status { height: 1; padding: 0 2; }
    #chats { height: 1fr; margin: 0 1; border: none; }
    #msgs { height: 1fr; border: none; padding: 0 1; }
    #hint { height: 1; padding: 0 1; background: $boost; }
    #mode { height: 1; padding: 0 1; }
    #compose { margin: 0 1; }
    HelpScreen, ConfirmScreen, InfoScreen, MessageViewScreen { align: center middle; }
    #help { width: 90; max-width: 100%; height: auto; max-height: 90%;
            border: round $primary; padding: 1 2; background: $surface; }
    .dialog { width: 64; max-width: 100%; height: auto; max-height: 80%;
              border: round $primary; padding: 1 2; background: $surface; }
    .dialog.wide { width: 100; height: 80%; }
    #viewer { height: 1fr; margin-top: 1; }
    """

    def __init__(self, gc: GuardedClient, folder: str, download_dir: Path | None = None,
                 tmp_dir: Path | None = None, voice_source: str = "default",
                 voice_release_ms: int = 250, voice_tap_ms: int = 700,
                 notifications: str = "silent"):
        super().__init__()
        self.notifier = Notifier(notifications)
        self.voice_source = voice_source
        self.voice_release_ms = voice_release_ms
        self.voice_tap_ms = voice_tap_ms
        self.gc = gc
        self.folder = folder
        self.download_dir = Path(download_dir or "~/Downloads/telegram-vim").expanduser()
        self.tmp_dir = Path(tmp_dir or "/tmp/telegram-vim").expanduser()
        self.chats: dict[int, ChatInfo] = {c.peer_id: c for c in gc.chats()}
        self.members: dict[int, list[Member]] = {}
        self._list_refresh_pending = False

    def on_mount(self) -> None:
        self.gc.on_new_message(self._on_tg_message)
        self.gc.on_message_edited(self._on_tg_edit)
        self.gc.on_message_deleted(self._on_tg_delete)
        self.gc.on_outbox_read(self._on_tg_read)
        self.gc.on_chats_changed(self._on_folder_changed)
        self.push_screen(ChatListScreen())

    def make_recorder(self, dest: Path) -> media.VoiceRecorder:
        return media.VoiceRecorder(dest, self.voice_source)

    def action_cycle_notifications(self) -> None:
        mode = self.notifier.cycle()
        detail = {"sound": "popup with sound", "silent": "popup, no sound", "off": "no popups"}[mode]
        self.notify(f"Notifications: {self.notifier.label} ({detail}) · until restart", timeout=2.5)
        self._refresh_list()

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def copy(self, text: str) -> None:
        """Copy via the system clipboard tool and the terminal (OSC 52)."""
        if not text:
            self.notify("nothing to copy", timeout=1.5)
            return
        ok = media.copy_text(text)
        try:
            self.copy_to_clipboard(text)
            ok = True
        except Exception:
            pass
        if ok:
            self.notify("copied", timeout=1.5)
        else:
            self.notify("could not copy (" + (media.clipboard_help() or "no clipboard") + ")", severity="warning")

    # ---- navigation ------------------------------------------------------- #

    def open_chat(self, peer_id: int) -> None:
        chat = self.chats.get(peer_id)
        if chat is None or not self.gc.guard.is_allowed(peer_id):
            self.notify("that chat is no longer in the folder", severity="warning")
            return
        self.push_screen(ChatScreen(chat))

    def _chat_screen(self) -> ChatScreen | None:
        for s in reversed(self.screen_stack):
            if isinstance(s, ChatScreen):
                return s
        return None

    def _list_screen(self) -> ChatListScreen | None:
        for s in self.screen_stack:
            if isinstance(s, ChatListScreen):
                return s
        return None

    def _refresh_list(self) -> None:
        """Redraw the chat list once per burst of events, and only while it's visible
        (it redraws itself on resume)."""
        ls = self._list_screen()
        if ls is None or not ls.is_mounted or self.screen is not ls or self._list_refresh_pending:
            return
        self._list_refresh_pending = True

        def run() -> None:
            self._list_refresh_pending = False
            if ls.is_mounted:
                ls.refresh_list()

        self.call_later(run)

    # ---- state updates ---------------------------------------------------- #

    def note_message(self, peer_id: int, msg: Any) -> None:
        chat = self.chats.get(peer_id)
        if chat is not None:
            chat.last_message = msg
            self._refresh_list()

    async def mark_read(self, chat: ChatInfo, max_id: int | None) -> None:
        try:
            await self.gc.mark_read(chat.peer_id, max_id=max_id)
        except Exception:
            return
        chat.unread = 0
        chat.unread_mentions = 0
        self._refresh_list()

    async def reload_chats(self) -> None:
        infos = await self.gc.refresh_chats()
        self._apply_chats(infos)

    def _apply_chats(self, infos: list[ChatInfo]) -> None:
        self.chats = {c.peer_id: c for c in infos}
        cs = self._chat_screen()
        if cs is not None and cs.chat.peer_id not in self.chats:
            while self._chat_screen() is not None:
                self.pop_screen()
            self.notify(f"{cs.chat.name} was removed from the folder", severity="warning")
        elif cs is not None:
            cs.chat = self.chats[cs.chat.peer_id]
        self._refresh_list()

    async def _on_folder_changed(self, infos: list[ChatInfo]) -> None:
        self._apply_chats(infos)
        self.notify("Folder updated")

    async def _on_tg_message(self, event: Any) -> None:
        msg = event.message
        try:
            await event.get_sender()
        except Exception:
            pass
        chat = self.chats.get(event.chat_id)
        if chat is None:
            # allowed (the guard already checked) but not in our list yet
            await self.reload_chats()
            chat = self.chats.get(event.chat_id)
            if chat is None:
                return
        chat.last_message = msg
        cs = self._chat_screen()
        viewing = cs is not None and cs.chat.peer_id == chat.peer_id
        # Pop up only when you can't already see it: another window has focus,
        # or you're inside a different chat.
        popup = not msg.out and (not self.app_focus or (cs is not None and not viewing))
        if popup:
            self._desktop_notify(chat, msg)
        if viewing:
            await cs.add_message(msg)
            if not msg.out:
                await self.mark_read(chat, msg.id)
        elif not msg.out:
            chat.unread += 1
            if getattr(msg, "mentioned", False):
                chat.unread_mentions += 1
                if not popup and self.notifier.mode != "off":
                    self.notify(f"@ {chat.name}: {render.snippet(msg)}", title="You were mentioned",
                                timeout=6)
        self._refresh_list()

    def _desktop_notify(self, chat: ChatInfo, msg: Any) -> None:
        title = chat.name
        if chat.kind == "group":
            title = f"{render.sender_name(msg)} · {chat.name}"
        if getattr(msg, "mentioned", False):
            title = "@ " + title
        self.notifier.send(title, render.snippet(msg))

    async def _on_tg_edit(self, event: Any) -> None:
        msg = event.message
        try:
            await event.get_sender()
        except Exception:
            pass
        cs = self._chat_screen()
        if cs is not None and cs.chat.peer_id == event.chat_id:
            cs.update_message(msg)
        chat = self.chats.get(event.chat_id)
        if chat is not None and getattr(chat.last_message, "id", None) == msg.id:
            chat.last_message = msg
            self._refresh_list()

    async def _on_tg_delete(self, event: Any) -> None:
        cs = self._chat_screen()
        if cs is None:
            return
        # Channel/supergroup deletions carry the chat id; private chats and small
        # groups send only ids, which are unique per account, so match the open chat.
        is_channel = str(cs.chat.peer_id).startswith("-100")
        if event.chat_id == cs.chat.peer_id or (event.chat_id is None and not is_channel):
            cs.remove_messages(list(event.deleted_ids))

    async def _on_tg_read(self, event: Any) -> None:
        chat = self.chats.get(event.chat_id)
        if chat is None or not event.max_id or event.max_id <= chat.read_outbox_max_id:
            return
        old = chat.read_outbox_max_id
        chat.read_outbox_max_id = event.max_id
        cs = self._chat_screen()
        if cs is not None and cs.chat.peer_id == chat.peer_id:
            cs.refresh_read(old)
        self._refresh_list()
