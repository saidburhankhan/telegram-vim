import json
import os
import stat
from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import types
from telethon.tl.types.messages import DialogFilters

from tg.guard import (
    AmbiguousChatError,
    CategoryRulesError,
    ChatInfo,
    ChatNotFoundError,
    FolderNotFoundError,
    Guard,
    GuardedClient,
    GuardError,
    NotAllowedError,
    RateLimitedError,
    build_allowlist,
    resolve_chat,
    title_text,
)

ALICE = types.InputPeerUser(111, 1111)
TEAM = types.InputPeerChannel(222, 2222)
OLDGROUP = types.InputPeerChat(333)
MOM = types.InputPeerUser(999, 9999)  # personal: never in the work folder

ALICE_ID = utils.get_peer_id(ALICE)
TEAM_ID = utils.get_peer_id(TEAM)
OLDGROUP_ID = utils.get_peer_id(OLDGROUP)
MOM_ID = utils.get_peer_id(MOM)


def work_folder(title="Work", **kw):
    args = dict(
        id=2,
        title=types.TextWithEntities(text=title, entities=[]),
        pinned_peers=[ALICE],
        include_peers=[TEAM, OLDGROUP],
        exclude_peers=[],
    )
    args.update(kw)
    return types.DialogFilter(**args)


def personal_folder():
    return types.DialogFilter(
        id=3, title=types.TextWithEntities(text="Family", entities=[]),
        pinned_peers=[], include_peers=[MOM], exclude_peers=[],
    )


@pytest.fixture
def guard(tmp_path):
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "allowlist.log")
    g.set_peers(build_allowlist([types.DialogFilterDefault(), work_folder(), personal_folder()], "Work"), "api")
    return g


# ---- parsing --------------------------------------------------------------- #


def test_title_str_and_text_with_entities():
    assert title_text("Work") == "Work"
    assert title_text(types.TextWithEntities(text="Work", entities=[])) == "Work"


@pytest.mark.parametrize("wrap", [list, lambda fs: DialogFilters(filters=fs)])
def test_build_allowlist_list_or_wrapped(wrap):
    result = wrap([types.DialogFilterDefault(), work_folder(), personal_folder()])
    peers = build_allowlist(result, "Work")
    assert set(peers) == {ALICE_ID, TEAM_ID, OLDGROUP_ID}
    assert MOM_ID not in peers


def test_plain_string_title():
    f = work_folder()
    f.title = "Work"
    assert set(build_allowlist([f], "work")) == {ALICE_ID, TEAM_ID, OLDGROUP_ID}


def test_chatlist_folder_supported():
    f = types.DialogFilterChatlist(
        id=5, title=types.TextWithEntities(text="Work", entities=[]),
        pinned_peers=[], include_peers=[TEAM],
    )
    assert set(build_allowlist([f], "Work")) == {TEAM_ID}


def test_default_filter_skipped_and_missing_folder():
    with pytest.raises(FolderNotFoundError):
        build_allowlist([types.DialogFilterDefault(), personal_folder()], "Work")


@pytest.mark.parametrize("flag", ["contacts", "non_contacts", "groups", "broadcasts", "bots"])
def test_category_rules_refused(flag):
    with pytest.raises(CategoryRulesError, match=flag):
        build_allowlist([work_folder(**{flag: True})], "Work")


def test_exclude_peers_subtracted():
    peers = build_allowlist([work_folder(exclude_peers=[TEAM])], "Work")
    assert TEAM_ID not in peers


def test_saved_messages_needs_self_id():
    f = work_folder(include_peers=[types.InputPeerSelf()])
    with pytest.raises(GuardError):
        build_allowlist([f], "Work")
    assert 42 in build_allowlist([f], "Work", self_id=42)


# ---- check() --------------------------------------------------------------- #


def test_check_allows_folder_chats(guard):
    assert guard.check(ALICE_ID) is ALICE
    assert guard.check(TEAM_ID) is TEAM


@pytest.mark.parametrize("bad", [MOM_ID, 0, -1, 123456789, None, "111", str(ALICE_ID), True, 1.0 * ALICE_ID, ALICE])
def test_check_blocks_everything_else(guard, bad):
    with pytest.raises(NotAllowedError):
        guard.check(bad)
    assert not guard.is_allowed(bad)


def test_empty_guard_blocks_all(tmp_path):
    g = Guard("Work", tmp_path / "c.json", tmp_path / "l.log")
    for cid in (ALICE_ID, TEAM_ID, MOM_ID):
        assert not g.is_allowed(cid)


# ---- every GuardedClient action is blocked for non-allowed chats ------------ #


class FakeResult(list):
    """Iterable like a message list, but also looks like a single message."""
    id = 1
    media = None


