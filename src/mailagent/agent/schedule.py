"""Parse what the operator actually says into a runnable schedule.

"give me a full brief review of the inbox at five", "check the inbox at 7",
"every morning at 6:30", "remind me about the invoice in 20 minutes".

Deliberately a small hand-written parser rather than a model call or an NLP
library: the input is a handful of shapes, the daemon runs this unattended,
and a misparse must fail loudly rather than schedule a job at the wrong time.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# Job kinds the executor knows how to run.
KINDS = ("brief", "scan", "quiet", "cal", "freeform")

_KIND_WORDS = [
    (r"\bbrief\b|\bdigest\b", "brief"),
    (r"\binbox\b|\bmail\b|\btriage\b|\bnew mail\b|\bscan\b", "scan"),
    (r"\bquiet\b|\bcold\b", "quiet"),
    (r"\bcalendar\b|\bcal\b", "cal"),
]

_WORD_HOUR = {
    "midnight": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "noon": 12,
}

CLOCK = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.|o'clock|oclock)?\b",
    re.I,
)
IN_MINUTES = re.compile(
    r"\b(?:in\s+)?(?P<n>\d{1,3})\s*(?P<unit>min|mins|minute|minutes|hour|hours|hr|hrs)\b",
    re.I,
)
EVERY_DAY = re.compile(
    r"\b(?:every\s*(?:day|morning|evening|night|daily|weekly)|daily|weekly)\b", re.I
)

# Strip these before hunting for a clock, so "in 20 minutes" is never read as
# the number 20.
_TIME_WORDS = re.compile(
    r"\b(?:in\s+)?\d{1,3}\s*(?:min|mins|minute|minutes|hour|hours|hr|hrs)\b", re.I
)


class ScheduleError(ValueError):
    """The request could not be understood. Never guess a time."""


def parse(text: str, now: datetime | None = None) -> dict:
    """Return {kind, at_time, repeat, prompt, in_minutes, fire_at}.

    Raises ScheduleError with a human-readable reason when no time is found.
    A missing time is an error rather than a default — defaulting to "now"
    would fire the job the moment it was created.
    """
    now = now or datetime.now()
    raw = (text or "").strip()
    low = raw.lower()
    if not low:
        raise ScheduleError("empty request")

    kind = "freeform"
    for pattern, k in _KIND_WORDS:
        if re.search(pattern, low):
            kind = k
            break

    repeat = "none"
    if EVERY_DAY.search(low):
        repeat = "weekly" if "week" in EVERY_DAY.search(low).group(0).lower() else "daily"

    # "in 20 minutes" / "in 2 hours" — a one-shot relative offset. Checked
    # before the clock, and the phrase is removed from the search text so its
    # digits cannot be mistaken for a time of day.
    m = IN_MINUTES.search(low)
    if m and not CLOCK.search(_TIME_WORDS.sub(" ", low)):
        n = int(m.group("n"))
        unit = m.group("unit").lower()
        minutes = n * 60 if unit[0] == "h" else n
        if minutes <= 0 or minutes > 60 * 24 * 14:
            raise ScheduleError(f"refusing to schedule {minutes} minutes out")
        return {
            "kind": kind, "in_minutes": minutes, "repeat": "none",
            "at_time": None, "prompt": raw,
        }

    hhmm = _find_clock(low)
    if hhmm is None:
        raise ScheduleError(
            "I could not tell what time you meant. Try 'brief at 5', "
            "'inbox at 7:30', or 'every morning at 6'."
        )

    # A one-shot time that has already passed today rolls to tomorrow rather
    # than firing immediately. A repeating job simply waits for its next slot.
    h, mi = hhmm
    when = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    rolled = when <= now
    if rolled:
        when += timedelta(days=1)

    return {
        "kind": kind,
        "at_time": when.strftime("%H:%M"),
        "repeat": repeat,
        "prompt": raw,
        "in_minutes": None,
        "fire_at": when.isoformat(),
        "rolled_to_tomorrow": rolled and repeat == "none",
    }


def _find_clock(low: str) -> tuple[int, int] | None:
    """Find a clock time. Handles 5, 5pm, 5:30, 5 o'clock, five, noon."""
    # Word times first: "five" and "noon" have no digits, so the numeric
    # regex cannot find them at all.
    for w, h in _WORD_HOUR.items():
        if re.search(rf"\b{w}\b", low):
            return h, 0

    m = CLOCK.search(low)
    if not m:
        return None
    h = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ap = (m.group("ampm") or "").lower().replace(".", "").rstrip("m")
    ap = "pm" if ap.startswith("p") else ("am" if ap.startswith("a") else "")

    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h == 12 and "noon" in low:
        h = 12
    if not (0 <= h <= 23) or not (0 <= minute <= 59):
        return None
    return h, minute


