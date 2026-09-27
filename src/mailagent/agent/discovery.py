"""Discover what an Anthropic-compatible endpoint can do.

The wizard cannot ask a user to type a model id: nobody knows these strings,
and a wrong one fails only later, mid-run, with an opaque 404. The endpoint
itself knows, so ask it — most expose `GET /v1/models`.

Everything here is best-effort. A provider that does not implement discovery
must still be usable, so every function returns None rather than raising, and
the caller falls back to asking.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT = 20

# Ordered best-first. The wizard presents these at the top of the list when a
# provider reports a catalogue; anything unrecognised is still offered, sorted
# alphabetically, because a user's new model may be exactly what they want.
PREFERRED = [
    "claude-opus-5.5", "claude-opus-5", "claude-opus-4.8",
    "claude-sonnet-5",
    "claude-fable-5.1", "claude-fable-5",
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
    "gemini-3.8-flash-high", "gemini-3.1-pro-high",
    "glm-5.3", "glm-5.2", "glm-5.3-flash",
    "deepseek-v4-pro-alibaba", "deepseek-v4.1-flash", "deepseek-v4-flash-alibaba",
    "agnes-3-flash", "agnes-2.5-flash",
]


def normalise_base(url: str) -> str:
    """Strip trailing slashes and a trailing /v1.

    The SDK appends /v1/messages itself, so a base of https://host/v1 produces
    https://host/v1/v1/messages — a 404 that looks like a bad API key.
    """
    u = (url or "").strip().rstrip("/")
    return re.sub(r"/v1$", "", u)


def list_models(base_url: str, api_key: str) -> list[str] | None:
    """Ask the endpoint which models it has. None if it will not say."""
    base = normalise_base(base_url)
    if not base or not api_key:
        return None
    for path in ("/v1/models", "/models"):
        url = base + path
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            log.debug("model discovery %s -> HTTP %s", path, e.code)
            continue
        except Exception as e:
            log.debug("model discovery %s -> %s", path, type(e).__name__)
            continue

        ids = _extract(data)
        if ids:
            return ids
    return None


def _extract(data: Any) -> list[str]:
    """Pull model ids out of the several shapes providers use."""
    items: Any = data
    if isinstance(data, dict):
        for key in ("data", "models", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            return []
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                out.append(str(mid))
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [m for m in out if not (m in seen or seen.add(m))]


def rank(models: list[str]) -> list[str]:
    """Best first: known-good ids in preference order, then the rest."""
    known = [m for m in PREFERRED if m in models]
    rest = sorted(m for m in models if m not in set(known))
    return known + rest


def probe(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """Check a base/key/model actually works, before it is written to config.

    Returns {ok, model, said, error}. A model that answers 'OK' is one the
    agent can use; failing here is much cheaper than failing at 3am.
    """
    from .client import build_client
    from ..config import RouterConfig

    try:
        client = build_client(RouterConfig(
            base_url=base_url, api_key=api_key, model=model, max_tokens=16,
        ))
        r = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        )
        said = ""
        for b in getattr(r, "content", []):
            if getattr(b, "type", "") == "text":
                said = b.text.strip()[:40]
        return {"ok": True, "model": getattr(r, "model", model) or model,
                "said": said, "error": ""}
    except Exception as e:
        return {"ok": False, "model": model, "said": "",
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
