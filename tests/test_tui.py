from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl import types
from textual.widgets import Input, OptionList, Static

from tg import media, notify
from tg.guard import ChatInfo, NotAllowedError
from tg.tui.app import ChatListScreen, ChatScreen, TelegramVim
from tg.tui.search import by_recent, filter_chats

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def spawned(monkeypatch):
    """Never pop real desktop notifications or play sounds from tests."""
    calls = []
    monkeypatch.setattr(notify, "_spawn", calls.append)
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(notify.sys, "platform", "linux")
    return calls
ALICE = types.User(id=1, first_name="Alice")


def msg(id, text, minutes_ago=0, out=False, sender=ALICE, **kw):
    m = SimpleNamespace(
        id=id, message=text, date=NOW - timedelta(minutes=minutes_ago), out=out,
        media=None, action=None, reply_to=None, sender=sender, mentioned=False,
        grouped_id=None, edit_date=None,
    )
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def chats():
    return [
        ChatInfo(10, "Backend Team", "backend", "group", 2, 0, msg(100, "deploy?", 5)),
        ChatInfo(20, "Frontend Team", None, "group", 0, 0, msg(200, "css", 60)),
        ChatInfo(30, "Alice Smith", "alice", "user", 0, 0, msg(300, "hi", 1)),
        ChatInfo(40, "Design Reviews", None, "group", 0, 0, None),
    ]


# ---- search ---------------------------------------------------------------- #


def test_order_by_last_message():
    assert [c.peer_id for c in by_recent(chats())] == [30, 10, 20, 40]


def test_empty_query_returns_all_recent_first():
    got, err = filter_chats("", chats())
    assert err is None and [c.peer_id for c in got] == [30, 10, 20, 40]


def test_fuzzy_prefix_word_substring_subsequence():
    assert [c.peer_id for c in filter_chats("back", chats())[0]] == [10]
    assert [c.peer_id for c in filter_chats("team", chats())[0]] == [10, 20]  # word prefix, recency
    assert [c.peer_id for c in filter_chats("bknd", chats())[0]] == [10]
    assert [c.peer_id for c in filter_chats("@alice", chats())[0]] == [30]
    assert filter_chats("zzz", chats())[0] == []


def test_regex():
    got, err = filter_chats("/^(front|back)", chats())
    assert err is None and {c.peer_id for c in got} == {10, 20}
    got, err = filter_chats("/team$", chats())
    assert {c.peer_id for c in got} == {10, 20}


def test_bad_regex_reports_error():
    got, err = filter_chats("/(", chats())
    assert got == [] and err.startswith("bad regex")


# ---- app ------------------------------------------------------------------- #


class FakeGuard:
    def __init__(self, ids):
        self.ids = set(ids)

    def is_allowed(self, cid):
        return cid in self.ids


