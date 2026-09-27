"""Email Brain: learn the user's voice, then keep learning it.

Two phases:

1. SEED  — mine Sent mail, produce an editable markdown style profile.
2. LEARN — after every send, diff the agent's draft against what the user
           actually sent. Every send is a labelled sample. Over time the
           profile converges on the real voice instead of the seed guess.

The profile is a file the user owns. The agent reads it and proposes edits; it
never silently overwrites. Every proposed change is shown in the approval
channel before it lands.

This is the part that makes "drafts in your writing" true rather than claimed.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..storage import db

log = logging.getLogger(__name__)

# 365 days, not 90. The 90-day window left only 15 usable samples out of 72,
# which is too thin to characterise anyone's writing — and it is the writing
# the agent imitates, so a small sample silently produces a bad voice.
SAMPLE_WINDOW_DAYS = 365
MIN_SAMPLES = 12

CONTRACTIONS = {
    "won't", "can't", "don't", "doesn't", "didn't", "isn't", "aren't", "wasn't",
    "weren't", "i'm", "i've", "i'll", "i'd", "you're", "you've", "you'll",
    "we're", "we've", "we'll", "they're", "it's", "that's", "there's", "let's",
    "haven't", "hasn't", "couldn't", "wouldn't", "shouldn't", "ain't", "gonna",
    "wanna", "yeah", "yep", "nope", "ok", "okay",
}

CLICHES = [
    "i hope this email finds you well", "hope you're well", "touching base",
    "circling back", "per my last email", "kindly", "please do not hesitate",
    "feel free to reach out", "at your earliest convenience", "as per",
    "further to", "with respect to", "dear sir", "dear madam", "sincerely yours",
    "best regards", "warm regards", "many thanks", "thanks in advance",
]


# --------------------------------------------------------------------- seed

# Bulk and machine mail is not a writing sample. Including it made 18 of 63
# "sent messages" identical outreach blasts, which pinned the profile to a
# single template and taught the agent to sound like a mail merge.
_MACHINE_MAIL = re.compile(
    r"this message was automatically generated|unsubscribe|"
    r"do not reply to this (e-?mail|message)|"
    r"you are receiving this (e-?mail|message) because|"
    r"if you no longer wish to receive",
    re.I,
)


def _sent_samples(account: str, limit: int = 400) -> list[dict[str, Any]]:
    """Recent hand-written sent messages.

    Filters out bulk mail. The voice the agent imitates should be the user's
    own writing, not the automated campaigns they send from the same account.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=SAMPLE_WINDOW_DAYS)).isoformat()
    rows = db.recent_sent(account, limit=limit, since=since)
    out = []
    for r in rows:
        body = (r.get("body") or "").strip()
        if len(body) <= 40:
            continue
        if _MACHINE_MAIL.search(body[:600]) or _MACHINE_MAIL.search(r.get("subject") or ""):
            continue
        out.append(r)
    return out


def _stats(texts: list[str]) -> dict[str, Any]:
    """Corpus-level features. No model call — cheap, deterministic, and
    these are the features the model is told to imitate."""
    sentences: list[str] = []
    for t in texts:
        sentences += [s.strip() for s in re.split(r"[.!?]+\s", t) if len(s.strip()) > 2]

    words = [w for t in texts for w in re.findall(r"[A-Za-z']+", t)]
    lower = [w.lower() for w in words]

    n_contractions = sum(1 for w in lower if w in CONTRACTIONS)
    n_cliches = sum(1 for t in texts for c in CLICHES if c in t.lower())

    openings = Counter()
    closings = Counter()
    greetings = Counter()
    for t in texts:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if lines:
            openings[re.sub(r"[^a-z ]", "", lines[0].lower())[:40]] += 1
        for ln in reversed(lines[-4:]):
            m = re.match(r"^(thanks|thank you|cheers|best|regards|warm|kind|all the best|talk soon|appreciate it)[,.!]?(.*)$", ln, re.I)
            if m:
                closings[ln.lower()[:40]] += 1
                break
        for ln in lines[:2]:
            if re.match(r"^(hi|hey|hello|dear|good morning|good afternoon|good evening)\b", ln, re.I):
                greetings[re.sub(r"[^a-z ]", "", ln.lower())[:40]] += 1
                break

    return {
        "sample_count": len(texts),
        "sentence_count": len(sentences),
        "avg_sentence_words": round(statistics.mean(len(s.split()) for s in sentences), 1) if sentences else 0,
        "median_sentence_words": statistics.median([len(s.split()) for s in sentences]) if sentences else 0,
        "longest_sentence": max((len(s.split()) for s in sentences), default=0),
        "avg_words_per_message": round(statistics.mean(len(t.split()) for t in texts), 1) if texts else 0,
        "contraction_rate": round(n_contractions / max(len(words), 1), 3),
        "cliche_hits": n_cliches,
        "question_rate": round(
            sum(1 for s in sentences if s.endswith("?")) / max(len(sentences), 1), 3
        ),
        "exclamation_rate": round(
            sum(1 for s in sentences if s.endswith("!")) / max(len(sentences), 1), 3
        ),
        "emoji_rate": round(
            sum(len(re.findall(r"[\U0001F300-\U0001FAFF]", t)) for t in texts) / max(len(texts), 1), 2
        ),
        "top_openings": openings.most_common(5),
        "top_closings": closings.most_common(5),
        "top_greetings": greetings.most_common(5),
        "vocabulary": [w for w, _ in Counter(lower).most_common(60)],
    }


