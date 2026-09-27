"""Microsoft 365 / Outlook provider via Graph REST.

Auth: MSAL device-code flow. Client credentials flow is also supported for
work tenants where a tenant admin consents an app registration.

Outlook nests replies as quoted HTML inside the body, so body extraction strips
the reply chain aggressively — otherwise the agent sees a wall of history and
writes a reply to five messages ago.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

import requests

from .base import DraftRequest, EventRequest, MailProvider, normalize_address, split_name

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite"]

# Outlook well-known folder names.
INBOX = "inbox"
SENT = "sentitems"
DRAFT = "drafts"


class GraphProvider(MailProvider):
    account = "microsoft"

    def __init__(self, tenant_id: str, client_id: str, client_secret: str,
                 address: str = "", display_name: str = "", auto_send: bool = False,
                 calendar_enabled: bool = True, token_file: str = ""):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.address = address
        self.display_name = display_name or address
        self.auto_send = auto_send
        self.calendar_enabled = calendar_enabled
        self.token_file = token_file
        self._token: dict[str, Any] | None = None

    # ---------------------------------------------------------------- auth
    def _app(self):
        import msal

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        return msal.ConfidentialClientApplication(
            self.client_id, authority=authority, client_credential=self.client_secret
        )

    def _load_token(self) -> dict[str, Any] | None:
        import json
        from pathlib import Path

        if not self.token_file or not Path(self.token_file).exists():
            return None
        try:
            return json.loads(Path(self.token_file).read_text())
        except Exception:
            return None

    def _save_token(self, tok: dict[str, Any]) -> None:
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        tok = dict(tok)
        tok["expires_at"] = (datetime.now(timezone.utc).timestamp()) + float(tok.get("expires_in", 3600))
        p = Path(self.token_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tok))
        p.chmod(0o600)
        self._token = tok

    def valid(self) -> bool:
        import time

        tok = self._token or self._load_token()
        if not tok or not tok.get("access_token"):
            return False
        if tok.get("expires_at", 0) < time.time() + 60:
            return self._refresh()
        self._token = tok
        return True

    def _refresh(self) -> bool:
        tok = self._token or self._load_token()
        if not tok or not tok.get("refresh_token"):
            return False
        try:
            r = self._app().acquire_token_by_refresh_token(tok["refresh_token"], SCOPES)
            if "access_token" not in r:
                log.error("microsoft token refresh failed")
                return False
            self._save_token({**r, "refresh_token": tok["refresh_token"]})
            return True
        except Exception as e:
            log.error("microsoft refresh exception: %s", type(e).__name__)
            return False

    def authenticate(self) -> bool:
        """Device-code flow. No browser redirect, works over SSH."""
        import msal

        app = msal.PublicClientApplication(self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant_id}")
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            log.error("device flow failed to start")
            return False

        print("\n" + "=" * 60)
        print("Open this URL in any browser:")
        print(f"  {flow['verification_uri']}")
        print(f"Then enter this code:  {flow['user_code']}")
        print("Waiting for sign-in...")
        print("=" * 60 + "\n")

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            log.error("device flow auth failed: %s", result.get("error_description", "unknown"))
            return False
        self._save_token(result)
        return True

    def _headers(self) -> dict[str, str]:
        if not self.valid():
            raise RuntimeError("microsoft token not available; run authenticate first")
        return {"Authorization": f"Bearer {self._token['access_token']}", "Content-Type": "application/json"}

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = requests.get(f"{GRAPH}{path}", headers=self._headers(), params=params or None, timeout=45)
        if r.status_code == 401:
            self._refresh()
            r = requests.get(f"{GRAPH}{path}", headers=self._headers(), params=params or None, timeout=45)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = requests.post(f"{GRAPH}{path}", headers=self._headers(), json=body, timeout=45)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = requests.patch(f"{GRAPH}{path}", headers=self._headers(), json=body, timeout=45)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _delete(self, path: str) -> bool:
        r = requests.delete(f"{GRAPH}{path}", headers=self._headers(), timeout=45)
        return r.status_code in (200, 204)

    # ---------------------------------------------------------------- read
    @staticmethod
    def _strip_html(raw: str) -> str:
        """HTML -> text, then drop the quoted chain and the Outlook header block."""
        t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = html.unescape(t)
        # Outlook reply block: "From: ... Sent: ... To: ... Subject: ..."
        t = re.split(r"^\s*From:\s.+$", t, maxsplit=1, flags=re.M)[0]
        t = re.split(r"^\s*-+\s*Original Message\s*-+\s*$", t, maxsplit=1, flags=re.M)[0]
        t = re.sub(r"_+_{5,}.*?_+_{5,}", " ", t, flags=re.S)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    def _norm(self, m: dict[str, Any], full: bool = False) -> dict[str, Any]:
        frm = m.get("from", {}) or {}
        sender_email = (frm.get("emailAddress", {}) or {}).get("address", "")
        to_addrs = ", ".join(
            (r.get("emailAddress", {}) or {}).get("address", "") for r in (m.get("toRecipients") or [])
        )
        body = ""
        if full:
            body = m.get("body", {}).get("content", "") or ""
            if m.get("body", {}).get("contentType") == "html":
                body = self._strip_html(body)
        return {
            "id": m.get("id"),
            "account": self.account,
            "thread_id": m.get("conversationId"),
            "sender": (sender_email or "").lower(),
            "sender_name": (frm.get("emailAddress", {}) or {}).get("name", ""),
            "recipients": to_addrs,
            "subject": m.get("subject", ""),
            "snippet": m.get("bodyPreview", ""),
            "body": body,
            "date": (m.get("receivedDateTime") or m.get("sentDateTime") or ""),
            "label_ids": [INBOX] if "inbox" in (m.get("parentFolderId") or "") else [],
            "has_attach": bool(m.get("hasAttachments")),
            "size_bytes": m.get("size", 0),
        }

    def list_messages(self, folder: str = INBOX, limit: int = 20, after_id: str | None = None) -> list[dict[str, Any]]:
        sel = "from,subject,bodyPreview,receivedDateTime,from,toRecipients,conversationId,hasAttachments,parentFolderId"
        data = self._get(f"/me/mailFolders/{folder}/messages", top=limit, select=sel, orderby="receivedDateTime desc")
        out = [self._norm(m) for m in data.get("value", [])]
        if after_id:
            ids = [m["id"] for m in out]
            if after_id in ids:
                out = out[: ids.index(after_id)]
        return out

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        try:
            m = self._get(f"/me/messages/{message_id}")
        except Exception as e:
            log.warning("get_message failed: %s", type(e).__name__)
            return None
        return self._norm(m, full=True)

    def get_thread(self, thread_id: str) -> list[dict[str, Any]]:
        sel = "from,subject,body,receivedDateTime,sentDateTime,from,toRecipients,conversationId,hasAttachments"
        data = self._get(f"/me/messages", filter=f"conversationId eq {thread_id}", top=50, select=sel)
        msgs = [self._norm(m, full=True) for m in data.get("value", [])]
        return sorted(msgs, key=lambda m: m.get("date", ""))

    def search(self, query: str = "", sender: str = "", subject: str = "", since: str = "", limit: int = 25) -> list[dict[str, Any]]:
        filters = []
        if query:
            filters.append(f"contains(subject,'{query}')")
        if sender:
            filters.append(f"from/emailAddress/address eq '{sender}'")
        if subject:
            filters.append(f"contains(subject,'{subject}')")
        if since:
            filters.append(f"receivedDateTime ge {since}")
        sel = "from,subject,bodyPreview,receivedDateTime,from,toRecipients,conversationId,hasAttachments"
        path = "/me/messages"
        params: dict[str, Any] = {"top": limit, "select": sel, "orderby": "receivedDateTime desc"}
        if filters:
            path += "/microsoft.graph.search"
            params["search"] = query or "*"
        data = self._get(path, **params)
        out = [self._norm(m) for m in data.get("value", [])]
        if sender:
            out = [m for m in out if normalize_address(m["sender"]) == normalize_address(sender)]
        if subject:
            out = [m for m in out if subject.lower() in m["subject"].lower()]
        return out[:limit]

    # --------------------------------------------------------------- triage
    def _categories(self) -> list[str]:
        try:
            return self._get("/me/mailFolders/inbox/childFolders").get("value", [])
        except Exception:
            return []

    def create_label(self, name: str) -> str | None:
        for c in self._categories():
            if (c.get("displayName") or "").lower() == name.lower():
                return c.get("id")
        try:
            res = self._post("/me/mailFolders", {"displayName": name, "isFolder": True})
            return res.get("id")
        except Exception as e:
            log.warning("create_label failed for %r: %s", name, type(e).__name__)
            return None

    def list_labels(self) -> list[dict[str, str]]:
        out = [{"id": "inbox", "name": "Inbox"}]
        for c in self._categories():
            out.append({"id": c.get("id", ""), "name": c.get("displayName", "")})
        return out

    def apply_label(self, message_id: str, label_id: str, add: bool = True) -> bool:
        if not label_id or label_id == "inbox":
            return False
        try:
            cats = self._get(f"/me/messages/{message_id}").get("categories", [])
            if add and label_id not in cats:
                cats.append(label_id)
            elif not add and label_id in cats:
                cats.remove(label_id)
            self._patch(f"/me/messages/{message_id}", {"categories": cats})
            return True
        except Exception:
            return False

    def mark_read(self, message_id: str, read: bool = True) -> bool:
        try:
            self._patch(f"/me/messages/{message_id}", {"isRead": read})
            return True
        except Exception:
            return False

    def archive(self, message_id: str) -> bool:
        try:
            self._post(f"/me/messages/{message_id}/move", {"destinationId": "archive"})
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- write
    def _draft_body(self, req: DraftRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "subject": req.subject,
            "body": {"contentType": "Text", "content": req.body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in req.to],
        }
        if req.cc:
            body["ccRecipients"] = [{"emailAddress": {"address": a}} for a in req.cc]
        if req.in_reply_to:
            body["conversationId"] = req.in_reply_to
        return body

    def create_draft(self, req: DraftRequest) -> str | None:
        try:
            res = self._post("/me/messages", self._draft_body(req))
            return res.get("id")
        except Exception as e:
            log.error("create_draft failed: %s", type(e).__name__)
            return None

    def send(self, req: DraftRequest) -> bool:
        try:
            self._post("/me/sendMail", {"message": self._draft_body(req), "saveToSentItems": True})
            return True
        except Exception as e:
            log.error("send failed: %s", type(e).__name__)
            return False

    # ------------------------------------------------------------- calendar
    def list_events(self, limit: int = 20, time_min: str = "") -> list[dict[str, Any]]:
        if not self.calendar_enabled:
            return []
        from datetime import datetime, timezone

        params = {"$top": limit, "$orderby": "start/dateTime"}
        params["startDateTime"] = time_min or datetime.now(timezone.utc).isoformat()
        try:
            data = self._get("/me/calendarView", **params)
        except Exception:
            return []
        return [
            {"id": e.get("id"), "summary": e.get("subject"), "start": e.get("start"), "end": e.get("end")}
            for e in data.get("value", [])
        ]

    def create_event(self, req: EventRequest) -> dict[str, Any] | None:
        if not self.calendar_enabled:
            return None
        body: dict[str, Any] = {
            "subject": req.summary,
            "body": {"contentType": "Text", "content": req.description},
            "start": {"dateTime": req.start, "timeZone": "UTC"},
            "end": {"dateTime": req.end, "timeZone": "UTC"},
        }
        if req.location:
            body["location"] = {"displayName": req.location}
        if req.attendees:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in req.attendees
            ]
        try:
            res = self._post("/me/events", body)
            return {"id": res.get("id"), "link": res.get("webLink"), "summary": req.summary}
        except Exception as e:
            log.error("create_event failed: %s", type(e).__name__)
            return None

    def delete_event(self, event_id: str) -> bool:
        if not self.calendar_enabled:
            return False
        return self._delete(f"/me/events/{event_id}")
