"""Typer CLI: one-shot commands. All chat access goes through GuardedClient."""

from __future__ import annotations

import asyncio
import getpass
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from telethon.errors import RPCError

from . import media, render
from .client import ConfigError, load_config, open_guarded
from .client import login as do_login
from .client import logout as do_logout
from .guard import GuardError

app = typer.Typer(
    help="Work-focused Telegram client. Only chats in your configured folder exist here.\n\n"
    "Run `tg` with no command to open the interactive app.",
    add_completion=False,
)
console = Console(highlight=False)
err = Console(stderr=True, highlight=False)


def run(coro_fn: Callable[[], Awaitable[Any]]) -> None:
    try:
        asyncio.run(coro_fn())
    except (ConfigError, GuardError) as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(1) from None
    except RPCError as e:
        err.print(f"[red]Telegram error:[/red] {e}")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def _show_flood_wait(seconds: int) -> None:
    err.print(f"[yellow]rate limited by Telegram, waiting {seconds}s…[/yellow]")


def _open(cfg, live: bool = False):
    """``open_guarded`` that tells the user when a rate limit makes it wait."""
    return open_guarded(cfg, live=live, on_flood_wait=_show_flood_wait)


def prompt_phone() -> str:
    while True:
        raw = typer.prompt("Phone number with country code (e.g. +998901234567)")
        phone = re.sub(r"[\s\-()]", "", raw)
        if re.fullmatch(r"\+\d{7,15}", phone):
            return phone
        err.print("[yellow]Include the country code, starting with + (e.g. +998...).[/yellow]")


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        ui()


@app.command()
def ui() -> None:
    """Interactive app: searchable chat list, Enter to open, Esc to go back."""
    from .tui.app import TelegramVim

    cfg = _cfg()
    # Keep library warnings out of the full-screen UI.
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=cfg.data_dir / "tg.log", level=logging.WARNING, force=True)

    async def main():
        async with _open(cfg, live=True) as gc:
            gc.on_flood_wait = None  # printing would garble the full-screen UI; it sets its own
            await TelegramVim(
                gc, cfg.folder, cfg.download_dir, cfg.data_dir / "tmp",
                voice_source=cfg.voice_source, voice_release_ms=cfg.voice_release_ms,
                voice_tap_ms=cfg.voice_tap_ms, notifications=cfg.notifications,
            ).run_async()

    run(main)


@app.command()
def login() -> None:
    """Log in with phone number, login code, and 2FA password if enabled."""
    cfg = _cfg()

    async def main():
        who = await do_login(
            cfg,
            phone=prompt_phone,
            code=lambda: typer.prompt("Login code (sent to your Telegram app)"),
            password=lambda: getpass.getpass("2FA password: "),
        )
        console.print(f"[green]Logged in as {who}.[/green] Session: {cfg.session_path}")
        async with _open(cfg) as gc:
            n = len(gc.guard.allowed_ids)
            console.print(f"Folder [bold]{cfg.folder!r}[/bold] allows {n} chat(s). Try `tg list`.")

    run(main)


@app.command()
def logout() -> None:
    """Terminate this session on Telegram's side."""
    cfg = _cfg()

    async def main():
        await do_logout(cfg)
        console.print("Logged out.")

    run(main)


@app.command("list")
def list_chats() -> None:
    """Allowed chats with unread counts."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            chats = sorted(gc.chats(), key=lambda c: c.name.casefold())  # loaded by open_guarded
            if gc.guard.source == "cache":
                err.print("[yellow]warning: using cached allowlist (Telegram unreachable)[/yellow]")
            if not chats:
                console.print(f"No chats in folder {cfg.folder!r}. Add some in the official app.")
                return
            for c in chats:
                unread = f"[bold yellow]{c.unread:>4}[/bold yellow]" if c.unread else "   ·"
                mention = f" [red]@{c.unread_mentions}[/red]" if c.unread_mentions else ""
                uname = f" [dim]@{c.username}[/dim]" if c.username else ""
                console.print(f"{unread}  {render.escape(c.name)}{uname} [dim]({c.kind}, {c.peer_id})[/dim]{mention}")

    run(main)


@app.command()
def read(
    chat: str = typer.Argument(..., help="Chat name (fuzzy), @username, or id"),
    n: int = typer.Option(50, "-n", "--limit", min=1, max=500, help="Number of messages"),
) -> None:
    """Show recent messages with sender, time, message ID, and reply context."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            info = await gc.resolve(chat)
            msgs, by_id = await gc.get_history(info.peer_id, limit=n)
            console.print(f"[bold]── {render.escape(info.name)} ──[/bold]")
            for album in render.group_albums(msgs):
                console.print(render.format_message(
                    album[0], by_id, read_max=info.read_outbox_max_id, album=album))

    run(main)