class RecordingClient:
    """Fake Telethon client: records any call that reaches it."""

    def __init__(self):
        self.calls = []
        self.handlers = []

    async def __call__(self, req):
        self.calls.append(("request", type(req).__name__))

    def __getattr__(self, name):
        async def f(*a, **kw):
            self.calls.append((name, a, kw))
            return FakeResult()
        return f

    def add_event_handler(self, cb, ev):
        self.handlers.append((cb, ev))


ACTIONS = {
    "read": lambda gc, c: gc.get_messages(c, limit=5),
    "read_by_id": lambda gc, c: gc.get_messages_by_id(c, [1, 2]),
    "participants": lambda gc, c: gc.get_participants(c),
    "search_in_chat": lambda gc, c: gc.search_messages(c, "deploy"),
    "history": lambda gc, c: gc.get_history(c, limit=5),
    "send": lambda gc, c: gc.send_message(c, "hi"),
    "reply": lambda gc, c: gc.reply(c, 1, "hi"),
    "send_file": lambda gc, c: gc.send_file(c, "/etc/hostname"),
    "send_file_reply": lambda gc, c: gc.send_file(c, ["/etc/hostname"], caption="x", reply_to=1),
    "react": lambda gc, c: gc.react(c, 1, "👍"),
    "download": lambda gc, c: gc.download(c, 1, "/tmp"),
    "forward_from": lambda gc, c: gc.forward(c, [1], ALICE_ID),
    "forward_to": lambda gc, c: gc.forward(ALICE_ID, [1], c),
    "pin": lambda gc, c: gc.pin(c, 1),
    "mark_read": lambda gc, c: gc.mark_read(c),
    "edit": lambda gc, c: gc.edit_message(c, 1, "fixed"),
    "voice": lambda gc, c: gc.send_voice(c, "/tmp/v.ogg", 3.2),
    "delete": lambda gc, c: gc.delete_messages(c, [1]),
    "read_participants": lambda gc, c: gc.read_participants(c, 1),
    "outbox_read_date": lambda gc, c: gc.outbox_read_date(c, 1),
}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("bad", [MOM_ID, 555, -100123, None, "Mom"])
async def test_every_action_blocked(guard, action, bad):
    fake = RecordingClient()
    gc = GuardedClient(fake, guard)
    with pytest.raises(NotAllowedError):
        await ACTIONS[action](gc, bad)
    assert fake.calls == [], "request reached Telegram for a blocked chat"


@pytest.mark.parametrize("action", ACTIONS)
async def test_every_action_allowed_for_folder_chat(guard, action):
    fake = RecordingClient()
    gc = GuardedClient(fake, guard)
    await ACTIONS[action](gc, TEAM_ID)
    assert fake.calls


async def test_incoming_filter_uses_live_allowlist(guard):
    fake = RecordingClient()
    gc = GuardedClient(fake, guard)
    seen = []

    async def cb(ev):
        seen.append(ev.chat_id)

    gc.on_new_message(cb)
    handler = fake.handlers[0][0]
    await handler(SimpleNamespace(chat_id=MOM_ID))
    await handler(SimpleNamespace(chat_id=TEAM_ID))
    assert seen == [TEAM_ID]

    # folder edited live: TEAM removed, MOM never added
    guard.set_peers({ALICE_ID: ALICE}, "api")
    await handler(SimpleNamespace(chat_id=TEAM_ID))
    await handler(SimpleNamespace(chat_id=ALICE_ID))
    assert seen == [TEAM_ID, ALICE_ID]


@pytest.mark.parametrize("register", ["on_message_edited", "on_outbox_read"])
async def test_edit_and_read_events_filtered(guard, register):
    fake = RecordingClient()
    gc = GuardedClient(fake, guard)
    seen = []

    async def cb(ev):
        seen.append(ev.chat_id)

    getattr(gc, register)(cb)
    handler = fake.handlers[0][0]
    await handler(SimpleNamespace(chat_id=MOM_ID))
    await handler(SimpleNamespace(chat_id=None))
    await handler(SimpleNamespace(chat_id=TEAM_ID))
    assert seen == [TEAM_ID]


async def test_delete_events_filtered(guard):
    fake = RecordingClient()
    gc = GuardedClient(fake, guard)
    seen = []

    async def cb(ev):
        seen.append(ev.chat_id)

    gc.on_message_deleted(cb)
    handler = fake.handlers[0][0]
    await handler(SimpleNamespace(chat_id=MOM_ID))   # channel outside folder: dropped
    await handler(SimpleNamespace(chat_id=None))     # id-only deletion: passed through
    await handler(SimpleNamespace(chat_id=TEAM_ID))
    assert seen == [None, TEAM_ID]


