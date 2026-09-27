"""SQLite storage. WAL, migrations, and the idempotency guarantees the agent
depends on.

Design rule: a message is processed exactly once. `cursors` tracks the high
water mark per account; `messages.processed_at` is the belt-and-braces check.
Re-running the agent after a crash must never re-send.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import DB_PATH, DATA_DIR

_lock = threading.RLock()

SCHEMA = [
    # ---------- accounts ----------
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id            TEXT PRIMARY KEY,          -- 'google' | 'microsoft'
        address       TEXT,
        display_name  TEXT,
        auto_send     INTEGER NOT NULL DEFAULT 0,
        calendar      INTEGER NOT NULL DEFAULT 1,
        enabled       INTEGER NOT NULL DEFAULT 1,
        updated_at    TEXT
    )
    """,
    # ---------- messages ----------
    """
    CREATE TABLE IF NOT EXISTS messages (
        id            TEXT PRIMARY KEY,          -- provider message id
        account       TEXT NOT NULL,
        thread_id     TEXT,
        sender        TEXT,
        sender_name   TEXT,
        recipients    TEXT,
        subject       TEXT,
        snippet       TEXT,
        body          TEXT,
        date          TEXT,
        label_ids     TEXT,                      -- JSON array
        has_attach    INTEGER NOT NULL DEFAULT 0,
        size_bytes    INTEGER,
        processed_at  TEXT,                      -- set once the agent has triaged it
        stored_at     TEXT NOT NULL,
        FOREIGN KEY (account) REFERENCES accounts(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_msg_account_date ON messages(account, date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_msg_processed ON messages(processed_at)",
    "CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(account, thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(account, sender)",
    # ---------- labels ----------
    """
    CREATE TABLE IF NOT EXISTS labels (
        id        TEXT NOT NULL,
        account   TEXT NOT NULL,
        name      TEXT NOT NULL,
        system    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (account, id)
    )
    """,
    # ---------- cursors (idempotency) ----------
    """
    CREATE TABLE IF NOT EXISTS cursors (
        account   TEXT NOT NULL,
        stream    TEXT NOT NULL,                -- 'inbox' | 'sent' | 'drafts'
        last_id   TEXT,
        last_run  TEXT,
        PRIMARY KEY (account, stream)
    )
    """,
    # ---------- agent runs ----------
    """
    CREATE TABLE IF NOT EXISTS runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        account       TEXT,
        trigger       TEXT,                      -- 'scan' | 'manual' | 'schedule'
        new_messages  INTEGER NOT NULL DEFAULT 0,
        triaged       INTEGER NOT NULL DEFAULT 0,
        drafted       INTEGER NOT NULL DEFAULT 0,
        sent          INTEGER NOT NULL DEFAULT 0,
        escalated     INTEGER NOT NULL DEFAULT 0,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read    INTEGER NOT NULL DEFAULT 0,
        cache_write   INTEGER NOT NULL DEFAULT 0,
        model         TEXT,
        status        TEXT,                      -- 'ok' | 'error' | 'capped'
        error         TEXT
    )
    """,
    # ---------- drafts ----------
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id            TEXT PRIMARY KEY,
        account       TEXT NOT NULL,
        run_id        INTEGER,
        in_reply_to   TEXT,
        to_addr       TEXT,
        subject       TEXT,
        body          TEXT,
        status        TEXT NOT NULL DEFAULT 'created',  -- created|sent|discarded
        created_at    TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )
    """,
    # ---------- approval queue (the human-in-the-loop gate) ----------
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id            TEXT PRIMARY KEY,
        account       TEXT NOT NULL,
        kind          TEXT NOT NULL,             -- 'send' | 'calendar_delete' | 'calendar_modify'
        payload       TEXT NOT NULL,             -- JSON
        reason        TEXT,                      -- why it escalated
        status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|expired|auto
        channel       TEXT,                      -- where it was pushed
        channel_msg_id TEXT,
        created_at    TEXT NOT NULL,
        decided_at    TEXT,
        decided_by    TEXT
    )
    """,
    # ---------- audit: every consequential action ----------
    """
    CREATE TABLE IF NOT EXISTS actions_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        account     TEXT,
        action      TEXT NOT NULL,               -- send|label|archive|calendar_create|...
        target      TEXT,
        actor       TEXT NOT NULL DEFAULT 'agent',  -- agent|user|system
        approval_id TEXT,
        detail      TEXT
    )
    """,
    # ---------- contacts (Email Brain) ----------
    """
    CREATE TABLE IF NOT EXISTS contacts (
        address      TEXT NOT NULL,
        account      TEXT NOT NULL,
        name         TEXT,
        sent_count   INTEGER NOT NULL DEFAULT 0,
        received_count INTEGER NOT NULL DEFAULT 0,
        last_contact TEXT,
        auto_send_ok INTEGER NOT NULL DEFAULT 0,
        approved_by_user INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (account, address)
    )
    """,
    # ---------- saved skills ----------
    """
    CREATE TABLE IF NOT EXISTS skills (
        name        TEXT PRIMARY KEY,
        description TEXT,
        instructions TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        run_count   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    # ---------- spend tracking ----------
    """
    CREATE TABLE IF NOT EXISTS usage_daily (
        day          TEXT PRIMARY KEY,            -- YYYY-MM-DD
        input_tokens   INTEGER NOT NULL DEFAULT 0,
        output_tokens  INTEGER NOT NULL DEFAULT 0,
        cache_read     INTEGER NOT NULL DEFAULT 0,
        cache_write    INTEGER NOT NULL DEFAULT 0,
        runs           INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ---------- operator-scheduled jobs ----------
    """
    CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id          TEXT PRIMARY KEY,
        account     TEXT NOT NULL,
        kind        TEXT NOT NULL,             -- brief|scan|quiet|cal|freeform
        prompt      TEXT,                      -- freeform instruction
        at_time     TEXT,                      -- HH:MM local, NULL if at_minutes set
        at_minutes  INTEGER,                   -- minutes from now, one-shot
        repeat      TEXT NOT NULL DEFAULT 'none', -- none|daily|weekly
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_run    TEXT,
        last_run_day TEXT,                    -- local YYYY-MM-DD of the last firing
        run_count   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    )
    """,
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def migrate() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- accounts

def upsert_account(
    id: str, address: str = "", display_name: str = "",
    auto_send: bool = False, calendar: bool = True, enabled: bool = True,
) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO accounts (id, address, display_name, auto_send, calendar, enabled, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 address=excluded.address, display_name=excluded.display_name,
                 auto_send=excluded.auto_send, calendar=excluded.calendar,
                 enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (id, address, display_name, int(auto_send), int(calendar), int(enabled), now()),
        )


def get_account(account_id: str) -> dict[str, Any] | None:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def list_accounts(enabled_only: bool = True) -> list[dict[str, Any]]:
    q = "SELECT * FROM accounts WHERE enabled=1" if enabled_only else "SELECT * FROM accounts"
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id")]


# ---------------------------------------------------------------- cursors

def get_cursor(account: str, stream: str) -> str | None:
    with db() as c:
        row = c.execute("SELECT last_id FROM cursors WHERE account=? AND stream=?", (account, stream)).fetchone()
    return row["last_id"] if row else None


def set_cursor(account: str, stream: str, last_id: str) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO cursors (account, stream, last_id, last_run) VALUES (?,?,?,?)
               ON CONFLICT(account, stream) DO UPDATE SET last_id=excluded.last_id, last_run=excluded.last_run""",
            (account, stream, last_id, now()),
        )


