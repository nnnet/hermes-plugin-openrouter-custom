"""openrouter_custom dashboard plugin — backend routes.

Mounted at ``/api/plugins/openrouter_custom/`` by the Hermes dashboard.

Endpoints
---------

* ``GET  /config``     — return the effective config (defaults + overrides).
* ``GET  /defaults``   — return only the bundled defaults from plugin.yaml.
* ``PUT  /config``     — replace overrides with the request body. Body is
                         the full ``config:`` block as nested JSON.
* ``POST /refresh``    — run selector immediately, rewrite state.json.
* ``GET  /state``      — return the last refresh result (state.json).

Concurrency
-----------
``PUT /config`` writes to a single config file using a tmp+rename
swap (see ``save_config``). On a multi-replica deploy the file would
need a lock; we don't have one here because the dashboard is a single
container.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Plugin package directory layout:
#   /opt/data/plugins/openrouter_custom/           ← the package
#       __init__.py                                ← module body
#       selector.py
#       dashboard/                                 ← this directory
#           plugin_api.py                          ← __file__
#
# To ``import openrouter_custom`` we need the parent of the package dir
# (``/opt/data/plugins``) on sys.path, not the package dir itself.
_pkg_parent = Path(__file__).resolve().parent.parent.parent
if str(_pkg_parent) not in sys.path:
    sys.path.insert(0, str(_pkg_parent))

import openrouter_custom as _pkg  # noqa: E402  — sys.path tweak above
from openrouter_custom.selector import pick_best  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter()


# ── helpers ────────────────────────────────────────────────────────────────

def _read_defaults() -> dict:
    """Return the immutable ``config:`` block from the bundled plugin.yaml."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        with open(_pkg.CONFIG_FILE) as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return raw.get("config") or {}


def _read_state_config() -> dict:
    """Read the mutable plugin config file as-is (raw, no defaults merge).

    Returns empty dict when the file does not exist yet (i.e. before the
    first load_config() call has bootstrapped it from plugin.yaml).
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    p = _pkg.config_file()
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _read_state() -> dict:
    p = _pkg.state_file()
    if not p.exists():
        return {}
    try:
        import json
        with open(p) as f:
            data = json.load(f) or {}
    except Exception:
        return {}
    # Augment with the rotation-strategy preview so the UI's candidate
    # pool can show position numbers without re-implementing the
    # strategies in JS. Computed live from current config + health, so
    # changes show up on the next /state poll (Save / Refresh / Probe
    # all trigger a reload).
    try:
        from openrouter_custom import health as _hlt
        from openrouter_custom import rotation as _rot
        cfg = _pkg.load_config()
        mode = cfg.get("rotation_mode") or "static"
        try:
            m = int((cfg.get("internal_fallback") or {}).get("sequential_count", 1) or 1)
        except (TypeError, ValueError):
            m = 1
        m = max(1, m)
        health = _hlt.load_health(_pkg._state_dir())
        order = _rot.request_order(mode, data.get("candidates_top") or [], health, m)
        data["next_request_order"] = order
        data["rotation_mode_effective"] = _rot.normalize_mode(mode)
        data["internal_fallback_depth_effective"] = m
    except Exception:  # never let UI break because of preview computation
        logger.exception("next_request_order computation failed")
        data["next_request_order"] = []
    return data


# ── pydantic models ────────────────────────────────────────────────────────

class _ConfigPayload(BaseModel):
    """Loose shape — the UI sends the whole ``config:`` block as JSON."""
    config: dict


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/config")
async def get_effective_config() -> dict:
    """Return the mutable config — what the plugin actually uses.

    ``defaults`` exposes the bundled plugin.yaml ``config:`` block for
    diffing in the UI; ``state_config`` is the raw on-disk file before
    the ``only_free`` shortcut expansion.
    """
    return {
        "config": _pkg.load_config(),
        "defaults": _read_defaults(),
        "state_config": _read_state_config(),
        "bundled_template": str(_pkg.CONFIG_FILE),
        "config_file": str(_pkg.config_file()),
    }


@router.put("/config")
async def put_config(body: _ConfigPayload) -> dict:
    """Replace the mutable config file with the supplied config block."""
    try:
        _pkg.save_config(body.config)
    except Exception as exc:
        logger.exception("openrouter_custom: save_config failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "config_file": str(_pkg.config_file())}


@router.post("/refresh")
async def refresh_now() -> dict:
    """Force an immediate re-pick — same logic as the cron entrypoint.

    Useful right after a UI config edit so the operator can see the new
    candidate pool without waiting for the next 30-minute cron tick.
    """
    cfg = _pkg.load_config()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or None
    last_state = _pkg.load_state()
    result = pick_best(cfg, api_key=api_key, last_state=last_state)
    import datetime as _dt
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
    _pkg.save_state(state)
    return state


@router.get("/state")
async def get_state() -> dict:
    """Return the latest state.json — chosen real id + ranked candidates."""
    return _read_state()


# ── health & probe ────────────────────────────────────────────────────────


@router.get("/health")
async def get_health() -> dict:
    """Return current ``health.json`` for the dashboard's Health table."""
    from openrouter_custom import health as _hlt
    return _hlt.load_health(_pkg._state_dir())


