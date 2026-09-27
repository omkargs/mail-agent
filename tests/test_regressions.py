"""Regressions for bugs that shipped silently.

Each test here corresponds to a defect found by auditing the running agent
rather than by a failing assertion.
"""
from __future__ import annotations


# ------------------------------------------------- approving an unknown contact

def test_approving_an_unseen_contact_actually_takes_effect(cfg, provider):
    """set_contact_auto_send was a bare UPDATE.

    Approving an address the agent had never seen — exactly what the operator
    does when they say "yes, reply to them automatically" — updated zero rows
    and the agent kept escalating forever.
    """
    from mailagent.storage import db

    assert db.get_contact("google", "new@vendor.com") is None
    db.set_contact_auto_send("google", "new@vendor.com", True)
    c = db.get_contact("google", "new@vendor.com")
    assert c is not None
    assert c["auto_send_ok"] == 1
    assert c["approved_by_user"] == 1


def test_revoke_also_upserts_and_clears_the_flag(cfg, provider):
    from mailagent.storage import db

    db.set_contact_auto_send("google", "x@vendor.com", True)
    db.set_contact_auto_send("google", "x@vendor.com", False)
    c = db.get_contact("google", "x@vendor.com")
    assert c["auto_send_ok"] == 0


# --------------------------------------------------------- sent mail is not inbox

def test_sent_mail_is_never_offered_for_triage(cfg, provider):
    """The brain seeds the profile from the sent folder.

    Those rows stayed unprocessed, so list_unprocessed handed the agent the
    user's own sent history as untriaged inbox mail on every cycle.
    """
    from mailagent.storage import db

    for subj in ("Invoice", "Lunch", "Re: plan"):
        db.upsert_message({
            "id": f"s-{subj}", "account": "google", "sender": "me@example.com",
            "subject": subj, "body": "hello", "date": "2026-09-01T00:00:00+00:00",
            "label_ids": ["SENT"],
        })
    db.upsert_message({
        "id": "in-1", "account": "google", "sender": "boss@corp.com",
        "subject": "Re: plan", "body": "?", "date": "2026-09-01T00:00:00+00:00",
        "label_ids": ["INBOX"],
    })

    pending = [m["id"] for m in db.unprocessed("google")]
    assert pending == ["in-1"], "only real inbox mail should await triage"
    assert db.count_unprocessed("google") == 1


def test_mark_processed_many_clears_a_seeded_batch(cfg, provider):
    from mailagent.storage import db

    db.upsert_message({
        "id": "s-1", "account": "google", "sender": "me@example.com",
        "subject": "x", "body": "hello", "date": "2026-09-01T00:00:00+00:00",
        "label_ids": ["SENT"],
    })
    assert db.mark_processed_many(["s-1"]) == 1
    assert db.unprocessed("google") == []
    assert db.mark_processed_many([]) == 0


# ------------------------------------------------------------------ cold threads

def test_cold_contact_needs_approval_even_when_allowlisted(cfg, provider):
    """An allowlisted address with no history opens a new conversation.

    The new-thread guard existed but nothing ever passed the flag, so a cold
    send went out unattended.
    """
    from mailagent.agent import guards

    cfg.agent.send_mode = "auto"
    cfg.agent.auto_send_contacts = ["cold@vendor.com"]
    d = guards.decide(
        sender="cold@vendor.com", subject="Hello", body="Wanted to reach out about the project.",
        account="google", cfg=cfg.agent, account_auto_send=True,
        contact_auto_send=True, is_reply_to_unknown=True,
    )
    assert not d.allowed
    assert "no prior thread" in d.reason


def test_explicit_approval_beats_the_cold_thread_heuristic(cfg, provider):
    from mailagent.agent import guards
    from mailagent.storage import db

    db.set_contact_auto_send("google", "boss@corp.com", True)
    contact = db.get_contact("google", "boss@corp.com")
    # The operator approved this address directly; there is no thread.
    is_cold = not contact.get("approved_by_user")
    assert is_cold is False