class FakeGC:
    """Stand-in for GuardedClient with an in-memory message store per chat."""

    def __init__(self, store=None):
        self._chats = chats()
        self.guard = FakeGuard(c.peer_id for c in self._chats)
        self.msg_handlers = []
        self.edit_handlers = []
        self.delete_handlers = []
        self.read_handlers = []
        self.edits = []
        self.deleted = []
        self.members = [types.User(id=2, first_name="Bob", last_name="Builder", username="bob"),
                        types.User(id=3, first_name="Bella", last_name="Ng"),
                        types.User(id=9, first_name="Me", is_self=True)]
        self.sent = []
        self.files = []
        self.read = []
        self.store = store or {
            10: [msg(98, "morning", 30), msg(99, "standup at 10", 20), msg(100, "deploy?", 5)],
        }

    def chats(self):
        return list(self._chats)

    def on_new_message(self, cb):
        self.msg_handlers.append(cb)

    def on_chats_changed(self, cb):
        pass

    def on_message_edited(self, cb):
        self.edit_handlers.append(cb)

    def on_message_deleted(self, cb):
        self.delete_handlers.append(cb)

    def on_outbox_read(self, cb):
        self.read_handlers.append(cb)

    async def edit_message(self, chat_id, msg_id, text):
        self._check(chat_id)
        self.edits.append((chat_id, msg_id, text))
        return msg(msg_id, text, out=True, edit_date=NOW)

    async def delete_messages(self, chat_id, ids, revoke=True):
        self._check(chat_id)
        self.deleted.append((chat_id, list(ids)))

    async def get_participants(self, chat_id, search="", limit=200):
        self._check(chat_id)
        return list(self.members)

    async def read_participants(self, chat_id, msg_id):
        self._check(chat_id)
        return [types.ReadParticipantDate(user_id=2, date=NOW)]

    async def outbox_read_date(self, chat_id, msg_id):
        return NOW

    async def send_voice(self, chat_id, path, duration, reply_to=None):
        self._check(chat_id)
        self.voices = getattr(self, "voices", []) + [(chat_id, path, duration)]
        return msg(800 + len(self.voices), "", out=True)

    def _check(self, chat_id):
        if not self.guard.is_allowed(chat_id):
            raise NotAllowedError(chat_id)

    async def refresh_chats(self):
        return self.chats()

    async def get_messages(self, chat_id, limit=50, offset_id=None, min_id=None, **kw):
        self._check(chat_id)
        ms = sorted(self.store.get(chat_id, []), key=lambda m: m.id, reverse=True)
        if offset_id:
            ms = [m for m in ms if m.id < offset_id]
        if min_id is not None:
            ms = [m for m in ms if m.id > min_id]
        return ms[:limit]

    async def get_history(self, chat_id, limit=50):
        ms = list(reversed(await self.get_messages(chat_id, limit=limit)))
        return ms, {m.id: m for m in ms}

    async def get_messages_by_id(self, chat_id, ids):
        self._check(chat_id)
        return [m for m in self.store.get(chat_id, []) if m.id in ids]

    async def search_messages(self, chat_id, query, limit=100):
        ms = await self.get_messages(chat_id, limit=10_000)
        return [m for m in ms if query.lower() in (m.message or "").lower()][:limit]

    async def send_message(self, chat_id, text, reply_to=None):
        self._check(chat_id)
        self.sent.append((chat_id, text, reply_to))
        m = msg(500 + len(self.sent), text, out=True)
        if reply_to:
            m.reply_to = SimpleNamespace(reply_to_msg_id=reply_to)
        return m

    async def send_file(self, chat_id, path, caption=None, reply_to=None):
        self._check(chat_id)
        self.files.append((chat_id, path, caption, reply_to))
        return msg(600 + len(self.files), caption or "", out=True)

    async def download(self, chat_id, msg_id, dest):
        self._check(chat_id)
        Path(dest).write_bytes(b"jpegdata")
        return dest

    async def mark_read(self, chat_id, max_id=None):
        self.read.append((chat_id, max_id))


def options(app, sel="#chats"):
    ol = app.screen.query_one(sel, OptionList)
    return [ol.get_option_at_index(i).id for i in range(ol.option_count)]


def selected_id(app):
    return app.screen.selected().id


def mode_text(app):
    return str(app.screen.query_one("#mode", Static).render())


async def open_backend(pilot, app):
    await pilot.press("s", *"bknd", "enter")
    await pilot.pause()
    await pilot.pause()
    assert isinstance(app.screen, ChatScreen) and app.screen.chat.peer_id == 10


async def _noop():
    return None


async def test_list_normal_mode_search_and_open():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ChatListScreen)
        assert options(app) == ["30", "10", "20", "40"]  # most recent first
        ol = app.screen.query_one("#chats", OptionList)

        await pilot.press("j", "j")            # vim keys move in NORMAL mode
        assert ol.highlighted == 2
        await pilot.press("k")
        assert ol.highlighted == 1

        await pilot.press("s", *"jk")          # in search box, j/k are just text
        await pilot.pause()
        assert app.screen.query_one("#search", Input).value == "jk"
        await pilot.press("escape")            # leave search (filter kept)
        assert app.screen.query_one("#search", Input).value == "jk"
        await pilot.press("escape")            # clear filter
        await pilot.pause()
        assert len(options(app)) == 4

        await open_backend(pilot, app)
        assert options(app, "#msgs") == ["m98", "m99", "m100"]
        assert selected_id(app) == 100
        assert gc.read == [(10, 100)]
        assert "NORMAL" in mode_text(app)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ChatListScreen)


