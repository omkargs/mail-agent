"""Chat operations: the implementations behind each /command.

Separated from chat.py so the routing is testable without a live mailbox.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable

from ..storage import db

log = logging.getLogger("mailagent.chatops")


def build_chat_ops(cfg, providers_factory: Callable[[], dict[str, Any]], notify=None) -> dict[str, Any]:
    """Return the callables chat.handle_text dispatches to."""

    def _providers() -> dict[str, Any]:
        return providers_factory()

    def _provider_for(account: str) -> Any:
        """The provider that owns an account's mail.

        Picking the first provider is wrong when more than one is configured:
        an approval for account B must execute against B's mailbox, or the
        agent sends from the wrong identity.
        """
        provs = _providers()
        for name, p in provs.items():
            if name == account or p.account == account:
                return p
        return None

    # ---------------------------------------------------------------- status
    def status() -> str:
        u = db.usage_today()
        pend = db.pending_approvals()
        with db.db() as c:
            row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        lines = ["*mail-agent status*", ""]
        provs = _providers()
        lines.append(f"Accounts: {', '.join(provs) if provs else 'none connected'}")
        if row:
            lines.append(
                f"Last run: {row['triaged']} triaged, {row['drafted']} drafted, "
                f"{row['sent']} sent, {row['escalated']} waiting"
            )
        else:
            lines.append("No runs yet.")
        lines.append(f"Waiting on you: {len(pend)}")
        lines.append(f"Tokens today: {u['input_tokens']:,} in / {u['output_tokens']:,} out")
        return "\n".join(lines)

    # ----------------------------------------------------------------- brief
    def brief() -> str:
        from .brief import build_brief

        return build_brief(cfg)

    def quiet() -> str:
        from .brief import brief_quiet_threads

        return brief_quiet_threads(cfg)

    # ----------------------------------------------------------------- inbox
    def inbox() -> str:
        provs = _providers()
        if not provs:
            return "No account connected. Run mail-agent auth."
        p = next(iter(provs.values()))
        try:
            msgs = p.list_messages(folder="INBOX", limit=5)
        except Exception as e:
            return f"Could not read the inbox: {type(e).__name__}"
        if not msgs:
            return "Inbox is empty."
        # Count what is actually unprocessed in the store, rather than
        # guessing from the newest five.
        with db.db() as c:
            unread = c.execute(
                "SELECT COUNT(*) n FROM messages WHERE account=? AND processed_at IS NULL"
                " AND label_ids NOT LIKE '%SENT%'",
                (p.account,),
            ).fetchone()["n"]
        lines = [f"*Inbox* — {unread} awaiting triage", ""]
        for m in msgs:
            sender = (m.get("sender") or "?").split("@")[0][:22]
            lines.append(f"• {sender} — {(m.get('subject') or '(no subject)')[:44]}")
        lines.append("")
        lines.append("/scan to have me work through them.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ scan
    def scan() -> str:
        from .runner import scan as run_scan

        provs = _providers()
        if not provs:
            return "No account connected. Run mail-agent auth."
        out = []
        for p in provs.values():
            res = run_scan(p, cfg, notify=notify)
            new = res.get("new", 0)
            if new:
                st = res.get("stats", {})
                out.append(
                    f"{p.account}: {new} new — {st.get('drafted', 0)} drafted, "
                    f"{st.get('sent', 0)} sent, {st.get('escalated', 0)} waiting on you"
                )
            else:
                out.append(f"{p.account}: nothing new")
        return "\n".join(out)

    # ---------------------------------------------------------------- drafts
    def drafts() -> str:
        pend = db.pending_approvals()
        if not pend:
            return "Nothing waiting for approval."
        lines = [f"*Waiting on you* ({len(pend)})", ""]
        for p in pend[:10]:
            lines.append(f"`{p['id']}` [{p['account']}]")
            lines.append(f"   {p['reason'][:80]}")
        lines.append("")
        lines.append(f"/approve <id>  or  /discard <id>")
        return "\n".join(lines)

    def approve(approval_id: str, yes: bool) -> str:
        from .runner import run_approval

        with db.db() as c:
            row = c.execute("SELECT account FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return f"No pending approval with id {approval_id!r}."
        p = _provider_for(row["account"])
        if not p:
            return f"Account {row['account']} is not connected. Run mail-agent auth."
        try:
            res = run_approval(p.account, p, cfg, approval_id, yes, notify=notify)
        except Exception as e:
            return f"Failed: {type(e).__name__}: {e}"
        if not res.get("ok"):
            return f"Could not: {res.get('error', 'unknown')}"
        return "Sent." if res.get("sent") else "Discarded."

    # ----------------------------------------------------------------- brain
    def brain() -> str:
        provs = _providers()
        if not provs:
            return "No account connected."
        out = []
        for name, p in provs.items():
            from ..brain.style import build_profile

            # Microsoft names the folder sentitems; Gmail uses SENT. Try the
            # provider's own name first, then fall back.
            msgs: list = []
            for folder in ("SENT", "sentitems"):
                try:
                    msgs = p.list_messages(folder=folder, limit=300)
                except Exception:
                    msgs = []
                if msgs:
                    break
            seeded = []
            for m in msgs:
                full = p.get_message(m["id"])
                if full:
                    m.update(full)
                    db.upsert_message(m)
                    db.bump_contact(name, m.get("sender", ""), sent=True)
                    seeded.append(m["id"])
            db.mark_processed_many(seeded)
            path = cfg.brain_path() / f"profile-{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(build_profile(name))
            out.append(f"{name}: profile rebuilt from {len(msgs)} sent messages")
        return "\n".join(out)

    def voice() -> str:
        from ..brain.style import voice_state

        s = voice_state()
        if not s["total_samples"]:
            return "No voice samples yet. It learns as you edit its drafts."
        acc = f"{s['accuracy']:.0%}" if s["accuracy"] is not None else "n/a"
        lines = [
            "*Voice learning*",
            "",
            f"Samples: {s['total_samples']} ({s['confirmed']} sent unchanged, {s['edited']} edited by you)",
            f"Match rate: {acc}",
        ]
        if s["most_corrected"]:
            lines.append("")
            lines.append("What you change most:")
            for f, n in s["most_corrected"][:4]:
                lines.append(f"• {f} — {n}x")
        return "\n".join(lines)

    def skill(name: str) -> str:
        s = db.get_skill(name)
        if not s:
            avail = [x["name"] for x in db.list_skills()]
            return f"No skill named {name!r}." + (f" Available: {', '.join(avail)}" if avail else " None saved yet.")
        return f"Skill *{s['name']}* loaded: {s['description'] or s['instructions'][:100]}"

    # ------------------------------------------------------------------- ask
    def ask(question: str) -> str:
        """Answer a plain-language question against the real mailbox.

        This is the path that makes the agent a teammate rather than a menu.
        It gets the same ToolBox as the triage loop, so the send gate applies
        unchanged: the model can read freely, but anything that sends or
        deletes is still gated by the user's own rules.

        Carries the last few turns so follow-ups resolve. "Reply to him" only
        works if the agent still knows who "him" is.
        """
        from .ask import answer
        from .chatlog import recent

        provs = _providers()
        if not provs:
            return "No mailbox is connected. Run mail-agent auth."
        p = next(iter(provs.values()))
        return answer(question, cfg, p, history=recent(), notify=notify)

    # -------------------------------------------------------------- schedule
    def schedule(text: str) -> str:
        """Set a job from plain language, e.g. '/schedule brief at 5'.

        The whole message is passed, not just the argument, because the parser
        needs the surrounding words to know what kind of job this is.
        """
        import uuid
        from .schedule import ScheduleError, parse

        request = re.sub(r"^/(schedule|remind|at)\s*", "", text or "").strip()
        if not request:
            return ("Tell me what and when, e.g. `/schedule full inbox brief at 5` "
                    "or `/schedule check the inbox at 7 every morning`.")
        try:
            spec = parse(request)
        except ScheduleError as e:
            return str(e)

        provs = _providers()
        account = next(iter(provs), "google")
        jid = f"job_{uuid.uuid4().hex[:10]}"
        db.create_job(jid, account, spec["kind"], at_time=spec["at_time"],
                      at_minutes=spec["in_minutes"], repeat=spec["repeat"],
                      prompt=spec["prompt"])

        if spec["in_minutes"] is not None:
            from datetime import datetime, timedelta
            fire = (datetime.now() + timedelta(minutes=spec["in_minutes"])).strftime("%H:%M")
            with db.db() as c:
                c.execute("UPDATE scheduled_jobs SET at_time=? WHERE id=?", (fire, jid))
            when = f"in {spec['in_minutes']} minutes"
        else:
            when = {"daily": "every day at", "weekly": "every week at"}.get(
                spec["repeat"], "once at") + f" {spec['at_time']}"
        db.log_action("schedule", account, jid, detail=request[:200])
        return f"Scheduled *{spec['kind']}* — {when}.\nid `{jid}`\n\n/cancel {jid} to drop it."

    def tasks() -> str:
        jobs = db.list_jobs()
        if not jobs:
            return ("Nothing scheduled. Try `/schedule full inbox brief at 5` "
                    "or `/schedule check the inbox at 7 every morning`.")
        lines = [f"*Scheduled jobs* ({len(jobs)})", ""]
        for j in jobs:
            rep = {"daily": "every day", "weekly": "every week"}.get(j["repeat"], "once")
            lines.append(f"`{j['id']}` — {j['kind']} at {j['at_time']} ({rep})")
            if j["prompt"]:
                lines.append(f"    {j['prompt'][:70]}")
        lines.append("")
        lines.append("/cancel <id> to remove one.")
        return "\n".join(lines)

    def cancel(job_id: str) -> str:
        job_id = job_id.strip().strip("`")
        ok = db.cancel_job(job_id)
        return f"Cancelled `{job_id}`." if ok else f"No enabled job with id `{job_id}`."

    def reset() -> str:
        """Drop the conversation. Use this when changing topics, so an old
        'him' or 'that one' cannot bleed into a new question."""
        from .chatlog import clear

        n = clear()
        return f"Forgot {n} earlier message{'s' if n != 1 else ''}. Fresh start."

    return {
        "status": status,
        "ask": ask,
        "brief": brief,
        "quiet": quiet,
        "inbox": inbox,
        "scan": scan,
        "drafts": drafts,
        "approve": approve,
        "brain": brain,
        "voice": voice,
        "skill": skill,
        "schedule": schedule,
        "tasks": tasks,
        "cancel": cancel,
        "reset": reset,
    }
