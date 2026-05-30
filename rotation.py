"""Rotation strategies for picking the ordered ``models:[...]`` list sent
to OpenRouter.

All strategies share the same signature::

    def strategy(
        candidates: list[dict],   # state.json candidates_top, ranked
        health: dict,             # health.json contents
        max_count: int,           # internal_fallback.sequential_count
    ) -> list[str]                # ordered model ids to send

Returned list length is ``<= max_count``. Empty list = "no candidates
available" — the caller should fall back to the single ``chosen`` id.

Strategies
----------

* ``static`` — strict ranking order, no health awareness. Identity of the
  current behaviour pre-rotation feature.

* ``failover_with_health`` — rank order minus blocked models (circuit OPEN
  and probe time not yet due). Recently-failing models drop out
  temporarily but bypass the half-open dance — they come back as soon as
  ``next_probe_iso`` passes.

* ``circuit_breaker`` — like failover_with_health but actively schedules
  HALF_OPEN probes: when a model's probe time arrives it's inserted at
  position 1 (single probe attempt) so a success closes the circuit; a
  failure re-opens it with longer backoff.

* ``sticky_health_weighted`` — keep the ranking order BUT multiply each
  candidate's effective rank by ``success_rate**weight``. Models with low
  success rates get demoted gradually instead of being binary-blocked.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, List

try:  # package-style import when used inside hermes runtime
    from . import health as _h
except ImportError:  # flat-module import used by the tests
    import health as _h  # type: ignore[no-redef]


RotationStrategy = Callable[[List[Dict[str, Any]], Dict[str, Any], int], List[str]]


# Public name → strategy function.
_REGISTRY: Dict[str, RotationStrategy] = {}

ALLOWED_MODES = ("static", "failover_with_health", "circuit_breaker", "sticky_health_weighted")


def _register(name: str) -> Callable[[RotationStrategy], RotationStrategy]:
    def deco(fn: RotationStrategy) -> RotationStrategy:
        _REGISTRY[name] = fn
        return fn
    return deco


def _candidate_ids(candidates: List[Dict[str, Any]]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for c in candidates:
        cid = str((c or {}).get("id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@_register("static")
def _static(
    candidates: List[Dict[str, Any]],
    health: Dict[str, Any],
    max_count: int,
) -> List[str]:
    """Strict ranking order. Ignores health."""
    return _candidate_ids(candidates)[: max(max_count, 1)]


def _interleave_main_tail(main: List[str], tail: List[str], max_count: int) -> List[str]:
    """Cap result at ``max_count`` while guaranteeing at least one tail
    slot when a tail exists.  Without this rule, the cap silently
    drops quarantined models when ``len(main) >= max_count`` —
    defeating the whole point of «push to end, don't exclude»."""
    max_count = max(max_count, 1)
    if not tail:
        return main[:max_count]
    if not main:
        return tail[:max_count]
    # Reserve 1 slot for tail. The very BEST quarantined (first in tail
    # = highest by ranking among the bad) gets the chance to be tried
    # last server-side. Any remaining tail entries are appended only if
    # there's still room.
    main_slots = max(max_count - 1, 1)
    out = main[:main_slots] + tail[:1]
    # Fill any remaining cap with more tail entries.
    if len(out) < max_count:
        more = tail[1 : max_count - len(out) + 1]
        out.extend(more)
    return out[:max_count]


@_register("failover_with_health")
def _failover_with_health(
    candidates: List[Dict[str, Any]],
    health: Dict[str, Any],
    max_count: int,
) -> List[str]:
    """Rank order; models in quarantine (OPEN circuit, cooldown not yet
    elapsed) move to the TAIL of the list.  They're not excluded — OR
    will still try them last after the healthy ones fail, because we
    can't be sure the quarantine state still reflects reality and we
    don't want to throw away a potentially-recovered top model."""
    now = _h._now()
    models = health.get("models", {})
    main: List[str] = []
    tail: List[str] = []
    for cid in _candidate_ids(candidates):
        entry = models.get(cid)
        if entry and _h.is_blocked(entry, now):
            tail.append(cid)
        else:
            main.append(cid)
    return _interleave_main_tail(main, tail, max_count)


