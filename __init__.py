"""openrouter_custom — OpenRouter live-filter plugin with pseudo-model masking.

Two extension points wire this into Hermes:

1. ProviderProfile ``openrouter_custom`` — registers in providers._REGISTRY so
   /model picker recognises ``--provider openrouter_custom``.  ``fetch_models``
   is overridden to return only the single pseudo alias name (the user picks
   that one stable handle; real-id resolution happens later).

2. on_session_start hook — when a new agent session starts with
   ``provider=openrouter_custom`` and ``model=<pseudo_alias>``, the hook
   reads the current state.json (written by the cron refresh script) and
   substitutes ``agent.model = real_id`` before any LLM call goes out.

State file layout (managed by refresh.py):

    {
      "real_model_id": "qwen/qwen3-coder:free",
      "candidate_count": 6,
      "candidates_top": [...],
      "last_refresh_iso": "2026-05-29T18:00:00Z",
      "reason": "ranked 6 candidates by prefer_match"
    }

Config is read from plugin.yaml at the plugin dir.  The refresh cron job
and the on_session_start hook both reload the YAML on every call, so
operators can edit it (or a web UI can POST to it) without restarting
the gateway.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
CONFIG_FILE = PLUGIN_DIR / "plugin.yaml"


def _state_dir() -> Path:
    """Resolve state directory inside HERMES_HOME (defaults to /opt/data)."""
    base = os.environ.get("HERMES_HOME", "/opt/data")
    return Path(base) / "plugins" / "openrouter_custom"


def state_file() -> Path:
    return _state_dir() / "state.json"


def load_config() -> dict:
    """Re-read plugin.yaml on every call so live edits apply at next tick.

    Returns the ``config:`` section verbatim. Missing keys are filled by
    the consumers (selector module honours defaults internally).
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover — Hermes always has pyyaml
        logger.warning("openrouter_custom: PyYAML missing, using empty config")
        return {}
    try:
        with open(CONFIG_FILE) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("openrouter_custom: plugin.yaml not found at %s", CONFIG_FILE)
        return {}
    except Exception as exc:
        logger.warning("openrouter_custom: failed to parse plugin.yaml: %s", exc)
        return {}
    return raw.get("config") or {}


def load_state() -> dict:
    try:
        with open(state_file()) as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("openrouter_custom: failed to load state.json: %s", exc)
        return {}


def save_state(state: dict) -> None:
    sf = state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(sf)


# ── ProviderProfile ────────────────────────────────────────────────────────

try:
    from providers import register_provider  # type: ignore[import-not-found]
    from providers.base import ProviderProfile  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only outside Hermes
    register_provider = None  # type: ignore[assignment]
    ProviderProfile = None  # type: ignore[assignment,misc]


if ProviderProfile is not None:

    class OpenRouterCustomProfile(ProviderProfile):  # type: ignore[misc]
        """OpenRouter aggregator behind a single pseudo-model handle."""

        def fetch_models(  # type: ignore[override]
            self,
            *,
            api_key: str | None = None,
            timeout: float = 8.0,
        ) -> list[str] | None:
            """Return only the pseudo alias — never the OR catalog.

            The picker uses this for its model list.  We surface a single
            stable name so the operator picks ``or-best-free`` once and
            never has to re-pick when the underlying real model id rotates.
            """
            cfg = load_config()
            alias = (cfg.get("pseudo_model_alias") or "or-best-free").strip()
            return [alias] if alias else None

    _cfg_for_profile = load_config()
    _pseudo_alias = (_cfg_for_profile.get("pseudo_model_alias") or "or-best-free").strip()

    openrouter_custom = OpenRouterCustomProfile(
        name="openrouter_custom",
        display_name="OpenRouter Custom",
        description=(
            "OpenRouter free-tier with live filter (context/tools/modality) "
            "behind a stable pseudo-name; refreshed by cron."
        ),
        aliases=("or-custom",),
        api_mode="chat_completions",
        env_vars=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1",
        models_url="https://openrouter.ai/api/v1/models",
        auth_type="api_key",
        default_aux_model=_pseudo_alias,
        fallback_models=(_pseudo_alias,),
    )

    if register_provider is not None:
        register_provider(openrouter_custom)
        logger.info(
            "openrouter_custom: registered provider with pseudo alias %r",
            _pseudo_alias,
        )


# ── on_session_start hook ──────────────────────────────────────────────────

def _on_session_start(agent: Any = None, **_kwargs: Any) -> None:
    """If agent points at openrouter_custom + pseudo alias, swap to real id.

    Called by Hermes plugin manager for every new session.  No-op when the
    state file is missing or the cron has not yet picked a model.
    """
    if agent is None:
        return
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    if provider != "openrouter_custom":
        return
    cfg = load_config()
    alias = (cfg.get("pseudo_model_alias") or "or-best-free").strip()
    current_model = (getattr(agent, "model", "") or "").strip()
    if current_model != alias:
        return
    state = load_state()
    real_id = (state.get("real_model_id") or "").strip()
    if not real_id:
        logger.warning(
            "openrouter_custom: pseudo alias %r in use but state has no real_model_id; "
            "session will hit OpenRouter with the alias and likely fail. "
            "Run scripts/refresh.py to populate state.",
            alias,
        )
        return
    agent.model = real_id
    # Stamp the pseudo on the agent so any UI that wants to display the
    # logical name (instead of the real rotating id) can read this back.
    setattr(agent, "_openrouter_custom_pseudo", alias)
    logger.info(
        "openrouter_custom: session %s — pseudo %r → real %r",
        getattr(agent, "session_id", "?"),
        alias,
        real_id,
    )


# Plugin manager auto-discovers ``register(ctx)`` for hook-style plugins; for
# model-provider plugins it auto-runs the module body.  We register the hook
# via the module-scope helper Hermes plugin manager exposes when available.

def register(ctx: Any) -> None:  # pragma: no cover — exercised by Hermes
    """Hermes plugin entry point — registers the session-start hook."""
    try:
        ctx.register_hook("on_session_start", _on_session_start)
    except Exception as exc:
        logger.warning("openrouter_custom: failed to register hook: %s", exc)