def _contact_graph(account: str) -> list[dict[str, Any]]:
    rows = db.list_contacts(account, limit=25)
    out = []
    for r in rows:
        total = r["sent_count"] + r["received_count"]
        out.append({
            "address": r["address"],
            "name": r["name"] or "",
            "sent": r["sent_count"],
            "received": r["received_count"],
            "total": total,
            "last_contact": r["last_contact"],
            "auto_send_ok": bool(r["auto_send_ok"]),
        })
    return out


def build_profile(account: str) -> str:
    """Generate the markdown style profile from sent mail. Overwrites the
    generated section only; hand-written sections are preserved."""
    samples = _sent_samples(account)
    if len(samples) < MIN_SAMPLES:
        return (
            f"# Email Brain — {account}\n\n"
            f"Not enough sent mail yet ({len(samples)} usable samples, need {MIN_SAMPLES}).\n"
            f"Keep sending normally; re-run `mail-agent brain` in a few weeks.\n"
        )

    st = _stats([s["body"] for s in samples])
    contacts = _contact_graph(account)

    close_line = (
        "Very formal — no contractions, no cliches, long complete sentences."
        if st["contraction_rate"] < 0.01 and st["cliche_hits"] == 0
        else "Relaxed and direct — contractions, short sentences."
        if st["contraction_rate"] > 0.02
        else "Neutral professional."
    )

    lines = [
        f"# Email Brain — {account}",
        "",
        f"> Generated from {st['sample_count']} sent messages over the last {SAMPLE_WINDOW_DAYS} days.",
        "> Edit anything below. The agent reads this file and never overwrites it.",
        "",
        "## Voice",
        "",
        f"- Register: {close_line}",
        f"- Average sentence: {st['avg_sentence_words']} words (median {st['median_sentence_words']}, longest {st['longest_sentence']})",
        f"- Average message: {st['avg_words_per_message']} words",
        f"- Contractions: {st['contraction_rate']:.1%} of words",
        f"- Questions: {st['question_rate']:.1%} of sentences end with '?'",
        f"- Exclamations: {st['exclamation_rate']:.1%} of sentences end with '!'",
        f"- Emoji: {st['emoji_rate']} per message",
        "",
        "## Patterns",
        "",
        "**Greetings**",
    ]
    lines += [f"- {g} ({n}×)" for g, n in st["top_greetings"]] or ["- (none detected)"]
    lines += ["", "**Openings**"]
    lines += [f"- \"{o}\" ({n}×)" for o, n in st["top_openings"]] or ["- (none detected)"]
    lines += ["", "**Sign-offs**"]
    lines += [f"- {c}" for c in st["top_closings"]] or ["- (none detected)"]

    if st["cliche_hits"]:
        lines += [
            "",
            "**Avoid** — present in your history but flagged:",
            *[f"- \"{c}\"" for c in CLICHES if any(c in (s["body"] or "").lower() for s in samples)][:6],
        ]

    lines += [
        "",
        "## Vocabulary",
        "",
        "Words you actually use: " + ", ".join(st["vocabulary"][:40]),
        "",
        "## Important contacts",
        "",
        "| Contact | Sent | Received | Last | Auto-send |",
        "|---|---|---|---|---|",
    ]
    for c in contacts[:20]:
        lines.append(
            f"| {c['name'] or c['address']} | {c['sent']} | {c['received']} | "
            f"{(c['last_contact'] or '')[:10]} | {'yes' if c['auto_send_ok'] else 'no'} |"
        )

    lines += [
        "",
        "## Active projects",
        "",
        "_Inferred from subject-line vocabulary in sent mail. Re-run `mail-agent brain` to refresh._",
        "",
        *sorted({t for t in _project_terms(samples)})[:20],
        "",
        "---",
        "",
        "## Learn loop",
        "",
        "Every message the agent sends is compared against what you actually sent.",
        "Differences accumulate in `data/voice_log.jsonl` and the corrections below",
        "are regenerated. Version: see `brain_state.json`.",
        "",
        "### Learned corrections",
        "",
    ]
    try:
        log_rows = read_voice_log()
        # Each row is a message; the per-field diffs live in row["edits"].
        # A row with no edits was sent unchanged — that is a confirmation.
        for row in log_rows[-15:]:
            for e in (row.get("edits") or [])[:3]:
                lines.append(
                    f"- `{e.get('field', '?')}`: {str(e.get('agent', ''))[:60]!r} "
                    f"→ **{str(e.get('actual', ''))[:60]}**"
                )
        else:
            lines.append("_No corrections learned yet._")
    except FileNotFoundError:
        lines.append("_No corrections learned yet._")

    return "\n".join(lines) + "\n"


