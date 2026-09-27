"""Router base_url handling.

The Anthropic SDK appends /v1/messages itself. Passing a base_url that already
ends in /v1 produced https://router.bynara.id/v1/v1/messages -> 404. Found by
running against the real router, not by a test.
"""
from __future__ import annotations

from mailagent.agent.client import build_client
from mailagent.config import RouterConfig


def _final_url(base: str) -> str:
    c = build_client(RouterConfig(api_key="k", base_url=base))
    return c.base_url


def test_bare_host_is_used_as_is():
    assert _final_url("https://router.bynara.id") == "https://router.bynara.id"


def test_trailing_slash_is_normalised():
    assert _final_url("https://router.bynara.id/") == "https://router.bynara.id"


def test_existing_v1_suffix_is_stripped():
    """A user pasting a /v1 base must not produce /v1/v1."""
    assert _final_url("https://router.bynara.id/v1") == "https://router.bynara.id"
    assert _final_url("https://router.bynara.id/v1/") == "https://router.bynara.id"


def test_missing_key_raises():
    import pytest

    with pytest.raises(RuntimeError, match="ROUTER_API_KEY"):
        build_client(RouterConfig(api_key="", base_url="https://x.example"))
