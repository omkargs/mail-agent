"""Configuration loading. Secrets come from env or ~/.config/mail-agent/.secrets.

Never log values from this module.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(
    os.environ.get("MAIL_AGENT_CONFIG_DIR", str(Path.home() / ".config" / "mail-agent"))
)
CONFIG_PATH = Path(os.environ.get("MAIL_AGENT_CONFIG", str(CONFIG_DIR / "config.json")))
DATA_DIR = Path(os.environ.get("MAIL_AGENT_DATA_DIR", str(Path.home() / ".local" / "share" / "mail-agent")))
DB_PATH = Path(os.environ.get("MAIL_AGENT_DB", str(DATA_DIR / "agent.db")))

DEFAULT_ROUTER_BASE = "https://router.bynara.id"
DEFAULT_MODEL = "combo/claude2mail"


def _read_secrets_file() -> dict[str, str]:
    """Parse shell-style KEY=value lines from .secrets without executing them.

    A regex cannot do this correctly. Values may contain quotes, backslashes,
    $, and backticks, and the writer uses POSIX single-quote escaping
    (a literal quote becomes '\''). Matching that with a pattern silently
    truncates the value at the first embedded quote.

    So parse it the way a shell does, character by character.
    """
    path = CONFIG_DIR / ".secrets"
    if not path.exists():
        return {}

    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        val = val.strip()
        out[key] = _unquote_shell(val)
    return out


def _unquote_shell(v: str) -> str:
    """Undo shell quoting the way a shell does, without executing anything.

    shlex in POSIX mode is the correct tool: it understands that a single
    quoted string can be written as 'a'\''b', which is what the writer emits
    for a value containing a quote. Hand-rolled first/last-character stripping
    gets this wrong.
    """
    import shlex

    try:
        parts = shlex.split(v, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to the raw text rather than losing it.
        return v.strip("'\"")
    if len(parts) == 1:
        return parts[0]
    # No surrounding quotes: shlex still split it. Rejoin so the value is
    # preserved even when it contained spaces.
    return v if not v[:1] in ("'", '"') else parts[0]


def _secrets() -> dict[str, str]:
    """Environment wins over the secrets file."""
    file_vals = _read_secrets_file()
    return {**file_vals, **{k: v for k, v in os.environ.items() if k.startswith(("MAIL_", "DISCORD_", "TELEGRAM_", "SLACK_", "ROUTER_"))}}


def cfg() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def _v(key: str, default: str = "") -> str:
    return _secrets().get(key, default) or cfg().get(key, default)


@dataclass
class RouterConfig:
    """AI provider. Anthropic-SDK-compatible endpoint."""

    base_url: str = field(default_factory=lambda: _v("ROUTER_BASE_URL", DEFAULT_ROUTER_BASE))
    api_key: str = field(default_factory=lambda: _v("ROUTER_API_KEY"))
    model: str = field(default_factory=lambda: _v("ROUTER_MODEL", DEFAULT_MODEL))
    # Lower effort on the high-volume triage path; Opus for drafting.
    triage_model: str = field(default_factory=lambda: _v("ROUTER_TRIAGE_MODEL", ""))
    max_tokens: int = field(default_factory=lambda: int(_v("ROUTER_MAX_TOKENS", "16000")))

    @property
    def triage_model_id(self) -> str:
        return self.triage_model or self.model

    @property
    def model_id(self) -> str:
        """Alias so callers can use either name."""
        return self.model


@dataclass
class GoogleConfig:
    credentials_file: str = field(default_factory=lambda: _v("GOOGLE_CREDENTIALS", str(CONFIG_DIR / "google-credentials.json")))
    token_file: str = field(default_factory=lambda: _v("GOOGLE_TOKEN", str(CONFIG_DIR / "google-token.json")))
    account: str = field(default_factory=lambda: _v("GOOGLE_ACCOUNT", ""))
    # Auto-send authority. Off by default; set per-account in config.json.
    auto_send: bool = field(default_factory=lambda: _v("GOOGLE_AUTO_SEND", "false").lower() == "true")
    display_name: str = field(default_factory=lambda: _v("GOOGLE_DISPLAY_NAME", ""))
    calendar_enabled: bool = field(default_factory=lambda: _v("GOOGLE_CALENDAR_ENABLED", "true").lower() == "true")
    # IMAP is only used for the IDLE push listener. OAuth tokens cannot drive
    # IMAP; this is a separate app password, and it is optional — without it the
    # agent falls back to interval polling.
    imap_host: str = field(default_factory=lambda: _v("GOOGLE_IMAP_HOST", "imap.gmail.com"))
    imap_port: int = field(default_factory=lambda: int(_v("GOOGLE_IMAP_PORT", "993")))
    imap_password: str = field(default_factory=lambda: _v("GOOGLE_IMAP_PASSWORD"))


@dataclass
class MicrosoftConfig:
    tenant_id: str = field(default_factory=lambda: _v("MS_TENANT_ID"))
    client_id: str = field(default_factory=lambda: _v("MS_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _v("MS_CLIENT_SECRET"))
    token_file: str = field(default_factory=lambda: str(CONFIG_DIR / "ms-token.json"))
    account: str = field(default_factory=lambda: _v("MS_ACCOUNT", ""))
    auto_send: bool = field(default_factory=lambda: _v("MS_AUTO_SEND", "false").lower() == "true")
    display_name: str = field(default_factory=lambda: _v("MS_DISPLAY_NAME", ""))
    calendar_enabled: bool = field(default_factory=lambda: _v("MS_CALENDAR_ENABLED", "true").lower() == "true")


@dataclass
class NotifyConfig:
    discord_token: str = field(default_factory=lambda: _v("DISCORD_BOT_TOKEN"))
    discord_user_id: str = field(default_factory=lambda: _v("DISCORD_USER_ID"))
    telegram_token: str = field(default_factory=lambda: _v("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _v("TELEGRAM_CHAT_ID"))
    slack_token: str = field(default_factory=lambda: _v("SLACK_BOT_TOKEN"))
    slack_channel: str = field(default_factory=lambda: _v("SLACK_CHANNEL"))


@dataclass
class AgentConfig:
    """Behaviour knobs. Authority lives here, not in the prompt."""

    # None => agent decides via the escalation check. "always" => never send.
    send_mode: str = field(default_factory=lambda: _v("AGENT_SEND_MODE", "auto"))
    # Senders the agent may auto-reply to without approval.
    auto_send_contacts: list[str] = field(default_factory=lambda: cfg().get("auto_send_contacts", []))
    # Never auto-send to these, even if on the allowlist.
    never_auto_send: list[str] = field(default_factory=lambda: cfg().get("never_auto_send", []))
    # Keywords that force escalation regardless of contact status.
    escalation_keywords: list[str] = field(
        default_factory=lambda: cfg().get(
            "escalation_keywords",
            ["invoice", "payment", "wire", "contract", "legal", "attorney", "invoice",
             "password", "otp", "verify", "bank", "account number", "salary", "offer",
             "termination", "medical", "diagnosis", "passport", "ssn"],
        )
    )
    daily_token_cap: int = field(default_factory=lambda: int(_v("AGENT_DAILY_TOKEN_CAP", "500000")))
    # Hard ceilings on model spend. The token cap is checked once per run; a
    # single run can make a dozen model calls, so the per-minute cap and the
    # circuit breaker are what actually stop a runaway loop costing money.
    max_calls_per_min: int = field(default_factory=lambda: int(_v("AGENT_MAX_CALLS_PER_MIN", "20")))
    breaker_threshold: int = field(default_factory=lambda: int(_v("AGENT_BREAKER_THRESHOLD", "5")))
    warn_spend_pct: int = field(default_factory=lambda: int(_v("AGENT_WARN_SPEND_PCT", "80")))
    max_drafts_per_run: int = field(default_factory=lambda: int(_v("AGENT_MAX_DRAFTS", "40")))
    scan_interval_sec: int = field(default_factory=lambda: int(_v("AGENT_SCAN_INTERVAL", "300")))
    brief_hour: int = field(default_factory=lambda: int(_v("AGENT_BRIEF_HOUR", "7")))
    enabled_accounts: list[str] = field(default_factory=lambda: cfg().get("enabled_accounts", []))


@dataclass
class Config:
    router: RouterConfig = field(default_factory=RouterConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)
    microsoft: MicrosoftConfig = field(default_factory=MicrosoftConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def brain_path(self) -> Path:
        """Directory holding the style profiles. Callers append the filename."""
        return Path(_v("MAIL_AGENT_BRAIN", str(Path(__file__).parent.parent.parent / "brain")))

    def validate(self) -> list[str]:
        """Return a list of problems. Empty means usable."""
        problems = []
        if not self.router.api_key:
            problems.append("ROUTER_API_KEY not set")
        if not self.router.base_url:
            problems.append("ROUTER_BASE_URL not set")
        if not self.google.credentials_file or not Path(self.google.credentials_file).exists():
            if self.agent.enabled_accounts in ([], ["google"]) or "google" in self.agent.enabled_accounts:
                problems.append(f"google credentials not found at {self.google.credentials_file}")
        if self.agent.send_mode not in ("auto", "never"):
            problems.append(f"AGENT_SEND_MODE must be 'auto' or 'never', got {self.agent.send_mode!r}")
        return problems


def load() -> Config:
    return Config()
