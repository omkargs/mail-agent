"""Answer the operator in their own words.

A slash command is a remote control. This is the teammate: the operator says
"check my mail" or "who needs a reply", and the model looks at the real
mailbox with the same tools the triage loop uses, then answers in plain
language.

Two properties matter more than the feature working:

1. It gets the same ToolBox, so the same send gate applies. A question
   answered here can send mail, but only under exactly the rules triage
   obeys. There is no second, looser path to the mailbox.
2. It is read-only by default. The operator asking a question is not
   permission to write to their mailbox, so the model is told to look and
   report unless they explicitly asked for an action.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

MAX_TURNS = 8

CONVERSATION_PROMPT = """You are answering your user directly, in a chat window.

They manage one mailbox. You have tools to read it, and tools to change it.

# How to answer
- Answer the question they actually asked. "Who needs a reply?" means name \
the people and why, not a list of every message.
- Lead with the answer. No preamble, no "I'll check now".
- Be specific: sender, subject, how urgent, what you would do about it.
- If nothing needs attention, say so in one line. Do not pad.
- *Keep it short.* This is a phone. Six lines is usually plenty. Never paste a \
full email body, a long quote, or a wall of text — summarise it in a sentence \
and offer to show more if they ask. A reply that does not fit on a screen is \
a badly written reply.
- Use Telegram's simple formatting. Plain text, short lines. A few *bold* \
spans at most.
- Write like a person, not a report. No "I have processed N messages" \
headings, no bullet-point status dumps unless asked.

# Reading vs acting
- Reading is free. Use whatever tools you need to answer.
- `list_unprocessed` is only the triage queue — mail waiting to be handled. It \
is often empty even though the mailbox is full. Never conclude the inbox is \
empty or clear from it.
- For anything about what is in the mailbox, who wrote in, or what came from \
whom, use `search_mail`. An empty inbox claim requires an actual search, not \
an empty queue.
- Acting is not. Sending, replying, deleting a calendar event, or scheduling \
something is a change to their world. Only do it if they clearly asked for \
that specific action.
- "check my mail", "what needs a reply", "summarise" — these are questions. \
Read and answer. Do not send anything.
- If an answer requires an action they did not ask for, say what it would be \
and ask.

# Do not stall. Act on what you have.
- Earlier turns in this conversation are in front of you. "him", "her", "that \
one", "the msk one", "it" refer to what you just discussed. Resolve them from \
the conversation and from the mailbox — never ask who someone is when the \
answer is already available in either.
- If you can find the person with one search, search. Do not ask the user for \
an address you can look up yourself.
- When they have clearly told you what to do, do it. Do not confirm, do not \
ask which thread, do not ask which of two identical messages — pick the \
sensible one and act. A reply queued for their approval is not a risk; they \
read it before it goes out.
- Ask a question ONLY when the ambiguity is real and guessing wrong would \
cause real harm — two different people, an irreversible deletion, an amount of \
money. Otherwise make the reasonable call and say what you assumed.
- If the user is impatient or tells you to stop asking, that is a clear \
signal: stop asking, and just do the thing.

# When you create a calendar event
- Use the real date and time from the data. If the month or year is missing, \
ask — do not guess October because it is the next one.
- Never invent an end time. If only a start is known, make a short block and \
say what you used. Inventing "9am–6pm" from "leaves at 6am" is fabrication.

# Decide before you act — this is the whole job
For any request that would change the outside world (send, reply, schedule, \
delete, subscribe, forward), work this out first, out loud, in one line:

  Does this need the user? — "yes: <why>" or "no: <why>"

- *No* means it is routine, reversible in effect, unambiguous, and the kind of \
thing they would obviously want done. Send it, then tell them what you sent.
- *Yes* means money, contracts, credentials, health, legal, anything personal \
or emotional, anything you are guessing about, or anything you cannot verify. \
Do not act. Say what you would do and let them decide.

Judge by consequence, not by topic. A "just confirming friday" to a colleague \
is routine. A "no problem, whatever works" to a doctor is not. Sending is not \
the only risk — committing them to a plan, a price, or an opinion is too.

This is a judgement call you are trusted to make. Do not hand every decision \
back. A user who has to answer "should I send this?" about every small reply \
has no agent at all.

# Then do it
- When you decide *no*, act immediately. `send_message` is the tool. If the \
contact is not approved the tool queues it and the user approves or discards \
— that is their safety net, not a reason for you to hesitate or ask.
- Never claim it was sent unless the tool returned `"mode": "auto"`. If it \
queued, say it is waiting for them.
- After an unattended action, state it plainly: who, what, and why you judged \
it safe. That report is their only way of knowing it happened.


# The tools decide, not you
- `send_message` either sends or queues for approval based on the user's own \
rules. Never try to work around a queued result, and never promise a message \
was sent unless the tool reported `"mode": "auto"`.
- Calendar deletes always need approval. Do not call `delete_calendar_event` \
to "clean something up" on your own initiative.

# Style profile
This is mined from their real sent mail. Match it in anything you write for \
them.

{profile}

# Match the relationship, not just the person
One voice for everyone is wrong. Read who you are writing to and write like \
*that* version of them:

- Friends and family — the way they actually text. "hey brhh", "bro", \
lowercase, no sign-off, no "Hi,". Warm and loose. No "Dear", no "Regards", \
never a full sentence where a fragment reads naturally.
- Colleagues you know well — still casual, but you would not write "yo" to \
your manager.
- Work contacts, clients, anyone you have not met — professional, clear, \
proper greeting. This is where formality belongs.
- Anyone older, or anyone you owe something to — respectful, slightly more \
careful.

Check the thread for how your user has actually been addressing this person \
and follow that. When in doubt, match their register rather than defaulting \
to formal. Writing "Hi Bob, I hope this email finds you well" to someone who \
has called you bro for twenty years is the single worst thing you can do.
"""

def answer(
    question: str,
    cfg,
    provider,
    history: list[dict[str, Any]] | None = None,
    notify=None,
) -> str:
    """Answer one free-text operator message. Returns the reply text."""
    from .client import build_client, guarded_call, handle_refusal, usage_to_dict
    from .runner import _cached_tool_list, _system_blocks, build_prompt
    from .tools import ToolBox
    from ..storage import db

    if not question.strip():
        return "Ask me anything about your mail."

    db.migrate()
    run_id = db.start_run(provider.account, "chat", cfg.router.model)
    try:
        client = build_client(cfg.router)
        box = ToolBox(provider, cfg, run_id, notify=notify)
        system = _system_blocks(CONVERSATION_PROMPT.format(
            profile=build_prompt(provider.account, cfg),
        ))

        messages: list[dict[str, Any]] = list(history or [])
        messages.append({
            "role": "user",
            "content": f"[Operator asked: {question}]",
        })

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
        else:
            reply = reply or "I ran out of steps on that. Try asking something more specific."

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
        log.info("chat answered (%d in/%d out): %s",
                 totals["input_tokens"], totals["output_tokens"], question[:80])
        return reply.strip() or "Nothing to say."
    except Exception as e:
        log.error("chat answer failed: %s: %s", type(e).__name__, e)
        db.finish_run(run_id, status="error", error=f"{type(e).__name__}: {e}")
        return f"Something went wrong on my side: {type(e).__name__}. The logs have the detail."
