#!/usr/bin/env python3
"""Fail if versioned files disagree with src/td_mcp/__init__.__version__.

Run locally or in CI after bumping pyproject.toml. Prevents the v1.3.2/v1.3.4
drift problem that accumulated across plugin_README, docs, skills, and npm.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def canonical_version() -> str:
    text = (ROOT / "src" / "td_mcp" / "__init__.py").read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Could not find __version__ in src/td_mcp/__init__.py")
    return match.group(1)


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("Could not find version in pyproject.toml")
    return match.group(1)


def check_line(path: Path, pattern: str, expected: str, label: str, optional: bool = False) -> str | None:
    """Verify ``path`` contains a regex match equal to ``expected``.

    When ``optional=True``, a missing file is treated as a non-error
    (returns None). Used for free-form docs that the public repo no
    longer ships — the audit-2026-05-04 cleanup removed several .md
    files from the index but the script still references them so this
    check stays useful for the files that DO ship.
    """
    if not path.exists():
        if optional:
            return None
        return f"{label}: missing file {path}"
    text = path.read_text()
    match = re.search(pattern, text)
    if not match:
        return f"{label}: pattern not found in {path.relative_to(ROOT)}"
    actual = match.group(1)
    if actual != expected:
        return f"{label}: {path.relative_to(ROOT)} says {actual}, expected {expected}"
    return None


def check_json_version(path: Path, expected: str, label: str, optional: bool = False) -> str | None:
    if not path.exists():
        if optional:
            return None
        return f"{label}: missing file {path}"
    data = json.loads(path.read_text())
    actual = data.get("version")
    if actual != expected:
        return f"{label}: {path.relative_to(ROOT)} says {actual}, expected {expected}"
    return None


def main() -> int:
    expected = canonical_version()
    errors: list[str] = []

    py_version = pyproject_version()
    if py_version != expected:
        errors.append(f"pyproject.toml says {py_version}, expected {expected}")

    errors += [
        check_json_version(ROOT / "npm" / "package.json", expected, "npm/package.json"),
        check_json_version(ROOT / ".claude-plugin" / "plugin.json", expected, ".claude-plugin/plugin.json"),
        check_line(
            ROOT / ".claude-plugin" / "marketplace.json",
            r'"version"\s*:\s*"([^"]+)"',
            expected,
            ".claude-plugin/marketplace.json tdpilot plugin",
        ),
        # v1.6.5: API_VERSION is now lockstep with __version__.
        # Pre-v1.6.5 history: this constant was deliberately decoupled from the
        # package version, on the theory that the TD-side HTTP protocol version
        # only needs bumping when route shapes change. In practice the
        # decoupling caused two user-visible drift bugs (v1.6.3 panel showing
        # "TDPilot 1.5.3" because nobody had bumped API_VERSION across the v1.6
        # line; v1.6.4 silently shipped with API_VERSION still at "1.6.3"
        # because no CI gate caught a missing Edit). The panel renderer reads
        # API_VERSION directly, so users expect it to match the package version.
        # We choose the simpler invariant: API_VERSION == __version__ on every
        # release. If you legitimately need a TD-side protocol version distinct
        # from the package version, introduce a separate TD_PROTOCOL_VERSION
        # constant rather than re-decoupling this one.
        check_line(
            # PR-16 (v1.8.3): API_VERSION moved from the deleted god module
            # into the callbacks/_header.py split. tests/test_startup_sweep.py
            # reads the same path; keep them in sync.
            ROOT / "td_component" / "callbacks" / "_header.py",
            r'API_VERSION\s*=\s*"([^"]+)"',
            expected,
            "td_component/callbacks/_header.py API_VERSION",
        ),
        check_line(
            ROOT / "plugin_README.md",
            r"TDPilot v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "plugin_README.md header",
        ),
        check_line(
            # 2026-05-11: README header was stale at v2.1.5 across two
            # releases (v2.2.0 + v2.3.0) because it wasn't on this
            # enforcement list. Add it so the next release can't repeat.
            # Match pattern: ``# TDPilot — DeepSeek v4 · vX.Y.Z`` exactly.
            ROOT / "README.md",
            r"# TDPilot — DeepSeek v4 · v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "README.md header",
        ),
        check_line(
            ROOT / "docs" / "API_REFERENCE.md",
            r"Auto-generated from TDPilot v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "docs/API_REFERENCE.md header",
            optional=True,  # Audit 2026-05-04: free-form doc, gitignored.
        ),
        check_line(
            ROOT / "docs" / "MANUAL.md",
            r"# TDPilot v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "docs/MANUAL.md title",
        ),
        check_line(
            ROOT / "npm" / "README.md",
            r"# TDPilot v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "npm/README.md title",
        ),
        check_line(
            ROOT / "skills" / "tdpilot-dpsk4-core" / "SKILL.md",
            r"TDPilot DPSK4 Core v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "skills/tdpilot-dpsk4-core/SKILL.md",
        ),
        check_line(
            ROOT / "skills" / "tdpilot-dpsk4-production" / "SKILL.md",
            r"TDPilot DPSK4 Production v([0-9]+\.[0-9]+\.[0-9]+)",
            expected,
            "skills/tdpilot-dpsk4-production/SKILL.md",
        ),
    ]

    errors = [e for e in errors if e]

    if errors:
        print(f"Canonical version (src/td_mcp/__init__.py): {expected}")
        print("Version drift detected:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(f"All versioned files are in sync at v{expected}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
