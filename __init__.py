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
    """Resolve mutable state directory inside HERMES_HOME (defaults to /opt/data).

    Lives under ``state/`` (not ``plugins/``) so the dashboard plugin
    scanner — which walks ``HERMES_HOME/plugins/<child>/dashboard/`` —
    does not collide with our state.json. The plugin's own files are
    symlinked under ``plugins/openrouter_custom`` by the host-side
    sync-external-plugins.sh; this state path stays isolated.
    """
    base = os.environ.get("HERMES_HOME", "/opt/data")
    return Path(base) / "state" / "openrouter_custom"


def state_file() -> Path:
    return _state_dir() / "state.json"


def alias_sessions_file() -> Path:
    """Where ``_alias_sessions`` is persisted so the marker survives
    gateway restarts.  Capped at 500 entries (most recent kept) to
    bound file growth — a session_id stays in memory ~1 day in normal
    Hermes use, more than long enough for a hundred turns of context."""
    return _state_dir() / "alias_sessions.json"


def load_alias_sessions() -> set[str]:
    """Read the persisted alias-session set. Returns empty set on any
    error or missing file."""
    fp = alias_sessions_file()
    if not fp.exists():
        return set()
    try:
        data = json.loads(fp.read_text())
        if isinstance(data, list):
            return {str(x) for x in data if x}
    except (OSError, json.JSONDecodeError):
        pass
    return set()


DEFAULT_ALIAS_SESSIONS_MAX = 500


