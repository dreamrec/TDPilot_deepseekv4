"""Tests for Phase 1.2.2 ``_mirror_tox_to_main_repo_if_worktree``.

The helper auto-copies a freshly-built .tox + hash file from a
worktree's ``td_component/`` into the main repo's ``td_component/``
so drag-and-go from Finder always picks up the latest build.

These tests don't exercise TouchDesigner — they mock subprocess +
filesystem to drive the mirror function through every code path.
"""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TD_COMPONENT_DIR = REPO_ROOT / "td_component"


@pytest.fixture
def mirror_module(monkeypatch):
    """Import ``build_export_mcp_tox`` with TD globals stubbed. Imports
    under the canonical module name (``build_export_mcp_tox``) so the
    PR #35 ``__name__ != "build_export_mcp_tox"`` guard at the bottom
    of the file recognises this as a module import and skips the
    ``build_and_export()`` auto-call.

    Yields the loaded module so tests can call
    ``_mirror_tox_to_main_repo_if_worktree`` directly.
    """
    monkeypatch.delenv("TDPILOT_NO_TOX_MIRROR", raising=False)
    monkeypatch.setitem(builtins.__dict__, "op", lambda path: None)
    monkeypatch.setitem(builtins.__dict__, "parent", lambda: SimpleNamespace())
    monkeypatch.setitem(builtins.__dict__, "me", SimpleNamespace())
    monkeypatch.setitem(builtins.__dict__, "ui", SimpleNamespace())
    monkeypatch.setitem(builtins.__dict__, "project", SimpleNamespace())
    monkeypatch.setitem(builtins.__dict__, "tdu", SimpleNamespace())
    monkeypatch.setitem(builtins.__dict__, "debug", lambda *a, **k: None)

    # Force a clean import each test under the canonical name.
    sys.path.insert(0, str(TD_COMPONENT_DIR))
    sys.modules.pop("build_export_mcp_tox", None)
    try:
        import build_export_mcp_tox  # noqa: PLC0415

        yield build_export_mcp_tox
    finally:
        sys.modules.pop("build_export_mcp_tox", None)
        try:
            sys.path.remove(str(TD_COMPONENT_DIR))
        except ValueError:
            pass


@pytest.fixture
def fake_worktree(tmp_path):
    """Build a minimal fake-worktree filesystem:

      <tmp>/main_repo/.git/                ← main repo's git common dir
      <tmp>/main_repo/td_component/        ← mirror destination (starts empty)
      <tmp>/main_repo/.claude/worktrees/wt/td_component/
                                           ← source: contains tox + hash to mirror

    Returns (main_repo_path, worktree_path, tox_path, hash_path) so
    tests can populate the source side and then assert on the
    destination.
    """
    main_repo = tmp_path / "main_repo"
    (main_repo / ".git").mkdir(parents=True)
    (main_repo / "td_component").mkdir()
    worktree = main_repo / ".claude" / "worktrees" / "wt"
    (worktree / "td_component").mkdir(parents=True)

    tox_path = worktree / "td_component" / "tdpilot_API.tox"
    hash_path = worktree / "td_component" / ".tox-api-source-hash.json"
    tox_path.write_bytes(b"FAKE_TOX_BYTES_v222")
    hash_path.write_text('{"tox_source_hash": "abc123", "built_at": "2026-05-11T20:00:00Z"}')

    return main_repo, worktree, tox_path, hash_path


