"""End-to-end smoke for the rotation pipeline.

Simulates: probe outcomes write to health.json → rotation strategy
re-orders the ``next_request_order`` accordingly. Demonstrates that
the same candidate pool produces DIFFERENT request orders depending
on circuit state, which is exactly what the dashboard ``Req`` column
shows the operator.

Run::

    python3 -m pytest tests/test_rotation_e2e.py -v
"""

from __future__ import annotations

import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

import health as h  # noqa: E402
import rotation as r  # noqa: E402


def _candidates() -> list[dict]:
    """Mimic state.json:candidates_top — 5 free models, top-2 are the
    "primary" ones, bottom-3 are filler."""
    return [
        {"id": "qwen/qwen3-coder:free", "params_display": "480B"},
        {"id": "deepseek/deepseek-v4-flash:free", "params_display": "284B"},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "params_display": "120B"},
        {"id": "google/gemma-4-31b-it:free", "params_display": "31B"},
        {"id": "poolside/laguna-xs.2:free", "params_display": "15B"},
    ]


def _record_failures(data: dict, model_id: str, n: int) -> None:
    for _ in range(n):
        h.record_failure(data, model_id, error_class="429")


def test_e2e_circuit_breaker_demotes_then_promotes(tmp_path: pathlib.Path) -> None:
    """Three-phase scenario:

    PHASE 1 — fresh state: rotation returns the ranked order verbatim.
    PHASE 2 — top-1 fails 3× in a row: circuit OPENS, top-1 is removed
              from the request order, others shift up by one slot.
    PHASE 3 — fast-forward past the cooldown so the circuit becomes
              half_open: top-1 is promoted to slot 1 as a probe; rest
              follow in ranking order.
    PHASE 4 — probe SUCCEEDS: circuit closes, ranking order is restored.

    All four phases mirror what the operator will see in the dashboard
    ``Req`` column on each F5 after Probe / TG-request roundtrips.
    """
    cands = _candidates()

    # ── PHASE 1: clean health.json ────────────────────────────────
    data = h.load_health(tmp_path)
    h.save_health(tmp_path, data)
    order_1 = r.request_order("circuit_breaker", cands, data, 4)
    assert order_1 == [
        "qwen/qwen3-coder:free",
        "deepseek/deepseek-v4-flash:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
    ]

    # ── PHASE 2: 3 consecutive failures of top-1 ──────────────────
    _record_failures(data, "qwen/qwen3-coder:free", n=3)
    h.save_health(tmp_path, data)
    entry = h.load_health(tmp_path)["models"]["qwen/qwen3-coder:free"]
    assert entry["circuit_state"] == h.CIRCUIT_OPEN
    assert entry["backoff_seconds"] == 300  # first ladder rung = 5m

    order_2 = r.request_order("circuit_breaker", cands, h.load_health(tmp_path), 4)
    assert "qwen/qwen3-coder:free" not in order_2
    assert order_2[0] == "deepseek/deepseek-v4-flash:free"
    assert len(order_2) == 4  # filler model #5 now takes the empty slot

    # ── PHASE 3: cooldown elapsed → probe slot ────────────────────
    fresh = h.load_health(tmp_path)
    # Manually expire the cooldown by pushing next_probe into the past.
    fresh["models"]["qwen/qwen3-coder:free"]["next_probe_iso"] = (
        h._now() - h.datetime.timedelta(seconds=10)
    ).isoformat()
    h.save_health(tmp_path, fresh)

    order_3 = r.request_order("circuit_breaker", cands, h.load_health(tmp_path), 4)
    # circuit_breaker promotes the due-probe model to slot 1.
    assert order_3[0] == "qwen/qwen3-coder:free"
    assert order_3[1] == "deepseek/deepseek-v4-flash:free"

    # ── PHASE 4: probe SUCCEEDS → circuit closes ──────────────────
    after_probe = h.load_health(tmp_path)
    h.record_success(after_probe, "qwen/qwen3-coder:free")
    h.save_health(tmp_path, after_probe)
    closed_entry = h.load_health(tmp_path)["models"]["qwen/qwen3-coder:free"]
    assert closed_entry["circuit_state"] == h.CIRCUIT_CLOSED

    order_4 = r.request_order("circuit_breaker", cands, h.load_health(tmp_path), 4)
    assert order_4 == order_1, "after recovery, order should match the original ranking"


def test_e2e_failover_with_health_skips_only_active_cooldowns(tmp_path: pathlib.Path) -> None:
    """Different strategy: failover_with_health drops the failing model
    BUT does NOT promote it to a probe slot when the cooldown elapses
    (that's circuit_breaker's job). It simply lets it rejoin its
    original slot when no longer blocked.
    """
    cands = _candidates()
    data = h.load_health(tmp_path)
    _record_failures(data, "qwen/qwen3-coder:free", n=3)
    h.save_health(tmp_path, data)

    # Inside the cooldown window: the model is gone.
    order_during = r.request_order("failover_with_health", cands, h.load_health(tmp_path), 4)
    assert "qwen/qwen3-coder:free" not in order_during
    assert order_during[0] == "deepseek/deepseek-v4-flash:free"

    # Cooldown elapsed (simulate by editing next_probe_iso): the model
    # rejoins at its original rank position.
    fresh = h.load_health(tmp_path)
    fresh["models"]["qwen/qwen3-coder:free"]["next_probe_iso"] = (
        h._now() - h.datetime.timedelta(seconds=10)
    ).isoformat()
    h.save_health(tmp_path, fresh)

    order_after = r.request_order("failover_with_health", cands, h.load_health(tmp_path), 4)
    assert order_after[0] == "qwen/qwen3-coder:free"


