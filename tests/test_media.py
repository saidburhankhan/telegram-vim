from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl import types

from tg import media, render

NOW = datetime.now(timezone.utc)


def doc_msg(id=7, size=2_400_000, mime="application/pdf", attrs=None):
    doc = types.Document(
        id=1, access_hash=1, file_reference=b"", date=NOW, mime_type=mime,
        size=size, dc_id=2, attributes=attrs or [],
    )
    return SimpleNamespace(id=id, media=types.MessageMediaDocument(document=doc), message="")


def photo_msg(id=5):
    photo = types.Photo(
        id=1, access_hash=1, file_reference=b"", date=NOW, dc_id=2,
        sizes=[types.PhotoSize(type="m", w=320, h=200, size=20_000),
               types.PhotoSize(type="y", w=1280, h=800, size=1_258_291)],
    )
    return SimpleNamespace(id=id, media=types.MessageMediaPhoto(photo=photo), message="")


def test_labels():
    assert media.attachment_label(photo_msg()) == "[photo 1.2MB]"
    pdf = doc_msg(attrs=[types.DocumentAttributeFilename("report.pdf")])
    assert media.attachment_label(pdf) == "[file report.pdf 2.3MB]"
    voice = doc_msg(size=9000, mime="audio/ogg",
                    attrs=[types.DocumentAttributeAudio(duration=12, voice=True)])
    assert media.attachment_label(voice) == "[voice 0:12]"
    video = doc_msg(size=4_300_000, mime="video/mp4",
                    attrs=[types.DocumentAttributeVideo(duration=32, w=1, h=1)])
    assert media.attachment_label(video) == "[video 0:32 4.1MB]"
    webpage = SimpleNamespace(id=1, media=types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)))
    assert media.attachment_label(webpage) is None
    assert media.attachment_label(SimpleNamespace(id=1, media=None)) is None


def test_label_shows_in_rendered_message():
    m = photo_msg()
    m.message = "look"
    m.date, m.out, m.sender, m.reply_to, m.action = NOW, False, None, None, None
    m.post_author = "Bob"
    out = render.format_message(m).replace("\\[", "[")
    lines = out.split("\n")
    assert "[photo 1.2MB]" in lines[1] and "magenta" in lines[1]   # media on its own colored line
    assert lines[2] == "  look"                                   # caption on the next line


def _meta(m, id, out=False, **kw):
    m.id, m.date, m.out, m.sender, m.reply_to, m.action = id, NOW, out, None, None, None
    m.post_author, m.mentioned, m.grouped_id, m.edit_date = "Bob", False, None, None
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def test_album_is_one_block_with_caption_after_media():
    a = _meta(photo_msg(), 10, grouped_id=7)
    b = _meta(doc_msg(attrs=[types.DocumentAttributeVideo(duration=5, w=1, h=1)], mime="video/mp4"), 11, grouped_id=7)
    b.message = "from the demo"
    c = _meta(photo_msg(), 12)
    items = render.group_albums([a, b, c])
    assert [[m.id for m in it] for it in items] == [[10, 11], [12]]
    out = render.format_message(a, album=items[0]).replace("\\[", "[").split("\n")
    assert "#10–11" in out[0]
    assert "photo" in out[1] and "magenta" in out[1]
    assert "video 0:05" in out[2] and "blue" in out[2]
    assert out[3] == "  from the demo"


def test_ticks_edited_and_mention_tag():
    mine = _meta(SimpleNamespace(media=None, message="done"), 50, out=True, edit_date=NOW)
    head = render.format_message(mine, read_max=49).split("\n")[0]
    assert "(edited)" in head and "✓" in head and "✓✓" not in head
    head = render.format_message(mine, read_max=50).split("\n")[0]
    assert "✓✓" in head
    theirs = _meta(SimpleNamespace(media=None, message="@me look"), 51, mentioned=True)
    assert "@you" in render.format_message(theirs, read_max=99).split("\n")[0]
    assert "✓" not in render.format_message(theirs, read_max=99)


def test_download_path_is_stable_and_safe(tmp_path):
    pdf = doc_msg(id=42, attrs=[types.DocumentAttributeFilename("../../etc/passwd")])
    p = media.download_path(tmp_path, -100123, pdf)
    assert p.parent == tmp_path / "-100123"
    assert "/" not in p.name and p.name.startswith("42_")
    assert media.download_path(tmp_path, 1, photo_msg(9)) == tmp_path / "1" / "9_photo.jpg"
    assert media.download_path(tmp_path, 1, SimpleNamespace(id=1, media=None)) is None


def test_parse_uri_list(tmp_path):
    a = tmp_path / "my file.png"
    a.write_bytes(b"x")
    text = f"# comment\nfile://{str(a).replace(' ', '%20')}\nfile:///does/not/exist\nhttps://x.y/z\n"
    assert media.parse_uri_list(text) == [a]


def test_parse_path_text(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x")
    assert media.parse_path_text(f"  '{a}'\n") == [a]
    assert media.parse_path_text("just some words") == []
    assert media.parse_path_text(f"{a}\n/nope") == []


def test_human_size():
    assert media.human_size(512) == "512B"
    assert media.human_size(2048) == "2KB"
    assert media.human_size(5 * 1024 * 1024) == "5.0MB"