async def test_insert_reply_and_live_messages():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)

        await pilot.press("k")                         # select #99
        assert selected_id(app) == 99
        await pilot.press("r")
        assert "REPLY" in mode_text(app) and "standup" in mode_text(app)
        await pilot.press(*"on it", "enter")
        await pilot.pause()
        assert gc.sent == [(10, "on it", 99)]
        assert "INSERT" in mode_text(app)              # keep typing after a reply

        await pilot.press(*"lgtm", "enter")
        await pilot.pause()
        assert gc.sent[-1] == (10, "lgtm", None)

        await pilot.press("escape")                    # INSERT → NORMAL
        assert "NORMAL" in mode_text(app)

        # live message while at the bottom: follows
        await gc.msg_handlers[0](SimpleNamespace(chat_id=10, message=msg(700, "shipped"), get_sender=_noop))
        await pilot.pause()
        assert selected_id(app) == 700

        # scrolled up: new message doesn't steal selection
        await pilot.press("k", "k")
        await gc.msg_handlers[0](SimpleNamespace(chat_id=10, message=msg(701, "again"), get_sender=_noop))
        await pilot.pause()
        assert selected_id(app) != 701 and "1 new" in mode_text(app)
        await pilot.press("G")
        assert selected_id(app) == 701

        await pilot.press("escape")
        await pilot.pause()
        assert options(app)[0] == "10"


async def test_follow_reply_chain_and_jump_back():
    old = [msg(i, f"old {i}", 300 - i) for i in range(1, 80)]
    parent = msg(80, "original question", 200)
    child = msg(160, "answer", 10)
    child.reply_to = SimpleNamespace(reply_to_msg_id=80)
    filler = [msg(i, f"chat {i}", 190 - (i - 81)) for i in range(81, 160)]
    gc = FakeGC(store={10: old + [parent] + filler + [child]})
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        assert selected_id(app) == 160
        assert "m80" not in options(app, "#msgs")   # not in the first page

        await pilot.press("u")                       # climb to the replied message
        await pilot.pause()
        assert selected_id(app) == 80

        await pilot.press("ctrl+o")                  # jump back
        await pilot.pause()
        assert selected_id(app) == 160

        await pilot.press("enter")                   # Enter on a reply also follows it
        await pilot.pause()
        assert selected_id(app) == 80


async def test_search_in_chat_n_N():
    gc = FakeGC()
    gc.store[10].append(msg(101, "standup moved", 1))
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("s", *"standup", "enter")
        await pilot.pause()
        assert selected_id(app) == 101 and "match 1/2" in mode_text(app)
        await pilot.press("n")
        await pilot.pause()
        assert selected_id(app) == 99 and "match 2/2" in mode_text(app)
        await pilot.press("N")
        await pilot.pause()
        assert selected_id(app) == 101


