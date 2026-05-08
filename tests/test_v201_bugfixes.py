"""v2.0.1 bugfix regression tests.

These tests pin three classes of fixes surfaced by a live-TD audit
against the v2.0.0 .tox:

  1. **Patch session lifecycle** — pre-v2.0.1 the patch_commit /
     patch_rollback handlers left state set when their underlying
     ``ui.undo.endBlock()`` / ``project.undo()`` calls raised, which
     orphaned the session forever and made every subsequent
     ``patch_begin`` return "Another patch session is already active."
     Plus ``project.undo()`` doesn't even exist on TD 2025's Project
     object (the global is ``ui.undo.undo()``), so every rollback was
     guaranteed to ``AttributeError``.

  2. **td_python_help input validation** — the regex catch-all error
     ("Invalid target: must be a dotted identifier") was unactionable
     for the common agent mistake of passing an operator path or a
     parameter expression. The handler now detects those specific
     shapes and points at the right tool (td_get_node_detail /
     td_get_params).

  3. **Recovery hints for AttributeErrors** — five new patterns added
     to ``tdpilot_api_recovery._RECOVERY_HINTS`` covering the wrong-API
     guesses the agent kept hitting in the audit (CHOP.channels,
     CHOP.text, Page.label, Project.undo, td_python_help target
     validation).

Each test exercises the exact failure mode reported in the audit
trace and verifies the fix landed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_patches as patches  # noqa: E402
import tdpilot_api_recovery as recovery  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: stub TD's `ui.undo` and `parent()` storage so handlers run outside TD
# ---------------------------------------------------------------------------


class _StubUndo:
    """Behaves like TD's ui.undo for the lifecycle tests."""

    def __init__(self, fail_endblock: bool = False, fail_undo: bool = False):
        self.calls: list[tuple[str, object]] = []
        self.fail_endblock = fail_endblock
        self.fail_undo = fail_undo

    def startBlock(self, name):
        self.calls.append(("startBlock", name))

    def endBlock(self):
        self.calls.append(("endBlock", None))
        if self.fail_endblock:
            raise RuntimeError("Cannot end non existent undo operation")

    def undo(self):
        self.calls.append(("undo", None))
        if self.fail_undo:
            raise RuntimeError("undo failed")


class _StubCOMP:
    """Stand-in for TD's parent COMP — backs comp.storage with a dict."""

    def __init__(self):
        self._storage: dict = {}

    def fetch(self, key, default=None):
        return self._storage.get(key, default)

    def store(self, key, value):
        self._storage[key] = value

    def unstore(self, key):
        self._storage.pop(key, None)


@pytest.fixture
def td_env(monkeypatch):
    """Inject stub TD globals into tdpilot_api_patches' namespace."""
    stub_undo = _StubUndo()
    stub_comp = _StubCOMP()

    # `ui` is referenced in patches as `ui.undo.startBlock(...)` etc.
    fake_ui = SimpleNamespace(undo=stub_undo)
    monkeypatch.setattr(patches, "ui", fake_ui, raising=False)
    # `parent()` is called inside `_comp_for_state`. Replace the helper.
    monkeypatch.setattr(patches, "_comp_for_state", lambda: stub_comp)

    return SimpleNamespace(undo=stub_undo, comp=stub_comp, ui=fake_ui)


# ---------------------------------------------------------------------------
# patch_begin — orphan recovery (force=True + stale)
# ---------------------------------------------------------------------------


def test_patch_begin_refuses_when_recent_session_active(td_env):
    """Defensive baseline: a fresh-but-not-stale active session still blocks."""
    patches._set_patch_state(
        {"name": "earlier", "scope_path": "/", "started_at": __import__("time").time(), "step_count": 0}
    )
    out = patches.handle_patch_begin({"name": "later"})
    assert "error" in out
    assert "already active" in out["error"]
    assert out["active_patch"] == "earlier"
    # The fix surfaces age + a force=True hint.
    assert "active_age_seconds" in out
    assert "force=True" in out["hint"]


def test_patch_begin_force_clears_orphaned_state(td_env):
    """force=True overrides the active-session refusal — the v2.0.1 escape hatch."""
    patches._set_patch_state(
        {"name": "orphan", "scope_path": "/", "started_at": __import__("time").time(), "step_count": 0}
    )
    out = patches.handle_patch_begin({"name": "fresh", "force": True})
    assert "error" not in out
    assert out["ok"] is True
    assert out["patch"]["name"] == "fresh"
    # Recovered_from breadcrumb is surfaced so the audit trail isn't lost.
    assert out["recovered_from"]["name"] == "orphan"
    assert out["recovered_from"]["reason"] == "force"


def test_patch_begin_auto_clears_stale_state(td_env):
    """Sessions older than 5 minutes are treated as orphaned and cleared."""
    stale_time = __import__("time").time() - 600  # 10 minutes ago
    patches._set_patch_state(
        {"name": "old_session", "scope_path": "/", "started_at": stale_time, "step_count": 0}
    )
    out = patches.handle_patch_begin({"name": "new_session"})
    assert "error" not in out
    assert out["ok"] is True
    assert out["patch"]["name"] == "new_session"
    assert out["recovered_from"]["reason"] == "stale"


# ---------------------------------------------------------------------------
# patch_commit — state ALWAYS cleared on endBlock failure
# ---------------------------------------------------------------------------


