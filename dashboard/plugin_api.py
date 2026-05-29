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
``PUT /config`` writes to a single overrides file using a tmp+rename
swap (see ``save_overrides``). On a multi-replica deploy the file would
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


def _read_overrides() -> dict:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    p = _pkg.overrides_file()
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
            return json.load(f) or {}
    except Exception:
        return {}


# ── pydantic models ────────────────────────────────────────────────────────

class _ConfigPayload(BaseModel):
    """Loose shape — the UI sends the whole ``config:`` block as JSON."""
    config: dict


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/config")
async def get_effective_config() -> dict:
    """Return defaults merged with overrides — what the plugin actually uses."""
    return {
        "config": _pkg.load_config(),
        "defaults": _read_defaults(),
        "overrides": _read_overrides(),
        "config_file": str(_pkg.CONFIG_FILE),
        "overrides_file": str(_pkg.overrides_file()),
    }


@router.put("/config")
async def put_overrides(body: _ConfigPayload) -> dict:
    """Replace the overrides file with the supplied config block."""
    try:
        _pkg.save_overrides(body.config)
    except Exception as exc:
        logger.exception("openrouter_custom: save_overrides failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "overrides_file": str(_pkg.overrides_file())}


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
