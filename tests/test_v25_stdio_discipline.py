"""Tests for v2.5.6 — npm wrapper stdout/stderr separation contract.

The MCP server uses ``stdio`` transport: stdout is reserved for
JSON-RPC traffic and any rogue text breaks the protocol. This contract
test pins that importing the ``td_mcp`` package and its core submodules
produces ZERO bytes on stdout. Stderr is unconstrained.

Upstream ``dreamrec/TDPilot`` v1.6.12 fixed a regression where Python
logging went to stdout instead of stderr, which broke Claude Desktop /
Claude Code MCP framing. We pin the no-stdout-leak contract here so any
future change that adds a top-level print to a hot-path module gets
caught in CI rather than at the user's TD startup.
"""

from __future__ import annotations

import importlib
import io
import sys

import pytest

# Submodules that MUST be import-safe under stdio (i.e. silent on
# stdout). The MCP server imports each of these during startup; any
# stdout output here corrupts the JSON-RPC handshake.
HOT_PATH_MODULES = (
    "td_mcp",
    "td_mcp.audit",
    "td_mcp.auth_bootstrap",
    "td_mcp.capabilities",
    "td_mcp.errors",
    "td_mcp.observability",
    "td_mcp.observability.activity_log",
    "td_mcp.release_gates",
    "td_mcp.services",
    "td_mcp.telemetry",
)


@pytest.mark.parametrize("module_name", HOT_PATH_MODULES)
def test_import_does_not_pollute_stdout(module_name: str, monkeypatch):
    """Each hot-path module imports cleanly with no bytes to stdout."""
    # Force a fresh import so we observe the actual top-level side effects.
    for cached in list(sys.modules):
        if cached == module_name or cached.startswith(module_name + "."):
            del sys.modules[cached]

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)
    monkeypatch.setattr(sys, "stderr", captured_err)

    importlib.import_module(module_name)

    assert captured_out.getvalue() == "", (
        f"{module_name} polluted stdout on import: {captured_out.getvalue()!r}. "
        "MCP server uses stdio transport; rogue stdout breaks the protocol. "
        "Route diagnostics to sys.stderr instead."
    )


def test_observability_record_activity_does_not_print_to_stdout(monkeypatch):
    """The activity-log record_activity helper is on the MCP hot path
    (called from _forward on every tool dispatch). Pin no stdout leak."""
    from td_mcp.observability import record_activity, reset_global_ring

    reset_global_ring()
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)
    monkeypatch.setattr(sys, "stderr", captured_err)

    record_activity(
        tool_name="td_get_info",
        args={"path": "/project1"},
        duration_ms=42,
        result_kind="ok",
    )

    assert captured_out.getvalue() == "", f"record_activity polluted stdout: {captured_out.getvalue()!r}"


def _print_calls_lacking_stderr_routing(source: str) -> list[tuple[int, str]]:
    """AST-based scan: find every ``print(...)`` call (any nesting) that
    does NOT pass ``file=sys.stderr`` (or any value to ``file=`` other
    than the bare default).

    Returns a list of ``(lineno, snippet)`` tuples for offending calls.
    """
    import ast

    tree = ast.parse(source)
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # print can be `print` or `builtins.print`. We only care about bare.
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            has_file_kw = any(kw.arg == "file" for kw in node.keywords)
            if not has_file_kw:
                # Snippet: the source line containing the call.
                src_line = source.splitlines()[node.lineno - 1].strip()
                offenders.append((node.lineno, src_line))
    return offenders


def test_no_print_to_stdout_in_hot_path_source():
    """Hot-path modules (loaded during MCP server startup or invoked from
    ``_forward``) must NOT contain ``print(...)`` calls without
    ``file=sys.stderr``. AST-based scan — robust to multi-line print
    formatting (the previous heuristic flagged the opening line of
    multi-line stderr-targeted prints).

    CLI subcommands in ``td_mcp.server`` (doctor, mcp-config, autopin)
    are intentionally excluded — those run via explicit ``tdpilot
    <subcommand>`` invocations, NOT under stdio MCP transport, so
    stdout is the right channel for their user-facing output.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    hot_paths = [
        repo_root / "src/td_mcp/observability/__init__.py",
        repo_root / "src/td_mcp/observability/activity_log.py",
        repo_root / "src/td_mcp/auth_bootstrap.py",
        repo_root / "src/td_mcp/release_gates.py",
        repo_root / "src/td_mcp/registry/tools_observability.py",
    ]
    offenders: list[str] = []
    for path in hot_paths:
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        for lineno, snippet in _print_calls_lacking_stderr_routing(source):
            offenders.append(f"{path.name}:{lineno}: {snippet}")
    assert not offenders, (
        "print() to stdout (no file=sys.stderr) in hot-path modules:\n"
        + "\n".join(offenders)
        + "\n\nRoute to stderr via `print(..., file=sys.stderr)` "
        "or move into a CLI-subcommand function."
    )