async def test_paste_file_from_clipboard(monkeypatch, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    monkeypatch.setattr(media, "read_clipboard_files", lambda tmp: media.ClipboardFiles([img]))
    gc = FakeGC()
    app = TelegramVim(gc, "Work", tmp_path / "dl", tmp_path / "tmp")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("p")
        await pilot.pause()
        assert "ATTACH" in mode_text(app) and "shot.png" in mode_text(app)
        await pilot.press(*"see this", "enter")
        await pilot.pause()
        assert gc.files == [(10, str(img), "see this", None)]
        assert "NORMAL" in mode_text(app)

        await pilot.press("p", "escape")               # cancel attach
        await pilot.pause()
        assert len(gc.files) == 1 and "NORMAL" in mode_text(app)


async def test_enter_on_photo_downloads_and_opens(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(media, "open_file", lambda p: opened.append(p))
    photo = msg(101, "", 1)
    photo.media = types.MessageMediaPhoto(photo=types.Photo(
        id=1, access_hash=1, file_reference=b"", date=NOW, dc_id=2,
        sizes=[types.PhotoSize(type="y", w=1280, h=720, size=1_200_000)]))
    gc = FakeGC()
    gc.store[10].append(photo)
    app = TelegramVim(gc, "Work", tmp_path / "dl", tmp_path / "tmp")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        assert selected_id(app) == 101
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert opened and opened[0].name == "101_photo.jpg"
        assert opened[0].parent == tmp_path / "dl" / "10"


async def test_help_screen():
    app = TelegramVim(FakeGC(), "Work")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert type(app.screen).__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ChatListScreen)


async def test_unread_in_background_keeps_highlight():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("j", "j")
        ol = app.screen.query_one("#chats", OptionList)
        await gc.msg_handlers[0](SimpleNamespace(chat_id=40, message=msg(401, "review pls"), get_sender=_noop))
        await pilot.pause()
        assert options(app)[0] == "40"
        assert app.chats[40].unread == 1
        assert ol.highlighted_option.id == "20"


def msg_text(app, msg_id):
    s = app.screen
    idx = s._item_index(msg_id)
    return str(s.msglist.get_option_at_index(idx).prompt)


async def test_album_shows_as_one_item(tmp_path):
    from test_media import photo_msg
    gc = FakeGC()
    a, b = photo_msg(101), photo_msg(102)
    for m in (a, b):
        m.date, m.out, m.sender, m.reply_to = NOW, False, ALICE, None
        m.action, m.mentioned, m.edit_date = None, False, None
        m.grouped_id = 55
    b.message = "two screenshots"
    gc.store[10] += [a, b]
    app = TelegramVim(gc, "Work", tmp_path / "dl", tmp_path / "tmp")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        assert options(app, "#msgs") == ["m98", "m99", "m100", "m101"]   # album = one option
        text = msg_text(app, 102)
        lines = text.split("\n")
        assert "#101–102" in lines[0] and "photo" in lines[1] and "photo" in lines[2]
        assert lines[3].strip() == "two screenshots"
        assert app.screen.selected().id == 102          # caption carrier is the target


async def test_edit_and_delete_own_message():
    gc = FakeGC()
    gc.store[10].append(msg(101, "typo heer", 1, out=True))
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("e")
        assert "EDIT" in mode_text(app)
        assert app.screen.input.value == "typo heer"
        await pilot.press(*["backspace"] * 4, *"here", "enter")
        await pilot.pause()
        assert gc.edits == [(10, 101, "typo here")]
        assert "typo here" in msg_text(app, 101) and "(edited)" in msg_text(app, 101)

        await pilot.press("d")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("n")                          # cancel
        await pilot.pause()
        assert gc.deleted == []
        await pilot.press("d", "y")
        await pilot.pause()
        assert gc.deleted == [(10, [101])]
        assert "m101" not in options(app, "#msgs")

        await pilot.press("k", "e")                     # someone else's message
        assert "NORMAL" in mode_text(app)


async def test_live_edit_delete_and_read_receipts():
    gc = FakeGC()
    gc.store[10].append(msg(101, "shipping now", 1, out=True))
    gc._chats[1].read_outbox_max_id = 100
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        assert "✓" in msg_text(app, 101) and "✓✓" not in msg_text(app, 101)

        await gc.read_handlers[0](SimpleNamespace(chat_id=10, max_id=101))
        await pilot.pause()
        assert "✓✓" in msg_text(app, 101)

        await gc.edit_handlers[0](SimpleNamespace(
            chat_id=10, message=msg(99, "standup at 11", 20, edit_date=NOW), get_sender=_noop))
        await pilot.pause()
        assert "standup at 11" in msg_text(app, 99) and "(edited)" in msg_text(app, 99)

        await gc.delete_handlers[0](SimpleNamespace(chat_id=None, deleted_ids=[98]))
        await pilot.pause()
        assert "m98" not in options(app, "#msgs")

        await pilot.press("w")                          # who read (group)
        await pilot.pause()
        assert type(app.screen).__name__ == "InfoScreen"
        body = str(app.screen.query_one(Static).render())
        assert "Bob Builder" in body


async def test_mention_autocomplete():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("i", *"hey @b")
        await app.workers.wait_for_complete()
        await pilot.pause()
        hint = str(app.screen.query_one("#hint", Static).render())
        assert "@bob" in hint and "@Bella" in hint and "@Me" not in hint

        await pilot.press("tab")
        assert app.screen.input.value == "hey @bob "

        await pilot.press(*"and @bel", "tab")
        assert app.screen.input.value == "hey @bob and @Bella "
        await pilot.press(*"thanks", "enter")
        await pilot.pause()
        assert gc.sent[-1][1] == "hey @bob and [Bella Ng](tg://user?id=3) thanks"


async def test_mention_badge_and_toast_in_list():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await gc.msg_handlers[0](SimpleNamespace(
            chat_id=20, message=msg(201, "@me can you check", mentioned=True), get_sender=_noop))
        await pilot.pause()
        assert app.chats[20].unread_mentions == 1
        row = str(app.screen.query_one("#chats", OptionList).get_option_at_index(0).prompt.columns[1]._cells[0])
        assert "@1" in row
        await open_backend(pilot, app)


async def test_copy_and_view(monkeypatch):
    copied = []
    monkeypatch.setattr(media, "copy_text", lambda t: copied.append(t) or True)
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("y")
        await pilot.pause()
        assert copied == ["deploy?"]
        await pilot.press("v")
        await pilot.pause()
        assert type(app.screen).__name__ == "MessageViewScreen"
        await pilot.press("y")
        await pilot.pause()
        assert copied[-1] == "deploy?"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ChatScreen)


def notes(app):
    return [str(n.message) for n in app._notifications]


