"""Tests for OpenRouterCustomProfile.build_extra_body sequential fallback.

The profile inspects two side-channels:

* ``_alias_sessions`` (class set, populated by resolve_runtime_model)
  to decide whether the call came in via the pseudo alias.
* ``internal_fallback.sequential_count`` from load_config().
* ``candidates_top`` from load_state().

Tests stub ``load_config`` / ``load_state`` so we can exercise the
decision matrix without touching disk.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# ── Stub the ``providers`` module so the plugin imports cleanly outside Hermes.
# __init__.py does:
#     from providers import register_provider
#     from providers.base import ProviderProfile
# so we hand it a minimal ProviderProfile that supports the surface our
# subclass uses (build_extra_body default, dataclass-style constructor).

if "providers" not in sys.modules:
    providers_mod = types.ModuleType("providers")
    providers_base_mod = types.ModuleType("providers.base")

    class _StubProfile:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        def build_extra_body(self, *, session_id=None, **context):
            return {}

        def resolve_runtime_model(self, model, **context):
            return model

        def prepare_messages(self, messages):
            return messages

        def fetch_models(self, **kwargs):
            return None

        def get_hostname(self):
            return ""

    providers_base_mod.ProviderProfile = _StubProfile  # type: ignore[attr-defined]
    providers_base_mod.OMIT_TEMPERATURE = object()  # type: ignore[attr-defined]
    providers_mod.register_provider = lambda profile: None  # type: ignore[attr-defined]
    providers_mod.base = providers_base_mod  # type: ignore[attr-defined]
    sys.modules["providers"] = providers_mod
    sys.modules["providers.base"] = providers_base_mod

# Make the plugin importable as a package by name.
_plugin_dir = Path(__file__).resolve().parent.parent
_parent = _plugin_dir.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

# Import via direct path because the source dir is not named after the
# package on disk (it's "openrouter_custom" only after sync, the dev
# checkout is just whatever path /tmp/claude/openrouter_custom/ resolves
# to). We import the package under its on-disk name.
_pkg_name = _plugin_dir.name  # "openrouter_custom"
import importlib  # noqa: E402

orc = importlib.import_module(_pkg_name)


@pytest.fixture(autouse=True)
def _clear_alias_sessions(monkeypatch):
    """Each test gets a fresh _alias_sessions set."""
    orc.OpenRouterCustomProfile._alias_sessions = set()
    yield


def _profile() -> "orc.OpenRouterCustomProfile":  # type: ignore[name-defined]
    return orc.OpenRouterCustomProfile(  # type: ignore[attr-defined]
        name="openrouter_custom",
        api_mode="chat_completions",
        env_vars=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1",
    )


def _stub_config(monkeypatch, sequential_count: int) -> None:
    monkeypatch.setattr(
        orc, "load_config",
        lambda: {
            "pseudo_model_alias": "best-free",
            "internal_fallback": {"sequential_count": sequential_count},
        },
    )


def _stub_state(monkeypatch, ids: list[str]) -> None:
    monkeypatch.setattr(
        orc, "load_state",
        lambda: {
            "real_model_id": ids[0] if ids else "",
            "candidates_top": [{"id": i} for i in ids],
        },
    )


# ── Decision matrix ────────────────────────────────────────────────────────


def test_sequential_count_1_returns_empty(monkeypatch):
    """M=1 — old behaviour, no models[] injection even from an alias session."""
    _stub_config(monkeypatch, sequential_count=1)
    _stub_state(monkeypatch, ["a:free", "b:free", "c:free"])
    p = _profile()
    p._mark_alias_session("sess-1")
    assert p.build_extra_body(session_id="sess-1") == {}


def test_non_alias_session_returns_empty_even_with_m3(monkeypatch):
    """User picked a concrete model from the picker — no fallback injection."""
    _stub_config(monkeypatch, sequential_count=3)
    _stub_state(monkeypatch, ["a:free", "b:free", "c:free"])
    p = _profile()
    # Note: NOT calling _mark_alias_session — this session was a direct pick.
    assert p.build_extra_body(session_id="sess-2") == {}


def test_alias_session_with_m3_returns_models_array(monkeypatch):
    """Happy path: alias session + M=3 → models[] of top-3 ids in rank order."""
    _stub_config(monkeypatch, sequential_count=3)
    _stub_state(monkeypatch, ["a:free", "b:free", "c:free", "d:free"])
    p = _profile()
    p._mark_alias_session("sess-3")
    out = p.build_extra_body(session_id="sess-3")
    assert out == {"models": ["a:free", "b:free", "c:free"]}


def test_alias_session_with_only_one_candidate_returns_empty(monkeypatch):
    """A single-candidate pool can't fall back — don't bother with models[]."""
    _stub_config(monkeypatch, sequential_count=3)
    _stub_state(monkeypatch, ["solo:free"])
    p = _profile()
    p._mark_alias_session("sess-4")
    assert p.build_extra_body(session_id="sess-4") == {}


def test_alias_session_with_m_larger_than_pool_caps_at_pool(monkeypatch):
    """M=10 with a 2-candidate pool → models[] returns just those 2."""
    _stub_config(monkeypatch, sequential_count=10)
    _stub_state(monkeypatch, ["a:free", "b:free"])
    p = _profile()
    p._mark_alias_session("sess-5")
    assert p.build_extra_body(session_id="sess-5") == {"models": ["a:free", "b:free"]}


def test_resolve_runtime_model_marks_alias_session(monkeypatch):
    """End-to-end: resolve from alias → build_extra_body sees the mark."""
    monkeypatch.setattr(
        orc, "load_config",
        lambda: {
            "pseudo_model_alias": "best-free",
            "internal_fallback": {"sequential_count": 2},
        },
    )
    _stub_state(monkeypatch, ["a:free", "b:free"])
    p = _profile()
    resolved = p.resolve_runtime_model("best-free", session_id="sess-6")
    assert resolved == "a:free"
    assert p.build_extra_body(session_id="sess-6") == {"models": ["a:free", "b:free"]}


def test_resolve_runtime_model_does_not_mark_non_alias(monkeypatch):
    """When user picks a real id directly, resolve is a no-op + no mark."""
    monkeypatch.setattr(
        orc, "load_config",
        lambda: {
            "pseudo_model_alias": "best-free",
            "internal_fallback": {"sequential_count": 2},
        },
    )
    _stub_state(monkeypatch, ["a:free", "b:free"])
    p = _profile()
    resolved = p.resolve_runtime_model("qwen3-coder:free", session_id="sess-7")
    assert resolved == "qwen3-coder:free"
    assert p.build_extra_body(session_id="sess-7") == {}
