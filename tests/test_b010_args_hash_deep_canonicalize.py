"""v2.4 / B-010 — args_hash deep-canonicalization (live-debug 2026-05-13).

Live monitor caught the agent calling:

    td_analyze_frame({"modes": ["histogram", "luminance"], "path": X})  # row 144
    td_analyze_frame({"modes": ["luminance", "histogram"], "path": X})  # row 149/157

Both should be the same identity for cycle-detect purposes — same end
effect on the project — but the pre-fix args_hash used
``json.dumps(args, sort_keys=True)`` which only sorts top-level dict
KEYS, not nested list VALUES. So the agent could permute list elements
to evade detection until some other limit caught it.

Fix: new ``_deep_canonicalize`` helper recursively sorts dict keys AND
list elements before JSON-serializing for the hash. List elements are
sorted by their own canonicalized JSON repr (stable across runs).

Tool-batch is intentionally affected: same sub-calls in different
order count as the same identity (the END EFFECT on the project is
the same). That matches the cycle-detector's purpose of catching
"agent is stuck" repeating equivalent work, rather than its literal
call signature.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


# =====================================================================
# Pre-existing contract preserved
# =====================================================================


def test_b010_none_and_empty_dict_still_collapse_to_braces():
    """The {} / None equivalence pre-dates B-010; keep it pinned."""
    from tdpilot_api_cycle_detector import args_hash

    assert args_hash(None) == "{}"
    assert args_hash({}) == "{}"
    assert args_hash(None) == args_hash({})


def test_b010_top_level_dict_key_order_still_canonical():
    """Pre-fix behavior: dict-key order canonicalized. Must still hold
    after the deep-canonicalize refactor."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"path": "/x", "mode": "lum"}
    b = {"mode": "lum", "path": "/x"}
    assert args_hash(a) == args_hash(b)


# =====================================================================
# B-010 core fix — nested list canonicalization
# =====================================================================


def test_b010_list_order_in_value_is_canonical():
    """The exact live-monitor evasion: same list contents in different
    order must hash identically. This is the regression-prevention pin."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"modes": ["histogram", "luminance"], "path": "/project1/kaleido_out"}
    b = {"modes": ["luminance", "histogram"], "path": "/project1/kaleido_out"}
    assert args_hash(a) == args_hash(b), (
        "list permutation must hash identically — this is the B-010 "
        f"evasion case. Got: {args_hash(a)!r} vs {args_hash(b)!r}"
    )


def test_b010_three_element_list_permutations_canonical():
    """Generalize: ALL 6 permutations of a 3-element list must hash the
    same. Otherwise the agent could rotate through orderings until
    cycle-detect missed."""
    from tdpilot_api_cycle_detector import args_hash

    base = {"modes": ["a", "b", "c"], "path": "X"}
    permutations = [
        {"modes": ["a", "b", "c"], "path": "X"},
        {"modes": ["a", "c", "b"], "path": "X"},
        {"modes": ["b", "a", "c"], "path": "X"},
        {"modes": ["b", "c", "a"], "path": "X"},
        {"modes": ["c", "a", "b"], "path": "X"},
        {"modes": ["c", "b", "a"], "path": "X"},
    ]
    h = args_hash(base)
    for p in permutations:
        assert args_hash(p) == h, (
            f"all 3-elem permutations must hash same, {p!r} → {args_hash(p)!r}"
        )


def test_b010_genuinely_different_lists_hash_differently():
    """Inverse check: different list CONTENTS (not just order) must
    still produce different hashes. Don't over-canonicalize."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"modes": ["histogram", "luminance"], "path": "X"}
    b = {"modes": ["histogram", "alpha"], "path": "X"}
    assert args_hash(a) != args_hash(b), (
        "different list contents must NOT collide"
    )


