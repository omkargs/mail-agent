"""Spend control.

The agent can burn through an API balance in an afternoon. A token cap alone
is not enough, because it is checked once per run — a single run can make a
dozen model calls, and a retry loop can make far more.

Four limits, cheapest first:

1. per-minute calls   — stops a runaway loop before it costs anything
2. daily tokens       — the hard ceiling, checked before every call
3. backoff            — 429 and 402 mean "stop", not "try again immediately"
4. circuit breaker    — after repeated failures, stop calling entirely and
                        tell the user, rather than hammering a dead endpoint

Every limit degrades to "do less", never to "ignore the limit".
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# Conservative defaults. A user with real money can raise them in config.
DEFAULT_MAX_CALLS_PER_MIN = 20
DEFAULT_DAILY_TOKEN_CAP = 500_000
DEFAULT_BACKOFF_BASE = 5.0
DEFAULT_BACKOFF_MAX = 300.0

# After this many consecutive provider failures, stop and report.
DEFAULT_BREAKER_THRESHOLD = 5

_429 = re.compile(r"\b429\b|rate.?limit|too many requests", re.I)
# Gmail's "403 Quota exceeded" is a rate limit, not an empty wallet. It must
# not be classified as credits: that would tell the user to top up a
# perfectly funded account and shut the circuit for the full cooldown.
_402 = re.compile(r"\b402\b|payment required|insufficient credit|"
                  r"insufficient (balance|funds)|credit balance is too low", re.I)
_403 = re.compile(r"\b403\b|forbidden|permission denied", re.I)
# Any 5xx, not just 50[0-9]. A Cloudflare 520 came back marked
# "retryable": true, "retry_after": 60 and was classified "other", so the
# agent died on a failure it was explicitly told to retry.
_5xx = re.compile(
    r"\b5\d{2}\b|internal server error|bad gateway|gateway timeout|"
    r"service unavailable|unavailable|cloudflare",
    re.I,
)


class BudgetExhausted(RuntimeError):
    """The configured spend limit was reached. Do not make more calls."""


class CircuitOpen(RuntimeError):
    """Too many consecutive provider failures. Stop calling entirely."""


def classify(err: Exception) -> str:
    """What kind of failure is this? Drives whether we back off or give up."""
    text = f"{err}"
    if _402.search(text):
        return "credits"
    if _429.search(text):
        return "rate_limit"
    if _403.search(text):
        return "forbidden"
    if _5xx.search(text):
        return "server"
    return "other"


class Limits:
    """Live spend state. One instance per process."""

    def __init__(
        self,
        max_calls_per_min: int = DEFAULT_MAX_CALLS_PER_MIN,
        daily_token_cap: int = DEFAULT_DAILY_TOKEN_CAP,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,
    ):
        self.max_calls_per_min = max_calls_per_min
        self.daily_token_cap = daily_token_cap
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.breaker_threshold = breaker_threshold

        self._calls: list[float] = []
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._cooldown = 0.0

    # ------------------------------------------------------------------ calls
    def take(self) -> None:
        """Account for one model call. Raises rather than exceeding the rate."""
        now = time.time()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60.0]
            if len(self._calls) >= self.max_calls_per_min:
                wait = 60.0 - (now - self._calls[0])
                raise BudgetExhausted(
                    f"rate limit: {self.max_calls_per_min} calls/min reached, "
                    f"retry in {wait:.0f}s"
                )
            if self._open_until and now < self._open_until:
                raise CircuitOpen(
                    f"circuit open after {self._failures} failures; "
                    f"retry in {self._open_until - now:.0f}s"
                )
            self._calls.append(now)

    def tokens_left(self) -> int:
        from .storage import db

        u = db.usage_today()
        used = u.get("input_tokens", 0) + u.get("output_tokens", 0)
        return max(0, self.daily_token_cap - used)

    def check_tokens(self) -> None:
        left = self.tokens_left()
        if left <= 0:
            raise BudgetExhausted(
                f"daily token cap reached ({self.daily_token_cap:,}); "
                f"no further calls today"
            )

    def spendable(self, est_tokens: int = 0) -> bool:
        """Whether another call of this size fits under the cap."""
        return self.tokens_left() > max(est_tokens, 1)

    # -------------------------------------------------------------- outcomes
    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._cooldown = 0.0
            self._open_until = 0.0

    @staticmethod
    def _retry_after(err: Exception) -> float:
        """Honour a server-supplied backoff hint.

        A Cloudflare 520 arrived carrying "retry_after": 60. Guessing our own
        backoff ignored the operator's instruction and retried sooner than the
        origin asked, which is how a single blip becomes a retry storm.
        """
        # The hint arrives inside a JSON error body, so the key is quoted:
        # '"retry_after": 60'. Match with or without the quotes.
        m = re.search(r"['\"]?retry_after['\"]?\s*[:=]\s*['\"]?(\d{1,4})", f"{err}")
        return float(m.group(1)) if m else 0.0

    def record_failure(self, err: Exception) -> str:
        """Back off. Returns the kind of failure, for the caller's log."""
        kind = classify(err)
        with self._lock:
            self._failures += 1
            # A credit failure is terminal, not transient. Backing off and
            # retrying just wastes the remaining balance on 402s.
            if kind in ("credits", "forbidden"):
                self._open_until = time.time() + self.backoff_max
                return kind
            self._cooldown = max(
                min(self.backoff_base * (2 ** (self._failures - 1)), self.backoff_max),
                min(self._retry_after(err), self.backoff_max),
            )
            if self._failures >= self.breaker_threshold:
                self._open_until = time.time() + min(self._cooldown * 2, self.backoff_max)
                log.error("circuit opened after %d failures", self._failures)
            return kind

    def wait_time(self) -> float:
        with self._lock:
            now = time.time()
            if self._open_until and now < self._open_until:
                return self._open_until - now
            return self._cooldown

    def status(self) -> dict[str, Any]:
        # Read the DB outside the lock. status() runs on the error path, and a
        # lock held across a query would stall every caller, not just this one.
        tokens_left = self.tokens_left()
        with self._lock:
            now = time.time()
            # Read the fields directly rather than calling wait_time(), which
            # takes the same non-reentrant lock. A deadlock here would freeze
            # the whole daemon, since status() is on the failure path.
            backoff = (self._open_until - now) if (
                self._open_until and now < self._open_until) else self._cooldown
            return {
                "calls_last_min": len([t for t in self._calls if now - t < 60.0]),
                "max_calls_per_min": self.max_calls_per_min,
                "daily_token_cap": self.daily_token_cap,
                "tokens_left": tokens_left,
                "consecutive_failures": self._failures,
                "circuit_open": bool(self._open_until and now < self._open_until),
                "backoff_sec": round(backoff, 1),
            }


# The daemon shares one instance so the chat thread, the scan loop and the
# scheduler all draw from the same budget. A per-thread counter would let each
# one spend the full allowance.
_GLOBAL = Limits()


def global_limits() -> Limits:
    return _GLOBAL


def from_config(cfg) -> Limits:
    """Build limits from the agent's config, then apply them globally."""
    a = cfg.agent
    L = global_limits()
    L.max_calls_per_min = getattr(a, "max_calls_per_min", DEFAULT_MAX_CALLS_PER_MIN)
    L.daily_token_cap = a.daily_token_cap
    L.breaker_threshold = getattr(a, "breaker_threshold", DEFAULT_BREAKER_THRESHOLD)
    return L