async def test_copy_runs_off_the_event_loop(monkeypatch):
    import asyncio
    import threading
    calls = []
    release = threading.Event()   # the "clipboard tool" hangs until the test lets it go

    def slow_copy(text):
        calls.append((text, threading.current_thread() is threading.main_thread()))
        release.wait(5)
        return True

    monkeypatch.setattr(media, "copy_text", slow_copy)
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        try:
            await pilot.press("y")
            await pilot.press("k")            # still handled while the copy hangs
            await pilot.pause()
            assert selected_id(app) == 99
            assert calls == [("deploy?", False)]
            assert "copied" not in notes(app)
        finally:
            release.set()
        for _ in range(100):
            if "copied" in notes(app):
                break
            await asyncio.sleep(0.02)
        assert "copied" in notes(app)


async def test_flood_wait_shows_toast():
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.gc.on_flood_wait(12)
        await pilot.pause()
        assert any("12s" in n for n in notes(app))
        n = next(n for n in app._notifications if "12s" in str(n.message))
        assert n.severity == "warning" and n.timeout == 10


class FakeRecorder:
    def __init__(self, dest):
        self.dest = Path(dest)
        self.started_at = None
        self.cancelled = False

    def start(self):
        import time
        self.started_at = time.monotonic()
        self.dest.parent.mkdir(parents=True, exist_ok=True)

    @property
    def elapsed(self):
        import time
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def stop(self):
        self.dest.write_bytes(b"OggS-fake")
        return self.elapsed

    def cancel(self):
        self.cancelled = True
        self.dest.unlink(missing_ok=True)


def voice_app(monkeypatch, tmp_path):
    import asyncio  # noqa: F401
    monkeypatch.setattr(media.VoiceRecorder, "available", staticmethod(lambda: True))
    gc = FakeGC()
    app = TelegramVim(gc, "Work", tmp_path / "dl", tmp_path / "tmp", voice_release_ms=150, voice_tap_ms=400)
    recs = []
    app.make_recorder = lambda dest: recs.append(FakeRecorder(dest)) or recs[-1]
    return gc, app, recs


async def hold_space(pilot, seconds, interval=0.03):
    import asyncio
    await pilot.press("space")
    await asyncio.sleep(0.1)               # like the keyboard's repeat delay
    loops = int(seconds / interval)
    for _ in range(loops):
        await pilot.press("space")          # autorepeat
        await asyncio.sleep(interval)