def test_b010_duplicate_elements_preserved():
    """A list ['a','a','b'] is NOT the same as ['a','b'] (set-vs-list
    semantics). Cycle-detect should treat them as different."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"modes": ["a", "a", "b"], "path": "X"}
    b = {"modes": ["a", "b"], "path": "X"}
    assert args_hash(a) != args_hash(b)


# =====================================================================
# Nested-structure handling
# =====================================================================


def test_b010_nested_dict_in_list_canonical():
    """tool_batch's ``calls`` is a list of dicts. Each dict's keys
    must canonicalize; the outer list ordering then canonicalizes too."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"calls": [
        {"tool": "td_get_nodes", "args": {"path": "/project1"}},
        {"tool": "td_get_errors", "args": {"path": "/project1"}},
    ]}
    # Same batch, different sub-call order — same end effect.
    b = {"calls": [
        {"tool": "td_get_errors", "args": {"path": "/project1"}},
        {"tool": "td_get_nodes", "args": {"path": "/project1"}},
    ]}
    assert args_hash(a) == args_hash(b)


def test_b010_nested_dict_key_order_in_list_canonical():
    """Each dict inside the list still gets its keys sorted."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"calls": [{"tool": "X", "args": {"a": 1, "b": 2}}]}
    b = {"calls": [{"args": {"b": 2, "a": 1}, "tool": "X"}]}
    assert args_hash(a) == args_hash(b)


def test_b010_deeply_nested_list_in_dict_in_list_canonical():
    """3-level nesting: outer dict → list → inner dict → list."""
    from tdpilot_api_cycle_detector import args_hash

    a = {"calls": [{"args": {"modes": ["x", "y"]}}]}
    b = {"calls": [{"args": {"modes": ["y", "x"]}}]}
    assert args_hash(a) == args_hash(b)


# =====================================================================
# Defensive paths — bad input shouldn't crash the agent
# =====================================================================


def test_b010_non_jsonable_value_falls_through_via_default_str():
    """default=str in json.dumps lets unusual values pass through.
    Pre/post fix this still works — just verify the safety net."""
    from tdpilot_api_cycle_detector import args_hash

    class _Weird:
        def __str__(self):
            return "weird"

    # default=str converts the unknown type to its str repr.
    result = args_hash({"x": _Weird()})
    assert "weird" in result


def test_b010_recursive_reference_falls_back_to_repr():
    """If the value graph is genuinely unserializable (cyclic ref,
    etc.), we must return SOMETHING — never crash the agent."""
    from tdpilot_api_cycle_detector import args_hash

    bad: dict = {}
    bad["self"] = bad  # cyclic reference
    # The exception path may either succeed (default=str helps) or
    # fall back to repr(args). Either way, no exception escapes.
    result = args_hash(bad)
    assert isinstance(result, str)
    assert len(result) > 0


# =====================================================================
# Integration with the ledger — actual cycle detection now catches the evasion
# =====================================================================


def test_b010_ledger_detects_list_permutation_evasion():
    """End-to-end: feed the ledger the exact pair of calls that the
    live agent issued. Pre-fix they hashed differently → only count=1
    each → never tripped. Post-fix they hash same → count=2 with a
    third call would trip."""
    from tdpilot_api_cycle_detector import CycleLedger

    ledger = CycleLedger(threshold=3)
    # Live-observed pair (rows 144 and 149) — pre-fix this was count=1
    # for each unique hash; post-fix it's count=2 on the same hash.
    c1 = ledger.record(
        "td_analyze_frame",
        {"modes": ["histogram", "luminance"], "path": "/project1/kaleido_out"},
    )
    c2 = ledger.record(
        "td_analyze_frame",
        {"modes": ["luminance", "histogram"], "path": "/project1/kaleido_out"},
    )
    assert c1 == 1
    assert c2 == 2, (
        "list-permuted second call must increment the same ledger entry "
        f"(would have been 1 pre-fix). Got count={c2}"
    )
    # A third truly-equivalent call (yet another permutation) trips.
    c3 = ledger.record(
        "td_analyze_frame",
        {"path": "/project1/kaleido_out", "modes": ["luminance", "histogram"]},
    )
    assert c3 == 3
    assert c3 >= ledger.threshold, "should trigger CycleDetected at the call site"