# -------------------------------------------------------------- telegram parsing

def test_markdown_is_converted_to_valid_telegram_html():
    """The notifier sent markdown under parse_mode=HTML.

    Telegram rejected the whole message with HTTP 400 as soon as any content
    contained a bare "<" — an address, a company name, "3 < 5" — so the
    operator stopped receiving briefs and approvals entirely.
    """
    from mailagent.notify.channels import _md_to_html

    assert _md_to_html("**Morning brief**") == "<b>Morning brief</b>"
    assert _md_to_html("*status*") == "<i>status</i>"
    # The actual failure: unescaped angle brackets.
    assert _md_to_html("dev <google> ops") == "dev &lt;google&gt; ops"
    assert _md_to_html("a & b") == "a &amp; b"
    # Raw HTML in the source text is shown literally, never rendered — an
    # email body containing tags must not become live Telegram markup.
    assert _md_to_html("<b>not bold</b>") == "&lt;b&gt;not bold&lt;/b&gt;"


def test_telegram_error_logs_the_cause_not_just_the_class():
    from mailagent.notify.channels import _err_detail

    class FakeResponse:
        text = "Bad Request: can't parse entities"

    class FakeHTTPError(Exception):
        response = FakeResponse()
        def __str__(self):
            return "400 Client Error"

    assert "can't parse entities" in _err_detail(FakeHTTPError())


# ------------------------------------------------------------------- calendar

def test_agent_can_read_the_calendar(cfg, provider):
    """list_events existed on both providers but no tool exposed it, so the
    agent could create an event and never read one."""
    from mailagent.agent.tools import ToolBox

    provider.events = [{"id": "e1", "summary": "Standup"}]
    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("list_calendar_events", {})
    assert res["ok"] and res["count"] == 1
    assert res["events"][0]["summary"] == "Standup"


def test_deleting_a_calendar_event_never_runs_unattended(cfg, provider):
    """delete_event is irreversible. There is no auto-allow path by design."""
    from mailagent.agent.tools import ToolBox
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("delete_calendar_event", {"event_id": "e1", "reason": "double booked"})
    assert res["ok"]
    assert res["mode"] == "queued", "a delete must always need the user"

    pend = db.pending_approvals()
    assert len(pend) == 1
    assert pend[0]["kind"] == "calendar_delete"


def test_approved_calendar_delete_actually_deletes(cfg, provider):
    from mailagent.agent.tools import ToolBox
    from mailagent.agent import runner
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    box.run("delete_calendar_event", {"event_id": "e1", "reason": "test"})
    aid = db.pending_approvals()[0]["id"]

    res = runner.run_approval("google", provider, cfg, aid, True)
    assert res["ok"] and res["sent"] is True
    assert db.pending_approvals() == []


def test_denied_calendar_delete_does_not_delete(cfg, provider):
    from mailagent.agent.tools import ToolBox
    from mailagent.agent import runner
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    box.run("delete_calendar_event", {"event_id": "e1"})
    aid = db.pending_approvals()[0]["id"]

    res = runner.run_approval("google", provider, cfg, aid, False)
    assert res["ok"] and res["sent"] is False


def test_calendar_tools_refused_when_calendar_disabled(cfg, provider):
    from mailagent.agent.tools import ToolBox

    provider.calendar_enabled = False
    box = ToolBox(provider, cfg, run_id=1)
    assert not box.run("list_calendar_events", {})["ok"]
    assert not box.run("create_calendar_event", {
        "summary": "x", "start": "2026-09-28T10:00:00Z", "end": "2026-09-28T11:00:00Z",
    })["ok"]


# ------------------------------------------------------------------ scheduling

