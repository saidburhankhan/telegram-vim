# telegram-vim

[![tests](https://github.com/saidburkhanmukhtorov/telegram-vim/actions/workflows/tests.yml/badge.svg)](https://github.com/saidburkhanmukhtorov/telegram-vim/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A distraction-free, vim-style Telegram client for your terminal — for work chats only.**

Use Telegram like Slack: telegram-vim shows *only* the chats you put in one
Telegram folder (e.g. "Work") and nothing else — no personal chats, no global
search, no contacts. Navigate with vim keys, reply to threads, paste screenshots,
hold Space to send voice messages, and see who read your messages, all without
leaving the terminal.

Built with Python, [Telethon](https://github.com/LonamiWebs/Telethon) and
[Textual](https://github.com/Textualize/textual). The command is `tg`.

---

The restriction isn't a setting you can switch off: global search, the full
chat list, contacts, and joining channels simply don't exist in the code, and
every action goes through one allowlist check (see [FEATURES.md](FEATURES.md)).

Status: login, list, read, send, reply, send-file, download, listen, desktop
notifications, and the interactive vim-style app. Reactions, pin, and forward
come next.

> **Unofficial.** This project is not affiliated with or endorsed by Telegram.
> It signs in as *your* user account through Telegram's public MTProto API,
> using an api_id you create yourself.

## Requirements

- Python 3.11+
- Linux or macOS (developed on Linux/Wayland; macOS support is lighter)
- Optional, for extra features:

| Tool | For |
|---|---|
| `ffmpeg` with libopus + PipeWire/PulseAudio | voice messages |
| `wl-clipboard` (Wayland) or `xclip` (X11) | paste screenshots/files, copy text |
| `notify-send` (libnotify) and `pw-play`/`paplay` | desktop notifications and sound |
| `pngpaste` (macOS) | pasting images |

## Setup

### 1. Get an api_id / api_hash

1. Go to <https://my.telegram.org> and log in with your phone number.
2. Open **API development tools** and create an app (any title/short name).
3. Copy the **api_id** (a number) and **api_hash** (a hex string).

These identify *your* client; keep them private.

### 2. Create the Work folder

In the official Telegram app: **Settings → Chat Folders → Create New Folder**.

- Name it `Work` (or anything; it must match `folder` in the config).
- Under **Included Chats**, add work chats **one by one**.
- Do **not** tick the category options (Contacts, Non-Contacts, Groups,
  Channels, Bots). telegram-vim refuses to start if any are enabled, because they
  can pull personal chats in.

Chats you add or remove later show up live in `tg listen` (and the TUI), with
no restart needed.

### 3. Install

```bash
git clone https://github.com/saidburkhanmukhtorov/telegram-vim.git
cd telegram-vim
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate      # puts `tg` on your PATH
```

### 4. Configure

```bash
cp config.example.toml config.toml   # or ~/.config/telegram-vim/config.toml
$EDITOR config.toml                  # set api_id, api_hash, folder
```

The config is looked up in `$TG_CONFIG`, then `./config.toml`, then
`~/.config/telegram-vim/config.toml`. `TG_API_ID` / `TG_API_HASH` env vars override
the file.

`config.toml`, `*.session`, the allowlist cache, and logs are in `.gitignore`.

### 5. Log in

```bash
tg login
```

Enter your phone number, the code Telegram sends you, and your 2FA password if
you have one. The session is stored at `<data_dir>/work.session` with mode
`0600` (directory `0700`).

## Interactive app

```bash
tg          # or: tg ui
```

Vim-style: when a list is focused you're in **NORMAL** mode and letters are
commands; when the input box is focused you're typing. **Esc** always steps
back one level. Press **?** anywhere for this list.

**Chat list**

| Key | Does |
|---|---|
| `j` `k` / ↓ ↑ | move |
| Enter | open chat |
| `s` or `/` | search chats: fuzzy (`bknd` → "Backend"), or `/regex` |
| Esc | leave search, then clear the filter |
| Ctrl+R | reload |
| `n` | notifications: sound → silent → off (Ctrl+N works anywhere) |
| `q` | quit |

**Chat — NORMAL mode**

| Key | Does |
|---|---|
| `j` `k` / ↓ ↑ | move between messages (`k` at the top loads older ones) |
| `G` | jump to latest |
| `i` / `a` | write a message (INSERT) — Enter sends, you stay in INSERT to keep chatting. In groups, type `@` for member suggestions and Tab to complete |
| `r` | reply to the selected message |
| `e` | edit your message (text is pre-filled; Enter saves) |
| `d` | delete the selected message / album for everyone (asks y/n first) |
| Enter | photo/file → download + open in your default app; reply → jump to the original |
| `o` | open photo/file |
| `u` | go to the message this one replies to (repeat to climb the thread) |
| Ctrl+O | jump back to where you were |
| `s` or `/` | search this chat; `n` / `N` next / previous match |
| Space (hold) | record a voice message — release to send. A quick tap = hands-free: records until you tap Space again. Esc cancels |
| `p` | paste from clipboard: a copied image (screenshot) or copied file(s) → type an optional caption → Enter sends |
| `y` | copy message text |
| `v` | open the message to select part of it (mouse drag or Shift+arrows), then `y` / Ctrl+C |
| `w` | who read your message and when (groups: list of readers; private: read time, if Telegram allows) |
| Esc / `q` | back to chat list |

**Marks:** `✓` sent · `✓✓` read · `(edited)` · a red `@you` tag on messages that
mention you. In the chat list a red `@N` badge counts unread mentions (and a
toast pops up when someone tags you in another chat).

Media is colored by type, one attachment per line, caption on the next line:
🖼 photo (magenta) · 🎬 video (blue) · 🎤 voice (yellow) · 📄 file (green).
Albums (several photos/videos sent together) show as a single block.

Copying: `y` copies the whole message (via `wl-copy`/`xclip` *and* the terminal's
OSC 52 clipboard). With the mouse, hold **Shift** while dragging to use your
terminal's own selection.

Attachments show inline as `[photo 1.2MB]`, `[file report.pdf 2.3MB]`,
`[voice 0:12]`, etc. Opened files are saved to
`download_dir/<chat id>/<msg id>_<name>` and reused on the next open.

**Voice messages** are recorded with `ffmpeg` (Opus, like the official apps)
from your default microphone (`[voice] source` in config; list with
`pactl list short sources`). Terminals only report key presses, not releases,
so "hold" is detected from keyboard autorepeat: if releasing Space doesn't send
or it sends too early, tune `[voice] release_ms` / `tap_ms` to your repeat
settings. Clips under 0.7s are discarded.

Clipboard paste needs `wl-clipboard` (Wayland) or `xclip` (X11); on macOS it
handles copied file paths, and images if `pngpaste` is installed.

Chats are ordered by last message. New messages arrive live: in the open
chat they appear immediately (and the chat is marked read). If you've scrolled
up, the selection stays put and the mode bar shows "↓ N new · G to jump".
Search only ever looks inside the folder. Warnings go to `<data_dir>/tg.log`.

**Notifications.** New messages from your work chats pop up as desktop
notifications, but only when you can't already see them: the terminal is in
the background, or you're inside a different chat. Set the mode with
`[notifications] mode` in config: `sound` (popup + sound), `silent` (popup, no
sound; the default) or `off` (no popups and no in-app mention toasts; unread
badges still update). `n` on the chat list or Ctrl+N anywhere switches mode
until you restart; the current mode shows in the top bar. Linux uses
`notify-send` and plays the sound with `pw-play`/`paplay`; macOS uses
`osascript`. If you also run the official Telegram app, turn its notifications
off, or you'll get every popup twice, from personal chats too.

## One-shot commands

Chats can be referenced by name (fuzzy, case-insensitive), `@username`, or
numeric id. Matching only ever searches the allowed folder. Ambiguous names
are an error, not a guess.

```bash
tg list                           # allowed chats, unread counts, @mention counts
tg read "backend team"            # last 50 messages
tg read backend -n 200            # prefix match, 200 messages
tg read @alice -n 10
tg send backend "deploy done ✅"
tg reply backend 4812 "on it"          # reply to message #4812
tg send-file backend ./report.pdf -c "Q3 numbers" [-r 4812]
tg download backend 4815               # saves to download_dir
tg listen                         # live stream; Ctrl-C to stop
tg logout                         # end this session on Telegram's side
```

`tg read` output looks like:

```
── Backend Team ──
09:14  Alice  #4812
  can someone review #231?

09:20  You  #4813  ✓✓
  ↳ reply to #4812 Alice: can someone review #231?
  on it
```

## How the allowlist works

`tg/guard.py` is the single source of truth.

- On startup it calls `messages.GetDialogFilters`, finds the folder by title,
  and builds the allowlist from `pinned_peers + include_peers` only.
- `Guard.check(chat_id)` is the only way to get the peer needed to talk to a
  chat. `GuardedClient` (the only Telegram API surface the rest of the app
  uses) calls it for every read, send, reply, react, download, forward (both
  source and destination), pin, and mark-as-read.
- Incoming messages are filtered against the *live* allowlist on every event.
- `UpdateDialogFilter` events trigger a refresh. If the folder is deleted or
  gains category rules while running, the allowlist is **cleared** (fail closed).
- The last good allowlist is cached at `<data_dir>/allowlist.json` (0600). It
  is used only when the connection works but Telegram fails to return your
  folders (a network drop or Telegram error mid-startup). It is **not** an
  offline mode: without a connection the app can't log in, so it stops. Any
  other error stops startup instead of silently using the cache.
- Changes (added/removed chat ids) are logged to `<data_dir>/allowlist.log`.

Rate limits (`FloodWaitError`): waits of up to 60 seconds are sat out
automatically (the app shows "Telegram rate limit: waiting Ns…"). Longer ones
stop with a clear "try again in N min" error instead of looking frozen.
Updates are event-driven, with no polling.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
```

No Telegram account or network is needed: the tests use fakes, and nothing
pops real notifications or touches your clipboard.

The guard tests assert that non-allowed chat ids are blocked for every action
and never reach the Telegram client.

## License

[MIT](LICENSE)