# ---------------------------------------------------------------- messages

def upsert_message(msg: dict[str, Any]) -> bool:
    """Store a message. Returns True if it was new (not previously seen)."""
    labels = json.dumps(msg.get("label_ids", []))
    with db() as c:
        # A message is fetched before any account row may exist. Guarantee the
        # FK target rather than failing the insert.
        c.execute(
            """INSERT OR IGNORE INTO accounts (id, updated_at) VALUES (?,?)""",
            (msg["account"], now()),
        )
        exists = c.execute("SELECT 1 FROM messages WHERE id=?", (msg["id"],)).fetchone()
        c.execute(
            """INSERT INTO messages
               (id, account, thread_id, sender, sender_name, recipients, subject,
                snippet, body, date, label_ids, has_attach, size_bytes, stored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 snippet=excluded.snippet, label_ids=excluded.label_ids,
                 has_attach=excluded.has_attach""",
            (
                msg["id"], msg["account"], msg.get("thread_id"), msg.get("sender"),
                msg.get("sender_name"), msg.get("recipients"), msg.get("subject"),
                msg.get("snippet", ""), msg.get("body", ""), msg.get("date"),
                labels, int(bool(msg.get("has_attach"))), msg.get("size_bytes"), now(),
            ),
        )
    return not exists


