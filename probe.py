"""Health probe — issue a tiny 1-token request to each top-K candidate
and record the outcome into ``health.json``.

This is the DRIVER for rotation strategies under the current architecture
(phase 1). It is called by the cron job ``openrouter-custom-probe`` on a
configurable schedule (default 10m) and by the dashboard's ``Probe now``
button via :func:`probe_all`.

Probes are cheap — a single "ping" prompt with ``max_tokens=1`` on every
candidate currently in ``state.json:candidates_top``. The actual model
that answered (echoed in ``response.model``) is what we record success
for.  Any HTTP / SDK error counts as a failure.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Dict, List

try:  # package-style
    from . import health as _h
    from . import _state_dir, load_state
except ImportError:  # flat-module for tests / scripts
    import health as _h  # type: ignore[no-redef]
    from __init__ import _state_dir, load_state  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def _client(api_key: str | None):
    """Build a transient OpenAI client pointed at OpenRouter."""
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("openai sdk not installed inside this Python") from exc
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=(api_key or os.environ.get("OPENROUTER_API_KEY", "") or "").strip(),
        timeout=15.0,
    )


def _probe_one(client: Any, model_id: str) -> tuple[bool, str, str]:
    """Issue a 1-token ping to ``model_id``.

    Returns ``(ok, responded_model, error_class)``. ``responded_model``
    is the model OR echoed in the response (may differ from the request
    when OR routes internally).
    """
    try:
        rsp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0.0,
        )
    except Exception as exc:
        # Best-effort error classification. Order matters: pick the most
        # specific signal first.
        name = type(exc).__name__
        msg = str(exc)
        msg_l = msg.lower()
        klass = name
        # Status-coded HTTP exceptions from openai SDK have integer code:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429 or "429" in msg or "rate limit" in msg_l:
            klass = "429"
        elif status_code == 404 or "404" in msg:
            klass = "404"
        elif status_code == 400 or "BadRequest" in name:
            klass = "400"
        elif status_code == 401 or "unauthorized" in msg_l or "401" in msg:
            klass = "401"
        elif "timeout" in msg_l or "timed out" in msg_l:
            klass = "timeout"
        elif status_code and 500 <= int(status_code) < 600:
            klass = "5xx"
        elif "connection" in msg_l or "ConnectError" in name:
            klass = "net"
        return False, "", klass[:24]
    actual = str(getattr(rsp, "model", "") or "").strip() or model_id
    return True, actual, ""


def probe_all(
    *,
    api_key: str | None = None,
    max_candidates: int | None = None,
    pause_min_seconds: float = 0.5,
    pause_max_seconds: float = 1.5,
) -> Dict[str, Any]:
    """Run probes against every model in state.json:candidates_top.

    Light jitter (``pause_min_seconds..pause_max_seconds`` between
    probes) avoids hammering OpenRouter's free-tier with 10 simultaneous
    requests, which itself trips rate limits and pollutes our health
    signal. With defaults: ~5-15s total for 10 candidates.

    Returns a summary dict::

        {
          "probed": 10,
          "ok": 8,
          "failed": 2,
          "details": [{"id":"qwen/...","ok":true,"error_class":""}, ...]
        }
    """
    state = load_state()
    candidates: List[Dict[str, Any]] = list(state.get("candidates_top") or [])
    if max_candidates and max_candidates > 0:
        candidates = candidates[:max_candidates]

    client = _client(api_key)
    health = _h.load_health(_state_dir())

    details: List[Dict[str, Any]] = []
    ok_count = 0
    fail_count = 0

    for c in candidates:
        cid = str((c or {}).get("id") or "").strip()
        if not cid:
            continue
        ok, actual, err = _probe_one(client, cid)
        if ok:
            _h.record_success(health, cid)
            ok_count += 1
            details.append({"id": cid, "ok": True, "responded": actual, "error_class": ""})
        else:
            _h.record_failure(health, cid, error_class=err)
            fail_count += 1
            details.append({"id": cid, "ok": False, "responded": "", "error_class": err})
        if pause_max_seconds > 0:
            time.sleep(random.uniform(pause_min_seconds, pause_max_seconds))

    _h.save_health(_state_dir(), health)
    return {
        "probed": len(details),
        "ok": ok_count,
        "failed": fail_count,
        "details": details,
    }
