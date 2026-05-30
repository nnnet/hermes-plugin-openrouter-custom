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
    from . import _state_dir, load_state, load_config
except ImportError:  # flat-module for tests / scripts
    import health as _h  # type: ignore[no-redef]
    from __init__ import _state_dir, load_state, load_config  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# Defaults — used only when load_config() doesn't provide a value or
# returns None. Each is exposed as a top-level field in plugin.yaml so
# operators can tune without touching code.
DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0
DEFAULT_PROBE_MAX_RETRIES = 0
DEFAULT_PROBE_JITTER_MIN_SECONDS = 0.5
DEFAULT_PROBE_JITTER_MAX_SECONDS = 1.5
DEFAULT_PROBE_AUTO_TUNE_TOP_N = 4
DEFAULT_PROBE_AUTO_TUNE_SUCCESS_RATE = 0.5
DEFAULT_PROBE_PROMPT = "ping"
DEFAULT_PROBE_MAX_TOKENS = 1
DEFAULT_PROBE_TEMPERATURE = 0.0
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _client(
    api_key: str | None,
    *,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    base_url: str | None = None,
):
    """Build a transient OpenAI client pointed at OpenRouter."""
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("openai sdk not installed inside this Python") from exc
    t = float(timeout_seconds) if timeout_seconds is not None else DEFAULT_PROBE_TIMEOUT_SECONDS
    r = int(max_retries) if max_retries is not None else DEFAULT_PROBE_MAX_RETRIES
    return OpenAI(
        base_url=(base_url or DEFAULT_OPENROUTER_BASE_URL),
        api_key=(api_key or os.environ.get("OPENROUTER_API_KEY", "") or "").strip(),
        timeout=max(t, 1.0),
        max_retries=max(r, 0),
    )