# ---- live refresh ---------------------------------------------------------- #


class FiltersClient:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    async def __call__(self, req):
        if self.exc:
            raise self.exc
        return self.result


async def test_refresh_adds_and_removes(guard):
    changes = []
    guard.on_change(lambda a, r: changes.append((a, r)))
    new = work_folder(pinned_peers=[], include_peers=[TEAM, types.InputPeerUser(444, 4444)])
    await guard.refresh(FiltersClient([new]))
    assert guard.allowed_ids == {TEAM_ID, 444}
    assert changes == [({444}, {ALICE_ID, OLDGROUP_ID})]
    log = guard.log_path.read_text()
    assert "added=[444]" in log and "removed=" in log


async def test_refresh_fails_closed_on_category_rules(guard):
    await guard.refresh(FiltersClient([work_folder(contacts=True)]))
    assert guard.allowed_ids == frozenset()


async def test_refresh_fails_closed_when_folder_deleted(guard):
    await guard.refresh(FiltersClient([personal_folder()]))
    assert guard.allowed_ids == frozenset()


@pytest.mark.parametrize("exc", [
    ConnectionError("offline"), TimeoutError(), RPCError(None, "INTERNAL"),
    FloodWaitError(request=None, capture=30),
])
async def test_refresh_api_error_keeps_current(guard, exc):
    before = guard.allowed_ids
    await guard.refresh(FiltersClient(exc=exc))
    assert guard.allowed_ids == before


async def test_refresh_unexpected_error_fails_closed(guard):
    assert await guard.refresh(FiltersClient(exc=AttributeError("bug"))) == (set(), set())
    assert guard.allowed_ids == frozenset()
    assert "allowlist cleared" in guard.log_path.read_text()


# ---- cache fallback -------------------------------------------------------- #


async def test_load_writes_private_cache_and_falls_back(tmp_path):
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g.load(FiltersClient([work_folder()]))
    assert g.source == "api"
    mode = stat.S_IMODE(os.stat(g.cache_path).st_mode)
    assert mode == 0o600

    g2 = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g2.load(FiltersClient(exc=ConnectionError("offline")))
    assert g2.source == "cache"
    assert g2.allowed_ids == {ALICE_ID, TEAM_ID, OLDGROUP_ID}
    assert not g2.is_allowed(MOM_ID)


async def test_load_no_cache_and_api_down_errors(tmp_path):
    g = Guard("Work", tmp_path / "none.json", tmp_path / "a.log")
    with pytest.raises(GuardError):
        await g.load(FiltersClient(exc=ConnectionError("offline")))


async def test_cache_for_other_folder_rejected(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps({"folder": "Family", "peers": [{"type": "user", "id": 999, "access_hash": 1}]}))
    g = Guard("Work", p, tmp_path / "a.log")
    with pytest.raises(GuardError):
        await g.load(FiltersClient(exc=ConnectionError("offline")))


@pytest.mark.parametrize("exc", [
    RPCError(None, "INTERNAL"), TimeoutError(), OSError("no route"),
    FloodWaitError(request=None, capture=3600),  # too long to wait: use the cache
])
async def test_load_network_errors_use_cache(tmp_path, exc):
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g.load(FiltersClient([work_folder()]))
    g2 = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g2.load(FiltersClient(exc=exc))
    assert g2.source == "cache"
    assert g2.allowed_ids == {ALICE_ID, TEAM_ID, OLDGROUP_ID}


async def test_load_unexpected_error_propagates_without_cache(tmp_path):
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g.load(FiltersClient([work_folder()]))
    g2 = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    with pytest.raises(AttributeError):
        await g2.load(FiltersClient(exc=AttributeError("bug")))
    assert g2.source == "none"
    assert g2.allowed_ids == frozenset()


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"folder": "Work", "peers": {"type": "user"}}),
    json.dumps({"folder": "Work", "peers": ["user"]}),
    json.dumps({"folder": "Work", "peers": [{"type": "user", "id": 111}]}),
    json.dumps({"folder": "Work", "peers": [{"id": 111}]}),
    json.dumps({"folder": "Work", "peers": [{"type": "martian", "id": 1}]}),
    json.dumps({"folder": "Work", "peers": [{"type": "channel", "id": "x", "access_hash": 1}]}),
])
async def test_corrupted_cache_is_clear_error(tmp_path, content):
    p = tmp_path / "allowlist.json"
    p.write_text(content)
    g = Guard("Work", p, tmp_path / "a.log")
    with pytest.raises(GuardError, match="corrupted"):
        await g.load(FiltersClient(exc=ConnectionError("offline")))
    assert g.allowed_ids == frozenset()