def mark_processed(message_id: str) -> None:
    with db() as c:
        c.execute("UPDATE messages SET processed_at=? WHERE id=?", (now(), message_id))


def unprocessed(account: str, limit: int = 50) -> list[dict[str, Any]]:
    """Messages awaiting triage.

    Sent mail is excluded. The Email Brain ingests the user's sent folder to
    learn their voice, and those rows are not triage material — without this
    filter the agent is handed the user's own sent history as untriaged inbox
    mail on every cycle.
    """
    with db() as c:
        rows = c.execute(
            """SELECT * FROM messages
               WHERE account=? AND processed_at IS NULL
                 AND label_ids NOT LIKE '%SENT%'
               ORDER BY date DESC LIMIT ?""",
            (account, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_processed_many(ids: list[str]) -> int:
    """Mark a batch processed. Used by the brain seeder, which consumes the
    sent folder and must not leave it looking like untriaged mail."""
    if not ids:
        return 0
    with db() as c:
        cur = c.executemany(
            "UPDATE messages SET processed_at=? WHERE id=?",
            [(now(), i) for i in ids],
        )
    return cur.rowcount


def count_unprocessed(account: str) -> int:
    with db() as c:
        return c.execute(
            """SELECT COUNT(*) FROM messages
               WHERE account=? AND processed_at IS NULL
                 AND label_ids NOT LIKE '%SENT%'""",
            (account,),
        ).fetchone()[0]


def recent_sent(account: str, limit: int = 500, since: str | None = None) -> list[dict[str, Any]]:
    """Sent mail for the Email Brain. Uses a label filter rather than an API call
    so it works identically across providers."""
    q = "SELECT * FROM messages WHERE account=? AND (label_ids LIKE '%SENT%' OR label_ids LIKE '%sent%')"
    params: list[Any] = [account]
    if since:
        q += " AND date >= ?"
        params.append(since)
    q += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    with db() as c:
        return [dict(r) for r in c.execute(q, params)]


# ---------------------------------------------------------------- runs

def start_run(account: str, trigger: str, model: str = "") -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO runs (started_at, account, trigger, model) VALUES (?,?,?,?)",
            (now(), account, trigger, model),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, **stats: Any) -> None:
    fields = ["finished_at=?"]
    params: list[Any] = [now()]
    allowed = {
        "new_messages", "triaged", "drafted", "sent", "escalated",
        "input_tokens", "output_tokens", "cache_read", "cache_write", "status", "error",
    }
    for k, v in stats.items():
        if k in allowed:
            fields.append(f"{k}=?")
            params.append(v)
    params.append(run_id)
    with db() as c:
        c.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id=?", params)


def record_usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO usage_daily (day, input_tokens, output_tokens, cache_read, cache_write, runs)
               VALUES (?,?,?,?,?,1)
               ON CONFLICT(day) DO UPDATE SET
                 input_tokens = input_tokens + excluded.input_tokens,
                 output_tokens = output_tokens + excluded.output_tokens,
                 cache_read = cache_read + excluded.cache_read,
                 cache_write = cache_write + excluded.cache_write,
                 runs = runs + 1""",
            (today(), inp, out, cache_read, cache_write),
        )


def usage_today() -> dict[str, int]:
    with db() as c:
        row = c.execute("SELECT * FROM usage_daily WHERE day=?", (today(),)).fetchone()
    if not row:
        return {"input_tokens": 0, "output_tokens": 0, "runs": 0}
    return dict(row)


# ---------------------------------------------------------------- approvals

def create_approval(
    id: str, account: str, kind: str, payload: dict[str, Any], reason: str = ""
) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO approvals (id, account, kind, payload, reason, status, created_at)
               VALUES (?,?,?,?,?,'pending',?)""",
            (id, account, kind, json.dumps(payload), reason, now()),
        )


