import pytest

from tg import notify


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "_spawn", calls.append)
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_check_mode():
    assert notify.check_mode(" Sound ") == "sound"
    with pytest.raises(ValueError, match="sound, silent, off"):
        notify.check_mode("loud")


def test_cycle():
    n = notify.Notifier("sound")
    assert [n.cycle(), n.cycle(), n.cycle()] == ["silent", "off", "sound"]


def test_linux_silent_sound_off(monkeypatch, spawned):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    notify.Notifier("off").send("t", "b")
    assert spawned == []
    notify.Notifier("silent").send("t", "b")
    assert spawned == [["notify-send", "-a", "telegram-vim", "-h", "boolean:suppress-sound:true", "--", "t", "b"]]
    spawned.clear()
    monkeypatch.setattr(notify, "SOUND_FILES", (notify.Path(__file__),))
    notify.Notifier("sound").send("t", "b")
    assert [c[0] for c in spawned] == ["notify-send", "pw-play"]


def test_macos_uses_osascript(monkeypatch, spawned):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    notify.Notifier("sound").send('Chat "A"', "hi")
    [cmd] = spawned
    assert cmd[:2] == ["osascript", "-e"]
    assert 'with title "Chat \\"A\\""' in cmd[2] and 'sound name "Glass"' in cmd[2]