def _project_terms(samples: list[dict[str, Any]]) -> set[str]:
    """Coarse subject-line signal for active project vocabulary."""
    stop = {"re:", "fw:", "fwd:", "the", "and", "for", "with", "about"}
    c: Counter = Counter()
    for s in samples:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", s.get("subject", "")):
            if w.lower() not in stop:
                c[w] += 1
    return {w for w, n in c.most_common(25) if n >= 2}


# -------------------------------------------------------------------- learn

def _voice_log_path() -> Path:
    from ..config import DATA_DIR

    return DATA_DIR / "voice_log.jsonl"


def record_voice_sample(
    account: str, thread_id: str, recipient: str, subject: str,
    agent_draft: str, actual_sent: str, context: str = "",
) -> dict[str, Any] | None:
    """Compare the agent's draft with what the user actually sent.

    Called when the agent later sees its own message superseded in the thread.
    Only records when the user actually edited — an unedited send is a
    confirmation and reinforces the current profile.
    """
    if not actual_sent or not agent_draft:
        return None

    norm_a = re.sub(r"\s+", " ", agent_draft).strip()
    norm_b = re.sub(r"\s+", " ", actual_sent).strip()
    identical = norm_a == norm_b

    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "thread_id": thread_id,
        "recipient": recipient,
        "subject": subject,
        "context": context[:500],
        "agent": agent_draft[:4000],
        "actual": actual_sent[:4000],
        "identical": identical,
        "edits": [] if identical else _diff_fields(norm_a, norm_b),
    }
    p = _voice_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row) + "\n")
    log.info("voice sample recorded (edited=%s) for %s", not identical, recipient)
    return row


def _diff_fields(before: str, after: str) -> list[dict[str, str]]:
    """Which things did the user actually change? This is the learnable signal."""
    out = []

    def field(name: str, a: str, b: str) -> None:
        if a != b:
            out.append({"field": name, "agent": a[:200], "actual": b[:200]})

    field("greeting", _first_line(before), _first_line(after))
    field("signoff", _last_line(before), _last_line(after))
    field("length", str(len(before.split())), str(len(after.split())))
    field("contractions",
          "yes" if _has_contraction(before) else "no",
          "yes" if _has_contraction(after) else "no")
    field("formality", _formality(before), _formality(after))
    field("emoji", str(bool(re.search(r"[\U0001F300-\U0001FAFF]", before))),
          str(bool(re.search(r"[\U0001F300-\U0001FAFF]", after))))

    return out


def _first_line(t: str) -> str:
    for ln in t.splitlines():
        if ln.strip():
            return ln.strip()[:120]
    return ""


def _last_line(t: str) -> str:
    for ln in reversed(t.splitlines()):
        if ln.strip():
            return ln.strip()[:120]
    return ""


def _has_contraction(t: str) -> bool:
    return any(w in t.lower() for w in CONTRACTIONS)


def _formality(t: str) -> str:
    score = 0
    lowered = t.lower()
    if any(c in lowered for c in CLICHES):
        score += 2
    if _has_contraction(t):
        score -= 2
    if t.count("!") > 0:
        score -= 1
    return "formal" if score > 0 else "casual"


def read_voice_log(limit: int = 500) -> list[dict[str, Any]]:
    p = _voice_log_path()
    if not p.exists():
        raise FileNotFoundError(p)
    rows = []
    for line in p.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def voice_state() -> dict[str, Any]:
    """How much has the agent actually learned? Drives the profile version."""
    try:
        rows = read_voice_log()
    except FileNotFoundError:
        rows = []
    edited = [r for r in rows if not r.get("identical")]
    confirmed = [r for r in rows if r.get("identical")]

    fields: Counter = Counter()
    for r in edited:
        for f in r.get("edits", []):
            fields[f["field"]] += 1

    return {
        "total_samples": len(rows),
        "confirmed": len(confirmed),
        "edited": len(edited),
        "accuracy": round(len(confirmed) / len(rows), 3) if rows else None,
        "most_corrected": fields.most_common(),
        "mature": len(rows) >= 40,
    }


def regenerate_learned_section() -> str:
    """Re-render the 'Learned corrections' block with current data."""
    state = voice_state()
    lines = [
        f"- Samples: {state['total_samples']} ({state['confirmed']} sent unchanged, "
        f"{state['edited']} edited by you)",
    ]
    if state["accuracy"] is not None:
        lines.append(f"- Voice match rate: {state['accuracy']:.0%}")
    if state["most_corrected"]:
        lines.append("")
        lines.append("What you change most:")
        for field, n in state["most_corrected"][:5]:
            lines.append(f"- `{field}` — {n}×")
    if state["mature"]:
        lines.append("")
        lines.append("_Profile is mature. Further edits are rare improvements._")
    return "\n".join(lines)
