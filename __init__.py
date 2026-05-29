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


def overrides_file() -> Path:
    """Mutable config overrides file written by the dashboard plugin UI.

    Lives in HERMES_HOME (rw) because the plugin source directory is
    bind-mounted read-only inside the container. Anything written here is
    merged on top of the bundled ``plugin.yaml`` ``config:`` block on
    every load_config() call.
    """
    return _state_dir() / "config_overrides.yaml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into a shallow copy of ``base``.

    Dict-valued keys merge; scalars / lists overwrite. Used to layer the
    dashboard-edited overrides on top of the bundled defaults.
    """
    if not isinstance(overlay, dict):
        return base
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """Read defaults from plugin.yaml, merge mutable overrides on top.

    Called fresh on every cron tick and every session start so changes
    written via the dashboard UI (or directly to the overrides file)
    apply at the next tick without a gateway restart.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover — Hermes always has pyyaml
        logger.warning("openrouter_custom: PyYAML missing, using empty config")
        return {}

    defaults: dict = {}
    try:
        with open(CONFIG_FILE) as f:
            raw = yaml.safe_load(f) or {}
        defaults = raw.get("config") or {}
    except FileNotFoundError:
        logger.warning("openrouter_custom: plugin.yaml not found at %s", CONFIG_FILE)
    except Exception as exc:
        logger.warning("openrouter_custom: failed to parse plugin.yaml: %s", exc)

    overrides: dict = {}
    overlay_path = overrides_file()
    if overlay_path.exists():
        try:
            with open(overlay_path) as f:
                overrides = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning(
                "openrouter_custom: failed to parse overrides at %s: %s",
                overlay_path, exc,
            )

    return _deep_merge(defaults, overrides)


def save_overrides(new_config: dict) -> None:
    """Persist UI-edited config to the mutable overrides file.

    Only the keys present in ``new_config`` are written — load_config()
    will merge them on top of the bundled defaults at next read.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML required to save overrides") from exc

    target = overrides_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(new_config, f, sort_keys=False, allow_unicode=True)
    tmp.replace(target)


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

        def resolve_runtime_model(self, model: str, **_context: object) -> str:  # type: ignore[override]
            """Swap the pseudo alias for the current real id from state.json.

            Called by conversation_loop on every new session. When the
            agent's pinned model is the pseudo alias, look up the
            cron-refreshed best candidate and substitute.  When the
            operator pinned a specific real id from the picker pool,
            we return it unchanged.
            """
            cfg = load_config()
            alias = (cfg.get("pseudo_model_alias") or "best-free").strip()
            if model != alias:
                return model
            state = load_state()
            real = (state.get("real_model_id") or "").strip()
            if not real:
                logger.warning(
                    "openrouter_custom: alias %r requested but state.json has no real_model_id — "
                    "the request will hit OpenRouter with the alias and fail. Run refresh.py.",
                    alias,
                )
                return model
            return real

        def fetch_models(  # type: ignore[override]
            self,
            *,
            api_key: str | None = None,
            timeout: float = 8.0,
        ) -> list[str] | None:
            """Surface the pseudo alias and every candidate that passed the filter.

            The picker calls this on every ``/model`` open.  We respond with
            ``[alias, *current_candidate_ids]`` from state.json so the operator
            can either ride the auto-picked alias (rotates per cron tick) or
            pin a specific id from the current pool.  Empty state.json falls
            back to just the alias — refresh.py has not run yet.
            """
            cfg = load_config()
            alias = (cfg.get("pseudo_model_alias") or "best-free").strip()
            state = load_state()
            ids = [
                str(c.get("id", "")).strip()
                for c in (state.get("candidates_top") or [])
                if c.get("id")
            ]
            result: list[str] = []
            if alias:
                result.append(alias)
            for mid in ids:
                if mid and mid != alias and mid not in result:
                    result.append(mid)
            return result or None

    _cfg_for_profile = load_config()
    _pseudo_alias = (_cfg_for_profile.get("pseudo_model_alias") or "best-free").strip()

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


# ── Plugin entry point ─────────────────────────────────────────────────────

def register(ctx: Any) -> None:  # pragma: no cover — exercised by Hermes
    """Hermes plugin entry point.

    All routing logic lives on the ProviderProfile subclass declared
    above (see ``resolve_runtime_model`` and ``fetch_models``).  The
    module body already calls ``register_provider`` at import time, so
    this function is intentionally a no-op — it exists only because the
    Hermes plugin manager requires every plugin to expose ``register``.
    """
    return None
