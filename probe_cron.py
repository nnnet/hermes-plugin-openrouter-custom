#!/usr/bin/env python3
"""Cron entry point — run a health probe pass against every top-K
candidate and persist outcomes in ``health.json``.

Wired to Hermes cron as job ``openrouter-custom-probe`` (see
``infra/hermes/.hermes/scripts/openrouter-custom-probe.sh``). Default
schedule is 10 minutes — adjust via the cron registration script if you
want faster recovery from a circuit-open state.

Manual invocation::

    python3 -m openrouter_custom.probe_cron
    python3 /opt/hermes/plugins/model-providers/openrouter_custom/probe_cron.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_self_dir = Path(__file__).parent
if str(_self_dir.parent) not in sys.path:
    sys.path.insert(0, str(_self_dir.parent))

try:
    from openrouter_custom import probe as _probe
except ImportError:
    sys.path.insert(0, str(_self_dir))
    import probe as _probe  # type: ignore[no-redef]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s openrouter_custom.probe: %(message)s",
    )
    log = logging.getLogger("openrouter_custom.probe")

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.warning(
            "OPENROUTER_API_KEY not set — public-only models can still be probed "
            "but private/gated entries will appear as failures"
        )
    summary = _probe.probe_all(api_key=api_key or None)
    log.info(
        "probed=%d ok=%d failed=%d",
        summary.get("probed", 0),
        summary.get("ok", 0),
        summary.get("failed", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
