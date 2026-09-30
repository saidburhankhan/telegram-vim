"""Chat list ordering and search. Only ever filters the (already allowlisted) chats."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from ..guard import ChatInfo

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def by_recent(chats: Iterable[ChatInfo]) -> list[ChatInfo]:
    """Newest last message first; chats with no messages go to the bottom."""

    def key(c: ChatInfo):
        d = c.last_date
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d or _EPOCH

    return sorted(chats, key=key, reverse=True)


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)


def filter_chats(query: str, chats: Iterable[ChatInfo]) -> tuple[list[ChatInfo], str | None]:
    """Return ``(matches, error)``.

    * empty query  → all chats, most recent first
    * ``/pattern`` → case-insensitive regex on name and @username
    * otherwise    → fuzzy: prefix, then word-prefix, then substring, then
                     in-order letters ("bknd" → "Backend"); recency breaks ties
    """
    ordered = by_recent(chats)
    q = query.strip()
    if not q:
        return ordered, None

    if q.startswith("/"):
        pattern = q[1:]
        if not pattern:
            return ordered, None
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return [], f"bad regex: {e}"
        return [c for c in ordered if rx.search(c.name) or (c.username and rx.search(c.username))], None

    key = q.casefold().lstrip("@")
    tiers: list[list[ChatInfo]] = [[], [], [], []]
    for c in ordered:
        name = c.name.casefold()
        uname = (c.username or "").casefold()
        if name.startswith(key) or uname.startswith(key):
            tiers[0].append(c)
        elif any(w.startswith(key) for w in re.split(r"[\s\-_.]+", name)):
            tiers[1].append(c)
        elif key in name or key in uname:
            tiers[2].append(c)
        elif _is_subsequence(key.replace(" ", ""), name) or (uname and _is_subsequence(key, uname)):
            tiers[3].append(c)
    return [c for t in tiers for c in t], None
