"""Anthropic SDK client pointed at the router.

The router is an Anthropic-SDK-compatible endpoint (it returns Anthropic's
error envelope, not OpenAI's), so the standard SDK works with a custom
base_url. No OpenAI shim, no raw HTTP.

All model calls go through `call()`, which is where the spend limits live.
Nothing here should call `client.messages.create` directly — the limits are
only real if there is exactly one door.
"""
from __future__ import annotations

import logging
import os
import time

import anthropic

from ..config import RouterConfig
from ..limits import BudgetExhausted, CircuitOpen, global_limits

log = logging.getLogger(__name__)

# Model IDs. The router exposes a combo alias; local models are used for the
# high-volume triage path and the primary model for drafting.
PRIMARY = "combo/claude2mail"

# Max 4 cache breakpoints. Two are used: tools+system prefix, and the tail of
# the stable profile. Volatile content always goes after the last breakpoint.
CACHE_TOOLS = {"type": "ephemeral"}
CACHE_SYSTEM = {"type": "ephemeral"}


def build_client(rc: RouterConfig) -> anthropic.Anthropic:
    if not rc.api_key:
        raise RuntimeError("ROUTER_API_KEY not set — run setup.sh or export it")
    # This SDK version appends "/v1/messages" itself. Passing a base_url that
    # already ends in /v1 produced ".../v1/v1/messages" -> 404. Normalise the
    # host only.
    base = rc.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return anthropic.Anthropic(
        api_key=rc.api_key,
        base_url=base,
        timeout=180.0,
        # Retries are decided by our own limits, not the SDK's. The SDK happily
        # retried a 402 payment-required three times, which spends balance to
        # learn the same thing again.
        max_retries=0,
        default_headers={"anthropic-beta": "context-management-2025-06-27"} if os.environ.get("MAIL_AGENT_CONTEXT_EDIT") else None,
    )


def guarded_call(client: anthropic.Anthropic, **kwargs):
    """The only sanctioned way to make a model call.

    Enforces, in order: the per-minute rate limit, the daily token cap, and the
    circuit breaker; then records the outcome so a failing provider is backed
    off instead of retried into the ground.

    Raises BudgetExhausted or CircuitOpen when it declines to call — callers
    should treat those as "stop and tell the user", not as a bug.
    """
    L = global_limits()
    L.take()            # raises on rate limit or open circuit
    L.check_tokens()    # raises when the daily cap is spent
    try:
        resp = client.messages.create(**kwargs)
    except Exception as e:
        kind = L.record_failure(e)
        if kind in ("credits", "forbidden"):
            # Not transient. Retrying cannot help and only drains the balance.
            raise BudgetExhausted(
                f"provider refused the call ({kind}): {str(e)[:160]}"
            ) from e
        log.warning("model call failed (%s): %s", kind, str(e)[:160])
        raise
    L.record_success()
    return resp


def usage_to_dict(u) -> dict[str, int]:
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def handle_refusal(response) -> str:
    """Route a refusal to a safe outcome. Never retry the same prompt."""
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        cat = getattr(details, "category", None) if details else None
        return f"model declined to act (category={cat}); message skipped, no action taken"
    return ""