async def test_hold_space_records_and_sends_on_release(monkeypatch, tmp_path):
    import asyncio
    gc, app, recs = voice_app(monkeypatch, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await hold_space(pilot, 0.9)
        assert "REC" in mode_text(app) and "release Space" in mode_text(app)
        await asyncio.sleep(0.4)            # released: repeats stopped
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(gc.voices) == 1
        chat_id, path, duration = gc.voices[0]
        assert chat_id == 10 and duration >= 0.9 and path.endswith(".ogg")
        assert not Path(path).exists()      # temp file cleaned up after sending
        assert "m801" in options(app, "#msgs")
        assert "NORMAL" in mode_text(app)


async def test_tap_space_is_hands_free_until_next_tap(monkeypatch, tmp_path):
    import asyncio
    gc, app, recs = voice_app(monkeypatch, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("space")          # tap, no autorepeat
        await asyncio.sleep(0.6)
        assert "tap Space to send" in mode_text(app)
        assert getattr(gc, "voices", []) == []
        await asyncio.sleep(0.4)
        await pilot.press("space")          # second tap sends
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(gc.voices) == 1 and gc.voices[0][2] >= 0.9


async def test_escape_cancels_recording(monkeypatch, tmp_path):
    import asyncio
    gc, app, recs = voice_app(monkeypatch, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("space")
        await asyncio.sleep(0.2)
        await pilot.press("escape")
        await pilot.pause()
        assert recs[0].cancelled
        assert isinstance(app.screen, ChatScreen)   # Esc cancelled, didn't leave the chat
        await asyncio.sleep(0.5)
        assert getattr(gc, "voices", []) == []


async def test_too_short_voice_is_discarded(monkeypatch, tmp_path):
    import asyncio
    gc, app, recs = voice_app(monkeypatch, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await pilot.press("space")
        await asyncio.sleep(0.05)
        await pilot.press("space")          # one repeat then release, ~0.25s total
        await asyncio.sleep(0.3)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert getattr(gc, "voices", []) == []
        assert not recs[0].dest.exists()


async def test_loading_older_reuses_rendered_messages(monkeypatch):
    store = {10: [msg(i, f"m{i}", 300 - i) for i in range(1, 121)]}
    gc = FakeGC(store)
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        s = app.screen
        assert len(s.messages) == 50
        rendered = []
        orig = ChatScreen._option
        monkeypatch.setattr(ChatScreen, "_option", lambda self, it: rendered.append(it[0].id) or orig(self, it))
        await s.action_older()
        await pilot.pause()
        assert len(s.messages) == 100
        assert sorted(rendered) == list(range(21, 71))  # only the new page
        assert s._item_index(120) == 99 and s._item_index(21) == 0


async def test_jump_to_latest_trims_long_history(monkeypatch):
    import tg.tui.app as tui
    monkeypatch.setattr(tui, "HISTORY_KEEP", 60)
    store = {10: [msg(i, f"m{i}", 300 - i) for i in range(1, 121)]}
    store[10][-1].reply_to = SimpleNamespace(reply_to_msg_id=5)
    gc = FakeGC(store)
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        s = app.screen
        await s.action_older()
        await pilot.pause()
        assert len(s.messages) == 100
        await pilot.press("G")
        await pilot.pause()
        assert [m.id for m in s.messages] == list(range(61, 121))
        assert set(s.by_id) - {5} == set(range(61, 121))  # 5 kept only as a reply target
        assert selected_id(app) == 120
        assert s._item_index(61) == 0


async def test_hidden_chat_list_is_not_redrawn_until_resume(monkeypatch):
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        ls = app._list_screen()
        calls = []
        monkeypatch.setattr(ls, "refresh_list", lambda: calls.append(1))
        for i in range(5):
            await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(700 + i, "x"), get_sender=_noop))
        await pilot.pause()
        assert calls == []
        assert app.chats[20].unread == 5
        await pilot.press("escape")
        await pilot.pause()
        assert calls  # redrawn on resume


async def test_burst_of_messages_redraws_list_once(monkeypatch):
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        ls = app.screen
        calls = []
        orig = ls.refresh_list
        monkeypatch.setattr(ls, "refresh_list", lambda: calls.append(1) or orig())
        for i in range(5):
            await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(700 + i, "x"), get_sender=_noop))
        await pilot.pause()
        assert len(calls) == 1
        assert options(app)[0] == "20"


def popups(calls):
    return [c for c in calls if c[0] == "notify-send"]


async def test_popup_for_other_chat_but_not_the_open_one(spawned):
    gc = FakeGC()
    app = TelegramVim(gc, "Work")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await open_backend(pilot, app)
        await gc.msg_handlers[0](SimpleNamespace(chat_id=10, message=msg(701, "in open chat"), get_sender=_noop))
        assert popups(spawned) == []
        await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(702, "<b>css</b> & js"), get_sender=_noop))
        [cmd] = popups(spawned)
        assert "boolean:suppress-sound:true" in cmd
        assert cmd[-2] == "Alice · Frontend Team" and cmd[-1] == "&lt;b&gt;css&lt;/b&gt; &amp; js"
        assert not any(c[0] in ("pw-play", "paplay") for c in spawned)  # silent is the default


async def test_no_popup_on_visible_chat_list_unless_terminal_unfocused(spawned):
    gc = FakeGC()
    app = TelegramVim(gc, "Work", notifications="sound")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(701, "hi"), get_sender=_noop))
        assert spawned == []
        app.app_focus = False
        await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(702, "hi"), get_sender=_noop))
        assert len(popups(spawned)) == 1
        assert any(c[0] == "pw-play" for c in spawned)
        await gc.msg_handlers[0](SimpleNamespace(chat_id=20, message=msg(703, "mine", out=True), get_sender=_noop))
        assert len(popups(spawned)) == 1  # never for your own messages


async def test_n_cycles_notification_modes_and_off_is_silent(spawned):
    gc = FakeGC()
    app = TelegramVim(gc, "Work", notifications="sound")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert app.notifier.mode == "silent"
        assert "silent" in str(app.screen.query_one("#topbar", Static).render())
        await pilot.press("n")
        assert app.notifier.mode == "off"
        app.app_focus = False
        await gc.msg_handlers[0](SimpleNamespace(
            chat_id=20, message=msg(701, "@me look", mentioned=True), get_sender=_noop))
        app.app_focus = True
        await open_backend(pilot, app)
        await pilot.press("ctrl+n")  # works inside a chat too
        assert app.notifier.mode == "sound"
    assert spawned == []