@_register("circuit_breaker")
def _circuit_breaker(
    candidates: List[Dict[str, Any]],
    health: Dict[str, Any],
    max_count: int,
) -> List[str]:
    """Healthy models in ranking order, then the one whose quarantine
    just elapsed promoted to slot 1 (probe), then quarantined models at
    the tail.

    Mid-quarantine models are pushed to the END (not dropped) so OR can
    still try them as a last resort — we never know for sure when the
    upstream provider recovered.

    Note: this function does NOT mutate health. The caller is expected to
    do the half_open transition + record outcome separately.
    """
    now = _h._now()
    models = health.get("models", {})
    half_open_probe: str | None = None
    main: List[str] = []
    quarantined: List[str] = []
    for cid in _candidate_ids(candidates):
        entry = models.get(cid)
        if not entry:
            main.append(cid)
            continue
        state = entry.get("circuit_state", _h.CIRCUIT_CLOSED)
        if state == _h.CIRCUIT_OPEN:
            next_probe = _h._parse_iso(entry.get("next_probe_iso"))
            if next_probe is None or now >= next_probe:
                # Quarantine elapsed — promote to slot 1 as probe.
                if half_open_probe is None:
                    half_open_probe = cid
                continue
            # Still in quarantine — push to tail (NOT skipped).
            quarantined.append(cid)
            continue
        if state == _h.CIRCUIT_HALF_OPEN:
            if half_open_probe is None:
                half_open_probe = cid
            continue
        main.append(cid)
    # Probe + main, then reserve a tail slot for quarantined just like
    # failover_with_health. Probe slot consumes one of max_count.
    probe_list = [half_open_probe] if half_open_probe else []
    # Don't double-count the probe in main if somehow it ended up there.
    main_filtered = [m for m in main if m != half_open_probe]
    quar_filtered = [q for q in quarantined if q != half_open_probe]
    remaining = max(max_count - len(probe_list), 1)
    tail_part = _interleave_main_tail(main_filtered, quar_filtered, remaining)
    ordered = probe_list + tail_part
    # Dedup while preserving order.
    seen: set[str] = set()
    out: List[str] = []
    for cid in ordered:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
        if len(out) >= max_count:
            break
    return out


@_register("sticky_health_weighted")
def _sticky_health_weighted(
    candidates: List[Dict[str, Any]],
    health: Dict[str, Any],
    max_count: int,
) -> List[str]:
    """Re-rank candidates by ``rank_score * success_rate``.

    The ``rank_score`` for each candidate is derived from its position in
    the input list — earlier positions get a higher score. We pick the
    simple ``1.0 / (position + 1)`` so position 0 → 1.0, position 1 →
    0.5, position 9 → 0.1. Multiplying by smoothed ``success_rate`` (Beta
    smoothing prevents one-failure models from collapsing to 0).
    """
    models = health.get("models", {})
    ids = _candidate_ids(candidates)

    def score(idx_id: tuple[int, str]) -> float:
        idx, cid = idx_id
        rank_score = 1.0 / (idx + 1)
        entry = models.get(cid)
        sr = _h.success_rate(entry, smoothing=2) if entry else _h.success_rate({}, smoothing=2)
        return rank_score * sr

    ranked = sorted(enumerate(ids), key=score, reverse=True)
    out = [cid for _, cid in ranked]
    return out[: max(max_count, 1)]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def normalize_mode(mode: str | None) -> str:
    """Map any rotation_mode value to a registered name, falling back to
    ``static`` on unknown input. Case-insensitive."""
    if not isinstance(mode, str):
        return "static"
    norm = mode.strip().lower()
    if norm in _REGISTRY:
        return norm
    return "static"


def request_order(
    mode: str | None,
    candidates: List[Dict[str, Any]],
    health: Dict[str, Any],
    max_count: int,
) -> List[str]:
    """Top-level dispatch. Returns an ordered list of model ids per the
    requested strategy. Empty list iff no candidates are available."""
    if not candidates:
        return []
    if max_count <= 0:
        max_count = 1
    fn = _REGISTRY[normalize_mode(mode)]
    return fn(candidates, health or {"models": {}}, max_count)