async def test_load_sleeps_through_short_flood_wait(tmp_path, sleeps):
    class Flaky:
        def __init__(self):
            self.n = 0

        async def __call__(self, req):
            self.n += 1
            if self.n == 1:
                raise FloodWaitError(request=None, capture=5)
            return [work_folder()]

    waits = []
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g.load(Flaky(), on_flood_wait=waits.append)
    assert g.source == "api"
    assert waits == [5] and sleeps == [6]


async def test_category_rules_do_not_fall_back_to_cache(tmp_path):
    g = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    await g.load(FiltersClient([work_folder()]))
    g2 = Guard("Work", tmp_path / "allowlist.json", tmp_path / "a.log")
    with pytest.raises(CategoryRulesError):
        await g2.load(FiltersClient([work_folder(groups=True)]))


# ---- rate limits ----------------------------------------------------------- #


@pytest.fixture
def sleeps(monkeypatch):
    """Record asyncio.sleep calls in tg.guard instead of actually sleeping."""
    import tg.guard

    recorded = []

    async def fake_sleep(s):
        recorded.append(s)

    monkeypatch.setattr(tg.guard.asyncio, "sleep", fake_sleep)
    return recorded


class FloodingClient(RecordingClient):
    """Raises FloodWaitError for the first ``times`` sends, then succeeds."""

    def __init__(self, seconds, times):
        super().__init__()
        self.seconds, self.times = seconds, times

    async def send_message(self, *a, **kw):
        self.calls.append(("send_message", a, kw))
        if len(self.calls) <= self.times:
            raise FloodWaitError(request=None, capture=self.seconds)
        return FakeResult()


async def test_short_flood_wait_retried(guard, sleeps):
    fake = FloodingClient(seconds=30, times=1)
    gc = GuardedClient(fake, guard)
    waits = []
    gc.on_flood_wait = waits.append
    await gc.send_message(TEAM_ID, "hi")
    assert waits == [30]
    assert sleeps == [31]
    assert len(fake.calls) == 2


async def test_flood_wait_hook_errors_swallowed(guard, sleeps):
    gc = GuardedClient(FloodingClient(seconds=5, times=1), guard)
    gc.on_flood_wait = lambda s: 1 / 0
    await gc.send_message(TEAM_ID, "hi")
    assert sleeps == [6]


async def test_long_flood_wait_raises_without_sleeping(guard, sleeps):
    fake = FloodingClient(seconds=720, times=1)
    gc = GuardedClient(fake, guard)
    gc.on_flood_wait = lambda s: pytest.fail("hook called for a long wait")
    with pytest.raises(RateLimitedError, match="12 min") as ei:
        await gc.send_message(TEAM_ID, "hi")
    assert ei.value.seconds == 720
    assert isinstance(ei.value, GuardError)
    assert sleeps == [] and len(fake.calls) == 1


async def test_repeated_short_flood_waits_give_up(guard, sleeps):
    fake = FloodingClient(seconds=10, times=100)
    gc = GuardedClient(fake, guard)
    with pytest.raises(RateLimitedError):
        await gc.send_message(TEAM_ID, "hi")
    assert sleeps == [11, 11, 11]
    assert len(fake.calls) == 4


@pytest.mark.parametrize("seconds,text", [(12, "12s"), (61, "2 min"), (3600, "1 h"), (5400, "1 h 30 min")])
def test_rate_limited_message(seconds, text):
    assert str(RateLimitedError(seconds)) == f"Telegram rate limit: try again in {text}"


def test_make_client_disables_telethon_flood_sleep(tmp_path, monkeypatch):
    import tg.client

    seen = {}

    def fake_client(*a, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(tg.client, "TelegramClient", fake_client)
    cfg = tg.client.Config(1, "hash", "Work", tmp_path / "data", tmp_path / "dl")
    tg.client.make_client(cfg)
    assert seen["flood_sleep_threshold"] == 0


# ---- name resolution only searches the allowlist --------------------------- #


CHATS = [
    ChatInfo(1, "Backend Team", "backend_team", "group"),
    ChatInfo(2, "Frontend Team", None, "group"),
    ChatInfo(3, "Alice Smith", "alice", "user"),
]


@pytest.mark.parametrize("ref,expected", [
    ("backend team", 1), ("@alice", 3), ("alice", 3), ("front", 2),
    ("3", 3), ("Bakend Team", 1),
])
def test_resolve(ref, expected):
    assert resolve_chat(ref, CHATS).peer_id == expected


def test_resolve_ambiguous():
    with pytest.raises(AmbiguousChatError):
        resolve_chat("team", CHATS)


@pytest.mark.parametrize("ref", ["Mom", "@mom", "999", ""])
def test_resolve_outside_allowlist(ref):
    with pytest.raises(ChatNotFoundError):
        resolve_chat(ref, CHATS)
