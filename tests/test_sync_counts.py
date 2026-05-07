"""Tests for ``scripts/sync_counts.py`` — Phase 5.3 contract.

The script is the single source of truth for the standalone tool
count. These tests pin three invariants:

  1. ``--check`` exits 0 when every mention matches ``len(TOOL_SCHEMAS)``.
  2. Running without ``--check`` is idempotent (re-running yields
     no further changes).
  3. Drifted counts in real files trigger non-zero exit on
     ``--check`` and get fixed on a normal run.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_counts.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("sync_counts_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_counts_mod"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sync():
    return _load_script_module()


# ---------------------------------------------------------------------------
# Repo-wide checks against the live README/MANUAL
# ---------------------------------------------------------------------------


def test_check_passes_against_committed_repo():
    """The committed README + MANUAL must already be in sync.

    Doubles as a CI gate: any future commit that bumps the schema
    without running the syncer fails this test.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"sync_counts --check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_canonical_count_matches_tool_schemas(sync):
    """``read_canonical_count`` returns ``len(TOOL_SCHEMAS)`` exactly."""
    spec = importlib.util.spec_from_file_location(
        "td_schema_defs",
        REPO_ROOT / "td_component" / "tdpilot_api_schema_defs.py",
    )
    schema_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema_mod)
    assert sync.read_canonical_count() == len(schema_mod.TOOL_SCHEMAS)


def test_apply_is_idempotent():
    """A no-op run after the syncer has already applied changes
    must not modify any file.
    """
    # First run brings everything in sync (this is the committed
    # state, but we re-run to be sure).
    first = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert first.returncode == 0

    # Second run must still claim "already at canonical count".
    second = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert second.returncode == 0
    assert "already at canonical count" in second.stdout


# ---------------------------------------------------------------------------
# Behaviour against a fixture clone — verifies drift detection + repair
# ---------------------------------------------------------------------------


def _clone_repo_to(tmp_path: Path) -> Path:
    """Copy README + docs/MANUAL + scripts/sync_counts + the schema
    into a tmp tree so tests can mutate freely without polluting the
    real repo.
    """
    dst = tmp_path / "repo"
    dst.mkdir()
    for relpath in (
        "README.md",
        "docs/MANUAL.md",
        "scripts/sync_counts.py",
        "td_component/tdpilot_api_schema_defs.py",
    ):
        src = REPO_ROOT / relpath
        out = dst / relpath
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    return dst


def test_drifted_repo_check_exits_nonzero(tmp_path):
    """If a count cell drifts, ``--check`` returns 1 and names the file."""
    dst = _clone_repo_to(tmp_path)
    readme = dst / "README.md"
    text = readme.read_text(encoding="utf-8")
    # Force a drift by replacing the standalone badge count with a wildly
    # wrong number. The exact substitution is anchored so we only mutate
    # the badge row and nothing else.
    drifted = re.sub(
        r"(tools-)\d+(%20%28standalone%29)",
        r"\g<1>9999\g<2>",
        text,
        count=1,
    )
    assert drifted != text, "fixture mutation didn't take"
    readme.write_text(drifted, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(dst / "scripts" / "sync_counts.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=dst,
    )
    assert result.returncode == 1
    assert "DRIFT: README.md" in result.stderr
    assert "9999" in result.stderr


def test_apply_repairs_drift(tmp_path):
    """After ``sync_counts.py``, ``--check`` passes again."""
    dst = _clone_repo_to(tmp_path)
    readme = dst / "README.md"
    text = readme.read_text(encoding="utf-8")
    drifted = re.sub(
        r"(\| \*\*Tools\*\* \| )\d+( curated for in-TD use)",
        r"\g<1>1234\g<2>",
        text,
        count=1,
    )
    assert drifted != text
    readme.write_text(drifted, encoding="utf-8")

    apply = subprocess.run(
        [sys.executable, str(dst / "scripts" / "sync_counts.py")],
        capture_output=True,
        text=True,
        cwd=dst,
    )
    assert apply.returncode == 0
    assert "updated" in apply.stdout

    after = subprocess.run(
        [sys.executable, str(dst / "scripts" / "sync_counts.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=dst,
    )
    assert after.returncode == 0
