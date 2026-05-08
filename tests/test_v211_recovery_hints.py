"""v2.1.1 recovery_hints regression tests.

Four new patterns surfaced from a 184-message lighting-redesign turn
that produced 11 tool_result errors — every one an agent-learning
error (wrong-API guess), zero TD-side bugs. Each pattern targets a
specific misuse and points at the right TD API:

  - ``td.Par.rawVal``        — deprecated TD-2022 name, removed in
                                TD 2025 → use ``par.eval`` /
                                ``par.val`` / ``par.expr``.
  - ``renderTOP`` attribute typos (``cooking`` / ``numCooks`` /
                                ``xres`` / ``yres``) — point at
                                ``top.par.resolutionw/h`` and
                                ``top.cookCount`` / ``top.cookTime``.
  - ``tdu.Matrix.translation`` — actual fields are ``.tx`` /
                                ``.ty`` / ``.tz``.
  - ``ParCollection.children`` — that's the parameter list, not the
                                operator children. Use ``op.children``.

Mirrors the v2.0.1 regression-test design (parametrized
``recovery.attach_hint`` fixture; one row per pattern).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_recovery as recovery  # noqa: E402


@pytest.mark.parametrize(
    "error_msg, expected_hint_substring",
    [
        # td.Par.rawVal — deprecated TD-2022 name, removed in TD 2025.
        ("'td.Par' object has no attribute 'rawVal'", "par.eval"),
        # renderTOP attribute typos — multiple variants share one pattern.
        ("'td.renderTOP' object has no attribute 'cooking'", "cookCount"),
        ("'td.renderTOP' object has no attribute 'numCooks'", "cookCount"),
        ("'td.renderTOP' object has no attribute 'xres'", "resolutionw"),
        ("'td.renderTOP' object has no attribute 'yres'", "resolutionh"),
        # tdu.Matrix.translation — actual fields are .tx / .ty / .tz.
        ("'tdu.Matrix' object has no attribute 'translation'", ".tx"),
        # ParCollection.children — it's the parameter list, not children.
        ("'td.ParCollection' object has no attribute 'children'", "op.children"),
    ],
)
def test_recovery_hints_v211_patterns(error_msg, expected_hint_substring):
    """Each AttributeError pattern from the lighting-redesign audit
    gets a specific actionable hint pointing at the right TD API."""
    enriched = recovery.attach_hint({"error": error_msg})
    assert "recovery_hint" in enriched, f"no hint attached for: {error_msg}"
    assert expected_hint_substring.lower() in enriched["recovery_hint"].lower()
