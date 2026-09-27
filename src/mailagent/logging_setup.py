"""Structured logging with redaction.

Email bodies, tokens, and addresses are sensitive. Nothing sensitive reaches
the log at INFO. The redaction is enforced in the formatter, not at call sites,
so a careless call site still cannot leak.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

_SECRET_KEYS = re.compile(
    r"(api[_-]?key|token|secret|password|client[_-]?secret|authorization|bearer|refresh)",
    re.I,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_LONG_TOKEN = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
# OTP-shaped: a short digit run adjacent to a trigger word, in either order.
_OTP = re.compile(
    r"((?:your\s+)?(?:otp|code|pin|password)(?:\s+is\s+(?:your\s+)?|:)?\s*)\b\d{4,8}\b",
    re.I,
)


# KEY=<value> or KEY: <value> — masks the value, keeps the key readable so
# "ROUTER_API_KEY not set" stays diagnosable.
_KV_SECRET = re.compile(
    r"((?:api[_-]?key|token|secret|password|client[_-]?secret)\s*[=:]\s*)(\S{4,})",
    re.I,
)


def redact(text: str) -> str:
    """Mask secret values, emails, tokens, and OTP-shaped strings."""
    if not text:
        return text
    out = _LONG_TOKEN.sub("[REDACTED_TOKEN]", text)
    out = _KV_SECRET.sub(r"\1[REDACTED]", out)
    out = _EMAIL.sub("[EMAIL]", out)
    out = _OTP.sub(r"\1[REDACTED_OTP]", out)
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        # Mask secret VALUES inline, but never blank a whole line just because
        # it mentions a secret-sounding key. An error like "ROUTER_API_KEY not
        # set" must stay readable — otherwise the operator cannot diagnose it.
        return redact(msg)


def setup(level: int = logging.INFO, logfile: str | Path | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if logfile:
        p = Path(logfile)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Third-party loggers are noisy and can echo request bodies.
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient", "google.auth", "msal"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
