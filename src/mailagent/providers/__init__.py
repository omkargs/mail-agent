"""Provider registry. Builds configured providers; the agent sees one interface."""
from __future__ import annotations

import logging

from .. import config
from .base import DraftRequest, EventRequest, MailProvider

log = logging.getLogger(__name__)

__all__ = ["MailProvider", "DraftRequest", "EventRequest", "build_providers"]


def build_providers(cfg: config.Config | None = None) -> dict[str, MailProvider]:
    """Return {account_id: provider} for every enabled, configured account."""
    cfg = cfg or config.load()
    out: dict[str, MailProvider] = {}
    enabled = cfg.agent.enabled_accounts

    want_google = not enabled or "google" in enabled
    if want_google and cfg.google.credentials_file:
        from .gmail import GmailProvider

        out["google"] = GmailProvider(
            credentials_file=cfg.google.credentials_file,
            token_file=cfg.google.token_file,
            address=cfg.google.account,
            display_name=cfg.google.display_name,
            auto_send=cfg.google.auto_send,
            calendar_enabled=cfg.google.calendar_enabled,
        )

    want_ms = "microsoft" in enabled or (enabled and "ms" in enabled)
    if want_ms and cfg.microsoft.client_id and cfg.microsoft.tenant_id:
        from .graph import GraphProvider

        out["microsoft"] = GraphProvider(
            tenant_id=cfg.microsoft.tenant_id,
            client_id=cfg.microsoft.client_id,
            client_secret=cfg.microsoft.client_secret,
            address=cfg.microsoft.account,
            display_name=cfg.microsoft.display_name,
            auto_send=cfg.microsoft.auto_send,
            calendar_enabled=cfg.microsoft.calendar_enabled,
            token_file=cfg.microsoft.token_file,
        )

    return out
