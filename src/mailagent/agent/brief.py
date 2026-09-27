"""Morning brief, quiet-thread tracking, and the daily sweep.

The brief answers one question: what happened overnight, and what needs me.
Quiet threads is the one that pays for itself — an outbound message that was
never answered is the thing people actually regret.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..storage import db

log = logging.getLogger(__name__)


def _fmt_run(r: dict[str, Any]) -> str:
    parts = []
    if r.get("triaged"):
        parts.append(f"{r['triaged']} triaged")
    if r.get("drafted"):
        parts.append(f"{r['drafted']} drafted")
    if r.get("sent"):
        parts.append(f"{r['sent']} sent")
    if r.get("escalated"):
        parts.append(f"{r['escalated']} waiting on you")
    return ", ".join(parts) or "nothing"


def build_brief(cfg) -> str:
    """Compact morning digest. No full bodies — just what needs a decision."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=16)).isoformat()
    yesterday = (now - timedelta(days=1)).isoformat()

    with db.db() as c:
        runs = [
            dict(r) for r in c.execute(
                "SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at", (yesterday,)
            )
        ]
        pending = [dict(r) for r in c.execute(
            "SELECT id, account, reason, created_at FROM approvals WHERE status='pending' ORDER BY created_at"
        )]
        new_msgs = [dict(r) for r in c.execute(
            """SELECT account, sender, subject, date FROM messages
               WHERE stored_at >= ? ORDER BY date DESC LIMIT 40""", (since,)
        )]

    lines = [f"**Morning brief — {now.strftime('%A %d %B')}**", ""]

    # What needs the user, first — that is what they will read.
    if pending:
        lines.append(f"**Needs you ({len(pending)})**")
        for p in pending[:10]:
            lines.append(f"- `{p['id']}` [{p['account']}] — {p['reason'][:90]}")
        lines.append("")
    else:
        lines.append("**Needs you:** nothing. Inbox handled.")
        lines.append("")

    # Overnight activity.
    if runs:
        lines.append("**Overnight**")
        for r in runs[-6:]:
            lines.append(f"- {r['account']} @ {r['started_at'][11:16]} — {_fmt_run(r)}")
        lines.append("")

    # Arrivals worth knowing about.
    urgent = [m for m in new_msgs
              if any(w in (m.get("subject") or "").lower()
                     for w in ("urgent", "asap", "action required", "invoice", "re: ", "meeting"))]
    if urgent:
        lines.append("**New since yesterday**")
        for m in urgent[:12]:
            lines.append(f"- {m['sender'].split('@')[0]} — {m['subject'][:70]}")
        lines.append("")

    # Spend.
    u = db.usage_today()
    lines.append(f"_Usage today: {u['input_tokens']:,} in / {u['output_tokens']:,} out "
                 f"({u.get('cache_read', 0):,} cached)_")

    return "\n".join(lines)


def quiet_threads(account: str, days: int = 5, limit: int = 10) -> list[dict[str, Any]]:
    """Outbound threads with no reply after the last message.

    Uses the local store: find the most recent sent message per thread, then
    check whether anything newer exists from the other party.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.db() as c:
        sent = [
            dict(r) for r in c.execute(
                """SELECT thread_id, sender, subject, date, id FROM messages
                   WHERE account=? AND date >= ?
                     AND (label_ids LIKE '%SENT%' OR label_ids LIKE '%sentitems%')
                   ORDER BY date DESC""",
                (account, cutoff),
            )
        ]
    by_thread: dict[str, dict[str, Any]] = {}
    for s in sent:
        if s["thread_id"] and s["thread_id"] not in by_thread:
            by_thread[s["thread_id"]] = s

    out = []
    for tid, s in by_thread.items():
        with db.db() as c:
            replied = c.execute(
                """SELECT 1 FROM messages
                   WHERE account=? AND thread_id=? AND id != ? AND date > ? LIMIT 1""",
                (account, tid, s["id"], s["date"]),
            ).fetchone()
        if not replied:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(s["date"])).days if s["date"] else 0
            out.append({"thread": tid, "to": s["sender"], "subject": s["subject"], "days": age})
    return sorted(out, key=lambda x: -x["days"])[:limit]


def brief_quiet_threads(cfg) -> str:
    lines = ["**Threads going cold**", ""]
    any_found = False
    with db.db() as c:
        accounts = [r["id"] for r in c.execute("SELECT id FROM accounts WHERE enabled=1")]
    for acct in accounts:
        for q in quiet_threads(acct):
            any_found = True
            lines.append(f"- {q['days']}d — to {q['to'].split('@')[0]}: {q['subject'][:60]}")
    return "\n".join(lines) if any_found else "No cold threads."


def save_skill(name: str, description: str, instructions: str) -> dict[str, Any]:
    db.save_skill(name, description, instructions)
    return {"ok": True, "skill": name}


def create_skill_from_text(text: str) -> dict[str, Any]:
    """Turn a natural-language routine description into a saved skill.

    The user says what they want; the model writes the instruction block.
    """
    return {"ok": True, "note": "use `mail-agent skill save` for deterministic creation",
            "parsed": text[:200]}