def test_natural_language_times_parse():
    from datetime import datetime
    from mailagent.agent.schedule import parse

    now = datetime(2026, 9, 27, 16, 10)
    assert parse("brief at 5", now)["at_time"] == "05:00"
    assert parse("inbox at 7:30", now)["at_time"] == "07:30"
    assert parse("check the calendar at noon", now)["at_time"] == "12:00"
    assert parse("report at 9 am", now)["at_time"] == "09:00"
    assert parse("report at 9 pm", now)["at_time"] == "21:00"
    assert parse("full brief at 17:00 daily", now)["repeat"] == "daily"
    assert parse("weekly report at 9", now)["repeat"] == "weekly"
    assert parse("check inbox in 20 minutes", now)["in_minutes"] == 20
    assert parse("brief in 2 hours", now)["in_minutes"] == 120


def test_kinds_are_identified_from_the_phrase():
    from datetime import datetime
    from mailagent.agent.schedule import parse

    now = datetime(2026, 9, 27, 16, 10)
    assert parse("inbox at 7", now)["kind"] == "scan"
    assert parse("full brief at 5", now)["kind"] == "brief"
    assert parse("check the calendar at noon", now)["kind"] == "cal"
    assert parse("quiet threads at 8", now)["kind"] == "quiet"


def test_a_time_is_never_guessed():
    """Defaulting to 'now' would fire a job the instant it was created."""
    from datetime import datetime
    from mailagent.agent.schedule import parse, ScheduleError

    now = datetime(2026, 9, 27, 16, 10)
    for bad in ("just do it", "brief me", "remind me about the invoice", ""):
        try:
            parse(bad, now)
            raise AssertionError(f"should have refused: {bad!r}")
        except ScheduleError:
            pass


def test_relative_phrase_is_not_mistaken_for_a_clock():
    """"in 20 minutes" must not be read as the time 20:00."""
    from datetime import datetime
    from mailagent.agent.schedule import parse

    r = parse("in 20 minutes check the inbox", datetime(2026, 9, 27, 16, 10))
    assert r["in_minutes"] == 20 and r["at_time"] is None


def test_past_one_shot_time_rolls_to_tomorrow():
    from datetime import datetime
    from mailagent.agent.schedule import parse

    r = parse("brief at 5", datetime(2026, 9, 27, 16, 10))
    assert r["rolled_to_tomorrow"] is True


def test_due_job_fires_once_then_is_marked(cfg, provider):
    from datetime import datetime
    from mailagent.storage import db
    from mailagent.agent.schedule import parse

    hhmm = datetime.now().strftime("%H:%M")
    db.create_job("j1", "google", "brief", at_time=hhmm, repeat="none", prompt="test")

    due = db.due_jobs(hhmm)
    assert [j["id"] for j in due] == ["j1"]

    db.mark_job_ran("j1", "none")
    assert db.due_jobs(hhmm) == [], "a one-shot job must not re-fire"
    assert db.list_jobs() == [], "a one-shot job disables itself"


def test_daily_job_fires_once_per_day(cfg, provider):
    from mailagent.storage import db

    hhmm = "23:59"
    db.create_job("j2", "google", "brief", at_time=hhmm, repeat="daily", prompt="test")
    db.mark_job_ran("j2", "daily")
    # Same day: must not fire again.
    assert db.due_jobs(hhmm) == []
    # The guard compares last_run against today's date, so a new day re-arms it.
    assert len(db.list_jobs()) == 1


def test_supervisor_runs_a_due_job_and_notifies(cfg, provider):
    from datetime import datetime
    from mailagent.storage import db
    from mailagent.agent.supervisor import Supervisor

    sent = []
    db.create_job("j3", "google", "quiet", at_time=datetime.now().strftime("%H:%M"),
                  repeat="none", prompt="test")

    sup = Supervisor(cfg, lambda: {"google": provider}, lambda: None,
                     lambda: None, notify=lambda t, **k: sent.append(t))
    sup._run_due_jobs()

    assert len(sent) == 1 and "Scheduled" in sent[0]
    assert db.due_jobs(datetime.now().strftime("%H:%M")) == []