def _stub_git_common_dir(monkeypatch, common_dir_path: Path | None):
    """Make ``subprocess.run`` return a fake ``git rev-parse
    --git-common-dir`` response that points at ``common_dir_path``.
    Passing ``None`` simulates ``git`` failing (returncode 1).
    """

    def fake_run(args, **kwargs):
        # Sanity — only intercept the rev-parse call.
        assert args[:3] == ["git", "-C", str(args[2])] or "rev-parse" in args, args
        if common_dir_path is None:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="git failed")
        return subprocess.CompletedProcess(args, returncode=0, stdout=str(common_dir_path) + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Happy path: worktree → main repo
# ---------------------------------------------------------------------------


class TestMirrorHappyPath:
    def test_mirror_copies_both_files_when_in_worktree(self, mirror_module, fake_worktree, monkeypatch):
        main_repo, worktree, tox_path, hash_path = fake_worktree
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # Destination files exist with the source bytes.
        dst_tox = main_repo / "td_component" / "tdpilot_API.tox"
        dst_hash = main_repo / "td_component" / ".tox-api-source-hash.json"
        assert dst_tox.is_file()
        assert dst_tox.read_bytes() == b"FAKE_TOX_BYTES_v222"
        assert dst_hash.is_file()
        assert "abc123" in dst_hash.read_text()

    def test_mirror_replaces_stale_symlink_in_main_repo(self, mirror_module, fake_worktree, monkeypatch):
        """The single most-important case: pre-1.2.2 the main repo's
        td_component/ held SYMLINKS pointing at a stale worktree. The
        mirror must delete those symlinks first, otherwise it would
        ``copyfile`` THROUGH the symlink into the wrong worktree.
        """
        main_repo, worktree, tox_path, _ = fake_worktree
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        # Plant a stale symlink at the destination.
        dst_tox = main_repo / "td_component" / "tdpilot_API.tox"
        stale_worktree = main_repo / ".claude" / "worktrees" / "stale_wt"
        (stale_worktree / "td_component").mkdir(parents=True)
        stale_target = stale_worktree / "td_component" / "tdpilot_API.tox"
        stale_target.write_bytes(b"STALE_BYTES")
        dst_tox.symlink_to(stale_target)
        assert dst_tox.is_symlink()
        # Sanity — through the symlink we'd read STALE_BYTES.
        assert dst_tox.read_bytes() == b"STALE_BYTES"

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # The symlink was replaced with a real file containing the
        # FRESH bytes. The stale_target is untouched.
        assert not dst_tox.is_symlink()
        assert dst_tox.read_bytes() == b"FAKE_TOX_BYTES_v222"
        assert stale_target.read_bytes() == b"STALE_BYTES"

    def test_mirror_replaces_existing_real_file(self, mirror_module, fake_worktree, monkeypatch):
        """Existing target is a real file (from a prior mirror) — should
        be overwritten with the fresh content."""
        main_repo, worktree, _, _ = fake_worktree
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        dst_tox = main_repo / "td_component" / "tdpilot_API.tox"
        dst_tox.write_bytes(b"OLDER_REAL_FILE")

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        assert dst_tox.read_bytes() == b"FAKE_TOX_BYTES_v222"


# ---------------------------------------------------------------------------
# No-op paths (the mirror correctly stays out of the way)
# ---------------------------------------------------------------------------


class TestMirrorNoOpPaths:
    def test_skip_when_env_var_set(self, mirror_module, fake_worktree, monkeypatch):
        main_repo, worktree, _, _ = fake_worktree
        monkeypatch.setenv("TDPILOT_NO_TOX_MIRROR", "1")
        # Don't even stub git — env var should short-circuit before that.

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        assert not (main_repo / "td_component" / "tdpilot_API.tox").exists()

    def test_skip_when_build_root_IS_main_repo(self, mirror_module, tmp_path, monkeypatch):
        """When the build is happening in the main checkout itself
        (not a worktree), there's nothing to mirror — the .tox is
        already in the right place. Should silently no-op."""
        main_repo = tmp_path / "main_repo"
        (main_repo / ".git").mkdir(parents=True)
        (main_repo / "td_component").mkdir()
        src_tox = main_repo / "td_component" / "tdpilot_API.tox"
        src_tox.write_bytes(b"BYTES")
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        # repo_root == main_repo: should hit the no-op branch.
        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(main_repo), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # File untouched (no other mirror destination exists).
        assert src_tox.read_bytes() == b"BYTES"

    def test_skip_when_git_unavailable(self, mirror_module, fake_worktree, monkeypatch):
        main_repo, worktree, _, _ = fake_worktree
        # git returns non-zero exit (e.g., not installed, not a repo).
        _stub_git_common_dir(monkeypatch, None)

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # Mirror destination is untouched.
        assert not (main_repo / "td_component" / "tdpilot_API.tox").exists()

    def test_skip_when_main_td_component_missing(self, mirror_module, tmp_path, monkeypatch):
        """Main repo exists but has no td_component/ directory — should
        skip silently rather than create a directory in some unexpected
        location."""
        main_repo = tmp_path / "weird_main"
        (main_repo / ".git").mkdir(parents=True)
        # NO td_component/ created.
        worktree = main_repo / ".claude" / "worktrees" / "wt"
        (worktree / "td_component").mkdir(parents=True)
        (worktree / "td_component" / "tdpilot_API.tox").write_bytes(b"BYTES")
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        # Should not raise.
        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        assert not (main_repo / "td_component").exists()

    def test_skip_when_source_file_missing(self, mirror_module, fake_worktree, monkeypatch):
        """Source .tox doesn't exist in the worktree (e.g., build
        aborted before the .tox got written) — mirror per-file logic
        should skip that file but not crash."""
        main_repo, worktree, tox_path, hash_path = fake_worktree
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        # Remove the .tox source.
        tox_path.unlink()

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # The hash file still got copied (it still exists in the source).
        assert (main_repo / "td_component" / ".tox-api-source-hash.json").is_file()
        # The .tox didn't get copied (nothing to copy).
        assert not (main_repo / "td_component" / "tdpilot_API.tox").exists()


# ---------------------------------------------------------------------------
# Error-handling paths (the mirror must never break a build)
# ---------------------------------------------------------------------------


class TestMirrorErrorHandling:
    def test_subprocess_raising_does_not_propagate(self, mirror_module, fake_worktree, monkeypatch):
        main_repo, worktree, _, _ = fake_worktree

        def raising_run(*args, **kwargs):
            raise OSError("simulated git CLI explosion")

        monkeypatch.setattr(subprocess, "run", raising_run)

        # Must not raise — mirror failure should never break a build.
        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

    def test_copy_failure_logs_but_continues_to_next_file(
        self, mirror_module, fake_worktree, monkeypatch, capsys
    ):
        """If shutil.copyfile raises mid-mirror (e.g., out of disk
        space, permission denied), the mirror should log and continue
        with the next file — not crash."""
        import shutil

        main_repo, worktree, _, _ = fake_worktree
        _stub_git_common_dir(monkeypatch, main_repo / ".git")

        original_copy = shutil.copyfile
        first_call = {"done": False}

        def flaky_copy(src, dst, *args, **kwargs):
            if not first_call["done"]:
                first_call["done"] = True
                raise OSError("disk full")
            return original_copy(src, dst, *args, **kwargs)

        monkeypatch.setattr(shutil, "copyfile", flaky_copy)

        mirror_module._mirror_tox_to_main_repo_if_worktree(
            str(worktree), "tdpilot_API.tox", ".tox-api-source-hash.json"
        )

        # The first file (tdpilot_API.tox) failed, the second
        # (.tox-api-source-hash.json) should have succeeded.
        assert not (main_repo / "td_component" / "tdpilot_API.tox").exists()
        assert (main_repo / "td_component" / ".tox-api-source-hash.json").is_file()
        out = capsys.readouterr().out
        assert "copy failed" in out