# ----------------------------------------------------------------- execution

def run_job(job: dict, cfg, providers_factory, notify=None) -> str:
    """Execute one due job and return the text to send the operator.

    Scheduled work goes through the act loop: the model reads the data and
    decides what to do, rather than the job shipping raw text for a human to
    parse. Routine items it handles itself; the rest it asks about.
    """
    kind = job.get("kind") or "freeform"
    account = job.get("account") or ""
    provs = providers_factory()
    if not provs:
        return "Scheduled task: no authenticated account."
    p = next(iter(provs.values()))

    data = _job_data(job, cfg, provs)
    if data is None:
        return "Scheduled task: no authenticated account."

    from .act import decide_and_act

    question = {
        "brief": "Morning digest — what happened, what needs a decision, and what can I just handle.",
        "quiet": "Threads going cold — which need a nudge and which are done.",
        "scan": "Inbox scan — triage, reply where a reply is clearly wanted, escalate what is not.",
        "cal": "Calendar check — anything in the data that should become an event.",
    }.get(kind, job.get("prompt") or "Decide what needs doing here.")

    text, _stats = decide_and_act(data, question, cfg, p, notify=notify)
    return text


def _job_data(job: dict, cfg, provs) -> str | None:
    """The raw data a job reasons over. Just facts, no conclusions."""
    kind = job.get("kind") or "freeform"
    p = next(iter(provs.values()))

    if kind == "brief":
        from .brief import build_brief
        return build_brief(cfg)

    if kind == "quiet":
        from .brief import brief_quiet_threads
        return brief_quiet_threads(cfg)

    if kind == "cal":
        if not getattr(p, "calendar_enabled", False):
            return "Calendar is disabled for this account."
        try:
            evs = p.list_events(limit=10)
        except Exception as e:
            return f"Calendar read failed: {type(e).__name__}"
        if not evs:
            return "Nothing on the calendar in the next two weeks."
        lines = []
        for e in evs:
            s = e.get("start") or {}
            when = s.get("dateTime") or s.get("date") or "?"
            lines.append(f"- {when} | {e.get('summary') or '(no title)'} | id={e.get('id')}")
        return "\n".join(lines)

    if kind == "scan":
        from ..storage import db
        rows = db.unprocessed(p.account, limit=30)
        if not rows:
            return "No new mail since the last check."
        lines = []
        for r in rows:
            lines.append(
                f"- id={r['id']} | from={r['sender']} | {r.get('date','')}\n"
                f"  subject: {r.get('subject','')[:100]}\n"
                f"  snippet: {(r.get('snippet') or '')[:250]}"
            )
        return "\n".join(lines)

    # freeform: give the model the mailbox to reason over.
    try:
        rows = p.search(query="in:inbox", limit=15, full=True)
    except Exception as e:
        return f"Mailbox read failed: {type(e).__name__}"
    if not rows:
        return "No recent mail."
    lines = []
    for r in rows:
        lines.append(
            f"- id={r['id']} | from={r['sender']} | {r.get('date','')}\n"
            f"  subject: {r.get('subject','')[:100]}\n"
            f"  body: {(r.get('body') or '')[:600]}"
        )
    return "\n".join(lines)
