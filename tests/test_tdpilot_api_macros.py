"""Pure-Python unit tests for the standalone macro engine.

These exercise the parts of tdpilot_api_macros that don't touch TD: the
parameter-range validator and the param-targets applicator. The full
``handle_macro_run`` flow needs a live cook-thread dispatcher and is
covered by integration tests separately.
"""

from __future__ import annotations

import pytest
from tdpilot_api_macros import (  # noqa: E402
    ExpressionSpec,
    MacroTemplate,
    NodeSpec,
    ParamSpec,
    ParamTarget,
    _apply_param_targets,
    _validate_param_ranges,
)


def _mk_template(**overrides):
    base = dict(
        name="t",
        description="",
        nodes=[NodeSpec(node_type="topNoise", name="noise")],
        connections=[],
        param_schema={
            "amplitude": ParamSpec(type="float", default=0.5, min_value=0.0, max_value=1.0),
            "octaves": ParamSpec(type="int", default=4, min_value=1, max_value=16),
        },
        param_targets={},
    )
    base.update(overrides)
    return MacroTemplate(**base)


def test_validate_param_ranges_accepts_in_range_values():
    t = _mk_template()
    # No raise — both within bounds.
    _validate_param_ranges(t, {"amplitude": 0.5, "octaves": 4})


def test_validate_param_ranges_below_minimum_raises():
    t = _mk_template()
    with pytest.raises(ValueError, match="below minimum"):
        _validate_param_ranges(t, {"amplitude": -0.1})


def test_validate_param_ranges_above_maximum_raises():
    t = _mk_template()
    with pytest.raises(ValueError, match="above maximum"):
        _validate_param_ranges(t, {"octaves": 99})


def test_validate_param_ranges_skips_unbounded_params():
    """A spec with min_value=None and max_value=None never trips."""
    t = _mk_template(param_schema={"label": ParamSpec(type="str", default="x")})
    _validate_param_ranges(t, {"label": "anything"})


def test_apply_param_targets_value_mode_writes_into_node_params():
    """When mode='value', the resolved value is set on node.params."""
    t = _mk_template(
        param_targets={
            "amplitude": [ParamTarget(node="noise", param="amp", mode="value")],
        },
    )
    nodes, exprs = _apply_param_targets(t, {"amplitude": 0.85})
    assert exprs == []
    assert nodes[0].params["amp"] == 0.85


def test_apply_param_targets_expr_mode_emits_expressionspec():
    """When mode='expr', the template string is rendered into an
    ExpressionSpec — node.params stays untouched."""
    t = _mk_template(
        param_targets={
            "amplitude": [
                ParamTarget(
                    node="noise",
                    param="amp",
                    mode="expr",
                    template="op('audio')['env'] * {value}",
                )
            ],
        },
    )
    nodes, exprs = _apply_param_targets(t, {"amplitude": 0.5})
    assert "amp" not in nodes[0].params
    assert len(exprs) == 1
    assert exprs[0] == ExpressionSpec(
        node="noise",
        param="amp",
        expr="op('audio')['env'] * 0.5",
    )


def test_apply_param_targets_returns_independent_node_copies():
    """Mutating the returned node list must not mutate the template's
    original nodes — a previous bug shared the dict reference."""
    original = NodeSpec(node_type="topNoise", name="noise", params={"x": 1})
    t = _mk_template(nodes=[original])
    nodes, _ = _apply_param_targets(t, {})
    nodes[0].params["new_key"] = 99
    assert "new_key" not in original.params


def test_apply_param_targets_skips_missing_node_silently():
    """If the template lists a target node that's not in template.nodes,
    the function logs nothing and skips — no crash."""
    t = _mk_template(
        param_targets={
            "amplitude": [ParamTarget(node="ghost", param="x", mode="value")],
        },
    )
    nodes, exprs = _apply_param_targets(t, {"amplitude": 0.5})
    assert nodes[0].params == {}
    assert exprs == []
