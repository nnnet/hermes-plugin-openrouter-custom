"""Per-model health tracking for rotation strategies.

State shape (``health.json``)::

    {
      "version": 1,
      "models": {
        "qwen/qwen3-coder:free": {
          "success": 42,
          "fail": 3,
          "last_success_iso": "2026-05-30T01:23:45+00:00",
          "last_fail_iso": "2026-05-30T01:15:11+00:00",
          "consecutive_fail": 0,
          "circuit_state": "closed",      # closed | open | half_open
          "next_probe_iso": null,
          "backoff_seconds": 0,
          "last_error_class": ""           # short tag for UI: "429" | "timeout" | "5xx"
        }
      }
    }

Reader functions are SAFE to call concurrently. Writers must serialize
through ``save_health`` which uses an atomic tmp + rename.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile
from typing import Any, Dict

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"

# Exponential backoff sequence in seconds: 5m, 10m, 20m, 40m, 80m capped.
BACKOFF_LADDER = [300, 600, 1200, 2400, 4800]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def empty_entry() -> Dict[str, Any]:
    return {
        "success": 0,
        "fail": 0,
        "last_success_iso": None,
        "last_fail_iso": None,
        "consecutive_fail": 0,
        "circuit_state": CIRCUIT_CLOSED,
        "next_probe_iso": None,
        "backoff_seconds": 0,
        "last_error_class": "",
    }


def health_file(state_dir: pathlib.Path) -> pathlib.Path:
    return state_dir / "health.json"


def load_health(state_dir: pathlib.Path) -> Dict[str, Any]:
    """Read health.json or return empty default."""
    fp = health_file(state_dir)
    if not fp.exists():
        return {"version": 1, "models": {}}
    try:
        data = json.loads(fp.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "models": {}}
    if not isinstance(data, dict):
        return {"version": 1, "models": {}}
    data.setdefault("version", 1)
    data.setdefault("models", {})
    if not isinstance(data["models"], dict):
        data["models"] = {}
    return data


def save_health(state_dir: pathlib.Path, data: Dict[str, Any]) -> None:
    """Atomic write: tmp file + rename."""
    fp = health_file(state_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_path = tempfile.mkstemp(prefix=".health.", suffix=".tmp", dir=str(fp.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, fp)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_entry(data: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """Return mutable entry for ``model_id``, creating it if missing."""
    models = data.setdefault("models", {})
    if model_id not in models:
        models[model_id] = empty_entry()
    return models[model_id]


def record_success(
    data: Dict[str, Any],
    model_id: str,
) -> None:
    """Mutate ``data`` to record a success. Resets consecutive_fail and closes
    the circuit when transitioning from half_open."""
    e = get_entry(data, model_id)
    e["success"] = int(e.get("success", 0)) + 1
    e["last_success_iso"] = _iso(_now())
    e["consecutive_fail"] = 0
    if e.get("circuit_state") in (CIRCUIT_OPEN, CIRCUIT_HALF_OPEN):
        e["circuit_state"] = CIRCUIT_CLOSED
        e["next_probe_iso"] = None
        e["backoff_seconds"] = 0


def record_failure(
    data: Dict[str, Any],
    model_id: str,
    error_class: str = "",
    *,
    open_at_consecutive: int = 3,
) -> None:
    """Mutate ``data`` to record a failure. Opens the circuit after
    ``open_at_consecutive`` consecutive failures and schedules the next
    probe via exponential backoff."""
    e = get_entry(data, model_id)
    e["fail"] = int(e.get("fail", 0)) + 1
    e["consecutive_fail"] = int(e.get("consecutive_fail", 0)) + 1
    e["last_fail_iso"] = _iso(_now())
    if error_class:
        e["last_error_class"] = str(error_class)[:32]
    cf = e["consecutive_fail"]
    if cf >= open_at_consecutive:
        # Pick backoff slot based on how many times we've opened.
        # We approximate via cf - open_at_consecutive (0,1,2,...).
        slot = min(cf - open_at_consecutive, len(BACKOFF_LADDER) - 1)
        backoff = BACKOFF_LADDER[max(slot, 0)]
        e["circuit_state"] = CIRCUIT_OPEN
        e["next_probe_iso"] = _iso(_now() + datetime.timedelta(seconds=backoff))
        e["backoff_seconds"] = backoff


def transition_half_open_if_due(
    data: Dict[str, Any],
    model_id: str,
    now: datetime.datetime | None = None,
) -> bool:
    """If circuit is open and probe time has arrived, flip to half_open.

    Returns True iff the entry was transitioned (caller may use this to
    decide whether to issue a probe request).
    """
    e = data.get("models", {}).get(model_id)
    if not e:
        return False
    if e.get("circuit_state") != CIRCUIT_OPEN:
        return False
    next_probe = _parse_iso(e.get("next_probe_iso"))
    if next_probe is None:
        return False
    cur = now or _now()
    if cur < next_probe:
        return False
    e["circuit_state"] = CIRCUIT_HALF_OPEN
    return True


def success_rate(entry: Dict[str, Any], smoothing: int = 1) -> float:
    """Beta-smoothed success rate. ``smoothing`` adds priors so brand-new
    models don't get a 0/0 = NaN problem.

    A fresh model with smoothing=1 starts at 50% (1 prior success, 1 prior
    fail). After 10 successes it's 11/12 ≈ 0.92.
    """
    s = int(entry.get("success", 0)) + smoothing
    f = int(entry.get("fail", 0)) + smoothing
    return s / (s + f)


def is_blocked(entry: Dict[str, Any], now: datetime.datetime | None = None) -> bool:
    """Return True iff the circuit is OPEN and probe time hasn't arrived."""
    state = entry.get("circuit_state", CIRCUIT_CLOSED)
    if state != CIRCUIT_OPEN:
        return False
    next_probe = _parse_iso(entry.get("next_probe_iso"))
    if next_probe is None:
        return True
    return (now or _now()) < next_probe
