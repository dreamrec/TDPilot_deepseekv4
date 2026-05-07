#!/usr/bin/env python3
"""Single source of truth for the standalone tool count.

Phase 5.3 of the standalone implementation plan.

The standalone agent's tool count is defined exactly once: it's
``len(TOOL_SCHEMAS)`` in ``td_component/tdpilot_api_schema_defs.py``.
Every user-facing surface that mentions the count (README badge,
README prose, MANUAL tables) drifts whenever a tool gets added or
removed. This script regenerates those mentions from the canonical
source, idempotently.

Out of scope: the CLI variant's ``103`` references (npm, plugin_README,
.claude-plugin, skills/tdpilot-dpsk4-*). Those are managed through
``EXPECTED_MIN_TOOL_COUNT`` in ``src/td_mcp/release_gates.py`` and
the rules documented in the user's global CLAUDE.md.

Usage::

    python3 scripts/sync_counts.py              # apply updates in place
    python3 scripts/sync_counts.py --check      # CI gate: exit non-zero on drift

The ``--check`` mode prints the offending file + line for each
mismatch and returns 1 if any drift exists, 0 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_canonical_count() -> int:
    """Return ``len(TOOL_SCHEMAS)`` from the schema-defs module.

    Imported via ``importlib`` so this script doesn't depend on
    ``td_component`` being on ``sys.path`` for the caller.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tdpilot_api_schema_defs",
        REPO_ROOT / "td_component" / "tdpilot_api_schema_defs.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return len(module.TOOL_SCHEMAS)


@dataclass(frozen=True)
class Replacement:
    """One regex-driven substitution.

    ``pattern`` MUST capture exactly one group: the count substring.
    The script swaps that group for the new count and keeps everything
    else byte-identical, so prose / tables / badge URLs that surround
    the count remain untouched even if their wording shifts.
    """

    relpath: str
    pattern: re.Pattern[str]
    description: str


# Each replacement targets a specific mention of the standalone count.
# Patterns are deliberately verbose — surrounding context must match
# exactly so we don't sweep up a CLI 103 by accident.
REPLACEMENTS: tuple[Replacement, ...] = (
    Replacement(
        "README.md",
        re.compile(r"(?P<prefix>tools-)(?P<count>\d+)(?P<suffix>%20%28standalone%29)"),
        "shields.io tools badge — standalone segment",
    ),
    Replacement(
        "README.md",
        re.compile(r"(?P<prefix>\| \*\*Tools\*\* \| )(?P<count>\d+)(?P<suffix> curated for in-TD use)"),
        "comparison table — standalone Tools row",
    ),
    Replacement(
        "README.md",
        re.compile(r"(?P<prefix>The standalone has )(?P<count>\d+)(?P<suffix> tools that cover)"),
        "README narrative — standalone tool count",
    ),
    Replacement(
        "docs/MANUAL.md",
        re.compile(r"(?P<prefix>\| \*\*Tool surface\*\* \| )(?P<count>\d+)(?P<suffix> tools \| )"),
        "MANUAL tool-surface table",
    ),
    Replacement(
        "docs/MANUAL.md",
        re.compile(r"(?P<prefix>\| \*\*Tool count\*\* \| )(?P<count>\d+)(?P<suffix> \| )"),
        "MANUAL tool-count comparison row",
    ),
    Replacement(
        "docs/MANUAL.md",
        re.compile(r"(?P<prefix>^)(?P<count>\d+)(?P<suffix> tools across )", re.MULTILINE),
        "MANUAL category-overview prose",
    ),
)


def find_drift(canonical_count: int) -> list[tuple[Replacement, str, int]]:
    """Return the list of replacements whose current value is stale.

    Each entry: (replacement, current_count_str, line_number).
    """
    drift: list[tuple[Replacement, str, int]] = []
    for r in REPLACEMENTS:
        path = REPO_ROOT / r.relpath
        if not path.is_file():
            print(f"warning: missing file {r.relpath}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        match = r.pattern.search(text)
        if match is None:
            print(
                f"warning: pattern not matched in {r.relpath}: {r.description}",
                file=sys.stderr,
            )
            continue
        current = match.group("count")
        if current != str(canonical_count):
            line_no = text.count("\n", 0, match.start()) + 1
            drift.append((r, current, line_no))
    return drift


def apply_replacements(canonical_count: int) -> int:
    """Rewrite each replacement target in place. Returns the number of
    files actually changed.
    """
    target = str(canonical_count)
    changed_files: set[str] = set()
    for r in REPLACEMENTS:
        path = REPO_ROOT / r.relpath
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")

        def _sub(m: re.Match[str]) -> str:
            # m.group(0) keeps prefix/suffix exactly; we only swap the
            # captured count.
            return m.group("prefix") + target + m.group("suffix")

        new_text, replaced = r.pattern.subn(_sub, text)
        if replaced and new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed_files.add(r.relpath)
    return len(changed_files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report drift without modifying files; exit 1 on any mismatch.",
    )
    args = parser.parse_args()

    canonical = read_canonical_count()

    if args.check:
        drift = find_drift(canonical)
        if not drift:
            print(f"sync_counts: OK — every mention matches len(TOOL_SCHEMAS)={canonical}.")
            return 0
        for r, current, line_no in drift:
            print(
                f"DRIFT: {r.relpath}:{line_no} — has {current}, canonical {canonical} ({r.description})",
                file=sys.stderr,
            )
        print(
            f"sync_counts: {len(drift)} drift sites. Run `python3 scripts/sync_counts.py` to fix.",
            file=sys.stderr,
        )
        return 1

    changed = apply_replacements(canonical)
    if changed:
        print(f"sync_counts: updated {changed} file(s) to canonical count {canonical}.")
    else:
        print(f"sync_counts: every mention already at canonical count {canonical}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