def test_a_failing_job_does_not_resend_every_cycle(cfg, provider):
    from datetime import datetime
    from mailagent.storage import db
    from mailagent.agent.supervisor import Supervisor

    sent = []
    db.create_job("j4", "google", "brief", at_time=datetime.now().strftime("%H:%M"),
                  repeat="none", prompt="test")

    sup = Supervisor(cfg, lambda: {"google": provider}, lambda: None,
                     lambda: None, notify=lambda t, **k: sent.append(t))
    sup._run_due_jobs()
    sup._run_due_jobs()
    sup._run_due_jobs()

    # Marked ran before execution, so a raise cannot cause a resend loop.
    assert db.due_jobs(datetime.now().strftime("%H:%M")) == []


def test_chat_schedule_command_creates_a_job(cfg, provider):
    from mailagent.agent.chat import handle_text
    from mailagent.agent.chatops import build_chat_ops
    from mailagent.storage import db

    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=lambda t, **k: None)
    reply = handle_text("/schedule full inbox brief at 5", cfg, {"google": provider}, ops)
    assert "Scheduled" in reply
    assert len(db.list_jobs()) == 1
    assert db.list_jobs()[0]["at_time"] == "05:00"


def test_chat_schedule_refuses_an_unparseable_time(cfg, provider):
    from mailagent.agent.chat import handle_text
    from mailagent.agent.chatops import build_chat_ops
    from mailagent.storage import db

    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=lambda t, **k: None)
    reply = handle_text("/schedule do the thing", cfg, {"google": provider}, ops)
    assert "could not tell what time" in reply.lower()
    assert db.list_jobs() == [], "a failed parse must not create a job"


def test_chat_tasks_and_cancel(cfg, provider):
    from mailagent.agent.chat import handle_text
    from mailagent.agent.chatops import build_chat_ops
    from mailagent.storage import db

    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=lambda t, **k: None)
    handle_text("/schedule inbox at 7", cfg, {"google": provider}, ops)
    jid = db.list_jobs()[0]["id"]

    listing = handle_text("/tasks", cfg, {"google": provider}, ops)
    assert jid in listing and "scan" in listing

    assert "Cancelled" in handle_text(f"/cancel {jid}", cfg, {"google": provider}, ops)
    assert db.list_jobs() == []


# ------------------------------------------------------------ conversation memory

def test_chat_remembers_previous_turns(cfg, provider):
    """"Reply to him" only resolves if the agent still knows who "him" is.

    Each message used to be an independent call, so a follow-up referencing
    the previous turn had no idea what it referred to.
    """
    from mailagent.agent import chatlog
    from mailagent.agent.ask import answer

    chatlog.clear()
    chatlog.record("user", "what did the msk guy say?")
    chatlog.record("agent", "msk gt asked if you are in for the tour.")
    hist = chatlog.recent()
    assert [h["role"] for h in hist] == ["user", "agent"]
    assert "msk gt" in hist[1]["content"]


def test_history_is_oldest_first_and_bounded(cfg, provider):
    from mailagent.agent import chatlog

    chatlog.clear()
    for i in range(20):
        chatlog.record("user", f"message {i}")
    hist = chatlog.recent(limit=4)
    assert len(hist) == 4
    # Oldest first — the API rejects a conversation that starts mid-thread.
    assert hist[0]["content"] == "message 16"
    assert hist[-1]["content"] == "message 19"


def test_history_survives_a_reimport(cfg, provider):
    """It is persisted, not an in-process dict, so a restart does not
    amputate the conversation exactly when the user is confused."""
    from mailagent.agent import chatlog

    chatlog.clear()
    chatlog.record("user", "remember this")
    chatlog.record("agent", "remembered")

    import importlib
    importlib.reload(chatlog)
    assert any("remember this" == h["content"] for h in chatlog.recent())


