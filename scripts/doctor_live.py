#!/usr/bin/env python3
"""TDPilot standalone — doctor for a LIVE install (Phase 5.1).

Probes the most common "why isn't it working" failure modes for a
running ``tdpilot_API.tox``: webserver reachable, API key on disk,
external brains discoverable, memory + user-tool dirs healthy, and
(optionally) DeepSeek key actually valid.

Most checks are filesystem-based and offline — they catch the bulk
of install issues. The ``--deep`` flag adds one optional network
probe to DeepSeek to verify the key is accepted, which costs a
fraction of a cent and a couple hundred ms. Skip ``--deep`` in CI.

Usage::

    python3 scripts/doctor_live.py             # default: all offline checks
    python3 scripts/doctor_live.py --deep      # also probe DeepSeek
    python3 scripts/doctor_live.py --json      # machine-readable output
    python3 scripts/doctor_live.py --url http://127.0.0.1:9988/  # alt port

Exit code: 0 if no ``fail`` results, 1 otherwise. ``warn`` doesn't
fail the run — those are advisory ("you have no external brains
installed, knowledge_search will only cover bundled entries").
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:9987/"
DEFAULT_CONFIG = Path.home() / ".tdpilot-api" / "config.json"
DEFAULT_MEMORY_DIR = Path.home() / ".tdpilot-api" / "memory"
DEFAULT_USER_TOOLS_DIR = Path.home() / ".tdpilot-api" / "tools"
DEFAULT_BRAINS_ROOTS = (
    Path.home() / ".tdpilot" / "data" / "normalized",
    Path.home() / ".tdpilot-dpsk4" / "data" / "normalized",
    Path.home() / ".tdpilot-api" / "data" / "normalized",
)

Status = str  # "pass" | "warn" | "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    message: str
    fix: str = ""


@dataclass(frozen=True)
class CheckSpec:
    name: str
    fn: Callable[..., CheckResult]
    description: str


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_webserver_up(url: str) -> CheckResult:
    """``GET /health`` on the standalone webserver. The .tox exposes
    this as a thin liveness probe — empty 200 body or
    ``{"ok": true}`` — both count as up.
    """
    target = url.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.URLError as exc:
        return CheckResult(
            name="webserver_up",
            status="fail",
            message=f"Could not reach {target}: {exc.reason}",
            fix=(
                "Drag the tdpilot_API.tox out of /project1 and back in to "
                "restart the webserver. Or check that the COMP's port "
                "parameter matches --url."
            ),
        )
    except (TimeoutError, OSError) as exc:
        return CheckResult(
            name="webserver_up",
            status="fail",
            message=f"I/O error reaching {target}: {exc}",
            fix="Verify TouchDesigner is running and the .tox is loaded.",
        )

    return CheckResult(
        name="webserver_up",
        status="pass",
        message=f"OK at {target} (response: {body[:80] or 'empty'})",
    )


def _check_api_key_set(config_path: Path = DEFAULT_CONFIG) -> CheckResult:
    """Look for a non-empty ``api_key`` in the standalone's config
    JSON. The COMP's ``Save Key to ~/.tdpilot-api/`` pulse writes
    this file with 0o600 permissions on POSIX.
    """
    if not config_path.is_file():
        return CheckResult(
            name="api_key_set",
            status="fail",
            message=f"Config file missing at {config_path}",
            fix=(
                "Paste your DeepSeek key into the COMP's API Key parameter "
                "and pulse Save Key to ~/.tdpilot-api/."
            ),
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="api_key_set",
            status="fail",
            message=f"Could not parse config: {exc}",
            fix=f"Delete {config_path} and re-save your key from the COMP.",
        )
    key = data.get("api_key", "")
    if not isinstance(key, str) or not key.strip():
        return CheckResult(
            name="api_key_set",
            status="fail",
            message="config.json present but api_key is empty",
            fix="Pulse Save Key on the COMP after pasting your DeepSeek key.",
        )
    suffix = key[-4:] if len(key) >= 4 else "????"
    return CheckResult(
        name="api_key_set",
        status="pass",
        message=f"key found (ends ...{suffix})",
    )


def _check_api_key_valid(
    config_path: Path = DEFAULT_CONFIG, base_url: str = "https://api.deepseek.com"
) -> CheckResult:
    """Cheap probe: send a 1-token max request to DeepSeek's
    ``/v1/messages`` endpoint. A 401 means the key is rejected; any
    other 4xx/5xx still counts as the key being recognised (the
    error is the body's structure, not auth).
    """
    if not config_path.is_file():
        return CheckResult(
            name="api_key_valid",
            status="warn",
            message="skipped (no config; run api_key_set first)",
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CheckResult(
            name="api_key_valid",
            status="warn",
            message="skipped (config unparseable)",
        )
    key = data.get("api_key", "")
    if not isinstance(key, str) or not key.strip():
        return CheckResult(
            name="api_key_valid",
            status="warn",
            message="skipped (no key)",
        )

    target = base_url.rstrip("/") + "/anthropic/v1/messages"
    body = json.dumps(
        {
            "model": data.get("model", "deepseek-v4-pro"),
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "x"}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        target,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp.read()
        return CheckResult(
            name="api_key_valid",
            status="pass",
            message="DeepSeek accepted the key",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return CheckResult(
                name="api_key_valid",
                status="fail",
                message="DeepSeek returned 401 — key rejected",
                fix=(
                    "Generate a fresh key at platform.deepseek.com and re-paste "
                    "it into the COMP's API Key parameter."
                ),
            )
        # Any other HTTP code is "key was processed"; the call shape
        # may have been wrong but auth passed.
        return CheckResult(
            name="api_key_valid",
            status="pass",
            message=f"key accepted (probe got HTTP {exc.code}, that's fine)",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CheckResult(
            name="api_key_valid",
            status="warn",
            message=f"could not reach DeepSeek: {exc}",
            fix="Network down or proxy required — try again later.",
        )


def _check_external_brains(roots: tuple[Path, ...] = DEFAULT_BRAINS_ROOTS) -> CheckResult:
    """List installed external corpora (brain.db OR pages.jsonl). This
    is a ``warn``, not a ``fail`` — knowledge_search still works
    against bundled entries with no external corpora. Surface the
    count so the user knows what they have.
    """
    seen: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name in seen:
                    continue
                if any(entry.glob("*brain.db")):
                    seen[entry.name] = "sqlite"
                elif (entry / "pages.jsonl").is_file():
                    seen[entry.name] = "jsonl"
        except OSError:
            continue
    if not seen:
        return CheckResult(
            name="external_brains",
            status="warn",
            message="no external brains installed",
            fix=(
                "Run ``npx tdpilot-dpsk4 brains add derivative`` to install "
                "the official-docs brain. Optional but recommended."
            ),
        )
    summary = ", ".join(f"{name}({kind})" for name, kind in sorted(seen.items()))
    return CheckResult(
        name="external_brains",
        status="pass",
        message=f"{len(seen)} corpora: {summary}",
    )


def _check_memory_dir(memory_dir: Path = DEFAULT_MEMORY_DIR) -> CheckResult:
    """Memory dir must be readable AND writable — the agent saves
    files into it via memory_save.
    """
    if not memory_dir.exists():
        # Auto-created on first memory_save; not a fail.
        return CheckResult(
            name="memory_dir",
            status="pass",
            message=f"{memory_dir} doesn't exist yet (will be auto-created)",
        )
    if not memory_dir.is_dir():
        return CheckResult(
            name="memory_dir",
            status="fail",
            message=f"{memory_dir} exists but is not a directory",
            fix=f"Delete {memory_dir} or move it aside; the runtime will recreate.",
        )
    if not os.access(memory_dir, os.R_OK | os.W_OK):
        return CheckResult(
            name="memory_dir",
            status="fail",
            message=f"{memory_dir} is not readable+writable by the current user",
            fix=f"Check ownership: chown $USER {memory_dir}",
        )
    files = list(memory_dir.glob("*.md"))
    return CheckResult(
        name="memory_dir",
        status="pass",
        message=f"{memory_dir} OK ({len(files)} memory file(s))",
    )


def _check_user_tools(tools_dir: Path = DEFAULT_USER_TOOLS_DIR) -> CheckResult:
    """Each ``.py`` in the user-tools dir should be importable. We
    don't run the validator (that needs the runtime); we just
    confirm syntax + imports. Empty / missing dir is not a fail.
    """
    if not tools_dir.exists() or not tools_dir.is_dir():
        return CheckResult(
            name="user_tools",
            status="pass",
            message=f"{tools_dir} not present (no user tools — fine)",
        )
    py_files = sorted(tools_dir.glob("*.py"))
    if not py_files:
        return CheckResult(
            name="user_tools",
            status="pass",
            message=f"{tools_dir} present, no user tools",
        )
    bad: list[tuple[str, str]] = []
    for path in py_files:
        try:
            text = path.read_text(encoding="utf-8")
            compile(text, str(path), "exec")
        except (OSError, SyntaxError) as exc:
            bad.append((path.name, str(exc).split("\n", 1)[0]))
    if bad:
        offenders = "; ".join(f"{n}: {msg}" for n, msg in bad)
        return CheckResult(
            name="user_tools",
            status="fail",
            message=f"{len(bad)} user tool(s) failed compile: {offenders}",
            fix=f"Fix the syntax errors in {tools_dir} or move the offending files out.",
        )
    return CheckResult(
        name="user_tools",
        status="pass",
        message=f"{len(py_files)} user tool(s) compile cleanly",
    )


# ---------------------------------------------------------------------------
# Driver — runs the registry, formats output
# ---------------------------------------------------------------------------


def _build_checks(url: str, deep: bool) -> list[Callable[[], CheckResult]]:
    """Return the list of checks to run, in display order."""
    checks: list[Callable[[], CheckResult]] = [
        lambda: _check_webserver_up(url),
        _check_api_key_set,
        _check_external_brains,
        _check_memory_dir,
        _check_user_tools,
    ]
    if deep:
        checks.insert(2, _check_api_key_valid)
    return checks


def run_all_checks(url: str = DEFAULT_URL, deep: bool = False) -> list[CheckResult]:
    """Run the full check registry and return a list of results.

    Pure-Python — used both by the CLI driver below AND by the
    extension's verify_setup pulse handler.
    """
    out: list[CheckResult] = []
    for check in _build_checks(url, deep):
        try:
            out.append(check())
        except Exception as exc:  # noqa: BLE001
            out.append(
                CheckResult(
                    name=getattr(check, "__name__", "check"),
                    status="fail",
                    message=f"check raised {type(exc).__name__}: {exc}",
                )
            )
    return out


def format_terminal(results: list[CheckResult]) -> str:
    """Pretty terminal output. Colourless so it's universal."""
    icons = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = [
        "TDPilot standalone — install doctor",
        "=" * 36,
    ]
    for r in results:
        lines.append(f"{icons.get(r.status, '??')}  {r.name}: {r.message}")
        if r.fix and r.status != "pass":
            lines.append(f"      → fix: {r.fix}")
    n_fail = sum(1 for r in results if r.status == "fail")
    n_warn = sum(1 for r in results if r.status == "warn")
    lines.append("")
    lines.append(f"summary: {n_fail} fail, {n_warn} warn, {len(results) - n_fail - n_warn} pass")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Standalone webserver base URL.")
    parser.add_argument("--deep", action="store_true", help="Also probe DeepSeek with the saved key.")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    args = parser.parse_args(argv)

    results = run_all_checks(url=args.url, deep=args.deep)

    if args.json:
        print(
            json.dumps(
                [{"name": r.name, "status": r.status, "message": r.message, "fix": r.fix} for r in results],
                indent=2,
            )
        )
    else:
        print(format_terminal(results))

    return 1 if any(r.status == "fail" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
