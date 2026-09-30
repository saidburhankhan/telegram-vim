"""Config loading, Telethon client construction, login, and session handling."""

from __future__ import annotations

import os
import tomllib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from telethon import TelegramClient
from telethon.errors import PhoneNumberInvalidError

from . import notify
from .guard import Guard, GuardedClient

CONFIG_SEARCH = (
    Path("config.toml"),
    Path("~/.config/telegram-vim/config.toml").expanduser(),
)


class ConfigError(Exception):
    pass


@dataclass
class Config:
    api_id: int
    api_hash: str
    folder: str
    data_dir: Path
    download_dir: Path
    path: Path | None = None
    voice_source: str = "default"   # PulseAudio/PipeWire source for recording
    voice_release_ms: int = 250     # no key repeat for this long = Space released
    voice_tap_ms: int = 700         # no repeat at all within this = a tap (hands-free mode)
    notifications: str = "silent"   # desktop popups: sound | silent | off

    @property
    def session_path(self) -> Path:
        return self.data_dir / "work.session"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "allowlist.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "allowlist.log"


def find_config_file() -> Path | None:
    env = os.environ.get("TG_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise ConfigError(f"TG_CONFIG points to {p}, which does not exist")
        return p
    for p in CONFIG_SEARCH:
        if p.is_file():
            return p
    return None


def load_config() -> Config:
    path = find_config_file()
    data: dict = {}
    if path:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    api_id = os.environ.get("TG_API_ID") or data.get("api_id")
    api_hash = os.environ.get("TG_API_HASH") or data.get("api_hash")
    if not api_id or not api_hash:
        raise ConfigError(
            "api_id/api_hash missing. Put them in config.toml (see config.example.toml) "
            "or set TG_API_ID and TG_API_HASH."
        )
    try:
        api_id = int(api_id)
    except ValueError:
        raise ConfigError("api_id must be an integer") from None

    folder = data.get("folder")
    if not folder or not str(folder).strip():
        raise ConfigError('config must set folder, e.g. folder = "Work"')

    try:
        notifications = notify.check_mode(str(data.get("notifications", {}).get("mode", "silent")))
    except ValueError as e:
        raise ConfigError(str(e)) from None

    return Config(
        api_id=api_id,
        api_hash=str(api_hash),
        folder=str(folder),
        data_dir=Path(data.get("data_dir", "~/.local/share/telegram-vim")).expanduser(),
        download_dir=Path(data.get("download_dir", "~/Downloads/telegram-vim")).expanduser(),
        path=path,
        voice_source=str(data.get("voice", {}).get("source", "default")),
        voice_release_ms=int(data.get("voice", {}).get("release_ms", 250)),
        voice_tap_ms=int(data.get("voice", {}).get("tap_ms", 700)),
        notifications=notifications,
    )


def _secure_session_file(path: Path) -> None:
    """Pre-create the session file 0600 so SQLite never creates it world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def make_client(cfg: Config) -> TelegramClient:
    _secure_session_file(cfg.session_path)
    # Telethon sleeps automatically on FloodWait up to this threshold.
    client = TelegramClient(
        str(cfg.session_path.with_suffix("")),
        cfg.api_id,
        cfg.api_hash,
        flood_sleep_threshold=24 * 3600,
        device_model="telegram-vim",
        app_version="0.1.0",
    )
    return client


async def login(
    cfg: Config,
    phone: Callable[[], str],
    code: Callable[[], str],
    password: Callable[[], str],
) -> str:
    client = make_client(cfg)
    try:
        for attempt in range(3):
            number = phone()
            try:
                await client.start(phone=lambda: number, code_callback=code, password=password)
                break
            except PhoneNumberInvalidError:
                if attempt == 2:
                    raise
                print(f"Telegram says {number} is not a valid phone number. Try again.")
        me = await client.get_me()
        return f"{me.first_name or ''} {me.last_name or ''}".strip() + (
            f" (@{me.username})" if me.username else ""
        )
    finally:
        await client.disconnect()
        _secure_session_file(cfg.session_path)


async def logout(cfg: Config) -> None:
    client = make_client(cfg)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
    finally:
        await client.disconnect()


@asynccontextmanager
async def open_guarded(cfg: Config, live: bool = False) -> AsyncIterator[GuardedClient]:
    """Connect, load the folder allowlist, and yield the guarded API surface.

    ``live=True`` also subscribes to folder updates so the allowlist refreshes
    without restarting (used by long-running commands like ``listen`` and ``ui``).
    """
    client = make_client(cfg)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ConfigError("not logged in — run `tg login` first")
        me = await client.get_me(input_peer=True)
        self_id = me.user_id
        guard = Guard(cfg.folder, cfg.cache_path, cfg.log_path)
        await guard.load(client, self_id)
        gc = GuardedClient(client, guard, self_id)
        if live:
            gc.install_live_refresh()
        await gc.refresh_chats()
        yield gc
    finally:
        await client.disconnect()