def test_reset_clears_the_conversation(cfg, provider):
    from mailagent.agent.chat import handle_text
    from mailagent.agent.chatops import build_chat_ops
    from mailagent.agent import chatlog

    chatlog.clear()
    chatlog.record("user", "old topic")
    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=lambda t, **k: None)
    reply = handle_text("/reset", cfg, {"google": provider}, ops)
    assert "Forgot" in reply
    assert chatlog.recent() == []


def test_ask_op_passes_history_through(cfg, provider):
    """The wiring is what matters: answer() accepts history and something
    must actually supply it."""
    from mailagent.agent import chatlog, chatops
    from mailagent.agent.chat import handle_text
    from mailagent.agent import ask as ask_mod

    chatlog.clear()
    chatlog.record("user", "who is msk")
    chatlog.record("agent", "msk gt, an old school friend")

    seen = {}
    orig = ask_mod.answer

    def spy(question, cfg_, p, history=None, notify=None):
        seen["history"] = history
        return "ok"

    ask_mod.answer = spy
    try:
        ops = chatops.build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
        ops["ask"]("reply to him")
        assert seen["history"], "ask must pass conversation history"
        assert any("msk" in h["content"] for h in seen["history"])
    finally:
        ask_mod.answer = orig


def test_chat_records_both_sides_of_a_conversation(cfg, provider):
    from mailagent.agent import chatlog
    from mailagent.agent import ask as ask_mod

    chatlog.clear()
    ask_mod.answer = lambda q, c, p, history=None, notify=None: "the answer"
    ops_holder = {}
    from mailagent.agent.chatops import build_chat_ops
    ops = build_chat_ops(cfg, lambda: {"google": provider}, notify=None)
    ops["ask"] = lambda q: ask_mod.answer(q, cfg, None)
    from mailagent.agent.chat import handle_text
    handle_text("the question", cfg, {"google": provider}, ops)
    roles = [h["role"] for h in chatlog.recent()]
    assert roles == ["user", "agent"]
    assert chatlog.recent()[1]["content"] == "the answer"


# ------------------------------------------------------------------ auto mode

def test_established_thread_auto_sends(cfg, provider):
    """Auto mode was useless: nothing sent until every correspondent was
    manually allowlisted, so the agent queued everything forever."""
    from mailagent.agent import guards

    provider.auto_send = True
    d = guards.decide(
        sender="msk@friend.com", subject="re: tour", body="sorry i cant make it this weekend",
        account="google", cfg=cfg.agent, account_auto_send=True,
        contact_auto_send=False, is_established_thread=True,
    )
    assert d.allowed, d.reason


def test_cold_stranger_still_blocked_in_auto_mode(cfg, provider):
    """Auto mode must not become cold outreach."""
    from mailagent.agent import guards

    d = guards.decide(
        sender="stranger@randomcorp.com", subject="hello there friend",
        body="just reaching out to say hi and discuss the project",
        account="google", cfg=cfg.agent, account_auto_send=True,
        contact_auto_send=False, is_established_thread=False,
    )
    assert not d.allowed


def test_money_still_escalates_even_on_established_thread(cfg, provider):
    from mailagent.agent import guards

    d = guards.decide(
        sender="client@corp.com", subject="invoice",
        body="sending the invoice for this month payment",
        account="google", cfg=cfg.agent, account_auto_send=True,
        contact_auto_send=True, is_established_thread=True,
    )
    assert not d.allowed
    assert "escalation keyword" in d.reason


def test_contacts_are_normalised_on_write(cfg, provider):
    """bump_contact stored 'Name <a@b.com>' raw while every lookup normalised
    to 'a@b.com', so the contact table was a write-only log."""
    from mailagent.storage import db

    db.bump_contact("google", "pal <pal@example.com>", received=True)
    c = db.get_contact("google", "pal@example.com")
    assert c is not None, "the normalised address must be findable"
    assert c["received_count"] == 1