def _probe_one(
    client: Any,
    model_id: str,
    *,
    prompt: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[bool, str, str]:
    """Issue a minimal ping to ``model_id``.

    Returns ``(ok, responded_model, error_class)``. ``responded_model``
    is the model OR echoed in the response (may differ from the request
    when OR routes internally).

    Payload knobs (prompt / max_tokens / temperature) come from config
    via probe_all; the keyword fallbacks here are used only by direct
    callers (e.g. the per-row UI ``ping`` button via the
    ``/probe/single`` endpoint).
    """
    try:
        rsp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": str(prompt or DEFAULT_PROBE_PROMPT)}],
            max_tokens=int(max_tokens if max_tokens is not None else DEFAULT_PROBE_MAX_TOKENS),
            temperature=float(temperature if temperature is not None else DEFAULT_PROBE_TEMPERATURE),
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
    pause_min_seconds: float | None = None,
    pause_max_seconds: float | None = None,
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
    # Early-out when probe is disabled in config. The operator turns it
    # off when no traffic is using the alias (no rotation = no point
    # spending OR's free-tier quota on probes).
    cfg = load_config()
    if cfg.get("probe_enabled") is False:
        return {"probed": 0, "ok": 0, "failed": 0, "details": [], "skipped": True}

    # All knobs come from config; fallbacks land in DEFAULT_* constants
    # at module top so nothing magic is hidden in this body.
    timeout_cfg = cfg.get("probe_timeout_seconds")
    retries_cfg = cfg.get("probe_max_retries")
    base_url_cfg = cfg.get("probe_base_url")
    jitter_min = pause_min_seconds if pause_min_seconds is not None \
        else float(cfg.get("probe_jitter_min_seconds", DEFAULT_PROBE_JITTER_MIN_SECONDS) or 0)
    jitter_max = pause_max_seconds if pause_max_seconds is not None \
        else float(cfg.get("probe_jitter_max_seconds", DEFAULT_PROBE_JITTER_MAX_SECONDS) or 0)
    prompt = str(cfg.get("probe_prompt") or DEFAULT_PROBE_PROMPT)
    max_toks = int(cfg.get("probe_max_tokens") or DEFAULT_PROBE_MAX_TOKENS)
    temp = float(cfg.get("probe_temperature") if cfg.get("probe_temperature") is not None else DEFAULT_PROBE_TEMPERATURE)
    quarantine_threshold = int(cfg.get("quarantine_consecutive_fail_threshold", _h.DEFAULT_OPEN_AT_CONSECUTIVE) or _h.DEFAULT_OPEN_AT_CONSECUTIVE)
    backoff_minutes = cfg.get("quarantine_backoff_minutes")
    backoff_ladder = [int(m) * 60 for m in backoff_minutes] if isinstance(backoff_minutes, list) and backoff_minutes else None

    state = load_state()
    candidates: List[Dict[str, Any]] = list(state.get("candidates_top") or [])
    if max_candidates and max_candidates > 0:
        candidates = candidates[:max_candidates]

    client = _client(api_key, timeout_seconds=timeout_cfg, max_retries=retries_cfg, base_url=base_url_cfg)
    health = _h.load_health(_state_dir())

    details: List[Dict[str, Any]] = []
    ok_count = 0
    fail_count = 0

    for c in candidates:
        cid = str((c or {}).get("id") or "").strip()
        if not cid:
            continue
        ok, actual, err = _probe_one(client, cid, prompt=prompt, max_tokens=max_toks, temperature=temp)
        if ok:
            _h.record_success(health, cid)
            ok_count += 1
            details.append({"id": cid, "ok": True, "responded": actual, "error_class": ""})
        else:
            _h.record_failure(
                health, cid, error_class=err,
                open_at_consecutive=quarantine_threshold,
                backoff_ladder=backoff_ladder,
            )
            fail_count += 1
            details.append({"id": cid, "ok": False, "responded": "", "error_class": err})
        if jitter_max > 0:
            time.sleep(random.uniform(jitter_min, jitter_max))

    _h.save_health(_state_dir(), health)

    # Auto-tune: when the top-N (config: probe_auto_tune_top_n) sample
    # shows < probe_auto_tune_success_rate success, the current pool is
    # mostly unhealthy — refresh state.json inline so the next ranking
    # can incorporate any newly-promoted candidates instead of waiting
    # for the regular refresh cron. Skipped when ok_count is zero
    # (likely a network outage — refresh would just rewrite the same
    # pool and lose nothing).
    top_n = int(cfg.get("probe_auto_tune_top_n", DEFAULT_PROBE_AUTO_TUNE_TOP_N) or DEFAULT_PROBE_AUTO_TUNE_TOP_N)
    success_threshold = float(cfg.get("probe_auto_tune_success_rate", DEFAULT_PROBE_AUTO_TUNE_SUCCESS_RATE) or DEFAULT_PROBE_AUTO_TUNE_SUCCESS_RATE)
    top_details = details[:max(top_n, 1)]
    top_ok = sum(1 for d in top_details if d.get("ok"))
    if top_details and 0 < top_ok / len(top_details) < success_threshold:
        try:
            _trigger_refresh()
            return {
                "probed": len(details),
                "ok": ok_count,
                "failed": fail_count,
                "details": details,
                "refresh_triggered": True,
                "skipped": False,
            }
        except Exception:
            logger.exception("auto-tune refresh failed (non-fatal)")

    return {
        "probed": len(details),
        "ok": ok_count,
        "failed": fail_count,
        "details": details,
        "refresh_triggered": False,
        "skipped": False,
    }


def _trigger_refresh() -> None:
    """Run the same refresh logic as ``refresh.py`` inline so the next
    rotation sees a freshly-ranked pool."""
    try:
        from . import load_config, load_state, save_state
        from .selector import pick_best
    except ImportError:  # flat-module
        from __init__ import load_config, load_state, save_state  # type: ignore[no-redef]
        from selector import pick_best  # type: ignore[no-redef]
    import datetime as _dt
    cfg = load_config()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or None
    result = pick_best(cfg, api_key=api_key, last_state=load_state())
    state = {
        "last_refresh_iso": _dt.datetime.now(_dt.UTC).isoformat(),
        "pseudo_alias": cfg.get("pseudo_model_alias", "best-free"),
        "config_snapshot": {
            "filters": cfg.get("filters", {}),
            "ranking": cfg.get("ranking", {}),
            "refresh": cfg.get("refresh", {}),
        },
        **result,
    }
    save_state(state)
    logger.info("auto-tune refresh: alias=%s real=%s", state["pseudo_alias"], state.get("real_model_id"))
