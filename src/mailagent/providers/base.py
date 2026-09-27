"""Provider interface.

One shape for Gmail and Microsoft 365 so the agent never knows which is which.
`account` is the provider key ('google' | 'microsoft').

Normalised message dict:
    id, account, thread_id, sender, sender_name, recipients, subject, snippet,
    body, date, label_ids, has_attach, size_bytes
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    """A file to attach. `path` must be a local file the user can see."""

    path: str
    filename: str = ""      # defaults to the basename
    content_type: str = ""   # sniffed if empty

    def resolved_name(self) -> str:
        return self.filename or self.path.rsplit("/", 1)[-1]


@dataclass
class DraftRequest:
    to: list[str]
    subject: str
    body: str
    in_reply_to: str | None = None
    cc: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class EventRequest:
    summary: str
    start: str
    end: str
    description: str = ""
    location: str = ""
    attendees: list[str] = field(default_factory=list)


class MailProvider(abc.ABC):
    """Mail + calendar for one account. Implementations must be idempotent
    where the caller expects it (create_draft returns the same id on retry)."""

    account: str
    address: str
    display_name: str
    auto_send: bool
    calendar_enabled: bool

    # ---------------------------------------------------------------- auth
    @abc.abstractmethod
    def authenticate(self) -> bool:
        """Run the auth flow if needed. Returns True when usable."""

    @abc.abstractmethod
    def valid(self) -> bool:
        """True when a live session exists, without triggering a new auth flow."""

    # ---------------------------------------------------------------- read
    @abc.abstractmethod
    def list_messages(
        self, folder: str = "INBOX", limit: int = 20, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest first. `after_id` is an exclusive high-water mark."""

    @abc.abstractmethod
    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """Full message including body."""

    @abc.abstractmethod
    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """All messages in a conversation, oldest first."""

    @abc.abstractmethod
    def search(
        self, query: str = "", sender: str = "", subject: str = "",
        since: str = "", limit: int = 25,
    ) -> list[dict[str, Any]]:
        ...

    # --------------------------------------------------------------- triage
    @abc.abstractmethod
    def apply_label(self, message_id: str, label_id: str, add: bool = True) -> bool:
        ...

    @abc.abstractmethod
    def mark_read(self, message_id: str, read: bool = True) -> bool:
        ...

    @abc.abstractmethod
    def archive(self, message_id: str) -> bool:
        ...

    @abc.abstractmethod
    def create_label(self, name: str) -> str | None:
        """Returns the label id, or None if it already existed / cannot be created."""

    @abc.abstractmethod
    def list_labels(self) -> list[dict[str, str]]:
        ...

    def get_messages(self, message_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch several messages at once.

        Not abstract: a provider without a batch endpoint falls back to
        sequential single gets, which is correct but slower. Gmail overrides
        this with a real HTTP batch.
        """
        out = []
        for mid in message_ids:
            m = self.get_message(mid)
            if m:
                out.append(m)
        return out

    # ---------------------------------------------------------------- write
    @abc.abstractmethod
    def create_draft(self, req: DraftRequest) -> str | None:
        """Returns draft id."""

    @abc.abstractmethod
    def send(self, req: DraftRequest) -> bool:
        """Irreversible. Callers must gate this."""

    # ------------------------------------------------------------- calendar
    @abc.abstractmethod
    def list_events(self, limit: int = 20, time_min: str = "") -> list[dict[str, Any]]:
        ...

    @abc.abstractmethod
    def create_event(self, req: EventRequest) -> dict[str, Any] | None:
        ...

    @abc.abstractmethod
    def delete_event(self, event_id: str) -> bool:
        """Irreversible. Callers must gate this."""


_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def is_valid_address(addr: str) -> bool:
    """Does this look like a deliverable email address?

    'vamshi@' reached the approval queue: normalize_address() only strips the
    display name, so a truncated address passed every gate and was queued for
    a real send. A missing domain is not a judgement call — it cannot be
    delivered to, so it must never be queued.
    """
    if not addr:
        return False
    return bool(_EMAIL.match(addr.strip()))


def normalize_address(addr: str) -> str:
    """Extract the bare address from 'Name <a@b.com>'."""
    if not addr:
        return ""
    if "<" in addr and ">" in addr:
        return addr[addr.index("<") + 1 : addr.index(">")].strip().lower()
    return addr.strip().lower()


def split_name(addr: str) -> str:
    if "<" in addr and ">" in addr:
        return addr[: addr.index("<")].strip().strip('"')
    return ""


def parse_rfc2822_date(raw: str) -> str:
    """Best-effort normalisation to ISO 8601. Returns the input on failure."""
    if not raw:
        return ""
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return raw