def pending_approvals(account: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM approvals WHERE status='pending'"
    params: list[Any] = []
    if account:
        q += " AND account=?"
        params.append(account)
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created_at", params)]


def resolve_approval(id: str, status: str, by: str = "user") -> dict[str, Any] | None:
    with db() as c:
        c.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            (status, now(), by, id),
        )
        row = c.execute("SELECT * FROM approvals WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def mark_approval_pushed(id: str, channel: str, channel_msg_id: str) -> None:
    with db() as c:
        c.execute("UPDATE approvals SET channel=?, channel_msg_id=? WHERE id=?", (channel, channel_msg_id, id))


# ---------------------------------------------------------------- scheduled jobs

def create_job(
    id: str, account: str, kind: str, at_time: str | None = None,
    at_minutes: int | None = None, repeat: str = "none", prompt: str = "",
) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO scheduled_jobs
               (id, account, kind, prompt, at_time, at_minutes, repeat, enabled, created_at)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (id, account, kind, prompt, at_time, at_minutes, repeat, now()),
        )


def due_jobs(local_hhmm: str, local_day: str = "") -> list[dict[str, Any]]:
    """Enabled jobs whose local clock time has arrived and that are due.

    last_run_day is compared against today so a daily job fires once per day,
    not once per supervisor cycle. It is a separate column because the time
    string carries no date — deriving one from "23:59" silently never matched,
    and a daily job re-fired every 5 minutes.
    """
    day = local_day or datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        rows = c.execute(
            """SELECT * FROM scheduled_jobs
               WHERE enabled=1 AND at_time IS NOT NULL AND at_time <= ?
                 AND (last_run IS NULL OR last_run_day IS NULL OR last_run_day < ?)
               ORDER BY at_time""",
            (local_hhmm, day),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_job_ran(id: str, repeat: str) -> None:
    """Record a firing. A one-shot job disables itself afterwards."""
    stamp = now()
    day = datetime.now().strftime("%Y-%m-%d")
    with db() as c:
        c.execute(
            "UPDATE scheduled_jobs SET last_run=?, last_run_day=?, run_count=run_count+1"
            " WHERE id=?",
            (stamp, day, id),
        )
        if repeat == "none":
            c.execute("UPDATE scheduled_jobs SET enabled=0 WHERE id=?", (id,))


def list_jobs(include_disabled: bool = False) -> list[dict[str, Any]]:
    q = "SELECT * FROM scheduled_jobs"
    if not include_disabled:
        q += " WHERE enabled=1"
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY at_time, created_at")]


def cancel_job(id: str) -> bool:
    with db() as c:
        cur = c.execute("UPDATE scheduled_jobs SET enabled=0 WHERE id=?", (id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------- audit

def log_action(
    action: str, account: str = "", target: str = "", actor: str = "agent",
    approval_id: str = "", detail: str = "",
) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO actions_log (ts, account, action, target, actor, approval_id, detail)
               VALUES (?,?,?,?,?,?,?)""",
            (now(), account, action, target, actor, approval_id, detail[:2000]),
        )


# ---------------------------------------------------------------- contacts

def upsert_contact(account: str, address: str, name: str = "") -> None:
    with db() as c:
        c.execute(
            """INSERT INTO contacts (address, account, name) VALUES (?,?,?)
               ON CONFLICT(account, address) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), contacts.name)""",
            (address, account, name),
        )


def bump_contact(account: str, address: str, sent: bool = False, received: bool = False) -> None:
    """Record contact with someone.

    Normalise the address on write. It was stored raw — so a contact arrived as
    'Alice Example <alice@example.com>' — while every lookup went
    through normalize_address, which returns the bare address. The two never
    matched, so the contact table was a write-only log and no correspondent
    could ever be found again.
    """
    from ..providers.base import normalize_address

    address = normalize_address(address)
    if not address:
        return
    # Skip the user's own address. The mailbox address is recorded on the
    # accounts row by the CLI, so read it from there — it is the authoritative
    # value and does not require loading config inside the DB layer.
    self_addr = ""
    with db() as c:
        row = c.execute("SELECT address FROM accounts WHERE id=?", (account,)).fetchone()
    if row and row["address"]:
        self_addr = normalize_address(row["address"])
    if address and address == self_addr:
        return  # the user writing to themselves is not a contact
    with db() as c:
        c.execute(
            """INSERT INTO contacts (address, account, sent_count, received_count, last_contact)
               VALUES (?,?,?,?,?)
               ON CONFLICT(account, address) DO UPDATE SET
                 sent_count = sent_count + ?,
                 received_count = received_count + ?,
                 last_contact = excluded.last_contact""",
            (address, account, int(sent), int(received), now(), int(sent), int(received)),
        )


def set_contact_auto_send(account: str, address: str, ok: bool) -> None:
    """Approve or revoke unattended replies for an address.

    Upserts. This used to be a bare UPDATE, so approving an address the agent
    had never seen — exactly what the operator does when they say "yes, reply
    to them automatically" — silently did nothing and the agent kept
    escalating.
    """
    with db() as c:
        c.execute(
            """INSERT INTO contacts (address, account, auto_send_ok, approved_by_user)
               VALUES (?,?,?,?)
               ON CONFLICT(address, account) DO UPDATE SET
                 auto_send_ok=excluded.auto_send_ok,
                 approved_by_user=excluded.approved_by_user""",
            (address, account, int(ok), int(ok)),
        )


def list_contacts(account: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    q = "SELECT * FROM contacts"
    params: list[Any] = []
    if account:
        q += " WHERE account=?"
        params.append(account)
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY (sent_count+received_count) DESC LIMIT ?", params + [limit])]


def get_contact(account: str, address: str) -> dict[str, Any] | None:
    with db() as c:
        row = c.execute("SELECT * FROM contacts WHERE account=? AND address=?", (account, address)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- skills

def save_skill(name: str, description: str, instructions: str) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO skills (name, description, instructions, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description, instructions=excluded.instructions,
                 updated_at=excluded.updated_at""",
            (name, description, instructions, now(), now()),
        )


