"""Tests for v2.5.4 — env→file migration in auth_bootstrap.

Covers ``maybe_migrate_env_to_file`` and its composition inside
``bootstrap_auth``. The migration solves: user exports
``TD_MCP_SHARED_SECRET`` in their shell, restarts TD, and would otherwise
hit 401 because the shell env doesn't survive the relaunch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_mcp.auth_bootstrap import (
    bootstrap_auth,
    maybe_migrate_env_to_file,
)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return tmp_path / ".tdpilot-dpsk4.env"


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure tests start with no TD_MCP_SHARED_SECRET in env."""
    monkeypatch.delenv("TD_MCP_SHARED_SECRET", raising=False)
    yield


class TestEnvToFileMigration:
    def test_no_env_no_migration(self, env_file: Path, clean_env):
        assert maybe_migrate_env_to_file(env_file) is False
        assert not env_file.exists()

    def test_env_set_migrates_to_new_file(self, env_file: Path, clean_env, monkeypatch):
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "shellsecret123")
        assert maybe_migrate_env_to_file(env_file) is True
        assert env_file.exists()
        content = env_file.read_text(encoding="utf-8")
        assert "TD_MCP_SHARED_SECRET=shellsecret123" in content

    def test_env_set_file_already_has_same_secret_is_idempotent(self, env_file: Path, clean_env, monkeypatch):
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "samesecret")
        env_file.write_text("TD_MCP_SHARED_SECRET=samesecret\n", encoding="utf-8")
        mtime_before = env_file.stat().st_mtime_ns
        assert maybe_migrate_env_to_file(env_file) is False
        # File untouched — no rewrite.
        assert env_file.stat().st_mtime_ns == mtime_before

    def test_env_set_file_has_different_secret_overwrites(self, env_file: Path, clean_env, monkeypatch):
        env_file.write_text("TD_MCP_SHARED_SECRET=oldfile\n", encoding="utf-8")
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "newenv")
        assert maybe_migrate_env_to_file(env_file) is True
        content = env_file.read_text(encoding="utf-8")
        assert "TD_MCP_SHARED_SECRET=newenv" in content
        assert "oldfile" not in content

    def test_env_migration_preserves_other_lines(self, env_file: Path, clean_env, monkeypatch):
        env_file.write_text("# user-edited file\nTD_OTHER_VAR=keepme\n", encoding="utf-8")
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "newsecret")
        assert maybe_migrate_env_to_file(env_file) is True
        content = env_file.read_text(encoding="utf-8")
        assert "TD_OTHER_VAR=keepme" in content
        assert "TD_MCP_SHARED_SECRET=newsecret" in content
        assert "# user-edited file" in content

    @pytest.mark.skipif(os.name != "posix", reason="chmod only meaningful on POSIX")
    def test_migrated_file_has_restrictive_perms(self, env_file: Path, clean_env, monkeypatch):
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "perms_test")
        assert maybe_migrate_env_to_file(env_file) is True
        mode = env_file.stat().st_mode & 0o777
        # 0o600 = owner read+write only.
        assert mode == 0o600, f"expected 0o600 perms, got 0o{mode:o}"


class TestBootstrapAuthIntegration:
    def test_bootstrap_runs_migration_after_load(self, env_file: Path, clean_env, monkeypatch):
        """bootstrap_auth should call the migration so an env-supplied
        secret ends up in the file after one server start."""
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "via_shell")
        # Disable autogen so it doesn't interfere.
        monkeypatch.delenv("TD_MCP_AUTOGENERATE_SECRET", raising=False)

        bootstrap_auth(path=env_file)

        assert env_file.exists()
        assert "TD_MCP_SHARED_SECRET=via_shell" in env_file.read_text(encoding="utf-8")
        # Env value still present (unchanged) for the running process.
        assert os.environ.get("TD_MCP_SHARED_SECRET") == "via_shell"

    def test_bootstrap_with_neither_env_nor_file_and_no_autogen_is_noop(
        self, env_file: Path, clean_env, monkeypatch
    ):
        monkeypatch.delenv("TD_MCP_AUTOGENERATE_SECRET", raising=False)
        monkeypatch.delenv("TD_MCP_REQUIRE_AUTH", raising=False)
        bootstrap_auth(path=env_file)
        # Migration found nothing to migrate; autogen disabled; file never created.
        assert not env_file.exists()

    def test_bootstrap_no_stdout_leak(self, env_file: Path, clean_env, monkeypatch, capsys):
        """v2.5.4 migration log line must go to stderr, never stdout.
        Stdout is the MCP transport channel — a leak there is fatal."""
        monkeypatch.setenv("TD_MCP_SHARED_SECRET", "stdoutleak_check")
        bootstrap_auth(path=env_file)
        captured = capsys.readouterr()
        assert captured.out == "", f"stdout leak: {captured.out!r}"
        # Stderr SHOULD contain the migration log.
        assert "Migrated TD_MCP_SHARED_SECRET" in captured.err
