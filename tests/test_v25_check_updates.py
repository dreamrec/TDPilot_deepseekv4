"""Tests for v2.5.7 — `td_check_for_updates`.

Mocks the GitHub Releases API + uses a temporary repo-root with synthetic
hash sidecars so we exercise the freshness check without depending on
the live .tox state. Cache TTL is asserted via two consecutive calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from td_mcp.lifecycle import update_check as uc
from td_mcp.lifecycle.update_check import (
    UpdateCheckResult,
    _compare_versions,
    _compute_source_hash,
    _parse_version,
    check_for_updates,
    clear_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Construct a minimal repo-root with a td_component/ dir + two
    .tox-source-hash sidecars + the matching source files. Hashes are
    computed via the live ``_compute_source_hash`` so they match by
    default; tests can mutate source files to simulate drift."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fake'\n")
    td_comp = tmp_path / "td_component"
    td_comp.mkdir()

    # MCP-side .tox sources.
    mcp_files = ["td_component/mcp_a.py", "td_component/mcp_b.py"]
    for rel in mcp_files:
        (tmp_path / rel).write_text(f"# {rel} content v1\n")
    mcp_hash = _compute_source_hash(tmp_path, mcp_files)
    (td_comp / ".tox-source-hash.json").write_text(
        json.dumps(
            {
                "tox_source_hash": mcp_hash,
                "built_at": "2026-05-18T00:00:00Z",
                "source_files": mcp_files,
            }
        )
    )

    # API-side .tox sources.
    api_files = ["td_component/api_a.py", "td_component/api_b.py"]
    for rel in api_files:
        (tmp_path / rel).write_text(f"# {rel} content v1\n")
    api_hash = _compute_source_hash(tmp_path, api_files)
    (td_comp / ".tox-api-source-hash.json").write_text(
        json.dumps(
            {
                "tox_source_hash": api_hash,
                "built_at": "2026-05-18T00:00:00Z",
                "source_files": api_files,
            }
        )
    )

    return tmp_path


def _mock_urlopen(release_json: dict, status: int = 200):
    """Patch ``urllib.request.urlopen`` to return a stub response."""

    class _StubResp:
        def __init__(self):
            self.status = status

        def read(self):
            return json.dumps(release_json).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    return mock.patch("urllib.request.urlopen", return_value=_StubResp())


# ---------------------------------------------------------------------------
# Version parsing + comparison
# ---------------------------------------------------------------------------


class TestVersionParsing:
    def test_v_prefix_stripped(self):
        assert _parse_version("v2.4.0") == (2, 4, 0)

    def test_compare_older(self):
        assert _compare_versions("2.4.0", "2.5.0") == "older"

    def test_compare_same(self):
        assert _compare_versions("2.5.0", "2.5.0") == "same"

    def test_compare_newer(self):
        assert _compare_versions("2.6.0", "2.5.0") == "newer"

    def test_prerelease_collapses_safely(self):
        # Don't crash on alpha/beta suffixes.
        assert _parse_version("v2.5.0-alpha.1") == (2, 5, 0, 0, 1)


# ---------------------------------------------------------------------------
# Hash function parity with build scripts
# ---------------------------------------------------------------------------


class TestHashCompat:
    def test_compute_matches_build_script_scheme(self, tmp_path):
        """Pin the NUL-separator scheme used by both build scripts."""
        files = ["a.py", "b.py"]
        for rel in files:
            (tmp_path / rel).write_text(f"content {rel}\n")

        # Manual reproduction of the scheme:
        # for rel: rel.encode + \x00 + bytes + \x00; then sha256.
        h = hashlib.sha256()
        for rel in files:
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update((tmp_path / rel).read_bytes())
            h.update(b"\x00")
        expected = h.hexdigest()

        actual = _compute_source_hash(tmp_path, files)
        assert actual == expected


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------


class TestToxFreshness:
    def test_clean_repo_both_fresh(self, fake_repo):
        with _mock_urlopen({"tag_name": "v2.5.0", "html_url": "https://x"}):
            result = check_for_updates(repo_root=fake_repo)
        assert result.tox["tdpilot-dpsk4.tox"]["hash_matches"] is True
        assert result.tox["tdpilot_API.tox"]["hash_matches"] is True
        assert not any(info["rebuild_needed"] for info in result.tox.values())

    def test_mutated_source_marks_tox_stale(self, fake_repo):
        # Modify one MCP source file post-hash-write.
        (fake_repo / "td_component/mcp_a.py").write_text("# changed\n")
        with _mock_urlopen({"tag_name": "v2.5.0", "html_url": "https://x"}):
            result = check_for_updates(repo_root=fake_repo)
        assert result.tox["tdpilot-dpsk4.tox"]["rebuild_needed"] is True
        assert "modified" in result.tox["tdpilot-dpsk4.tox"]["reason"]
        # API side unchanged.
        assert result.tox["tdpilot_API.tox"]["rebuild_needed"] is False

    def test_missing_hash_file_marks_stale(self, fake_repo):
        (fake_repo / "td_component/.tox-source-hash.json").unlink()
        with _mock_urlopen({"tag_name": "v2.5.0", "html_url": "https://x"}):
            result = check_for_updates(repo_root=fake_repo)
        assert result.tox["tdpilot-dpsk4.tox"]["rebuild_needed"] is True
        assert "missing" in result.tox["tdpilot-dpsk4.tox"]["reason"]


# ---------------------------------------------------------------------------
# Server-version comparison via mocked GitHub
# ---------------------------------------------------------------------------


class TestServerVersionFromGitHub:
    def test_advisory_says_update_when_remote_is_newer(self, fake_repo, monkeypatch):
        monkeypatch.setattr(uc, "CURRENT_VERSION", "2.4.0")
        with _mock_urlopen(
            {
                "tag_name": "v2.5.0",
                "html_url": "https://github.com/dreamrec/TDPilot_deepseekv4/releases/tag/v2.5.0",
            }
        ):
            result = check_for_updates(repo_root=fake_repo)
        assert result.server["has_update"] is True
        assert result.server["latest"] == "2.5.0"
        assert "Server update available" in result.advice
        assert "tdpilot-dpsk4" in result.advice

    def test_advisory_silent_when_up_to_date(self, fake_repo, monkeypatch):
        monkeypatch.setattr(uc, "CURRENT_VERSION", "2.5.0")
        with _mock_urlopen({"tag_name": "v2.5.0", "html_url": "https://x"}):
            result = check_for_updates(repo_root=fake_repo)
        assert result.server["has_update"] is False
        assert "Up to date" in result.advice

    def test_network_error_reports_check_failed_but_tox_still_checked(self, fake_repo, monkeypatch):
        import urllib.error

        def _raise(*a, **kw):
            raise urllib.error.URLError("network down")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        result = check_for_updates(repo_root=fake_repo)
        assert result.server.get("check_failed") is True
        assert "network down" in result.server["reason"]
        # Tox freshness still computed locally.
        assert result.tox["tdpilot-dpsk4.tox"]["hash_matches"] is True


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCacheTtl:
    def test_second_call_within_ttl_does_not_hit_github(self, fake_repo, monkeypatch):
        monkeypatch.setattr(uc, "CURRENT_VERSION", "2.4.0")
        call_count = {"n": 0}

        def _counting_urlopen(*a, **kw):
            call_count["n"] += 1

            class _StubResp:
                status = 200

                def read(self):
                    return json.dumps({"tag_name": "v2.5.0", "html_url": "x"}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return _StubResp()

        monkeypatch.setattr("urllib.request.urlopen", _counting_urlopen)

        check_for_updates(repo_root=fake_repo)
        check_for_updates(repo_root=fake_repo)
        assert call_count["n"] == 1, "second call should be served from cache"

    def test_clear_cache_forces_refetch(self, fake_repo, monkeypatch):
        monkeypatch.setattr(uc, "CURRENT_VERSION", "2.4.0")
        call_count = {"n": 0}

        def _counting_urlopen(*a, **kw):
            call_count["n"] += 1

            class _StubResp:
                status = 200

                def read(self):
                    return json.dumps({"tag_name": "v2.5.0", "html_url": "x"}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            return _StubResp()

        monkeypatch.setattr("urllib.request.urlopen", _counting_urlopen)

        check_for_updates(repo_root=fake_repo)
        clear_cache()
        check_for_updates(repo_root=fake_repo)
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Result-shape contract
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_to_dict_has_stable_keys(self, fake_repo):
        with _mock_urlopen({"tag_name": "v2.5.0", "html_url": "x"}):
            result = check_for_updates(repo_root=fake_repo)
        d = result.to_dict()
        assert set(d.keys()) == {"server", "tox", "advice", "checked_at"}
        assert "current" in d["server"]
        assert "tdpilot-dpsk4.tox" in d["tox"]
        assert "tdpilot_API.tox" in d["tox"]
