"""Trace-reader helpers — v2.5.8.

Pure-Python (no registry / tool_registry imports). Lives outside the
registry submodule namespace so unit tests can import the helpers
without tripping the registry's intentional-cycle import path.

The chat-pipe writes per-turn JSONL traces to
``~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl`` (one record per
completed agent turn). The MCP tool ``td_get_traces`` wraps these
helpers to expose trace history to upstream agent callers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TRACES_DIR = Path.home() / ".tdpilot-api" / "traces"


def iter_jsonl_files(traces_dir: Path, days_back: int) -> list[Path]:
    """Return ``*.jsonl`` files from the last ``days_back`` days,
    newest-first. Filenames that don't parse as ``YYYY-MM-DD.jsonl``
    are silently skipped."""
    if not traces_dir.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    out: list[Path] = []
    for path in sorted(traces_dir.glob("*.jsonl"), reverse=True):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day >= cutoff:
            out.append(path)
    return out


def read_last_n_jsonl(paths: list[Path], limit: int) -> list[dict[str, Any]]:
    """Read up to ``limit`` records from the newest files, newest-first.

    Tail-reads each file to avoid loading multi-MB files into memory
    when the caller only wants the last N turns. Malformed JSON lines
    are silently skipped (one bad record shouldn't blind the caller
    to the rest of the file).
    """
    out: list[dict[str, Any]] = []
    for path in paths:
        if len(out) >= limit:
            break
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
    return out


__all__ = [
    "DEFAULT_TRACES_DIR",
    "iter_jsonl_files",
    "read_last_n_jsonl",
]
