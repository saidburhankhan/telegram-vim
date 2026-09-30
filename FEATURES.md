# work-tg: what's built

A work-only Telegram client for the terminal. It connects as your own user
account (MTProto via Telethon, not the Bot API) and **only ever sees the chats
you put in one Telegram folder** (e.g. "Work"). Personal chats, the global chat
list, contacts, global search, and joining channels don't exist in the app.

This file is an inventory of what the app does and how it's put together.
For setup and usage, see [README.md](README.md).

---

## Tech stack

| Piece | Used for |
|---|---|
| Python 3.11+ | runtime |
| Telethon | Telegram MTProto client (user account) |
| Typer + Rich | one-shot CLI commands and colored output |
| Textual | interactive full-screen terminal app |
| TOML | config file |
| ffmpeg | voice recording (OGG/Opus) |
| wl-clipboard / xclip | clipboard paste and copy |
| notify-send / osascript | desktop notifications |
| cryptg | native AES for Telegram's encryption (fast media transfers) |
| pytest + pytest-asyncio | tests |

Installed as a `tg` command through `pyproject.toml`.

---

## Security model: the folder allowlist

All of this lives in `tg/guard.py`, the single source of truth.

- **Folder = allowlist.** At startup the app reads your Telegram chat folders
  (`GetDialogFilters`), finds the configured one by title, and allows **only
  chats explicitly added to it** (pinned + included chats).
- **Handles every API variant:** folder titles as plain strings or
  `TextWithEntities`, results as a list or wrapped in `.filters`, regular and
  shared (chatlist) folders. The default "All chats" filter is skipped.
- **Refuses risky folders.** If the folder uses category rules (Contacts,
  Non-contacts, Groups, Channels, Bots), which could pull in personal chats,
  the app prints a clear warning and won't start.
- **One gate for everything.** `Guard.check(chat_id)` is the only way to get the
  handle needed to talk to a chat. `GuardedClient` is the only Telegram object
  the rest of the app can use, and every method calls `check` first: read,
  history, search in a chat, members, send, reply, send file, send voice, edit,
  delete, react, download, forward (source *and* destination), pin, mark read,
  and read receipts.
- **No global features exist.** There's no method for dialog listing, global
  search, contacts, or joining, so nothing can call them.
- **Live updates are filtered per event** against the current allowlist (new,
  edited, and deleted messages, read receipts). The static `chats=` filter isn't
  used.
- **Live folder sync.** Adding or removing a chat in the official app updates
  the running client immediately (`UpdateDialogFilter`), with no restart.
- **Fails closed.** If the folder is deleted, or gains category rules while the
  app is running, the allowlist is emptied.
- **Offline fallback.** The last good allowlist is cached (`allowlist.json`,
  mode 0600) and used only if Telegram can't be reached at startup, never to
  get around a folder problem.
- **Audit log.** Chats added to or removed from the allowlist are logged to
  `allowlist.log`.
- **Chat names resolve only inside the folder:** fuzzy matching, `@username`, or
  id. Ambiguous names are an error, not a guess.

## Account and session

- `tg login`: phone number (checked for a `+country` prefix, re-prompts on an
  invalid number), login code, 2FA password.
- `tg logout`: ends the session on Telegram's side.
- Session file pre-created with mode `0600` in a `0700` data directory.
- `config.toml`, session files, cache, and logs are in `.gitignore`.
- `api_id` / `api_hash` come from the config file or the `TG_API_ID` /
  `TG_API_HASH` environment variables.
- Rate limits (`FloodWaitError`) are handled by waiting the required time.
  Updates are event-driven, with no polling.

---

## One-shot CLI commands

| Command | What it does |
|---|---|
| `tg` / `tg ui` | open the interactive app |
| `tg login` / `tg logout` | sign in / out |
| `tg list` | allowed chats with unread and @mention counts |
| `tg read <chat> [-n N]` | recent messages with sender, time, ID, reply context, albums, ✓/✓✓ |
| `tg send <chat> "<text>"` | send text |
| `tg reply <chat> <msg_id> "<text>"` | reply to a message |
| `tg send-file <chat> <path> [-c caption] [-r msg_id]` | send a photo or file |
| `tg download <chat> <msg_id>` | save an attachment to `download_dir` |
| `tg listen` | live stream of new messages from allowed chats |