def get_skill(name: str) -> dict[str, Any] | None:
    with db() as c:
        row = c.execute("SELECT * FROM skills WHERE name=? AND enabled=1", (name,)).fetchone()
    return dict(row) if row else None


def list_skills() -> list[dict[str, Any]]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM skills WHERE enabled=1 ORDER BY name")]


def delete_skill(name: str) -> bool:
    with db() as c:
        return c.execute("DELETE FROM skills WHERE name=?", (name,)).rowcount > 0


# ---------------------------------------------------------------- labels

def upsert_label(account: str, id: str, name: str, system: bool = False) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO labels (id, account, name, system) VALUES (?,?,?,?)
               ON CONFLICT(account, id) DO UPDATE SET name=excluded.name""",
            (id, account, name, int(system)),
        )


def list_labels(account: str) -> list[dict[str, Any]]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM labels WHERE account=? ORDER BY name", (account,))]


# ---------------------------------------------------------------- drafts

def record_draft(
    id: str, account: str, run_id: int | None, in_reply_to: str,
    to_addr: str, subject: str, body: str,
) -> None:
    with db() as c:
        c.execute(
            """INSERT INTO drafts (id, account, run_id, in_reply_to, to_addr, subject, body, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (id, account, run_id, in_reply_to, to_addr, subject, body, now()),
        )


def list_drafts(account: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = "SELECT * FROM drafts WHERE status='created'"
    params: list[Any] = []
    if account:
        q += " AND account=?"
        params.append(account)
    with db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", params + [limit])]
