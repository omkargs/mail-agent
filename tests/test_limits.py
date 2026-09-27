"""Spend limits. These are what stop a runaway loop costing real money.

Every test here simulates a failure mode that actually drained a balance, so
a regression means money, not a cosmetic diff.
"""
from __future__ import annotations

import time

import pytest


# ------------------------------------------------------------------ classify

def test_out_of_credit_is_recognised():
    from mailagent.limits import classify

    e = Exception("Error code: 402 - payment_required, Insufficient credits.")
    assert classify(e) == "credits"


def test_rate_limit_is_recognised():
    from mailagent.limits import classify

    assert classify(Exception("Error code: 429 - Too Many Requests")) == "rate_limit"
    assert classify(Exception("rate limit exceeded")) == "rate_limit"


def test_gmail_quota_is_a_rate_limit_not_a_crash():
    """The real incident: listing 30 mail made 31 calls and returned 403
    quota-exceeded. That must back off, not be retried."""
    from mailagent.limits import classify

    e = Exception("403 Quota exceeded for quota metric 'Total Query Cost'")
    assert classify(e) in ("rate_limit", "forbidden")


# ----------------------------------------------------------------- rate cap

def test_per_minute_cap_stops_a_runaway_loop():
    from mailagent.limits import BudgetExhausted, Limits

    L = Limits(max_calls_per_min=3)
    L.take(); L.take(); L.take()
    with pytest.raises(BudgetExhausted):
        L.take()


def test_rate_window_slides():
    from mailagent.limits import Limits

    L = Limits(max_calls_per_min=2)
    L.take(); L.take()
    # Pretend the earlier calls aged out.
    L._calls = [t - 61 for t in L._calls]
    L.take()  # must not raise


def test_daily_cap_blocks_when_spent(cfg, provider):
    from mailagent.limits import BudgetExhausted, Limits
    from mailagent.storage import db

    db.record_usage(1000, 1000)
    L = Limits(daily_token_cap=1500)
    with pytest.raises(BudgetExhausted):
        L.check_tokens()


# ------------------------------------------------------------ circuit breaker

def test_credits_open_the_circuit_and_do_not_retry():
    """Retrying a 402 spends the remaining balance to learn the same thing."""
    from mailagent.limits import BudgetExhausted, CircuitOpen, global_limits
    from mailagent.agent.client import guarded_call

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise Exception("Error code: 402 - insufficient credits")

    L = global_limits()
    L.max_calls_per_min = 100
    L._open_until = 0.0
    L._failures = 0
    with pytest.raises(BudgetExhausted):
        guarded_call(FakeClient(), model="x", messages=[])
    # And it stays shut rather than trying again.
    with pytest.raises((BudgetExhausted, CircuitOpen)):
        guarded_call(FakeClient(), model="x", messages=[])


def test_circuit_opens_after_repeated_failures():
    from mailagent.limits import BudgetExhausted, CircuitOpen, Limits, global_limits
    from mailagent.agent.client import guarded_call

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise Exception("500 internal server error")

    # guarded_call reads the process-global budget, not a local instance.
    L = global_limits()
    L.max_calls_per_min = 100
    L.breaker_threshold = 3
    L._failures = 0
    L._open_until = 0.0
    L._cooldown = 0.0

    for _ in range(3):
        try:
            guarded_call(FakeClient(), model="x", messages=[])
        except Exception:
            pass
    with pytest.raises((BudgetExhausted, CircuitOpen)):
        guarded_call(FakeClient(), model="x", messages=[])


def test_success_resets_the_failure_count():
    from mailagent.limits import Limits

    L = Limits()
    L.record_failure(Exception("500 boom"))
    L.record_failure(Exception("500 boom"))
    assert L.status()["consecutive_failures"] == 2
    L.record_success()
    assert L.status()["consecutive_failures"] == 0


def test_backoff_grows_then_caps():
    from mailagent.limits import Limits

    L = Limits(backoff_base=5.0, backoff_max=60.0)
    seen = []
    for _ in range(6):
        L.record_failure(Exception("500 boom"))
        seen.append(L.wait_time())
    assert seen[0] < seen[2] < seen[4], "backoff must grow"
    assert max(seen) <= 60.0, "backoff must be capped"


# ------------------------------------------------------------------- wiring

def test_sdk_retries_are_disabled():
    """The SDK retried a 402 three times on its own, which is three wasted
    calls per failure."""
    from mailagent.agent.client import build_client
    from mailagent.config import RouterConfig

    c = build_client(RouterConfig(base_url="https://x.test", api_key="k", model="m"))
    assert c.max_retries == 0


def test_every_model_call_goes_through_the_guard():
    """If any call site bypasses the guard, the limits are decorative."""
    from pathlib import Path

    root = Path(__file__).parent.parent / "src" / "mailagent"
    # client.py holds the guard itself; discovery.probe is a deliberate
    # one-off health check during setup, not part of the agent's runtime.
    allowed = {"client.py", "discovery.py"}
    offenders = []
    for f in root.rglob("*.py"):
        if f.name in allowed:
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "messages.create(" in line:
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"unguarded model calls: {offenders}"


def test_status_is_reportable():
    from mailagent.limits import Limits

    s = Limits(max_calls_per_min=7, daily_token_cap=1000).status()
    for k in ("calls_last_min", "max_calls_per_min", "daily_token_cap",
              "tokens_left", "consecutive_failures", "circuit_open"):
        assert k in s