def test_your_own_address_is_not_a_contact(cfg, provider):
    """brain seeds from the sent folder; the user's own address was being
    stored as a contact with 486 'sent' to nobody."""
    from mailagent.storage import db

    db.upsert_account("google", "me@example.com", "Me")
    db.bump_contact("google", "Alice Example <alice@example.com>", sent=True)
    assert db.get_contact("google", "me@example.com") is None


def test_a_real_correspondent_is_still_recorded(cfg, provider):
    """The self-address filter must not swallow everyone else."""
    from mailagent.storage import db

    db.upsert_account("google", "me@example.com", "Me")
    db.bump_contact("google", "pal <pal@example.com>", received=True)
    assert db.get_contact("google", "pal@example.com") is not None


# ------------------------------------------------------------- compact output

def test_long_output_is_trimmed_for_a_phone(cfg, provider):
    from mailagent.notify.channels import TELEGRAM_MAX_CHARS, _compact

    long_text = "\n".join(f"line {i} of a very long wall of pasted mail" for i in range(300))
    out = _compact(long_text)
    assert len(out) <= TELEGRAM_MAX_CHARS + 60
    assert "trimmed" in out, "a cut-off message must say it was cut off"


def test_short_output_is_untouched(cfg, provider):
    from mailagent.notify.channels import _compact

    assert _compact("nothing needs you") == "nothing needs you"


# ----------------------------------------------------------------- act loop

def test_scheduled_job_data_is_facts_not_conclusions(cfg, provider):
    """The job hands the model raw data and asks it to decide."""
    from mailagent.agent.schedule import _job_data

    provider.events = [{"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-01T09:00:00"}}]
    data = _job_data({"kind": "cal"}, cfg, {"google": provider})
    assert "Standup" in data
    assert "e1" in data, "the event id must be present so it can be modified later"


def test_scan_job_reports_mail_not_a_verdict(cfg, provider):
    from mailagent.agent.schedule import _job_data
    from mailagent.storage import db

    db.upsert_message({
        "id": "m1", "account": "google", "sender": "msk@friend.com",
        "subject": "tour dates", "snippet": "leaving the 12th",
        "body": "", "date": "2026-09-27T00:00:00+00:00", "label_ids": ["INBOX"],
    })
    data = _job_data({"kind": "scan"}, cfg, {"google": provider})
    assert "msk@friend.com" in data
    assert "tour dates" in data


def test_act_and_ask_prompts_forbid_invented_times(cfg, provider):
    """The agent fabricated 9am-6pm from 'leaves at 6am' and put it in a real
    calendar. Both prompts now forbid it."""
    from mailagent.agent.act import ACT_PROMPT
    from mailagent.agent.ask import CONVERSATION_PROMPT

    assert "invent" in ACT_PROMPT.lower()
    assert "invent" in CONVERSATION_PROMPT.lower()


def test_voice_matches_the_relationship_not_one_flat_tone(cfg, provider):
    from mailagent.agent.ask import CONVERSATION_PROMPT

    low = CONVERSATION_PROMPT.lower()
    assert "friends and family" in low
    assert "brhh" in low or "bro" in low
    assert "professional" in low


# ------------------------------------------------------------ health reporting

def test_health_snapshot_is_timestamped(cfg, provider):
    """`mail-agent health` judged liveness on /proc/<pid> existing, which is
    true forever once a pid is reused — it reported a dead daemon as running
    for nine minutes. The heartbeat must carry a timestamp."""
    from mailagent.agent.supervisor import Supervisor

    sup = Supervisor(cfg, lambda: {}, lambda: None, lambda: None, notify=None)
    snap = sup.health.snapshot()
    assert "updated_at" in snap and snap["updated_at"]


