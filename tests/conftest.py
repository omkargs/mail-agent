"""Shared fixtures. Every test runs against a temp DB and mock providers —
no live mailbox is ever touched."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_tmp = tempfile.mkdtemp(prefix="mailagent-test-")
os.environ["MAIL_AGENT_DB"] = str(Path(_tmp) / "test.db")
os.environ["MAIL_AGENT_DATA_DIR"] = _tmp
os.environ["MAIL_AGENT_CONFIG_DIR"] = str(Path(_tmp) / "config")


@pytest.fixture(autouse=True)
def clean_db():
    from mailagent.storage import db
    from mailagent import limits

    db.migrate()
    # Child tables first — drafts references runs, messages references accounts.
    for table in ("drafts", "approvals", "actions_log", "messages", "cursors",
                  "runs", "contacts", "skills", "labels", "usage_daily",
                  "scheduled_jobs", "accounts"):
        with db.db() as c:
            c.execute(f"DELETE FROM {table}")
    # Spend limits are process-global so every thread shares one budget. Reset
    # per test so one test's usage cannot exhaust the next test's budget.
    L = limits.global_limits()
    L._calls.clear()
    L._failures = 0
    L._cooldown = 0.0
    L._open_until = 0.0
    L.daily_token_cap = 10_000_000
    yield


class FakeProvider:
    """In-memory provider. Records what it was asked to do."""

    def __init__(self, account="google", auto_send=False, calendar_enabled=True):
        from mailagent.providers.base import MailProvider

        self.account = account
        self.address = "me@example.com"
        self.display_name = "Me"
        self.auto_send = auto_send
        self.calendar_enabled = calendar_enabled
        self.sent: list[dict] = []
        self.drafted: list[dict] = []
        self.labels: dict[str, str] = {"lbl_1": "Urgent"}
        self.events: list[dict] = []
        self.inbox: list[dict] = []
        self._next = 1
        MailProvider.register(FakeProvider)

    def _id(self):
        self._next += 1
        return f"m{self._next}"

    def add_message(self, sender="a@b.com", subject="Hello", body="Body text here", **kw):
        mid = self._id()
        msg = {
            "id": mid, "account": self.account, "thread_id": f"t{mid}",
            "sender": sender, "sender_name": "", "recipients": "me@example.com",
            "subject": subject, "snippet": body[:100], "body": body,
            "date": "2026-09-26T10:00:00+00:00", "label_ids": ["INBOX"],
            "has_attach": False, "size_bytes": 100,
        }
        msg.update(kw)
        self.inbox.insert(0, msg)
        return msg

    # --- interface ---
    def valid(self): return True
    def authenticate(self): return True
    def list_messages(self, folder="INBOX", limit=20, after_id=None): return list(self.inbox)[:limit]
    def get_message(self, message_id):
        return next((m for m in self.inbox if m["id"] == message_id), None)
    def get_thread(self, thread_id):
        return [m for m in self.inbox if m["thread_id"] == thread_id]
    def search(self, query="", sender="", subject="", since="", limit=25):
        return self.inbox[:limit]
    def apply_label(self, message_id, label_id, add=True): return True
    def mark_read(self, message_id, read=True): return True
    def archive(self, message_id): return True
    def create_label(self, name): return self.labels.setdefault(name, f"lbl_{name}")
    def list_labels(self): return [{"id": k, "name": v} for k, v in self.labels.items()]
    def create_draft(self, req):
        did = f"d{self._id()}"
        self.drafted.append({"to": req.to, "subject": req.subject, "body": req.body})
        return did
    def send(self, req):
        self.sent.append({"to": req.to, "subject": req.subject, "body": req.body})
        return True
    def list_events(self, limit=20, time_min=""): return self.events
    def create_event(self, req):
        ev = {"id": f"e{self._id()}", "summary": req.summary}
        self.events.append(ev)
        return ev
    def delete_event(self, event_id): return True


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def cfg():
    from mailagent.config import Config

    c = Config()
    c.agent.send_mode = "auto"
    c.agent.auto_send_contacts = ["boss@corp.com"]
    c.agent.daily_token_cap = 10_000_000
    # A dummy key so build_client does not refuse before the mock intercepts.
    c.router.api_key = "test-key-not-real"
    return c
