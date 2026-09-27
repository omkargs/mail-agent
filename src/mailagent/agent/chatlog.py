"""Conversation memory.

Without this the agent has amnesia between messages: ask "what did msk say"
and then "reply to him" gets "who's him?" — because each message was a
fresh, context-free call. Pronouns, "the msk one", "that invoice" all need
the last few turns.

Persisted to SQLite rather than kept in a dict so a restart does not wipe the
thread mid-conversation, which is exactly when a user is most confused.
"""
from __future__ import annotations

import json
import logging

from ..storage import db

log = logging.getLogger(__name__)

MAX_TURNS = 8          # roughly the last 4 exchanges
MAX_CHARS = 1200       # truncate a long reply; the gist is what matters

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,          -- 'user' | 'agent'
    content TEXT NOT NULL
);
"""


def _ensure() -> None:
    with db.db() as c:
        c.execute(SCHEMA)


def record(role: str, content: str) -> None:
    """Append one turn. Never let a logging failure break a conversation."""
    if not content or not content.strip():
        return
    try:
        _ensure()
        with db.db() as c:
            c.execute("INSERT INTO chat_history (ts, role, content) VALUES (?,?,?)",
                      (db.now(), role, content.strip()[:MAX_CHARS]))
    except Exception as e:
        log.warning("could not record chat history: %s", type(e).__name__)


def recent(limit: int = MAX_TURNS) -> list[dict[str, str]]:
    """The last few turns, oldest first, ready for the API."""
    try:
        _ensure()
        with db.db() as c:
            rows = c.execute(
                "SELECT role, content FROM chat_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception as e:
        log.warning("could not read chat history: %s", type(e).__name__)
        return []
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear() -> int:
    """Forget the conversation. Used by /reset and after a topic change."""
    try:
        _ensure()
        with db.db() as c:
            cur = c.execute("DELETE FROM chat_history")
        return cur.rowcount
    except Exception as e:
        log.warning("could not clear chat history: %s", type(e).__name__)
        return 0
