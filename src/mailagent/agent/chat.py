"""Chat commands — the operator's remote control.

Telegram is the control channel; email is the work channel. You message the
agent here, it acts on your mailbox, and results come back here.

Commands are deliberately small and explicit. Anything that could send mail
routes through the normal approval queue; a chat message can never bypass it.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any

log = logging.getLogger("mailagent.chat")

HELP = """\
*mail-agent* — your inbox, on autopilot.

*/status* — is it alive, what did it do
*/brief* — the morning digest
*/quiet* — threads going cold
*/inbox* — unread count and the 5 newest
*/scan* — process new mail now
*/drafts* — drafts waiting for you
*/approve <id>* — send a queued reply
*/discard <id>* — drop a queued reply
*/brain* — rebuild the voice profile
*/voice* — how well it has learned your writing
*/schedule <what and when>* — e.g. "brief at 5", "inbox at 7 every morning"
*/tasks* — list scheduled jobs
*/cancel <id>* — cancel a scheduled job
*/skill <name>* — run a saved automation
*/help* — this message
"""


def handle_text(
    text: str,
    cfg,
    providers: dict[str, Any],
    chat_ops: dict[str, Any],
) -> str | None:
    """Route one inbound chat message. Returns the reply text, or None to stay silent."""
    t = (text or "").strip()
    if not t:
        return None

    if not t.startswith("/"):
        # Plain language, not a command. Answer it against the real mailbox
        # with the same tools the triage loop uses, under the same send gate.
        from .chatlog import record

        record("user", t)
        reply = chat_ops["ask"](t)
        if reply:
            record("agent", reply)
        return reply

    try:
        parts = shlex.split(t)
    except ValueError:
        parts = t.split()
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("help", "start"):
        return HELP

    if cmd == "status":
        return chat_ops["status"]()

    if cmd == "brief":
        return chat_ops["brief"]()

    if cmd == "quiet":
        return chat_ops["quiet"]()

    if cmd == "inbox":
        return chat_ops["inbox"]()

    if cmd == "scan":
        return chat_ops["scan"]()

    if cmd == "drafts":
        return chat_ops["drafts"]()

    if cmd in ("approve", "discard", "deny"):
        if not arg:
            return f"Usage: /{cmd} <id> — run /drafts to see the waiting ids."
        return chat_ops["approve"](arg, cmd in ("approve",))

    if cmd == "brain":
        return chat_ops["brain"]()

    if cmd == "voice":
        return chat_ops["voice"]()

    if cmd in ("schedule", "remind", "at"):
        return chat_ops["schedule"](t)

    if cmd in ("tasks", "jobs"):
        return chat_ops["tasks"]()

    if cmd in ("cancel", "unschedule"):
        if not arg:
            return "Usage: /cancel <job-id> — run /tasks to see the ids."
        return chat_ops["cancel"](arg)

    if cmd == "skill":
        if not arg:
            return "Usage: /skill <name>"
        return chat_ops["skill"](arg)

    if cmd in ("reset", "forget", "clear"):
        return chat_ops["reset"]()

    return f"Unknown command /{cmd}. Try /help."
