"""Unit tests for the pure-function selector module."""

import sys
from pathlib import Path

# Allow running tests from the plugin root.
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from selector import apply_filters, rank, pick_best, _extract_params  # noqa: E402


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


def test_strict_free_excludes_zero_priced_non_free_wildcards() -> None:
    """price 0/0 must also require the ``:free`` suffix.

    OpenRouter reports ``openrouter/auto`` and similar platform wildcards
    with a literal "0" prompt/completion price. Without strict_free
    those leak into the free-tier alias and bill at underlying paid
    model rates.
    """
    items = [
        _item("qwen3-coder:free", 200000, ["tools"], prompt_per_token=0.0, completion_per_token=0.0),
        _item("openrouter/auto", 2000000, ["tools"], prompt_per_token=0.0, completion_per_token=0.0),
        _item("foo/bar:beta", 100000, ["tools"], prompt_per_token=0.0, completion_per_token=0.0),
    ]
    out = apply_filters(
        items,
        {"require_tools": True, "min_context": 0, "modality": "any",
         "price": {"prompt_max": 0, "completion_max": 0}},
    )
    # Strict-free dropped the two non-:free zero-priced wildcards.
    assert [i["id"] for i in out] == ["qwen3-coder:free"]


def test_negative_pricing_excluded_under_any_finite_budget() -> None:
    """OR uses ``"-1"`` as a "dynamic" pricing sentinel for platform
    wildcards (openrouter/auto routes to underlying paid models). It
    must NOT pass as "cheaper than zero" — that would silently leak
    paid traffic into a free-tier alias."""
    items = [
        _item("openrouter/auto", 2000000, ["tools"],
              prompt_per_token=-1.0, completion_per_token=-1.0),
        _item("qwen3-coder:free", 200000, ["tools"]),
    ]
    # finite budget (paid mode) — auto must still be excluded.
    out = apply_filters(
        items,
        {"require_tools": True, "min_context": 0, "modality": "any",
         "price": {"prompt_max": 5.0, "completion_max": 5.0}},
    )
    assert "openrouter/auto" not in [i["id"] for i in out]
    assert "qwen3-coder:free" in [i["id"] for i in out]


def test_strict_free_disengages_at_nonzero_budget() -> None:
    """When the operator allows any non-zero budget, the strict :free
    rule must NOT engage — otherwise paid id pools would silently shed
    every non-:free candidate that fits the budget."""
    items = [
        _item("qwen3-coder:free", 200000, ["tools"], prompt_per_token=0.0),
        _item("foo/bar", 100000, ["tools"], prompt_per_token=0.3e-6),  # 0.3/M
    ]
    out = apply_filters(
        items,
        {"require_tools": True, "min_context": 0, "modality": "any",
         "price": {"prompt_max": 1.0, "completion_max": 1.0}},
    )
    assert sorted(i["id"] for i in out) == ["foo/bar", "qwen3-coder:free"]


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


def test_extract_params_from_description() -> None:
    assert _extract_params({"id": "x:free", "description": "Llama 3.3 70B Instruct"}) == ("70B", 70.0)
    assert _extract_params({"id": "x:free", "description": "Qwen3 235B base model"}) == ("235B", 235.0)
    assert _extract_params({"id": "x:free", "description": "Trained 0.5B params"}) == ("0.5B", 0.5)


def test_extract_params_hardcoded_overrides() -> None:
    assert _extract_params({"id": "poolside/laguna-xs.2:free", "description": ""}) == ("15B", 15.0)
    assert _extract_params({"id": "poolside/laguna-m.1:free", "description": ""}) == ("40B", 40.0)
    assert _extract_params({"id": "moonshotai/kimi-k2.6:free", "description": ""}) == ("70B+", 70.0)


def test_extract_params_missing_description() -> None:
    assert _extract_params({"id": "x:free", "description": ""}) == ("Not Specified", 0.0)
    assert _extract_params({"id": "x:free", "description": "Tools, structured output"}) == ("Not Specified", 0.0)
    assert _extract_params({"id": "x:free"}) == ("Not Specified", 0.0)


def test_rank_params_desc() -> None:
    items = [
        _item("small:free", 100000, ["tools"]) | {"description": "Mini 8B variant"},
        _item("big:free", 100000, ["tools"]) | {"description": "Large 70B mixture-of-experts"},
        _item("mid:free", 100000, ["tools"]) | {"description": "Balanced 32B model"},
    ]
    out = rank(items, {"rank_by": "params_desc", "tiebreakers": []}, [])
    assert [i["id"] for i in out] == ["big:free", "mid:free", "small:free"]


def test_rank_params_asc() -> None:
    """params_asc reverses the order — smaller model wins, but
    unparseable descriptions (params_billions==0) rank LAST, same as
    params_desc convention."""
    items = [
        _item("small:free", 100000, ["tools"]) | {"description": "Mini 8B variant"},
        _item("big:free", 100000, ["tools"]) | {"description": "Large 70B mixture-of-experts"},
        _item("mid:free", 100000, ["tools"]) | {"description": "Balanced 32B model"},
        _item("unknown:free", 100000, ["tools"]) | {"description": "No size info here"},
    ]
    out = rank(items, {"rank_by": "params_asc", "tiebreakers": []}, [])
    assert [i["id"] for i in out] == ["small:free", "mid:free", "big:free", "unknown:free"]


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
