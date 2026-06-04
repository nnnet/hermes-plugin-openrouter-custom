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


def fetch_live_catalog(
    api_key: str | None = None,
    timeout: float = 8.0,
    models_url: str | None = None,
) -> list[dict]:
    """Pull the live OpenRouter model catalog. Returns raw item dicts.

    ``models_url`` overrides the default OpenRouter endpoint — used when
    the plugin is routed through a local Bifrost gateway (see
    config_overrides.yaml ``models_url``).
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = (models_url or OR_MODELS_URL).strip()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    data = payload.get("data", [])
    return [m for m in data if isinstance(m, dict) and m.get("id")]


_PARAMS_RX = re.compile(r"(\d+(?:\.\d+)?)\s*[Bb]\b")


def _extract_params(item: dict) -> tuple[str, float]:
    """Return ``(display_string, billions_float)`` for a model.

    OpenRouter doesn't expose parameter count as a structured field, so
    we sniff it out of ``description`` with a regex. A few model
    families (laguna-xs, laguna-m, kimi) ship descriptions that omit
    the count and need a hard-coded fallback — these mirror the same
    overrides used in the operator's reference ``curl | jq`` pipeline.

    The display string is human-friendly ("70B", "15B", "Not Specified");
    the float is exposed as the ``params_desc`` ranking feature
    (higher = bigger model preferred). "Not Specified" maps to 0.0 so
    those candidates rank last when ``params_desc`` is selected.
    """
    mid = str(item.get("id") or "").lower()
    if "laguna-xs" in mid:
        return ("15B", 15.0)
    if "laguna-m" in mid:
        return ("40B", 40.0)
    if "kimi" in mid:
        return ("70B+", 70.0)
    desc = str(item.get("description") or "")
    m = _PARAMS_RX.search(desc)
    if not m:
        return ("Not Specified", 0.0)
    raw = m.group(1)
    try:
        val = float(raw)
    except ValueError:
        return ("Not Specified", 0.0)
    return (f"{raw}B", val)


def _price_per_million(pricing: dict, key: str) -> float:
    """Return USD per 1 000 000 tokens for the given pricing field.

    OpenRouter exposes pricing as decimal strings keyed by direction
    (``prompt``, ``completion``, ``request``, ``image``) in **USD per
    token**. Multiply by 1e6 to get a human-comparable per-million rate.

    Defensive handling:

    * Missing / malformed value → +inf (excluded under any finite budget).
    * Negative value (e.g. ``"-1"``) → +inf as well. OR uses ``-1`` as a
      "dynamic / not-fixed" sentinel for platform wildcards like
      ``openrouter/auto`` whose real per-token cost depends on which
      underlying paid model wins the routing. Treating it as cheaper
      than zero would silently leak paid traffic into the free-tier
      candidate pool.
    """
    try:
        raw = float(pricing.get(key, "0") or 0)
    except (TypeError, ValueError):
        return float("inf")
    if raw < 0:
        return float("inf")
    return raw * 1_000_000


def apply_filters(items: list[dict], filters: dict) -> list[dict]:
    """Keep only items that pass every filter clause."""
    if not items:
        return []
    excludes = _compile_patterns(filters.get("exclude_patterns", []))
    modality_req = (filters.get("modality") or "any").strip().lower()
    min_ctx = int(filters.get("min_context", 0) or 0)
    require_tools = bool(filters.get("require_tools", False))
    # free_only kept as back-compat shortcut; superseded by price.{prompt,completion}_max.
    free_only = bool(filters.get("free_only", False))

    price_cfg = filters.get("price") or {}
    try:
        prompt_max = float(price_cfg.get("prompt_max", 0) or 0)
    except (TypeError, ValueError):
        prompt_max = 0.0
    try:
        completion_max = float(price_cfg.get("completion_max", 0) or 0)
    except (TypeError, ValueError):
        completion_max = 0.0

    # When the operator declares a zero-budget filter (prompt_max == 0 AND
    # completion_max == 0), also require the id to end in ``:free``.
    # Background: OpenRouter reports platform wildcards like
    # ``openrouter/auto`` with a literal "0" prompt/completion price even
    # though that endpoint routes to (and bills at) whichever underlying
    # paid model wins the auto-selection. Without this strictness, those
    # wildcards leak into the candidate pool and surprise the operator
    # with paid traffic on a free-tier alias. Set free_only or use the
    # exclude_patterns escape hatch to override.
    strict_free = prompt_max == 0.0 and completion_max == 0.0

    kept: list[dict] = []
    for item in items:
        mid = str(item.get("id") or "")
        if (free_only or strict_free) and not mid.endswith(":free"):
            continue
        pricing = item.get("pricing") or {}
        prompt_per_m = _price_per_million(pricing, "prompt")
        completion_per_m = _price_per_million(pricing, "completion")
        if prompt_per_m > prompt_max:
            continue
        if completion_per_m > completion_max:
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
        _, params_b = _extract_params(item)
        return {
            "prefer_match": sum(1 for rx in prefer_rxs if rx.search(mid)),
            "context_desc": int(item.get("context_length") or 0),
            "modality_pref": 2 if "image" in modality else 1,
            "tools_count": len(params) if isinstance(params, list) else 0,
            "params_desc": params_b,
            # Reverse: smaller models score higher. Negated so the
            # outer ``-feats[k]`` flip still produces ascending order.
            # Items without parseable B (params_b==0) get a very negative
            # score so they rank LAST in asc order, matching the desc
            # convention where unparseable also ranks last.
            "params_asc": -params_b if params_b > 0 else -1e9,
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

    # Catalog endpoint override (e.g. through a local Bifrost gateway).
    # Falls back to the OpenRouter default when omitted.
    models_url = (config.get("models_url") or "").strip() or None

    try:
        items = fetch_live_catalog(api_key=api_key, models_url=models_url)
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
                "prompt_per_million_usd": _price_per_million(c.get("pricing") or {}, "prompt"),
                "completion_per_million_usd": _price_per_million(c.get("pricing") or {}, "completion"),
                "params_display": _extract_params(c)[0],
                "params_billions": _extract_params(c)[1],
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
