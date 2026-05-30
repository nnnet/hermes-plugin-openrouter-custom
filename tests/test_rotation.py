"""Unit tests for health.py + rotation.py.

Health storage is exercised through pure-dict mutation (no file IO) for
speed; one integration test verifies the atomic save/load round-trip.
"""

from __future__ import annotations

import datetime
import pathlib

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

import pytest  # noqa: E402

import health as h  # noqa: E402
import rotation as r  # noqa: E402


def _cand(*ids: str) -> list[dict]:
    return [{"id": i} for i in ids]


def _open_circuit_entry(probe_in_seconds: int) -> dict:
    e = h.empty_entry()
    e["circuit_state"] = h.CIRCUIT_OPEN
    e["next_probe_iso"] = (h._now() + datetime.timedelta(seconds=probe_in_seconds)).isoformat()
    e["consecutive_fail"] = 3
    return e


# ── health.py ─────────────────────────────────────────────────────────────


def test_record_success_resets_consecutive() -> None:
    data = {"models": {}}
    h.record_failure(data, "a")
    h.record_failure(data, "a")
    assert data["models"]["a"]["consecutive_fail"] == 2
    h.record_success(data, "a")
    assert data["models"]["a"]["consecutive_fail"] == 0


def test_record_failure_opens_circuit_after_threshold() -> None:
    data = {"models": {}}
    for _ in range(3):
        h.record_failure(data, "a", error_class="429")
    e = data["models"]["a"]
    assert e["circuit_state"] == h.CIRCUIT_OPEN
    assert e["backoff_seconds"] == h.BACKOFF_LADDER[0]
    assert e["next_probe_iso"] is not None
    assert e["last_error_class"] == "429"


def test_record_failure_uses_backoff_ladder() -> None:
    data = {"models": {}}
    for _ in range(6):
        h.record_failure(data, "a")
    e = data["models"]["a"]
    # After 6 failures: slot = 6 - 3 = 3 → BACKOFF_LADDER[3] = 2400
    assert e["backoff_seconds"] == h.BACKOFF_LADDER[3]


def test_success_after_open_closes_circuit() -> None:
    data = {"models": {"a": _open_circuit_entry(60)}}
    h.record_success(data, "a")
    assert data["models"]["a"]["circuit_state"] == h.CIRCUIT_CLOSED
    assert data["models"]["a"]["next_probe_iso"] is None


def test_is_blocked_only_when_open_and_cooldown_active() -> None:
    closed = h.empty_entry()
    assert not h.is_blocked(closed)
    open_future = _open_circuit_entry(60)
    assert h.is_blocked(open_future)
    open_past = _open_circuit_entry(-60)
    assert not h.is_blocked(open_past)


def test_success_rate_beta_smoothing() -> None:
    fresh = h.empty_entry()
    assert h.success_rate(fresh, smoothing=1) == 0.5
    fresh["success"] = 10
    fresh["fail"] = 0
    assert h.success_rate(fresh, smoothing=1) == 11 / 12
    fresh["success"] = 0
    fresh["fail"] = 10
    assert h.success_rate(fresh, smoothing=1) == 1 / 12


def test_health_atomic_round_trip(tmp_path: pathlib.Path) -> None:
    data = h.load_health(tmp_path)
    assert data["models"] == {}
    h.record_success(data, "qwen:free")
    h.record_failure(data, "llama:free", error_class="timeout")
    h.save_health(tmp_path, data)
    loaded = h.load_health(tmp_path)
    assert loaded["models"]["qwen:free"]["success"] == 1
    assert loaded["models"]["llama:free"]["last_error_class"] == "timeout"


# ── rotation.py ──────────────────────────────────────────────────────────


def test_normalize_mode_falls_back_to_static() -> None:
    assert r.normalize_mode("UNKNOWN") == "static"
    assert r.normalize_mode(None) == "static"
    assert r.normalize_mode("") == "static"
    assert r.normalize_mode("Failover_With_Health") == "failover_with_health"


def test_static_preserves_order() -> None:
    out = r.request_order("static", _cand("a", "b", "c"), {"models": {}}, 5)
    assert out == ["a", "b", "c"]


def test_static_caps_at_max_count() -> None:
    out = r.request_order("static", _cand("a", "b", "c"), {"models": {}}, 2)
    assert out == ["a", "b"]


def test_failover_with_health_quarantined_at_tail() -> None:
    """Cooldown ACTIVE → model is in quarantine → pushed to the END."""
    health = {"models": {"a": _open_circuit_entry(60)}}
    out = r.request_order("failover_with_health", _cand("a", "b", "c"), health, 5)
    assert out == ["b", "c", "a"]


def test_failover_with_health_probe_returns_to_ranking_slot() -> None:
    """Cooldown ELAPSED → «probe» phase → returns to its ranking slot
    (not promoted to slot 1, not stuck at tail)."""
    health = {"models": {"a": _open_circuit_entry(-60)}}
    out = r.request_order("failover_with_health", _cand("a", "b", "c"), health, 5)
    assert out == ["a", "b", "c"]


def test_circuit_breaker_same_layout_as_failover() -> None:
    """v0.7.8: circuit_breaker has the same list layout as
    failover_with_health. Difference is in health.py (exp backoff)."""
    # Active cooldown → tail
    health = {"models": {"a": _open_circuit_entry(600)}}
    out = r.request_order("circuit_breaker", _cand("a", "b", "c"), health, 5)
    assert out == ["b", "c", "a"]
    # Elapsed cooldown → probe → back to ranking slot
    health = {"models": {"b": _open_circuit_entry(-60)}}
    out = r.request_order("circuit_breaker", _cand("a", "b", "c"), health, 5)
    assert out == ["a", "b", "c"]


def test_circuit_breaker_half_open_at_ranking_slot() -> None:
    """HALF_OPEN entries are treated as «probe» — back to their natural
    ranking slot, not pinned to slot 1."""
    e = h.empty_entry()
    e["circuit_state"] = h.CIRCUIT_HALF_OPEN
    health = {"models": {"c": e}}
    out = r.request_order("circuit_breaker", _cand("a", "b", "c"), health, 5)
    assert out == ["a", "b", "c"]


def test_sticky_health_weighted_demotes_failing() -> None:
    health = {
        "models": {
            "a": {"success": 0, "fail": 100, "circuit_state": h.CIRCUIT_CLOSED},
            "b": {"success": 100, "fail": 0, "circuit_state": h.CIRCUIT_CLOSED},
        }
    }
    out = r.request_order("sticky_health_weighted", _cand("a", "b", "c"), health, 5)
    # a starts ranked first but its rate is tiny; b should rise.
    assert out[0] == "b"
    assert "a" in out


def test_request_order_handles_empty_candidates() -> None:
    assert r.request_order("static", [], {"models": {}}, 5) == []
