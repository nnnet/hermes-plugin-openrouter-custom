"""Unit tests for the pure-function selector module."""

import sys
from pathlib import Path

# Allow running tests from the plugin root.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from selector import apply_filters, rank, pick_best  # noqa: E402


def _item(
    mid: str,
    ctx: int,
    params=None,
    modality="text",
    prompt_per_token: float = 0.0,
    completion_per_token: float = 0.0,
) -> dict:
    return {
        "id": mid,
        "context_length": ctx,
        "supported_parameters": list(params) if params else [],
        "architecture": {"modality": modality},
        "pricing": {
            "prompt": str(prompt_per_token),
            "completion": str(completion_per_token),
        },
    }


def test_filter_free_only_shortcut() -> None:
    items = [
        _item("a:free", 100000, ["tools"]),
        _item("b:paid", 100000, ["tools"], prompt_per_token=1e-7),
    ]
    out = apply_filters(
        items,
        {
            "free_only": True,
            "require_tools": True,
            "min_context": 0,
            "modality": "any",
            "price": {"prompt_max": 1.0, "completion_max": 1.0},
        },
    )
    # free_only shortcut still wins over price budget.
    assert [i["id"] for i in out] == ["a:free"]


def test_filter_price_default_is_free() -> None:
    items = [
        _item("a:free", 100000, ["tools"], prompt_per_token=0.0, completion_per_token=0.0),
        _item("b:paid", 100000, ["tools"], prompt_per_token=1e-7, completion_per_token=1e-7),
    ]
    out = apply_filters(
        items, {"require_tools": True, "min_context": 0, "modality": "any"}
    )  # default price 0/0
    assert [i["id"] for i in out] == ["a:free"]


def test_filter_price_budget_accepts_paid() -> None:
    items = [
        _item("a:free", 100000, ["tools"], prompt_per_token=0.0),
        _item(
            "b:paid", 100000, ["tools"],
            prompt_per_token=0.5e-6, completion_per_token=0.8e-6,
        ),  # 0.5 / 0.8 per million
        _item(
            "c:premium", 100000, ["tools"],
            prompt_per_token=3.0e-6, completion_per_token=3.0e-6,
        ),  # too expensive
    ]
    out = apply_filters(
        items,
        {
            "require_tools": True,
            "min_context": 0,
            "modality": "any",
            "price": {"prompt_max": 1.0, "completion_max": 1.0},
        },
    )
    assert sorted(i["id"] for i in out) == ["a:free", "b:paid"]


def test_filter_context_threshold() -> None:
    items = [_item("a:free", 32000, ["tools"]), _item("b:free", 200000, ["tools"])]
    out = apply_filters(items, {"min_context": 65536, "require_tools": True, "free_only": True, "modality": "any"})
    assert [i["id"] for i in out] == ["b:free"]


def test_filter_require_tools() -> None:
    items = [_item("a:free", 100000, []), _item("b:free", 100000, ["tools"])]
    out = apply_filters(items, {"require_tools": True, "min_context": 0, "free_only": True, "modality": "any"})
    assert [i["id"] for i in out] == ["b:free"]


def test_filter_exclude_pattern() -> None:
    items = [_item("llama-3.2-3b:free", 100000, ["tools"]), _item("qwen3:free", 100000, ["tools"])]
    out = apply_filters(
        items,
        {"min_context": 0, "require_tools": True, "free_only": True, "modality": "any",
         "exclude_patterns": ["llama-3\\.2-3b"]},
    )
    assert [i["id"] for i in out] == ["qwen3:free"]


def test_rank_prefer_match() -> None:
    items = [
        _item("zzz:free", 200000, ["tools"]),
        _item("qwen3-coder:free", 100000, ["tools"]),
    ]
    out = rank(items, {"rank_by": "prefer_match", "tiebreakers": ["context_desc"]}, ["qwen3"])
    assert out[0]["id"] == "qwen3-coder:free"


def test_rank_context_desc_tiebreak() -> None:
    items = [
        _item("qwen3-a:free", 100000, ["tools"]),
        _item("qwen3-b:free", 200000, ["tools"]),
    ]
    out = rank(items, {"rank_by": "prefer_match", "tiebreakers": ["context_desc"]}, ["qwen3"])
    assert out[0]["id"] == "qwen3-b:free"


def test_pick_best_picks_top_after_filter_and_rank(monkeypatch) -> None:
    sample = [
        _item("llama-3.2-3b:free", 100000, ["tools"]),
        _item("qwen3-coder:free", 262144, ["tools", "response_format"]),
        _item("zzz:free", 200000, ["tools"], modality="text+image"),
    ]
    # Stub network
    import selector
    monkeypatch.setattr(selector, "fetch_live_catalog", lambda **_: sample)
    cfg = {
        "filters": {
            "free_only": True,
            "min_context": 65536,
            "require_tools": True,
            "modality": "any",
            "exclude_patterns": ["llama-3\\.2-3b"],
            "prefer_patterns": ["qwen3"],
        },
        "ranking": {"rank_by": "prefer_match", "tiebreakers": ["context_desc"]},
        "max_candidates": 5,
        "refresh": {"on_failure": "keep_last", "fallback_static_id": ""},
    }
    out = pick_best(cfg, api_key=None, last_state=None)
    assert out["real_model_id"] == "qwen3-coder:free"
    assert out["candidate_count"] == 2  # zzz also passes
    assert out["candidates_top"][0]["id"] == "qwen3-coder:free"


def test_pick_best_failure_keep_last(monkeypatch) -> None:
    def boom(**_):
        raise RuntimeError("network down")
    import selector
    monkeypatch.setattr(selector, "fetch_live_catalog", boom)
    last = {"real_model_id": "old:free", "candidate_count": 1, "candidates_top": []}
    out = pick_best({"refresh": {"on_failure": "keep_last"}}, last_state=last)
    assert out["real_model_id"] == "old:free"


def test_pick_best_failure_fallback_static(monkeypatch) -> None:
    def boom(**_):
        raise RuntimeError("network down")
    import selector
    monkeypatch.setattr(selector, "fetch_live_catalog", boom)
    out = pick_best(
        {"refresh": {"on_failure": "fallback_static", "fallback_static_id": "qwen3:free"}},
        last_state=None,
    )
    assert out["real_model_id"] == "qwen3:free"