@router.post("/probe")
async def probe_now() -> dict:
    """Issue a 1-token ping to every top-K candidate, update health.json.

    Returns the same summary structure as the cron probe so the UI can
    display ``probed/ok/failed`` counters and per-model outcomes.
    """
    from openrouter_custom import probe as _probe
    try:
        return _probe.probe_all()
    except Exception as exc:  # surface failure to the UI without 500
        logger.exception("probe_all failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class _SingleProbePayload(BaseModel):
    model_id: str


@router.post("/probe/single")
async def probe_single(payload: _SingleProbePayload) -> dict:
    """Probe a single model — useful for the per-row UI button when an
    operator wants to test recovery of one circuit-open model without
    triggering a full pass of 10 candidates."""
    from openrouter_custom import probe as _probe
    from openrouter_custom import health as _hlt
    mid = (payload.model_id or "").strip()
    if not mid:
        raise HTTPException(status_code=400, detail="model_id required")
    try:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or None
        client = _probe._client(api_key)
        ok, actual, err = _probe._probe_one(client, mid)
        data = _hlt.load_health(_pkg._state_dir())
        if ok:
            _hlt.record_success(data, mid)
        else:
            _hlt.record_failure(data, mid, error_class=err)
        _hlt.save_health(_pkg._state_dir(), data)
        return {"ok": ok, "model_id": mid, "responded": actual, "error_class": err}
    except Exception as exc:
        logger.exception("probe_single failed for %s", mid)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class _ResetSinglePayload(BaseModel):
    model_id: str


class _HealthModePayload(BaseModel):
    mode: str  # "observe_outcome" | "observe_prob"


@router.get("/meta")
async def meta() -> dict:
    """Return plugin metadata (name, version, kind) from plugin.yaml.

    UI's version badge reads this so we have a single source of truth
    for the plugin version — bump only ``plugin.yaml`` and every
    surface updates after the next /meta poll.
    """
    try:
        import yaml as _yaml
        fp = _pkg.PLUGIN_DIR / "plugin.yaml"
        with open(fp) as f:
            data = _yaml.safe_load(f) or {}
        return {
            "name": str(data.get("name", "")),
            "version": str(data.get("version", "")),
            "kind": str(data.get("kind", "")),
        }
    except Exception as exc:
        logger.exception("meta read failed")
        return {"name": "", "version": "?", "kind": "", "error": str(exc)}


@router.get("/health-mode")
async def get_health_mode() -> dict:
    """Return the current ``health_mode`` from effective config."""
    cfg = _pkg.load_config()
    mode = str(cfg.get("health_mode") or "observe_outcome").strip().lower()
    return {"mode": mode}


@router.put("/health-mode")
async def set_health_mode(payload: _HealthModePayload) -> dict:
    """Switch between passive (observe_outcome) and active (observe_prob)
    health collection. The change is hot — next ``probe_all`` reads
    it via ``load_config``."""
    import yaml as _yaml
    mode = str(payload.mode or "").strip().lower()
    if mode not in {"observe_outcome", "observe_prob"}:
        raise HTTPException(status_code=400, detail="mode must be observe_outcome or observe_prob")
    # Use load_config() so the file is bootstrapped on first call and any
    # legacy config_overrides.yaml is migrated before we mutate.
    current = _pkg.load_config()
    ofile = _pkg.config_file()
    ofile.parent.mkdir(parents=True, exist_ok=True)
    current["health_mode"] = mode
    # Drop the now-removed probe_enabled key if it leaked into an older
    # overrides file.
    current.pop("probe_enabled", None)
    tmp = ofile.with_suffix(".yaml.tmp")
    tmp.write_text(_yaml.safe_dump(current, sort_keys=False, allow_unicode=True))
    tmp.replace(ofile)
    return {"ok": True, "mode": mode}


@router.post("/health/reset/single")
async def reset_single_health(payload: _ResetSinglePayload) -> dict:
    """Reset a single model's counters/quarantine in ``health.json`` —
    zero out success/fail/streak and close the circuit, but keep the
    row visible in the Health table (don't remove it). Useful when an
    operator wants to clear a stuck quarantine without losing the
    row's place in the UI or the history of other models."""
    from openrouter_custom import health as _hlt
    mid = (payload.model_id or "").strip()
    if not mid:
        raise HTTPException(status_code=400, detail="model_id required")
    data = _hlt.load_health(_pkg._state_dir())
    if mid in data.get("models", {}):
        data["models"][mid] = _hlt.empty_entry()
        existed = True
    else:
        existed = False
    _hlt.save_health(_pkg._state_dir(), data)
    return {"ok": True, "reset": existed, "model_id": mid}


@router.post("/health/reset")
async def reset_health() -> dict:
    """Wipe ``health.json``. Useful after changing the candidate pool or
    when an operator wants to clear past circuit-open marks."""
    from openrouter_custom import health as _hlt
    empty = {"version": 1, "models": {}}
    _hlt.save_health(_pkg._state_dir(), empty)
    return {"ok": True, "models": {}}
