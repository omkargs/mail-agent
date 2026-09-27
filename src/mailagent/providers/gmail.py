"""Gmail provider via google-api-python-client.

OAuth2 only. No app password. Scopes are read/modify/compose/labels/settings,
which covers triage and drafting without delete.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
from pathlib import Path
from typing import Any

from .base import Attachment, DraftRequest, EventRequest, MailProvider, parse_rfc2822_date, split_name

log = logging.getLogger(__name__)


def _guess_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    # Calendar: read + write so the agent can create events from mail.
    # Narrow — it cannot delete other calendars or change sharing.
    "https://www.googleapis.com/auth/calendar",
]

# Directories an attachment may be read from. The agent can attach files the
# user can already see; it must never be able to attach ~/.ssh/id_rsa, the
# secrets file, or anything under /etc. Configurable via MAIL_ATTACH_DIRS.
_DEFAULT_ATTACH_DIRS = ("~/Downloads", "~/Documents", "~/Desktop", "~/axren")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # Gmail's per-message total ceiling


def _allowed_attach_dirs() -> list[Path]:
    import os as _os

    raw = _os.environ.get("MAIL_ATTACH_DIRS", ",".join(_DEFAULT_ATTACH_DIRS))
    out = []
    for d in raw.split(","):
        d = d.strip()
        if d:
            out.append(Path(d).expanduser().resolve())
    return out


# Labels the agent must never remove or re-purpose.
PROTECTED_LABELS = {"INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "STARRED", "IMPORTANT", "CATEGORY_PERSONAL"}


def _batch_fetch(svc, ids: list[str], fmt: str = "full") -> list[dict[str, Any]]:
    """Fetch many messages in one HTTP request.

    BatchHttpRequest.execute() returns None by design — results arrive in the
    per-request callback. Code that tries to unpack its return value gets a
    TypeError and silently falls back to one request per message, which is
    exactly the latency this is meant to remove.
    """
    out: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _collect(request, response, exception):
        if exception is not None or response is None:
            return
        with lock:
            out.append(response)

    batch = svc.new_batch_http_request()
    for mid in ids:
        batch.add(svc.users().messages().get(userId="me", id=mid, format=fmt),
                  callback=_collect)
    batch.execute()
    return out


class GmailProvider(MailProvider):
    account = "google"

    def __init__(self, credentials_file: str, token_file: str, address: str = "",
                 display_name: str = "", auto_send: bool = False, calendar_enabled: bool = True):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.address = address
        self.display_name = display_name or address
        self.auto_send = auto_send
        self.calendar_enabled = calendar_enabled
        self._gmail = None
        self._calendar = None

    # ---------------------------------------------------------------- auth
    def _creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(self.token_file).write_text(creds.to_json())
        return creds

    def valid(self) -> bool:
        try:
            creds = self._creds()
            return bool(creds and creds.valid)
        except Exception as e:
            log.warning("google creds check failed: %s", type(e).__name__)
            return False

    def authenticate(self) -> bool:
        """Headless when a token exists; otherwise the browser consent flow."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not Path(self.credentials_file).exists():
            log.error("google credentials file missing: %s", self.credentials_file)
            return False

        creds = self._creds()
        if creds and creds.valid:
            self._service()
            return True

        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.token_file).write_text(creds.to_json())
        Path(self.token_file).chmod(0o600)
        self._service()
        return True

    def _cal_service(self):
        """Build the calendar client on demand.

        This used to be built inside _service(), a Gmail-only path, so calling
        list_events() directly — which is every calendar call — found
        self._calendar still None and quietly returned an empty list. The
        agent reported "nothing on the calendar" while five events existed.
        """
        if not self.calendar_enabled:
            return None
        if self._calendar is None:
            from googleapiclient.discovery import build

            try:
                self._calendar = build("calendar", "v3", credentials=self._creds(),
                                       cache_discovery=False)
            except Exception as e:
                log.warning("calendar unavailable, continuing mail-only: %s: %s",
                            type(e).__name__, e)
                return None
        return self._calendar

    def _service(self):
        from googleapiclient.discovery import build

        if self._gmail is None:
            self._gmail = build("gmail", "v1", credentials=self._creds(), cache_discovery=False)
        self._cal_service()
        return self._gmail

    # ---------------------------------------------------------------- read
    @staticmethod
    def _decode_body(payload: dict[str, Any]) -> str:
        """Walk the MIME tree and return the first usable text body.

        Prefers text/plain. Strips quoted reply chains and signatures so the
        agent sees what was actually written, not a wall of history.
        """
        def walk(p: dict[str, Any]) -> str:
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                data = p["body"]["data"]
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "ignore")
            for part in p.get("parts", []) or []:
                if part.get("mimeType") == "multipart/alternative":
                    continue
                found = walk(part)
                if found:
                    return found
            return ""

        body = walk(payload)
        body = re.split(r"^On .*wrote:$", body, maxsplit=1, flags=re.M)[0]
        body = re.split(r"^-----Original Message-----$", body, maxsplit=1, flags=re.M)[0]
        body = re.split(r"^-- \n", body, maxsplit=1, flags=re.M)[0]
        return body.strip()

    def _norm(self, m: dict[str, Any], full: bool = False) -> dict[str, Any]:
        payload = m.get("payload", {}) or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        frm = headers.get("from", "")
        return {
            "id": m["id"],
            "account": self.account,
            "thread_id": m.get("threadId"),
            "sender": frm.lower(),
            "sender_name": split_name(frm),
            "recipients": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "snippet": m.get("snippet", ""),
            "body": self._decode_body(payload) if full else "",
            "date": parse_rfc2822_date(headers.get("date", "")),
            "label_ids": m.get("labelIds", []),
            "has_attach": "ATTACHMENT" in m.get("labelIds", []) or any(
                p.get("filename") for p in payload.get("parts", []) or []
            ),
            "size_bytes": m.get("sizeEstimate", 0),
        }

    def list_messages(self, folder: str = "INBOX", limit: int = 20, after_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first. `after_id` is an exclusive high-water mark.

        A hardcoded date window here would silently drop mail older than the
        window that arrives after a cursor exists — if the agent was down for
        two days, that mail would never be triaged. We fetch the newest page
        and slice at the cursor instead, so the cursor is the only boundary.
        """
        svc = self._service()
        # Gmail search syntax: 'in:inbox' / 'in:sent', NOT '"INBOX" in labels'
        # (that form matches nothing and silently returns an empty inbox).
        # Accept both a provider folder name and a Gmail operator.
        folder_map = {
            "INBOX": "in:inbox", "SENT": "in:sent", "DRAFT": "in:draft",
            "SPAM": "in:spam", "TRASH": "in:trash", "STARRED": "is:starred",
        }
        q = folder_map.get(folder.upper(), f'in:{folder.lower()}')
        res = svc.users().messages().list(userId="me", q=q, maxResults=limit).execute()
        refs = res.get("messages", [])[:limit]
        if not refs:
            return []

        # One batched request for every id. Fetching each message individually
        # cost `limit` API calls per scan, which blew Gmail's per-minute quota
        # on a 30-message page — every scan then failed with a 403 and the
        # agent looked like it had hung.
        out = []
        try:
            for resp in _batch_fetch(svc, [r["id"] for r in refs], fmt="metadata"):
                out.append(self._norm(resp))
        except Exception as e:
            log.warning("batched list failed (%s); falling back", type(e).__name__)
            out = []
        if not out:
            for r in refs:
                try:
                    m = svc.users().messages().get(
                        userId="me", id=r["id"], format="metadata").execute()
                    out.append(self._norm(m))
                except Exception:
                    continue
        if after_id:
            ids = [m["id"] for m in out]
            if after_id in ids:
                out = out[: ids.index(after_id)]
        return out

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        svc = self._service()
        try:
            m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as e:
            log.warning("get_message failed: %s", type(e).__name__)
            return None
        return self._norm(m, full=True)

    def get_messages(self, message_ids: list[str]) -> list[dict[str, Any]]:
        """Fetch several messages in one HTTP batch.

        The agent otherwise calls get_message once per id, and each call is a
        separate round trip through the model. Batching keeps a 20-message
        triage to one request.
        """
        if not message_ids:
            return []
        svc = self._service()
        try:
            responses = _batch_fetch(svc, message_ids)
        except Exception as e:
            log.warning("batched get failed (%s); falling back to single", type(e).__name__)
            return [m for m in (self.get_message(i) for i in message_ids) if m]
        out = []
        for resp in responses:
            try:
                out.append(self._norm(resp, full=True))
            except Exception as e:
                log.warning("normalise failed in batch: %s", type(e).__name__)
        # A partial batch still beats falling back wholesale.
        return out or [m for m in (self.get_message(i) for i in message_ids) if m]

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        svc = self._service()
        res = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return [self._norm(m, full=True) for m in res.get("messages", [])]

    def search(self, query: str = "", sender: str = "", subject: str = "", since: str = "",
               limit: int = 25, full: bool = False) -> list[dict[str, Any]]:
        """Search the mailbox.

        `full=True` returns decoded bodies. That matters for latency: the
        alternative is the model calling get_message once per result, and each
        of those is a sequential model round trip. One batched fetch here
        replaces up to `limit` round trips.
        """
        svc = self._service()
        parts = []
        if query:
            parts.append(query)
        if sender:
            parts.append(f"from:{sender}")
        if subject:
            parts.append(f'subject:"{subject}"')
        if since:
            parts.append(f"after:{since}")
        res = svc.users().messages().list(userId="me", q=" ".join(parts), maxResults=limit).execute()
        refs = res.get("messages", [])[:limit]
        if not refs:
            return []

        if not full:
            return [
                self._norm(svc.users().messages().get(
                    userId="me", id=r["id"], format="metadata").execute())
                for r in refs
            ]

        # One batched request for every id, rather than one per message.
        out = []
        try:
            for resp in _batch_fetch(svc, [r["id"] for r in refs]):
                out.append(self._norm(resp, full=True))
        except Exception as e:
            log.warning("batched search fetch failed (%s); falling back", type(e).__name__)
            out = []
        if not out:
            for r in refs:
                m = svc.users().messages().get(userId="me", id=r["id"], format="full").execute()
                out.append(self._norm(m, full=True))
        return out

    # --------------------------------------------------------------- triage
    def apply_label(self, message_id: str, label_id: str, add: bool = True) -> bool:
        if label_id in PROTECTED_LABELS:
            return False
        svc = self._service()
        try:
            svc.users().messages().modify(
                userId="me", id=message_id,
                body={"addLabelIds": [label_id] if add else [], "removeLabelIds": [] if add else [label_id]},
            ).execute()
            return True
        except Exception as e:
            log.warning("apply_label failed: %s", type(e).__name__)
            return False

    def mark_read(self, message_id: str, read: bool = True) -> bool:
        svc = self._service()
        try:
            svc.users().messages().modify(
                userId="me", id=message_id,
                body={"removeLabelIds": [] if read else ["UNREAD"], "addLabelIds": ["UNREAD"] if not read else []},
            ).execute()
            return True
        except Exception:
            return False

    def archive(self, message_id: str) -> bool:
        svc = self._service()
        try:
            svc.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}).execute()
            return True
        except Exception:
            return False

    def create_label(self, name: str) -> str | None:
        svc = self._service()
        for lbl in self.list_labels():
            if lbl["name"].lower() == name.lower():
                return lbl["id"]
        try:
            res = svc.users().labels().create(userId="me", body={"name": name}).execute()
            return res["id"]
        except Exception as e:
            log.warning("create_label failed for %r: %s", name, type(e).__name__)
            return None

    def list_labels(self) -> list[dict[str, str]]:
        svc = self._service()
        res = svc.users().labels().list(userId="me").execute()
        return [{"id": l["id"], "name": l["name"]} for l in res.get("labels", [])]

    # ---------------------------------------------------------------- write
    def _build_message(self, req: DraftRequest):
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["To"] = ", ".join(req.to)
        if req.cc:
            msg["Cc"] = ", ".join(req.cc)
        msg["Subject"] = req.subject
        if self.display_name and "<" not in (self.display_name or ""):
            msg["From"] = f"{self.display_name} <{self.address}>"
        else:
            msg["From"] = self.display_name
        msg.set_content(req.body)

        for att in req.attachments:
            try:
                path = Path(att.path).expanduser().resolve()
            except Exception:
                log.warning("attachment path could not be resolved: %r", att.path)
                continue
            allowed = _allowed_attach_dirs()
            if not any(path == d or d in path.parents for d in allowed):
                log.warning("attachment rejected, outside allowed dirs: %s", path)
                continue
            if not path.is_file():
                log.warning("attachment not a file: %s", path)
                continue
            size = path.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                log.warning("attachment too large (%d bytes): %s", size, path)
                continue
            ctype = att.content_type or _guess_type(path)
            maintype, _, subtype = ctype.partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=att.resolved_name())

        if req.in_reply_to:
            svc = self._service()
            try:
                src = svc.users().messages().get(userId="me", id=req.in_reply_to, format="full").execute()
                ph = {h["name"].lower(): h["value"] for h in src["payload"].get("headers", [])}
                if ph.get("message-id"):
                    msg["In-Reply-To"] = ph["message-id"]
                    msg["References"] = ph["message-id"]
                if ph.get("subject") and not req.subject:
                    msg["Subject"] = "Re: " + ph["subject"]
            except Exception as e:
                log.warning("threading headers failed: %s", type(e).__name__)
        return msg

    def create_draft(self, req: DraftRequest) -> str | None:
        svc = self._service()
        raw = self._build_message(req).as_bytes()
        try:
            res = svc.users().drafts().create(userId="me", body={"message": {"raw": base64.urlsafe_b64encode(raw).decode()}}).execute()
            return res["id"]
        except Exception as e:
            log.error("create_draft failed: %s", type(e).__name__)
            return None

    def send(self, req: DraftRequest) -> bool:
        svc = self._service()
        raw = self._build_message(req).as_bytes()
        try:
            svc.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(raw).decode()}).execute()
            return True
        except Exception as e:
            log.error("send failed: %s", type(e).__name__)
            return False

    # ------------------------------------------------------------- calendar
    def list_events(self, limit: int = 20, time_min: str = "", days: int = 14) -> list[dict[str, Any]]:
        """Upcoming events within a bounded window.

        A timeMin-only query is not enough: this calendar carries annual
        recurrences, so an unbounded list is filled with birthdays years
        ahead and every real event is pushed off the end. The window defaults
        to two weeks — what "what's on my calendar" actually means.
        """
        cal = self._cal_service()
        if cal is None:
            return []
        from datetime import datetime, timedelta, timezone

        start = time_min or datetime.now(timezone.utc).isoformat()
        end = (
            datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(days=days)
        ).isoformat()
        res = cal.events().list(
            calendarId="primary",
            maxResults=limit,
            singleEvents=True,
            orderBy="startTime",
            timeMin=start,
            timeMax=end,
        ).execute()
        return [
            {"id": e.get("id"), "summary": e.get("summary"), "start": e.get("start"), "end": e.get("end")}
            for e in res.get("items", [])
        ]

    def create_event(self, req: EventRequest) -> dict[str, Any] | None:
        cal = self._cal_service()
        if cal is None:
            return None
        body: dict[str, Any] = {"summary": req.summary, "description": req.description}
        if req.location:
            body["location"] = req.location
        if "T" in req.start:
            body["start"] = {"dateTime": req.start}
            body["end"] = {"dateTime": req.end}
        else:
            body["start"] = {"date": req.start}
            body["end"] = {"date": req.end}
        if req.attendees:
            body["attendees"] = [{"email": a} for a in req.attendees]
        try:
            ev = cal.events().insert(calendarId="primary", body=body).execute()
            return {"id": ev.get("id"), "link": ev.get("htmlLink"), "summary": req.summary}
        except Exception as e:
            log.error("create_event failed: %s", type(e).__name__)
            return None

    def delete_event(self, event_id: str) -> bool:
        cal = self._cal_service()
        if cal is None:
            return False
        try:
            cal.events().delete(calendarId="primary", eventId=event_id).execute()
            return True
        except Exception:
            return False
