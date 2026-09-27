"""Turn data into a decision, and a decision into an action.

This is the loop the agent was missing. A scheduled job used to dump a block
of text: "3 messages, 2 need a reply, 1 has a meeting attached." That is
reporting, not judgement. The model never looked at it.

The flow now is:

    job fires -> model reads the data -> judges each item
              -> acts on the routine ones (calendar entry, ack)
              -> tells the user what it did and what needs them
              -> user replies -> same tools, same authority -> it acts

The model here is not asked to summarise. It is asked to decide, and the
tools it calls are the real ones, under the same send gate.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

MAX_TURNS = 8

ACT_PROMPT = """You are your user's agent. You are looking at data they \
asked you to watch, and you are about to decide what to do with it.

# What you have
A report from a scheduled job. It may be an inbox scan, a digest, a list of \
threads, or calendar data. It is raw — your job is to read it and decide.

# Decide, do not describe
For each item, one of:
- ACT: the action is obvious, routine, and reversible in effect. Do it with \
the tools. Do not ask.
- ASK: it needs a human — money, credentials, health, legal, commitments, \
anything personal, or anything you are not certain about. Do not act. Put \
the specific question in `escalate`.
- NONE: nothing to do. Say nothing about it.

Judge by consequence, not by topic or length. A meeting time mentioned in a \
mail is obvious — put it in the calendar. A meeting time that the user would \
need to negotiate, travel for, or decline is not.

# Actions available
- `create_calendar_event` — a date, time, or meeting stated in the data. \
Parse it into ISO 8601. If a time is missing, do not invent one: escalate \
and ask. Getting someone's availability wrong is worse than asking. Never \
pad a stated start time with a made-up end — use the real one, or a short \
block, and say which you used.
- `send_message` — an acknowledgement or routine reply that is clearly wanted. \
The tool decides whether it goes out or waits for approval; do not work \
around it.
- `escalate` — a question the user must answer.

# Reporting
Then tell the user, in a few lines, what you did and what needs them. \
Lead with anything that needs a decision. Say what you already handled so \
they do not have to check. Do not paste raw data or mail bodies back. \
No preamble, no "I've reviewed".

Be decisive. An agent that asks about everything is a menu, not an agent.
"""


def decide_and_act(
    data: str,
    question: str,
    cfg,
    provider,
    notify=None,
) -> str:
    """Read the data, act where it is safe, report what remains.

    Returns the text to send the operator.
    """
    from .client import build_client, guarded_call, handle_refusal, usage_to_dict
    from .runner import _cached_tool_list, _system_blocks
    from .tools import ToolBox
    from ..storage import db

    db.migrate()
    run_id = db.start_run(provider.account, "act", cfg.router.model)
    try:
        client = build_client(cfg.router)
        box = ToolBox(provider, cfg, run_id, notify=notify)
        system = _system_blocks(ACT_PROMPT)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": f"# Job: {question}\n\n# Data\n{data[:12000]}",
        }]

        totals = {"input_tokens": 0, "output_tokens": 0}
        reply = ""
        for _ in range(MAX_TURNS):
            resp = guarded_call(client, 
                model=cfg.router.model,
                max_tokens=cfg.router.max_tokens,
                system=system,
                tools=_cached_tool_list(),
                messages=messages,
            )
            refusal = handle_refusal(resp)
            if refusal:
                reply = refusal
                break

            u = usage_to_dict(resp.usage)
            totals["input_tokens"] += u["input_tokens"]
            totals["output_tokens"] += u["output_tokens"]
            db.record_usage(u["input_tokens"], u["output_tokens"],
                            u["cache_read"], u["cache_write"])

            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                reply = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                )
                break

            results = []
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                out = box.run(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str)[:8000],
                    "is_error": not out.get("ok", True),
                })
            if not results:
                break
            messages.append({"role": "user", "content": results})

        db.finish_run(
            run_id,
            triaged=box.stats["triaged"],
            drafted=box.stats["drafted"],
            sent=box.stats["sent"],
            escalated=box.stats["escalated"],
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            status="ok",
        )
        log.info("act loop: %d sent, %d escalated",
                 box.stats["sent"], box.stats["escalated"])
        return (reply.strip() or "Nothing needed doing."), box.stats
    except Exception as e:
        log.error("act loop failed: %s: %s", type(e).__name__, e)
        db.finish_run(run_id, status="error", error=f"{type(e).__name__}: {e}")
        return f"That check failed: {type(e).__name__}.", {}