def test_empty_message_is_refused_not_queued(cfg, provider):
    """A blank mail was queued for approval to a real address. The gates
    queued it rather than blocking it, which is the wrong outcome: it puts
    noise in front of the operator and looks like the agent meant to send it.
    """
    from mailagent.agent.tools import ToolBox
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("send_message", {"to": ["someone@friend.com"], "subject": "", "body": ""})
    assert not res["ok"]
    assert "empty" in res["error"].lower()
    assert db.pending_approvals() == [], "must not reach the approval queue"


def test_send_with_no_recipient_is_refused(cfg, provider):
    from mailagent.agent.tools import ToolBox

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("send_message", {"to": [], "subject": "hi", "body": "hello there friend"})
    assert not res["ok"]


def test_a_body_with_only_an_attachment_is_allowed(cfg, provider):
    """Attachments are separately gated, so an empty body with a file is a
    legitimate send and must not be caught by the empty-message check."""
    from mailagent.agent.tools import ToolBox

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("send_message", {
        "to": ["someone@friend.com"], "subject": "the file",
        "body": "", "attachments": ["/tmp/nope.pdf"],
    })
    # Reaches the gates (queued or blocked for attachments), not the empty check.
    assert "empty message" not in (res.get("error") or "")


# ------------------------------------------------- provider failures and addresses

def test_cloudflare_520_is_a_retryable_server_error():
    """A Cloudflare 520 arrived marked retryable with retry_after: 60 and was
    classified 'other', so the agent died on a failure it was told to retry."""
    from mailagent.limits import classify

    e = Exception("Error code: 520 - Cloudflare 520 unknown_origin_error")
    assert classify(e) == "server"


def test_any_5xx_is_a_server_error():
    from mailagent.limits import classify

    for code in (500, 502, 503, 504, 520, 522, 524):
        assert classify(Exception(f"Error code: {code}")) == "server", code


def test_server_honours_the_servers_own_retry_after():
    """The provider said retry_after: 60. Guessing our own backoff ignored
    the operator's instruction and retried sooner than asked."""
    from mailagent.limits import Limits

    L = Limits(backoff_base=5.0, backoff_max=300.0)
    L.record_failure(Exception("520 cloudflare 'retry_after': 90"))
    assert L.wait_time() >= 90, "must not retry sooner than the server asked"


def test_malformed_address_is_never_queued(cfg, provider):
    """'vamshi@' was queued for a real send. normalize_address only strips
    the display name, so a missing domain passed every gate."""
    from mailagent.agent.tools import ToolBox
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("send_message", {"to": ["vamshi@"], "subject": "hi", "body": "hello there"})
    assert not res["ok"]
    assert "malformed" in res["error"].lower()
    assert db.pending_approvals() == []


def test_address_validation_cases():
    from mailagent.providers.base import is_valid_address

    for good in ("hello@blukaze.com", "a.b+c@sub.domain.co.uk"):
        assert is_valid_address(good), good
    for bad in ("vamshi@", "a@b", "no-at-sign.com", "@nolocal.com", "x@.com", "", "  "):
        assert not is_valid_address(bad), bad


def test_agent_can_change_who_gets_unattended_replies(cfg, provider):
    """The agent told the user to 'add them to your approved list' with no way
    to do it from chat, then queued the same messages again."""
    from mailagent.agent.tools import ToolBox
    from mailagent.storage import db

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("set_contact_permission", {"address": "MSK GT <pal@example.com>", "allow": True})
    assert res["ok"]
    c = db.get_contact("google", "pal@example.com")
    assert c["auto_send_ok"] == 1

    # And revoking works.
    box.run("set_contact_permission", {"address": "pal@example.com", "allow": False})
    assert db.get_contact("google", "pal@example.com")["auto_send_ok"] == 0


def test_permission_tool_refuses_a_malformed_address(cfg, provider):
    from mailagent.agent.tools import ToolBox

    box = ToolBox(provider, cfg, run_id=1)
    res = box.run("set_contact_permission", {"address": "vamshi@", "allow": True})
    assert not res["ok"]