@app.command()
def send(
    chat: str = typer.Argument(..., help="Chat name (fuzzy), @username, or id"),
    text: str = typer.Argument(..., help="Message text"),
) -> None:
    """Send a text message."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            info = await gc.resolve(chat)
            m = await gc.send_message(info.peer_id, text)
            console.print(f"[green]sent[/green] #{m.id} → {render.escape(info.name)}")

    run(main)


@app.command()
def reply(
    chat: str = typer.Argument(..., help="Chat name (fuzzy), @username, or id"),
    msg_id: int = typer.Argument(..., help="Message ID to reply to (shown as #123)"),
    text: str = typer.Argument(..., help="Reply text"),
) -> None:
    """Reply to a specific message."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            info = await gc.resolve(chat)
            m = await gc.reply(info.peer_id, msg_id, text)
            console.print(f"[green]replied[/green] #{m.id} → #{msg_id} in {render.escape(info.name)}")

    run(main)


@app.command()
def download(
    chat: str = typer.Argument(..., help="Chat name (fuzzy), @username, or id"),
    msg_id: int = typer.Argument(..., help="Message ID with the attachment"),
) -> None:
    """Save a message's photo/file to download_dir."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            info = await gc.resolve(chat)
            msgs = await gc.get_messages_by_id(info.peer_id, [msg_id])
            if not msgs:
                err.print(f"[red]error:[/red] message #{msg_id} not found in {info.name}")
                raise typer.Exit(1)
            dest = media.download_path(cfg.download_dir, info.peer_id, msgs[0])
            if dest is None:
                err.print(f"[red]error:[/red] message #{msg_id} has no downloadable attachment")
                raise typer.Exit(1)
            dest.parent.mkdir(parents=True, exist_ok=True)
            path = await gc.download(info.peer_id, msg_id, str(dest))
            console.print(f"[green]saved[/green] {path}")

    run(main)


@app.command("send-file")
def send_file(
    chat: str = typer.Argument(..., help="Chat name (fuzzy), @username, or id"),
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="File to send"),
    caption: str = typer.Option(None, "--caption", "-c", help="Optional caption"),
    reply_to: int = typer.Option(None, "--reply-to", "-r", help="Message ID to reply to"),
) -> None:
    """Send a photo or file."""
    cfg = _cfg()

    async def main():
        async with _open(cfg) as gc:
            info = await gc.resolve(chat)
            m = await gc.send_file(info.peer_id, str(path), caption=caption, reply_to=reply_to)
            console.print(f"[green]sent[/green] #{m.id} {path.name} → {render.escape(info.name)}")

    run(main)


@app.command()
def listen() -> None:
    """Live stream of new messages from allowed chats (Ctrl-C to stop)."""
    cfg = _cfg()

    async def main():
        async with _open(cfg, live=True) as gc:

            async def on_msg(event):
                await event.get_sender()
                info = gc.chat_info(event.chat_id)
                name = info.name if info else str(event.chat_id)
                console.print(render.format_message(event.message, chat_name=name))

            def on_change(added, removed):
                console.print(f"[yellow]folder updated: +{len(added)} −{len(removed)} chat(s)[/yellow]")

            gc.guard.on_change(on_change)
            gc.on_new_message(on_msg)
            console.print(
                f"[dim]Listening to {len(gc.guard.allowed_ids)} chat(s) in {cfg.folder!r}. Ctrl-C to stop.[/dim]"
            )
            await gc.run_until_disconnected()

    run(main)


def _cfg():
    try:
        return load_config()
    except ConfigError as e:
        err.print(f"[red]error:[/red] {e}")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
