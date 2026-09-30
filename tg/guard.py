"""Folder-based allowlist: the single source of truth for which chats are reachable.

Everything that touches a chat goes through ``Guard.check(chat_id)``. That method
is the only way to obtain the ``InputPeer`` needed to talk to Telegram about a
chat, and ``GuardedClient`` is the only object the rest of the app uses to talk
to Telegram. The raw Telethon client is kept private inside ``GuardedClient``.

Allowlist rules:
  * Only chats explicitly added to the configured folder (pinned_peers +
    include_peers) are allowed. exclude_peers are subtracted for good measure.
  * Folders with category rules (contacts, non_contacts, groups, broadcasts,
    bots) are refused, because those can pull in personal chats.
  * On any doubt (folder vanished, rules added live) the guard fails closed.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from telethon import events, utils
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

log = logging.getLogger("tg.guard")

CATEGORY_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots")


class GuardError(Exception):
    """Base class for allowlist errors."""


class NotAllowedError(GuardError):
    """Raised when an action targets a chat that is not in the folder."""

    def __init__(self, chat_id: Any):
        super().__init__(f"chat {chat_id!r} is not in the allowed folder")
        self.chat_id = chat_id


class FolderNotFoundError(GuardError):
    pass


class CategoryRulesError(GuardError):
    pass


class ChatNotFoundError(GuardError):
    pass


class AmbiguousChatError(GuardError):
    pass


# --------------------------------------------------------------------------- #
# Pure parsing helpers (no network) — unit tested directly
# --------------------------------------------------------------------------- #


def title_text(title: Any) -> str:
    """Folder titles are ``str`` in older layers and ``TextWithEntities`` in newer."""
    if title is None:
        return ""
    text = getattr(title, "text", title)
    return str(text)


def iter_filters(result: Any) -> list[Any]:
    """GetDialogFiltersRequest returns a list (old) or ``messages.DialogFilters`` (new)."""
    if result is None:
        return []
    if isinstance(result, (list, tuple)):
        return list(result)
    filters = getattr(result, "filters", None)
    if filters is None:
        raise GuardError(f"unexpected GetDialogFilters result: {type(result).__name__}")
    return list(filters)


def _is_default(f: Any) -> bool:
    return isinstance(f, types.DialogFilterDefault)


def _is_supported(f: Any) -> bool:
    return isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))


def find_folder(filters: Iterable[Any], name: str) -> Any:
    wanted = name.strip().casefold()
    for f in filters:
        if _is_default(f) or not _is_supported(f):
            continue
        if title_text(getattr(f, "title", None)).strip().casefold() == wanted:
            return f
    raise FolderNotFoundError(f"no chat folder named {name!r} found")


def enabled_category_rules(f: Any) -> list[str]:
    # DialogFilterChatlist has no category flags; getattr covers that.
    return [flag for flag in CATEGORY_FLAGS if getattr(f, flag, None)]


def input_peer_id(peer: Any, self_id: int | None = None) -> int:
    if isinstance(peer, types.InputPeerSelf):
        if self_id is None:
            raise GuardError("folder contains Saved Messages but own user id is unknown")
        return self_id
    return utils.get_peer_id(peer)


def folder_peers(f: Any, self_id: int | None = None) -> dict[int, Any]:
    """Explicitly added chats of a folder, as ``{peer_id: InputPeer}``."""
    rules = enabled_category_rules(f)
    if rules:
        raise CategoryRulesError(
            "folder uses category rules (" + ", ".join(rules) + "), which can include "
            "personal chats. Remove those rules in Telegram (Settings → Folders → "
            "edit folder → Included chats) and add work chats explicitly."
        )
    peers: dict[int, Any] = {}
    for p in list(getattr(f, "pinned_peers", []) or []) + list(getattr(f, "include_peers", []) or []):
        peers[input_peer_id(p, self_id)] = p
    for p in getattr(f, "exclude_peers", []) or []:
        peers.pop(input_peer_id(p, self_id), None)
    return peers


def build_allowlist(result: Any, folder: str, self_id: int | None = None) -> dict[int, Any]:
    return folder_peers(find_folder(iter_filters(result), folder), self_id)


# ---- serialization for the local fallback cache --------------------------- #


def peer_to_json(peer: Any) -> dict:
    if isinstance(peer, types.InputPeerUser):
        return {"type": "user", "id": peer.user_id, "access_hash": peer.access_hash}
    if isinstance(peer, types.InputPeerChat):
        return {"type": "chat", "id": peer.chat_id}
    if isinstance(peer, types.InputPeerChannel):
        return {"type": "channel", "id": peer.channel_id, "access_hash": peer.access_hash}
    if isinstance(peer, types.InputPeerSelf):
        return {"type": "self"}
    raise GuardError(f"cannot cache peer of type {type(peer).__name__}")


def peer_from_json(d: dict) -> Any:
    kind = d["type"]
    if kind == "user":
        return types.InputPeerUser(d["id"], d["access_hash"])
    if kind == "chat":
        return types.InputPeerChat(d["id"])
    if kind == "channel":
        return types.InputPeerChannel(d["id"], d["access_hash"])
    if kind == "self":
        return types.InputPeerSelf()
    raise GuardError(f"unknown cached peer type {kind!r}")


# ---- chat name resolution (only ever searches the allowlist) -------------- #


@dataclass
class ChatInfo:
    peer_id: int
    name: str
    username: str | None = None
    kind: str = "chat"  # user | group | channel | bot | self
    unread: int = 0
    unread_mentions: int = 0
    last_message: Any = None  # top message of the dialog, if any
    read_outbox_max_id: int = 0  # my messages with id <= this have been read

    @property
    def last_date(self) -> datetime | None:
        return getattr(self.last_message, "date", None)


def resolve_chat(ref: str, chats: Iterable[ChatInfo]) -> ChatInfo:
    """Fuzzy-match ``ref`` against allowed chats only.

    Order: numeric id, exact name, @username, unique prefix, unique substring,
    then difflib close match. Ambiguity is an error rather than a guess.
    """
    chats = list(chats)
    ref = ref.strip()
    if not ref:
        raise ChatNotFoundError("empty chat reference")

    if ref.lstrip("-").isdigit():
        n = int(ref)
        for c in chats:
            if c.peer_id == n:
                return c
        raise ChatNotFoundError(f"chat id {n} is not in the allowed folder")

    key = ref.casefold()
    uname = key.lstrip("@")

    def pick(matches: list[ChatInfo]) -> ChatInfo | None:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(repr(m.name) for m in matches[:8])
            raise AmbiguousChatError(f"{ref!r} matches several chats: {names}")
        return None

    for matches in (
        [c for c in chats if c.name.casefold() == key],
        [c for c in chats if c.username and c.username.casefold() == uname],
        [c for c in chats if c.name.casefold().startswith(key)],
        [c for c in chats if key in c.name.casefold()],
    ):
        hit = pick(matches)
        if hit:
            return hit

    by_name = {c.name.casefold(): c for c in chats}
    close = difflib.get_close_matches(key, list(by_name), n=1, cutoff=0.6)
    if close:
        return by_name[close[0]]
    raise ChatNotFoundError(f"no allowed chat matches {ref!r}")


# --------------------------------------------------------------------------- #
# Guard: live allowlist state
# --------------------------------------------------------------------------- #


class Guard:
    def __init__(self, folder: str, cache_path: Path, log_path: Path):
        self.folder = folder
        self.cache_path = Path(cache_path)
        self.log_path = Path(log_path)
        self._peers: dict[int, Any] = {}
        self.source = "none"  # "api" | "cache" | "none"
        self._listeners: list[Callable[[set[int], set[int]], Any]] = []
        self._audit = self._make_audit_logger()

    # ---- the one check everything goes through ---------------------------- #

    def check(self, chat_id: Any) -> Any:
        """Return the InputPeer for ``chat_id`` or raise ``NotAllowedError``."""
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise NotAllowedError(chat_id)
        peer = self._peers.get(chat_id)
        if peer is None:
            raise NotAllowedError(chat_id)
        return peer

    def is_allowed(self, chat_id: Any) -> bool:
        try:
            self.check(chat_id)
            return True
        except NotAllowedError:
            return False

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(self._peers)

    def on_change(self, cb: Callable[[set[int], set[int]], Any]) -> None:
        self._listeners.append(cb)

    # ---- state updates ---------------------------------------------------- #

    def set_peers(self, peers: dict[int, Any], source: str) -> tuple[set[int], set[int]]:
        old = set(self._peers)
        self._peers = dict(peers)
        self.source = source
        added, removed = set(peers) - old, old - set(peers)
        if added or removed:
            self._audit.info(
                "allowlist change (source=%s folder=%r): added=%s removed=%s",
                source, self.folder, sorted(added), sorted(removed),
            )
        return added, removed

    def clear(self, reason: str) -> None:
        """Fail closed."""
        self._audit.warning("allowlist cleared: %s", reason)
        self.set_peers({}, "none")

    async def load(self, client: Any, self_id: int | None = None) -> None:
        """Initial load: API first, local cache as fallback for API failures only."""
        try:
            while True:
                try:
                    result = await client(functions.messages.GetDialogFiltersRequest())
                    break
                except FloodWaitError as e:
                    log.warning("rate limited by Telegram, waiting %ss", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # network / RPC failure
            log.warning("GetDialogFilters failed (%s); trying cached allowlist", e)
            self._audit.warning("API load failed: %s", e)
            self._load_cache()
            return
        # Folder-not-found and category-rule errors propagate: never fall back.
        peers = build_allowlist(result, self.folder, self_id)
        self.set_peers(peers, "api")
        self._save_cache()

    async def refresh(self, client: Any, self_id: int | None = None) -> tuple[set[int], set[int]]:
        try:
            result = await client(functions.messages.GetDialogFiltersRequest())
        except Exception as e:
            self._audit.warning("live refresh failed, keeping current allowlist: %s", e)
            return set(), set()
        try:
            peers = build_allowlist(result, self.folder, self_id)
        except GuardError as e:
            self.clear(str(e))
            return set(), set()
        changes = self.set_peers(peers, "api")
        self._save_cache()
        for cb in self._listeners:
            try:
                r = cb(*changes)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("allowlist listener failed")
        return changes

    # ---- cache ------------------------------------------------------------ #

    def _save_cache(self) -> None:
        data = {
            "folder": self.folder,
            "updated_at": int(time.time()),
            "peers": [peer_to_json(p) for p in self._peers.values()],
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.cache_path)

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except FileNotFoundError:
            raise GuardError("could not reach Telegram and no cached allowlist exists") from None
        if data.get("folder") != self.folder:
            raise GuardError(
                f"cached allowlist is for folder {data.get('folder')!r}, not {self.folder!r}"
            )
        peers: dict[int, Any] = {}
        for d in data.get("peers", []):
            p = peer_from_json(d)
            if isinstance(p, types.InputPeerSelf):
                continue  # can't compute id offline; skip rather than guess
            peers[utils.get_peer_id(p)] = p
        self.set_peers(peers, "cache")

    def _make_audit_logger(self) -> logging.Logger:
        lg = logging.getLogger(f"tg.guard.audit.{id(self)}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(self.log_path)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
        return lg


# --------------------------------------------------------------------------- #
# GuardedClient: the only Telegram API surface the app is allowed to use
# --------------------------------------------------------------------------- #


def _name_of(entity: Any) -> tuple[str, str | None, str]:
    if isinstance(entity, types.User):
        kind = "self" if entity.is_self else ("bot" if entity.bot else "user")
        name = "Saved Messages" if entity.is_self else (utils.get_display_name(entity) or str(entity.id))
        return name, entity.username, kind
    if isinstance(entity, types.Channel):
        return entity.title, entity.username, "group" if entity.megagroup else "channel"
    if isinstance(entity, (types.Chat, types.ChatForbidden, types.ChannelForbidden)):
        return entity.title, None, "group"
    return str(getattr(entity, "id", "?")), None, "chat"


class GuardedClient:
    """Wraps a Telethon client; every chat-scoped method calls ``guard.check``.

    There is deliberately no global search, dialog listing, contacts, or join
    method here. If it isn't on this class, the app can't do it.
    """

    def __init__(self, client: Any, guard: Guard, self_id: int | None = None):
        self._client = client
        self.guard = guard
        self.self_id = self_id
        self._info: dict[int, ChatInfo] = {}
        self._chat_listeners: list[Callable[[list[ChatInfo]], Any]] = []

    # ---- lifecycle -------------------------------------------------------- #

    def install_live_refresh(self) -> None:
        async def on_raw(update):
            if isinstance(update, (types.UpdateDialogFilter, types.UpdateDialogFilters)):
                added, removed = await self.guard.refresh(self._client, self.self_id)
                if added or removed or not self.guard.allowed_ids:
                    infos = await self.refresh_chats()
                    for cb in self._chat_listeners:
                        r = cb(infos)
                        if asyncio.iscoroutine(r):
                            await r

        self._client.add_event_handler(on_raw, events.Raw)

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def _call(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        """Run an API call, honouring FloodWaitError by sleeping and retrying."""
        while True:
            try:
                return await fn(*args, **kwargs)
            except FloodWaitError as e:
                log.warning("rate limited by Telegram, waiting %ss", e.seconds)
                await asyncio.sleep(e.seconds + 1)

    # ---- chat metadata ---------------------------------------------------- #

    async def refresh_chats(self) -> list[ChatInfo]:
        """Names + unread counts for allowed chats only (GetPeerDialogs, no dialog list)."""
        ids = list(self.guard.allowed_ids)
        info: dict[int, ChatInfo] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            peers = [types.InputDialogPeer(self.guard.check(pid)) for pid in chunk]
            res = await self._call(self._client, functions.messages.GetPeerDialogsRequest(peers=peers))
            entities = {utils.get_peer_id(e): e for e in list(res.users) + list(res.chats)}
            tops = {(utils.get_peer_id(m.peer_id), m.id): m for m in res.messages}
            for d in res.dialogs:
                pid = utils.get_peer_id(d.peer)
                if not self.guard.is_allowed(pid):
                    continue
                name, uname, kind = _name_of(entities.get(pid))
                info[pid] = ChatInfo(
                    pid, name, uname, kind,
                    getattr(d, "unread_count", 0) or 0,
                    getattr(d, "unread_mentions_count", 0) or 0,
                    tops.get((pid, d.top_message)),
                    getattr(d, "read_outbox_max_id", 0) or 0,
                )
        for pid in ids:  # chats Telegram returned no dialog for still get a row
            info.setdefault(pid, ChatInfo(pid, str(pid)))
        self._info = info
        return sorted(info.values(), key=lambda c: c.name.casefold())

    def on_chats_changed(self, cb: Callable[[list[ChatInfo]], Any]) -> None:
        """Called with the new chat list after the folder changes live."""
        self._chat_listeners.append(cb)

    def chats(self) -> list[ChatInfo]:
        return [c for c in self._info.values() if self.guard.is_allowed(c.peer_id)]

    def chat_info(self, chat_id: int) -> ChatInfo | None:
        return self._info.get(chat_id) if self.guard.is_allowed(chat_id) else None

    async def resolve(self, ref: str) -> ChatInfo:
        if not self._info:
            await self.refresh_chats()
        return resolve_chat(ref, [c for c in self._info.values() if self.guard.is_allowed(c.peer_id)])

    # ---- read ------------------------------------------------------------- #

    async def get_messages(self, chat_id: int, limit: int = 50, **kw) -> list[Any]:
        peer = self.guard.check(chat_id)
        return list(await self._call(self._client.get_messages, peer, limit=limit, **kw))

    async def get_messages_by_id(self, chat_id: int, ids: list[int]) -> list[Any]:
        peer = self.guard.check(chat_id)
        if not ids:
            return []
        res = await self._call(self._client.get_messages, peer, ids=ids)
        return [m for m in (res or []) if m is not None]

    async def get_history(self, chat_id: int, limit: int = 50) -> tuple[list[Any], dict[int, Any]]:
        """Oldest-first messages plus ``{id: message}`` including reply targets."""
        msgs = await self.get_messages(chat_id, limit=limit)
        msgs.reverse()
        by_id = {m.id: m for m in msgs}
        missing = sorted({
            r for m in msgs
            if (r := getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None)) and r not in by_id
        })
        for m in await self.get_messages_by_id(chat_id, missing):
            by_id[m.id] = m
        return msgs, by_id

    async def search_messages(self, chat_id: int, query: str, limit: int = 100) -> list[Any]:
        """Search inside ONE allowed chat (there is no global search)."""
        peer = self.guard.check(chat_id)
        return list(await self._call(self._client.get_messages, peer, search=query, limit=limit))

    async def get_participants(self, chat_id: int, search: str = "", limit: int = 200) -> list[Any]:
        peer = self.guard.check(chat_id)
        return list(await self._call(self._client.get_participants, peer, search=search, limit=limit))

    # ---- write ------------------------------------------------------------ #

    async def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> Any:
        peer = self.guard.check(chat_id)
        return await self._call(self._client.send_message, peer, text, reply_to=reply_to)

    async def reply(self, chat_id: int, msg_id: int, text: str) -> Any:
        return await self.send_message(chat_id, text, reply_to=msg_id)

    async def send_file(
        self, chat_id: int, path: Any, caption: str | None = None, reply_to: int | None = None
    ) -> Any:
        """``path`` may be one path or a list (sent as an album)."""
        peer = self.guard.check(chat_id)
        return await self._call(self._client.send_file, peer, path, caption=caption, reply_to=reply_to)

    async def edit_message(self, chat_id: int, msg_id: int, text: str) -> Any:
        peer = self.guard.check(chat_id)
        return await self._call(self._client.edit_message, peer, msg_id, text)

    async def delete_messages(self, chat_id: int, msg_ids: list[int], revoke: bool = True) -> Any:
        peer = self.guard.check(chat_id)
        return await self._call(self._client.delete_messages, peer, msg_ids, revoke=revoke)

    async def read_participants(self, chat_id: int, msg_id: int) -> list[Any]:
        """Who read my message in a group: ``[ReadParticipantDate(user_id, date)]``.

        Telegram only provides this for small groups and recent messages.
        """
        peer = self.guard.check(chat_id)
        res = await self._call(self._client, functions.messages.GetMessageReadParticipantsRequest(peer, msg_id))
        return list(res or [])

    async def outbox_read_date(self, chat_id: int, msg_id: int) -> Any:
        """When the other person read my message in a private chat (if privacy allows)."""
        peer = self.guard.check(chat_id)
        res = await self._call(self._client, functions.messages.GetOutboxReadDateRequest(peer, msg_id))
        return getattr(res, "date", None)

    async def send_voice(self, chat_id: int, path: str, duration: float, reply_to: int | None = None) -> Any:
        """Send an OGG/Opus file as a voice message (the round play-button bubble)."""
        peer = self.guard.check(chat_id)
        attrs = [types.DocumentAttributeAudio(duration=max(1, round(duration)), voice=True)]
        return await self._call(
            self._client.send_file, peer, path, voice_note=True, attributes=attrs,
            mime_type="audio/ogg", reply_to=reply_to,
        )

    async def react(self, chat_id: int, msg_id: int, emoji: str) -> Any:
        peer = self.guard.check(chat_id)
        req = functions.messages.SendReactionRequest(
            peer=peer, msg_id=msg_id, reaction=[types.ReactionEmoji(emoticon=emoji)]
        )
        return await self._call(self._client, req)

    async def download(self, chat_id: int, msg_id: int, dest: str) -> str | None:
        peer = self.guard.check(chat_id)
        msg = await self._call(self._client.get_messages, peer, ids=msg_id)
        if msg is None or not msg.media:
            return None
        return await self._call(self._client.download_media, msg, file=dest)

    async def forward(self, from_chat_id: int, msg_ids: list[int], to_chat_id: int) -> Any:
        src = self.guard.check(from_chat_id)
        dst = self.guard.check(to_chat_id)
        return await self._call(self._client.forward_messages, dst, msg_ids, from_peer=src)

    async def pin(self, chat_id: int, msg_id: int, notify: bool = False) -> Any:
        peer = self.guard.check(chat_id)
        return await self._call(self._client.pin_message, peer, msg_id, notify=notify)

    async def mark_read(self, chat_id: int, max_id: int | None = None) -> Any:
        peer = self.guard.check(chat_id)
        return await self._call(self._client.send_read_acknowledge, peer, max_id=max_id, clear_mentions=True)

    # ---- incoming --------------------------------------------------------- #

    def on_new_message(self, callback: Callable[[Any], Awaitable[Any]]) -> None:
        """Register a handler; messages from non-allowed chats are dropped.

        Uses the *live* allowlist on every event instead of ``chats=`` (which is
        frozen at registration time and would miss live folder changes).
        """

        async def handler(event):
            if not self.guard.is_allowed(event.chat_id):
                return
            await callback(event)

        self._client.add_event_handler(handler, events.NewMessage())

    def on_message_edited(self, callback: Callable[[Any], Awaitable[Any]]) -> None:
        async def handler(event):
            if self.guard.is_allowed(event.chat_id):
                await callback(event)

        self._client.add_event_handler(handler, events.MessageEdited())

    def on_message_deleted(self, callback: Callable[[Any], Awaitable[Any]]) -> None:
        """Deletions. For private chats and small groups Telegram sends only the
        message ids (``chat_id`` is None); those carry no chat content, so they
        are passed through and the receiver matches ids against the open chat.
        """

        async def handler(event):
            if event.chat_id is None or self.guard.is_allowed(event.chat_id):
                await callback(event)

        self._client.add_event_handler(handler, events.MessageDeleted())

    def on_outbox_read(self, callback: Callable[[Any], Awaitable[Any]]) -> None:
        """Someone read my messages up to ``event.max_id`` in an allowed chat."""

        async def handler(event):
            if self.guard.is_allowed(event.chat_id):
                await callback(event)

        self._client.add_event_handler(handler, events.MessageRead(inbox=False))
