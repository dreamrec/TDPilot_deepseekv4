"""Tests for v2.5.8 — `td_get_traces` MCP tool.

The tool reads the chat-pipe's per-turn JSONL traces. Tests construct a
synthetic traces directory with known content and verify newest-first
ordering, limit clamping, days_back filtering, and graceful behavior
when the directory is missing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from td_mcp.lifecycle.traces import (
    iter_jsonl_files as _iter_jsonl_files,
)
from td_mcp.lifecycle.traces import (
    read_last_n_jsonl as _read_last_n_jsonl,
)


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    """Build a synthetic traces dir with three days of records."""
    today = datetime.now(timezone.utc).date()
    for offset in (0, 1, 2):  # today, yesterday, two-days-ago
        day = today - timedelta(days=offset)
        path = tmp_path / f"{day.isoformat()}.jsonl"
        # 3 records per day, marked with the offset for traceability.
        lines = [json.dumps({"turn_id": f"t-{offset}-{i}", "day_offset": offset}) for i in range(3)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# _iter_jsonl_files — date filtering
# ---------------------------------------------------------------------------


class TestIterJsonlFiles:
    def test_returns_files_within_days_back(self, traces_dir):
        files = _iter_jsonl_files(traces_dir, days_back=7)
        assert len(files) == 3
        # Newest first (sorted reverse alphabetically since dates).
        assert files[0].stem >= files[1].stem >= files[2].stem

    def test_excludes_files_outside_window(self, traces_dir):
        # Add an old file (40 days back) — should NOT be returned.
        old_day = (datetime.now(timezone.utc) - timedelta(days=40)).date()
        (traces_dir / f"{old_day.isoformat()}.jsonl").write_text("{}\n")
        files = _iter_jsonl_files(traces_dir, days_back=7)
        assert len(files) == 3  # still 3 — the 40-day-old file excluded

    def test_missing_dir_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "no_such_dir"
        assert _iter_jsonl_files(nonexistent, days_back=7) == []

    def test_ignores_malformed_filenames(self, traces_dir):
        # A file that doesn't match YYYY-MM-DD is silently skipped.
        (traces_dir / "garbage.jsonl").write_text("{}\n")
        files = _iter_jsonl_files(traces_dir, days_back=7)
        assert all("garbage" not in str(p) for p in files)


# ---------------------------------------------------------------------------
# _read_last_n_jsonl — newest-first + limit + skip-bad-lines
# ---------------------------------------------------------------------------


class TestReadLastNJsonl:
    def test_returns_records_newest_first(self, traces_dir):
        files = _iter_jsonl_files(traces_dir, days_back=7)
        records = _read_last_n_jsonl(files, limit=20)
        # 3 days * 3 records = 9 total.
        assert len(records) == 9
        # First file is today's; reading reverse → last line of today
        # is record-2, then record-1, record-0; then yesterday's
        # record-2, etc.
        assert records[0] == {"turn_id": "t-0-2", "day_offset": 0}
        assert records[1] == {"turn_id": "t-0-1", "day_offset": 0}

    def test_limit_clamps_total(self, traces_dir):
        files = _iter_jsonl_files(traces_dir, days_back=7)
        records = _read_last_n_jsonl(files, limit=2)
        assert len(records) == 2

    def test_skips_malformed_json(self, traces_dir):
        # Mix bad lines into today's file.
        today_file = next(p for p in traces_dir.glob("*.jsonl") if p.read_text().count("{") != 0)
        original = today_file.read_text(encoding="utf-8")
        today_file.write_text(
            original + "not-json-line\n" + json.dumps({"turn_id": "valid"}) + "\n",
            encoding="utf-8",
        )
        files = _iter_jsonl_files(traces_dir, days_back=7)
        records = _read_last_n_jsonl(files, limit=20)
        # The valid record appears; the malformed line is skipped.
        ids = [r.get("turn_id") for r in records]
        assert "valid" in ids
        assert "not-json-line" not in ids


# Tool-registration coverage lives in:
#   * tests/test_tools_schema_snapshot.py — pins td_get_traces in the snapshot
#   * tests/test_tools_contract.py        — pins decorator count vs manifest
# We deliberately don't import the registered wrapper here because the
# registry module participates in the @mcp intentional-cycle pattern and
# top-level test imports of registry functions trip the partial-init.
