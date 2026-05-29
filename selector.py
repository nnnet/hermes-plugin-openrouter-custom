"""OpenRouter live-catalog filter, ranker, and best-pick logic.

Pure functions — no I/O side effects. Used by both the refresh script
(refresh.py) and the on_session_start hook in __init__.py.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

OR_MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_live_catalog(api_key: str | None = None, timeout: float = 8.0) -> list[dict]:
    """Pull the live OpenRouter model catalog. Returns raw item dicts."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(OR_MODELS_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    data = payload.get("data", [])
    return [m for m in data if isinstance(m, dict) and m.get("id")]


def apply_filters(items: list[dict], filters: dict) -> list[dict]:
    """Keep only items that pass every filter clause."""
    if not items:
        return []
    excludes = _compile_patterns(filters.get("exclude_patterns", []))
    modality_req = (filters.get("modality") or "any").strip().lower()
    min_ctx = int(filters.get("min_context", 0) or 0)
    require_tools = bool(filters.get("require_tools", False))
    free_only = bool(filters.get("free_only", False))

    kept: list[dict] = []
    for item in items:
        mid = str(item.get("id") or "")
        if free_only and not mid.endswith(":free"):
            continue
        ctx = int(item.get("context_length") or 0)
        if ctx < min_ctx:
            continue
        params = item.get("supported_parameters") or []
        if require_tools and isinstance(params, list) and "tools" not in params:
            continue
        modality = str((item.get("architecture") or {}).get("modality") or "").lower()
        if modality_req != "any" and modality_req not in modality:
            continue
        if any(rx.search(mid) for rx in excludes):
            continue
        kept.append(item)
    return kept


def rank(items: list[dict], ranking: dict, prefer_patterns: list[str]) -> list[dict]:
    """Stable sort by primary then tiebreakers."""
    if not items:
        return []
    prefer_rxs = _compile_patterns(prefer_patterns)
    primary = (ranking.get("rank_by") or "prefer_match").strip().lower()
    tiebreakers = list(ranking.get("tiebreakers") or [])

    def feature(item: dict) -> dict:
        mid = str(item.get("id") or "")
        modality = str((item.get("architecture") or {}).get("modality") or "").lower()
        params = item.get("supported_parameters") or []
        return {
            "prefer_match": sum(1 for rx in prefer_rxs if rx.search(mid)),
            "context_desc": int(item.get("context_length") or 0),
            "modality_pref": 2 if "image" in modality else 1,
            "tools_count": len(params) if isinstance(params, list) else 0,
            "latency_p95": -1 * (item.get("_latency_p95") or 0),  # state-injected
        }

    def key(item: dict) -> tuple:
        feats = feature(item)
        # higher is better → negate to use ascending sort
        ordered_keys = [primary] + [k for k in tiebreakers if k != primary]
        return tuple(-feats.get(k, 0) for k in ordered_keys)

    return sorted(items, key=key)


def pick_best(
    config: dict,
    api_key: str | None = None,
    last_state: dict | None = None,
) -> dict:
    """End-to-end: fetch → filter → rank → pick.

    Returns a dict with the chosen real id, the full ranked candidate
    list (truncated), and a summary suitable for the state file.

    On fetch failure honours ``config['refresh']['on_failure']``:

    * ``keep_last`` — return the previous state.json untouched
    * ``rotate``    — return the next candidate down the previous list
    * ``fallback_static`` — fall back to ``fallback_static_id``
    """
    last_state = last_state or {}
    refresh_cfg = config.get("refresh") or {}
    on_failure = (refresh_cfg.get("on_failure") or "keep_last").strip().lower()
    fallback_id = refresh_cfg.get("fallback_static_id") or ""

    try:
        items = fetch_live_catalog(api_key=api_key)
    except Exception as exc:
        logger.warning("openrouter_custom.pick_best: catalog fetch failed: %s", exc)
        return _apply_failure_strategy(on_failure, fallback_id, last_state, reason=str(exc))

    filters = config.get("filters") or {}
    ranking = config.get("ranking") or {}
    max_cand = int(config.get("max_candidates") or 10)

    filtered = apply_filters(items, filters)
    ranked = rank(filtered, ranking, filters.get("prefer_patterns") or [])

    if not ranked:
        return _apply_failure_strategy(on_failure, fallback_id, last_state, reason="no candidates pass filters")

    top = ranked[:max_cand]
    chosen = top[0]
    return {
        "real_model_id": chosen["id"],
        "candidate_count": len(ranked),
        "candidates_top": [
            {
                "id": c["id"],
                "context_length": int(c.get("context_length") or 0),
                "modality": str((c.get("architecture") or {}).get("modality") or ""),
                "supports_tools": "tools" in (c.get("supported_parameters") or []),
            }
            for c in top
        ],
        "reason": f"ranked {len(ranked)} candidates by {ranking.get('rank_by', 'prefer_match')}",
    }


def _apply_failure_strategy(
    strategy: str, fallback_id: str, last_state: dict, reason: str
) -> dict:
    if strategy == "rotate" and last_state.get("candidates_top"):
        rotate_list = last_state["candidates_top"]
        # demote current head; pick next
        if len(rotate_list) > 1:
            next_id = rotate_list[1]["id"]
            return {
                "real_model_id": next_id,
                "candidate_count": last_state.get("candidate_count", len(rotate_list)),
                "candidates_top": rotate_list[1:] + rotate_list[:1],
                "reason": f"rotate (last fetch failed: {reason})",
            }
    if strategy == "fallback_static" and fallback_id:
        return {
            "real_model_id": fallback_id,
            "candidate_count": 0,
            "candidates_top": [],
            "reason": f"static fallback (last fetch failed: {reason})",
        }
    # keep_last — return prior state, or empty if none
    if last_state.get("real_model_id"):
        result = dict(last_state)
        result["reason"] = f"kept last (live fetch failed: {reason})"
        return result
    return {
        "real_model_id": fallback_id or "",
        "candidate_count": 0,
        "candidates_top": [],
        "reason": f"no last state, no fallback (live fetch failed: {reason})",
    }


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    out: list[re.Pattern] = []
    for p in patterns or []:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            logger.warning("openrouter_custom: bad regex %r: %s", p, exc)
    return out