Errors (not logged in, chat not in folder, Telegram errors) print one line
instead of a traceback.

---

## Interactive app (`tg`)

### Navigation and modes
- **Vim-style modal interface.** A focused list is NORMAL mode, where letters are
  commands. A focused input box is typing mode (INSERT, REPLY, EDIT, SEARCH, or
  ATTACH). Esc always steps back one level.
- A mode bar shows the current mode and context (reply target, attachment,
  search match number, new-message count, recording timer).
- `?` opens a help screen, and the footer shows the keys for the focused area.

### Chat list
- Only folder chats, **ordered by last message**.
- Each row shows the chat name and type icon, time of the last message, a
  preview (with sender in groups, and ✓/✓✓ on your own), a yellow unread
  badge, and a red **@N unread mentions** badge.
- The top bar shows totals: chats, unread, mentions.
- **Search box:** fuzzy matching (prefix, word start, substring, in-order letters
  such as `bknd` → "Backend") or `/regex`. It only searches the folder.
- j/k and arrow keys, Enter to open. The selection stays on the same chat when
  the list reorders.
- Chats added to or removed from the folder appear or disappear live. If the
  open chat is removed, the app returns to the list.

### Chat view: reading
- Readable message blocks: a header line (time, sender, `#id`, `(edited)`, ✓ or
  ✓✓, a red `@you` tag if you're mentioned), a reply-context line, the body
  indented, and a blank line between messages.
- **Media colored by type,** one per line with an icon: 🖼 photo (magenta),
  🎬 video (blue), 🎤 voice (yellow), 🎵 audio, 📄 file (green), GIF, sticker,
  location, contact, poll. Sizes and durations are shown (`[photo 1.2MB]`,
  `[video 0:32 4.1MB]`, `[voice 0:12]`).
- **Albums** (several photos or videos sent together) are one block: every item
  listed, then the caption on the next line.
- Loads the last 50 messages. Pressing `k` at the top loads older ones.
- **Live:** new messages appear. If you've scrolled up, the selection stays put
  and the bar shows "↓ N new · G to jump".
- Opening a chat marks it read, including clearing mentions.

### Chat view: moving around
- j/k to move between messages, `G` for the latest.
- **Reply threads:** `u` (or Enter on a reply) jumps to the replied-to message,
  loading history back as far as needed (up to 1000 messages). Repeat to climb
  the thread. `Ctrl+O` jumps back.
- **Search in the current chat** (`s` or `/`) uses Telegram's server-side search,
  scoped to that one chat. `n` / `N` step through matches.

### Chat view: writing
- `i` writes (you stay in INSERT to keep chatting), `r` replies to the selected
  message.
- **@mentions in groups:** typing `@` loads the member list, a hint bar shows
  matches, a ghost suggestion appears, and Tab completes. People without a
  username get a real mention link that notifies them.
- `e` **edits** your own message (text pre-filled). `d` **deletes** a message or
  whole album for everyone, with a y/n confirm.
- Edits and deletions made by others (or from your phone) show up live.
- A failed send puts your text back in the box, so nothing is lost.

### Media
- Enter or `o` on a photo, video, or file **downloads and opens it** in the
  desktop's default app (`xdg-open` / `open`). Albums open every item.
- Downloads go to `download_dir/<chat>/<msg id>_<name>`, are reused on the next
  open, and file names are sanitized.
- **Paste to send** (`p`): a copied image (screenshot) or copied file(s) from the
  clipboard. Add an optional caption and Enter sends. Several files go as an
  album. Supports Wayland (`wl-paste`), X11 (`xclip`), and macOS (paths, images
  with `pngpaste`).
- **Voice messages:** hold Space to record, release to send (push-to-talk). A
  quick tap switches to hands-free mode, and the next tap sends. Esc cancels.
  - Recording is OGG/Opus through ffmpeg, from the configured microphone, so
    Telegram shows a real voice bubble with its duration.
  - Release is detected from keyboard autorepeat, with tunable timing in
    `[voice]`.
  - Clips under 0.7s are discarded. Temp files are removed after sending.

### Read receipts and mentions
- ✓ sent / ✓✓ read on your messages, updated live when the other side reads.
- `w` shows **who read it and when**: the list of readers in groups, or the read
  time in private chats, when Telegram and privacy settings allow. Otherwise it
  shows ✓/✓✓ and the reason.
- Being mentioned produces a red `@you` tag on the message, an `@N` badge on the
  chat, a total in the top bar, and an in-app pop-up when it happens in a chat
  that isn't open (unless notifications are off).

### Desktop notifications
- New messages in allowed chats trigger a desktop notification (`notify-send` on
  Linux, `osascript` on macOS). Only folder chats can notify, so personal chats
  never interrupt you.
- **Only when you can't already see the message:** the terminal is in the
  background, or you're inside a different chat. No pop-up for the open chat,
  the visible chat list, or your own messages.
- Group notifications show the sender and the chat name. `@` marks a mention.
- Three modes:
  - **sound**: pop-up + sound (`pw-play` / `paplay`, freedesktop message sound)
  - **silent**: pop-up only; the notification daemon is asked not to play its
    own sound. This is the default.
  - **off**: no pop-ups and no in-app mention toasts. Unread and `@N` badges
    still update.
- Set the default with `[notifications] mode`. Switch with `n` in the chat list
  or `Ctrl+N` anywhere; the switch lasts until restart, and the top bar shows
  the current mode.
- Message text is escaped, so markup in a message can't change how the
  notification looks.

### Performance
- Media encryption uses `cryptg` (native AES), about 45× faster than the
  fallback for uploads and downloads.
- Loading older messages renders only the new page; messages already on screen
  are reused. Finding a message by id uses an index instead of a scan.
- Jumping to the latest message (`G`) after scrolling back more than 1,000
  messages drops the oldest ones to free memory. `k` at the top reloads them.
- The chat list isn't redrawn while it's hidden behind a chat, and a burst of
  incoming messages redraws it once.

### Copying
- `y` copies the whole message through the system clipboard *and* the
  terminal's OSC 52, so it works over SSH and in most terminals.
- `v` opens the message in a read-only view where you can **select part of the
  text** with the mouse or Shift+arrows, then `y` / Ctrl+C.
- Holding Shift while dragging uses the terminal's own selection.

---

## Project layout

```
work-tg/
  pyproject.toml         package + `tg` entry point
  config.example.toml    api keys, folder, data/download dirs, [voice], [notifications]
  README.md              setup and usage
  FEATURES.md            this file
  tg/
    guard.py             folder allowlist + GuardedClient (the only Telegram API surface)
    client.py            config loading, Telethon client, login/logout, session security
    commands.py          Typer CLI commands
    render.py            message formatting (colors, albums, ticks, mentions)
    media.py             attachments, downloads, opening files, clipboard, voice recorder
    notify.py            desktop notifications (sound / silent / off)
    tui/app.py           Textual app: chat list, chat view, modes, modals, help
    tui/search.py        chat ordering + fuzzy/regex filtering
  tests/
    test_guard.py        allowlist parsing, every action blocked outside the folder,
                         event filtering, live refresh, fail-closed, cache
    test_media.py        labels, albums, ticks, download paths, clipboard parsing
    test_notify.py       notification modes and the commands they run
    test_tui.py          headless UI tests driving real key presses
```

About 4,500 lines of Python. **210 tests**, all passing.

The core security tests check that every `GuardedClient` action rejects chats
outside the folder, for many kinds of bad input, and that no request reaches
Telegram when it does.

---

## Not built yet

From the original plan, still to do:

- **Reactions, pin, forward in the UI or CLI:** the guarded methods exist and are
  tested, but there are no keys or commands for them yet.
- **Optional SQLite message cache** for faster scrolling.
- **Web interface:** discussed; the terminal app was chosen instead.
