#!/usr/bin/env python3
"""Cron entry point — pulls live OR catalog, filters, ranks, persists best.

Wired to Hermes cron via a job entry created by the bootstrap script. Can
also be invoked manually:

    python3 -m openrouter_custom.refresh         # if package is on sys.path
    python3 /opt/hermes/plugins/model-providers/openrouter_custom/refresh.py

Exits 0 on success, non-zero on failure. Writes a single state.json under
HERMES_HOME/plugins/openrouter_custom/.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from pathlib import Path

# Allow direct script invocation without package install.
_self_dir = Path(__file__).parent
if str(_self_dir.parent) not in sys.path:
    sys.path.insert(0, str(_self_dir.parent))

try:
    from openrouter_custom import (
        load_config,
        load_state,
        save_state,
        state_file,
        sync_hermes_picker_models,
    )
    from openrouter_custom.selector import pick_best
except ImportError:
    # Fallback for when the plugin dir is not a package on sys.path.
    sys.path.insert(0, str(_self_dir))
    from __init__ import (  # type: ignore[no-redef]
        load_config,
        load_state,
        save_state,
        state_file,
        sync_hermes_picker_models,
    )
    from selector import pick_best  # type: ignore[no-redef]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s openrouter_custom.refresh: %(message)s",
    )
    log = logging.getLogger("openrouter_custom.refresh")

    cfg = load_config()
    if not cfg:
        log.error("plugin.yaml not readable or empty — refusing to refresh")
        return 2

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.warning("OPENROUTER_API_KEY not set — public catalog is still pullable but private models will be missing")

    last_state = load_state()
    result = pick_best(cfg, api_key=api_key or None, last_state=last_state)

    state = {
        "last_refresh_iso": _dt.datetime.now(_dt.UTC).isoformat(),
        "pseudo_alias": cfg.get("pseudo_model_alias", "or-best-free"),
        "config_snapshot": {
            "filters": cfg.get("filters", {}),
            "ranking": cfg.get("ranking", {}),
            "refresh": cfg.get("refresh", {}),
        },
        **result,
    }
    save_state(state)
    log.info(
        "wrote %s — alias=%s real=%s (%s candidates)",
        state_file(),
        state["pseudo_alias"],
        state.get("real_model_id"),
        state.get("candidate_count"),
    )

    # Keep the Hermes /model picker in sync with the fresh candidate
    # pool. Failure here must not poison the cron exit code — the
    # refresh itself succeeded.
    try:
        sync_hermes_picker_models()
    except Exception:
        log.exception("openrouter_custom: sync_hermes_picker_models failed")

    return 0 if state.get("real_model_id") else 3


if __name__ == "__main__":
    raise SystemExit(main())