def save_alias_sessions(sessions: set[str]) -> None:
    """Atomic write of ``_alias_sessions`` to disk. Capped at the last
    ``alias_sessions_max`` entries from config (default 500); the cap
    bounds disk growth — operators raise it for high-traffic deployments,
    lower it for embedded ones.

    Best-effort: any IO failure (including parent mkdir failure on
    read-only filesystems used in unit tests) is swallowed because the
    in-memory set still works for the current process.
    """
    try:
        cap = DEFAULT_ALIAS_SESSIONS_MAX
        try:
            cap_cfg = load_config().get("alias_sessions_max")
            if cap_cfg is not None:
                cap = max(int(cap_cfg), 1)
        except Exception:
            pass
        fp = alias_sessions_file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        capped = list(sessions)[-cap:]
        tmp = fp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(capped, ensure_ascii=False))
        tmp.replace(fp)
    except OSError:
        pass


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

    Post-merge, the ``only_free`` shortcut is expanded into the granular
    price filter: ``only_free: true`` forces both ``filters.price.prompt_max``
    and ``filters.price.completion_max`` to ``0`` so refresh.py picks only
    zero-cost OpenRouter models. ``only_free: false`` is a no-op (operator
    keeps whatever explicit price caps they set). Default is ``true`` so a
    fresh install picks free-tier models without extra config.
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

    merged = _deep_merge(defaults, overrides)

    # ``only_free`` shortcut — default True, can be turned off explicitly.
    only_free = merged.get("only_free")
    if only_free is None:
        only_free = True
    if only_free:
        price = merged.setdefault("filters", {}).setdefault("price", {})
        price["prompt_max"] = 0
        price["completion_max"] = 0

    return merged


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

        # Set of session_ids whose ``resolve_runtime_model`` matched the
        # pseudo alias.  ``build_extra_body`` reads this to decide whether
        # to inject the OR-native ``models: [...]`` sequential-fallback
        # array.  Persisted to disk so the marker survives gateway
        # restarts (without persistence, the first TG turn after restart
        # falls through to no-rotation because the in-memory set is
        # empty).
        _alias_sessions: set[str] | None = None  # lazy-loaded

        @classmethod
        def _ensure_loaded(cls) -> set[str]:
            if cls._alias_sessions is None:
                cls._alias_sessions = load_alias_sessions()
            return cls._alias_sessions

        @classmethod
        def _mark_alias_session(cls, session_id: object) -> None:
            sid = str(session_id or "")
            if not sid:
                return
            sessions = cls._ensure_loaded()
            if sid in sessions:
                return  # avoid spurious disk write on repeat hits
            sessions.add(sid)
            save_alias_sessions(sessions)

        @classmethod
        def _unmark_alias_session(cls, session_id: object) -> None:
            """Remove a session_id from the alias set.

            Called from ``resolve_runtime_model`` when the agent's model
            is NOT the pseudo alias — covers the case where the operator
            ran ``/model <concrete-id>`` mid-session to opt out of
            rotation. Without this, the marker from a prior alias turn
            would persist and ``build_extra_body`` would still inject
            ``models[...]`` even though the operator explicitly pinned
            a single model.
            """
            sid = str(session_id or "")
            if not sid:
                return
            sessions = cls._ensure_loaded()
            if sid not in sessions:
                return
            sessions.discard(sid)
            save_alias_sessions(sessions)

        @classmethod
        def _is_alias_session(cls, session_id: object) -> bool:
            sid = str(session_id or "")
            return bool(sid) and sid in cls._ensure_loaded()

        def resolve_runtime_model(self, model: str, **_context: object) -> str:  # type: ignore[override]
            """Swap the pseudo alias for the current real id from state.json.

            Called by conversation_loop on every new session and on every
            turn (Hermes builds a fresh AIAgent per inbound message).
            When the agent's pinned model is the pseudo alias, look up
            the cron-refreshed best candidate and substitute.  When the
            operator pinned a specific real id from the picker pool,
            we return it unchanged AND remove any stale alias-marker
            that a prior turn of the same session might have set.

            Side effects:

            * Alias match → ``_mark_alias_session(session_id)``. Later
              ``build_extra_body`` reads this to decide whether to inject
              the ``models: [...]`` rotation array.
            * Non-alias model → ``_unmark_alias_session(session_id)``.
              Critical when the operator runs ``/model <concrete-id>``
              mid-session to opt out of rotation; without this, the
              marker from the prior alias turn would persist and
              rotation would keep firing against the operator's intent.
            """
            cfg = load_config()
            alias = (cfg.get("pseudo_model_alias") or "best-free").strip()
            if model != alias:
                # Operator pinned a concrete model — drop any stale
                # marker so rotation deactivates for the rest of this
                # session.
                self._unmark_alias_session(_context.get("session_id"))
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
            # Remember this session resolved from the alias so
            # build_extra_body can opt into sequential fallback below.
            self._mark_alias_session(_context.get("session_id"))
            return real

        # ── Mid-session pseudo-alias substitution ─────────────────────────
        # Hermes calls ``build_api_kwargs_extras`` from the chat-completions
        # transport on EVERY outbound request (see
        # /opt/hermes/agent/transports/chat_completions.py around line 496).
        # The returned ``top_level_kwargs`` dict is merged into ``api_kwargs``
        # via ``api_kwargs.update(top_level_from_profile)`` — meaning a
        # ``{"model": ...}`` entry overrides whatever ``agent.model`` resolved
        # to. We exploit that to swap the pseudo alias for the live real id
        # without touching any other provider's request flow.
        #
        # ``resolve_runtime_model`` (above) has been a no-op in current
        # Hermes for a while because no caller invokes it. Doing the
        # substitution here keeps the swap inside the documented profile
        # contract — no monkey-patching of openai SDK, no override of
        # Hermes core.
        def build_api_kwargs_extras(  # type: ignore[override]
            self,
            *,
            reasoning_config: object = None,
            **context: object,
        ) -> tuple[dict[str, object], dict[str, object]]:
            model = context.get("model")
            session_id = context.get("session_id")
            top_level: dict[str, object] = {}
            if isinstance(model, str):
                cfg = load_config()
                alias = (cfg.get("pseudo_model_alias") or "best-free").strip()
                if model == alias:
                    state = load_state()
                    real = str(state.get("real_model_id") or "").strip()
                    if real:
                        # Bifrost OpenAI-shape passthrough only routes to
                        # the openrouter upstream when the model carries
                        # the ``openrouter/`` provider prefix.
                        if not real.startswith("openrouter/"):
                            real = f"openrouter/{real}"
                        top_level["model"] = real
                        # Mark the session so build_extra_body below can
                        # opt into rotation when configured.
                        self._mark_alias_session(session_id)
                        logger.info(
                            "openrouter_custom: alias %r -> %r (transport "
                            "build_api_kwargs_extras)", alias, real,
                        )
                    else:
                        logger.warning(
                            "openrouter_custom: alias %r requested but "
                            "state.json has no real_model_id — request "
                            "will fail upstream; run refresh.py",
                            alias,
                        )
                else:
                    # Operator picked a concrete id — clear any stale
                    # alias-session marker so build_extra_body's rotation
                    # stays off for the remainder of the session.
                    self._unmark_alias_session(session_id)
            return {}, top_level

        def build_extra_body(  # type: ignore[override]
            self, *, session_id: str | None = None, **context: object
        ) -> dict[str, object]:
            """Inject OR-native ``models: [...]`` for alias-resolved sessions.

            Only fires when both conditions hold:

            1. The session's model was resolved from the pseudo alias
               (recorded by ``resolve_runtime_model`` above).
            2. ``config.internal_fallback.sequential_count > 1``.

            We pull the top-M candidate ids from state.json and pass them
            as the request body's ``models`` field. OpenRouter walks the
            list server-side and only returns a hard failure when every
            candidate refuses — at which point Hermes's external
            fallback chain (claude-haiku-4-5, etc.) takes over.

            When the operator picked a concrete real id from the
            ``/model`` picker instead of riding the alias, we return an
            empty dict and the request goes through as a single-model
            call.
            """
            # Pure alias-marker semantics: rotation activates ONLY when
            # the session was resolved from the pseudo alias (set by
            # resolve_runtime_model). When the operator pinned a
            # concrete model via /model, no marker is set and rotation
            # stays off — the operator's explicit pick is respected and
            # OR is not asked to fall through other ids.
            if not self._is_alias_session(session_id):
                return {}
            cfg = load_config()
            internal = cfg.get("internal_fallback") or {}
            try:
                m = int(internal.get("sequential_count", 1) or 1)
            except (TypeError, ValueError):
                m = 1
            if m <= 1:
                return {}
            state = load_state()
            candidates = state.get("candidates_top") or []

            # Rotation: consult the configured strategy + current health.
            try:
                from . import rotation as _rot  # type: ignore[import-not-found]
                from . import health as _hlt
            except ImportError:  # pragma: no cover — flat import path
                import rotation as _rot  # type: ignore[no-redef,import-not-found]
                import health as _hlt  # type: ignore[no-redef]
            mode = (cfg.get("rotation_mode") or "static")
            health_data = _hlt.load_health(_state_dir())
            ids = _rot.request_order(mode, candidates, health_data, m)
            if not ids:
                # Nothing in the pool survived the rotation — let the
                # bare agent.model go to OR as-is; external fallback
                # chain will take over if it fails.
                return {}
            # Inject even a single-element list: when rotation has only
            # one live candidate but it differs from agent.model (the
            # alias's frozen ranking pick), OR will route to the
            # rotation pick instead of the dead-circuit one.
            return {"models": ids}

        # ── Outcome observation (phase 2 — wired by host hook later) ─────
        def observe_outcome(
            self,
            *,
            response: object = None,
            error: object = None,
            request_models: list[str] | None = None,
            **_ctx: object,
        ) -> None:
            """Record success/failure for rotation health tracking.

            Phase 1 (current): called only by the probe cron via
            ``record_outcome`` helper. The host conversation loop does
            NOT call this hook yet — adding that call site is a small
            patch to ``chat_completion_helpers.py`` in the hermes-agent
            fork, planned as a follow-up.

            ``response.model`` (OpenRouter echoes the actual model that
            served the request) is used to attribute success. Any models
            that were listed BEFORE the responder in ``request_models``
            are recorded as having implicitly failed — that's the OR
            sequential-fallback signal we promised the user.
            """
            try:
                from . import health as _hlt
            except ImportError:
                import health as _hlt  # type: ignore[no-redef]
            data = _hlt.load_health(_state_dir())
            req = list(request_models or [])

            # Quarantine knobs from config — fall back to module
            # defaults when missing so existing deployments keep working.
            cfg = load_config()
            qthr = cfg.get("quarantine_consecutive_fail_threshold")
            backoff_minutes = cfg.get("quarantine_backoff_minutes")
            backoff_ladder = (
                [int(m) * 60 for m in backoff_minutes]
                if isinstance(backoff_minutes, list) and backoff_minutes
                else None
            )

            def _fail(cid: str, klass: str) -> None:
                _hlt.record_failure(
                    data, cid, error_class=klass,
                    open_at_consecutive=int(qthr) if qthr is not None else None,
                    backoff_ladder=backoff_ladder,
                )

            if error is not None:
                # Top-level error → every requested model is considered
                # failed for this turn (OR couldn't satisfy any).
                tag = type(error).__name__[:24]
                for cid in req:
                    _fail(cid, tag)
                _hlt.save_health(_state_dir(), data)
                return

            actual = ""
            if response is not None:
                actual = str(getattr(response, "model", "") or "").strip()
            if not actual:
                # No model echo — nothing we can attribute.
                return
            if req and actual in req:
                idx = req.index(actual)
                for cid in req[:idx]:
                    _fail(cid, "bypassed")
                _hlt.record_success(data, actual)
            elif req:
                # Responder not in requested list (e.g. OR auto-route).
                _hlt.record_success(data, actual)
            else:
                _hlt.record_success(data, actual)
            _hlt.save_health(_state_dir(), data)

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

    # Allow the operator to redirect traffic — both inference and the
    # catalog refresh — through a local Bifrost gateway by overriding
    # base_url / models_url in config_overrides.yaml. Default keeps the
    # direct OpenRouter endpoint.
    _base_url = (
        _cfg_for_profile.get("base_url")
        or "https://openrouter.ai/api/v1"
    ).strip()
    _models_url = (
        _cfg_for_profile.get("models_url")
        or f"{_base_url.rstrip('/')}/models"
    ).strip()

    openrouter_custom = OpenRouterCustomProfile(
        name="openrouter_custom",
        display_name="OpenRouter Custom",
        description=(
            "OpenRouter free-tier with live filter (context/tools/modality) "
            "behind a stable pseudo-name; refreshed by cron."
        ),
        aliases=("or-custom",),
        api_mode="chat_completions",
        # No env-var auth required: when traffic is routed through the
        # local Bifrost gateway, Bifrost itself injects the upstream
        # key. The default direct mode also tolerates a missing key for
        # the free-tier endpoint, so listing the env var here would
        # only mark the provider 'unauthenticated' in the picker.
        env_vars=(),
        base_url=_base_url,
        models_url=_models_url,
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
    above. Pseudo-alias substitution is wired through
    ``build_api_kwargs_extras`` — Hermes' chat-completions transport
    merges the returned ``top_level`` dict into ``api_kwargs`` on every
    outbound request, so returning ``{"model": "<real id>"}`` swaps the
    alias without touching Hermes core or the openai SDK.
    """
    return None