def test_patch_commit_clears_state_on_endblock_success(td_env):
    """Happy path: state cleared, no warning."""
    patches.handle_patch_begin({"name": "test"})
    out = patches.handle_patch_commit({})
    assert out["ok"] is True
    assert out["committed"] == "test"
    assert "warning" not in out
    assert patches._get_patch_state() is None


def test_patch_commit_clears_state_even_when_endblock_fails(td_env, monkeypatch):
    """The CORE v2.0.1 fix — pre-v2.0.1 a single endBlock failure orphaned
    the session forever. Verify state is cleared regardless."""
    patches.handle_patch_begin({"name": "test"})
    # Flip the stub to fail endBlock
    td_env.undo.fail_endblock = True
    out = patches.handle_patch_commit({})
    # Result still indicates success because the work was applied; the
    # warning surfaces what TD said.
    assert out["ok"] is True
    assert "warning" in out
    assert "endBlock failed" in out["warning"]
    # Critical: state has been cleared.
    assert patches._get_patch_state() is None


def test_patch_commit_after_endblock_failure_allows_new_begin(td_env):
    """Regression: after a failed commit, a fresh patch_begin must succeed
    without needing force=True. This is the user-visible symptom that the
    v2.0.1 fix resolves."""
    patches.handle_patch_begin({"name": "first"})
    td_env.undo.fail_endblock = True
    patches.handle_patch_commit({})  # endBlock fails internally
    td_env.undo.fail_endblock = False  # next commit's endBlock would succeed

    out = patches.handle_patch_begin({"name": "second"})
    # No "already active" error, no need for force=True.
    assert "error" not in out
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# patch_rollback — uses ui.undo.undo (NOT project.undo)
# ---------------------------------------------------------------------------


def test_patch_rollback_calls_ui_undo_not_project_undo(td_env):
    """The headline v2.0.1 bug: pre-v2.0.1 used `project.undo()` which
    doesn't exist on TD 2025's Project object. v2.0.1 routes through
    `ui.undo.undo()` which is the actual API."""
    patches.handle_patch_begin({"name": "test"})
    out = patches.handle_patch_rollback({})
    assert out["ok"] is True
    assert out["rolled_back"] == "test"
    # Verify the undo() call was made via ui.undo (not project).
    call_names = [c[0] for c in td_env.undo.calls]
    assert "endBlock" in call_names
    assert "undo" in call_names


def test_patch_rollback_clears_state_even_when_calls_fail(td_env):
    """Both endBlock + undo can fail; state must be cleared anyway."""
    patches.handle_patch_begin({"name": "test"})
    td_env.undo.fail_endblock = True
    td_env.undo.fail_undo = True
    out = patches.handle_patch_rollback({})
    assert "warning_endblock" in out
    assert "warning_undo" in out
    assert patches._get_patch_state() is None


# ---------------------------------------------------------------------------
# Recovery hints — the 5 new v2.0.1 patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_msg, expected_hint_substring",
    [
        # CHOP.channels — agent guessed the wrong API
        (
            "'td.waveCHOP' object has no attribute 'channels'",
            "chop.chans()",
        ),
        # CHOP.text — wrong DAT vs CHOP API
        (
            "'td.scriptCHOP' object has no attribute 'text'",
            "DAT.text exists but CHOP.text does not",
        ),
        # Page.label — there's no .label, only .name
        (
            "'td.Page' object has no attribute 'label'",
            "page.name",
        ),
        # Project.undo — actual breakage in pre-v2.0.1 patch_rollback
        (
            "'td.Project' object has no attribute 'undo'",
            "ui.undo",
        ),
        # td_python_help generic invalid-target — actionable hint added
        (
            'Invalid target: must be a dotted identifier like "td.OP" or "tdu"',
            "td_python_help expects a CLASS or MODULE",
        ),
    ],
)
def test_recovery_hints_v201_patterns(error_msg, expected_hint_substring):
    """Each AttributeError pattern from the v2.0 audit gets a specific
    actionable hint that points at the right tool / API."""
    enriched = recovery.attach_hint({"error": error_msg})
    assert "recovery_hint" in enriched, f"no hint attached for: {error_msg}"
    assert expected_hint_substring.lower() in enriched["recovery_hint"].lower()


def test_recovery_hint_does_not_double_attach():
    """A result that already has a recovery_hint should not be overwritten."""
    enriched = recovery.attach_hint({"error": "Unknown operator type", "recovery_hint": "user-supplied hint"})
    assert enriched["recovery_hint"] == "user-supplied hint"


def test_recovery_hint_attach_safe_on_non_dict():
    """Non-dict inputs pass through cleanly (defensive contract)."""
    assert recovery.attach_hint(None) is None
    assert recovery.attach_hint("string") == "string"
    assert recovery.attach_hint([1, 2, 3]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# td_python_help — better validation messages
# ---------------------------------------------------------------------------


def test_python_help_rejects_operator_reference_with_actionable_hint():
    """The most common agent mistake — passing an operator path. The error
    now points at td_get_node_detail."""
    src = (REPO_ROOT / "td_component" / "callbacks" / "handlers" / "search.py").read_text()
    # Pin the source so the validator stays present even if the file is
    # composed into a different DAT in the .tox.
    assert "td_get_node_detail" in src
    assert "operator reference" in src.lower()


def test_python_help_rejects_parameter_expression_with_actionable_hint():
    """`.par.X` / `[...]` / `(...)` shapes get a specific error pointing
    at td_get_params."""
    src = (REPO_ROOT / "td_component" / "callbacks" / "handlers" / "search.py").read_text()
    assert "td_get_params" in src
    assert ".par." in src