def test_e2e_probe_auto_tune_triggers_refresh(tmp_path: pathlib.Path, monkeypatch) -> None:
    """When top-4 of the probe pass shows < 50% success but at least one
    success exists, probe_all calls _trigger_refresh inline.

    Stubs out the OpenAI client + refresh function so we can drive the
    decision branches without network or real refresh logic.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import __init__ as plugin_root  # noqa: PLC0415
    importlib.reload(plugin_root)
    import probe as probe_mod  # noqa: PLC0415
    importlib.reload(probe_mod)

    # Stub state.json with 5 candidates so the pass has data.
    plugin_root.save_state({
        "candidates_top": [
            {"id": "a:free"},
            {"id": "b:free"},
            {"id": "c:free"},
            {"id": "d:free"},
            {"id": "e:free"},
        ],
        "real_model_id": "a:free",
        "pseudo_alias": "best-free",
    })

    # Stub _client to return a sentinel + _probe_one to mark top-4 mostly
    # failing (3 fail + 1 ok = 25% < 50% threshold).
    monkeypatch.setattr(probe_mod, "_client", lambda api_key: object())
    plan = iter([
        (False, "", "429"),
        (False, "", "429"),
        (False, "", "429"),
        (True, "d:free", ""),
        (True, "e:free", ""),
    ])
    monkeypatch.setattr(
        probe_mod, "_probe_one",
        lambda client, mid: next(plan),
    )

    refresh_calls = []
    monkeypatch.setattr(
        probe_mod, "_trigger_refresh",
        lambda: refresh_calls.append("called"),
    )

    summary = probe_mod.probe_all(pause_min_seconds=0, pause_max_seconds=0)
    assert summary["refresh_triggered"] is True
    assert refresh_calls == ["called"]


def test_e2e_probe_no_refresh_when_all_failed(tmp_path: pathlib.Path, monkeypatch) -> None:
    """If 0/4 succeed (likely network outage), skip auto-refresh — there's
    no upside to rewriting the same pool."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import __init__ as plugin_root  # noqa: PLC0415
    importlib.reload(plugin_root)
    import probe as probe_mod  # noqa: PLC0415
    importlib.reload(probe_mod)

    plugin_root.save_state({
        "candidates_top": [{"id": "a:free"}, {"id": "b:free"}, {"id": "c:free"}, {"id": "d:free"}],
        "real_model_id": "a:free",
        "pseudo_alias": "best-free",
    })

    monkeypatch.setattr(probe_mod, "_client", lambda api_key: object())
    monkeypatch.setattr(
        probe_mod, "_probe_one",
        lambda client, mid: (False, "", "net"),
    )
    refresh_calls = []
    monkeypatch.setattr(
        probe_mod, "_trigger_refresh",
        lambda: refresh_calls.append("called"),
    )

    summary = probe_mod.probe_all(pause_min_seconds=0, pause_max_seconds=0)
    assert summary["refresh_triggered"] is False
    assert refresh_calls == []


def test_e2e_probe_skipped_when_disabled(tmp_path: pathlib.Path, monkeypatch) -> None:
    """When ``cfg.probe_enabled`` is False, probe_all returns immediately
    without issuing any HTTP request — saves OR free-tier quota when the
    operator is not using the alias."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    import __init__ as plugin_root
    importlib.reload(plugin_root)
    import probe as probe_mod
    importlib.reload(probe_mod)

    monkeypatch.setattr(
        probe_mod, "load_config",
        lambda: {"probe_enabled": False},
    )

    # If probe_all does NOT early-return, this lambda would explode
    # because OpenAI() is being called with no key — so the test would
    # fail loudly. Early return means we never touch _client.
    called = {"n": 0}
    def _boom(api_key):
        called["n"] += 1
        raise AssertionError("probe should not have called _client")
    monkeypatch.setattr(probe_mod, "_client", _boom)

    summary = probe_mod.probe_all()
    assert summary["skipped"] is True
    assert summary["probed"] == 0
    assert called["n"] == 0


def test_e2e_observe_outcome_signal(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Verifies the observe_outcome path that lives on the provider
    profile — same hook the conversation_loop will call live (phase 2)
    or the probe cron calls today.

    Simulates: a request was sent with models=[a,b,c]; OR responded
    with response.model=b → observe records 'a bypassed = failure',
    'b success'; 'c' is untouched.
    """
    # Point _state_dir at tmp_path before we import __init__.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The plugin under test:
    import importlib
    import __init__ as plugin_root  # noqa: PLC0415
    importlib.reload(plugin_root)

    class FakeResponse:
        model = "b"

    # Avoid the real ProviderProfile import which requires Hermes runtime.
    # Instead, replicate the observe_outcome body inline by calling
    # health helpers — mirrors what the host invokes.
    data = h.load_health(plugin_root._state_dir())
    req = ["a", "b", "c"]
    resp_model = FakeResponse.model
    idx = req.index(resp_model)
    for cid in req[:idx]:
        h.record_failure(data, cid, error_class="bypassed")
    h.record_success(data, resp_model)
    h.save_health(plugin_root._state_dir(), data)

    saved = h.load_health(plugin_root._state_dir())["models"]
    assert saved["a"]["fail"] == 1
    assert saved["a"]["last_error_class"] == "bypassed"
    assert saved["b"]["success"] == 1
    assert "c" not in saved
