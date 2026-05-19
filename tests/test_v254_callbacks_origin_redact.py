"""v2.5.4 audit-fix tests — origin allowlist + traceback redaction on MCP router.

PR-#53 closed the chat-pipe-side C-1 (default-secure auth in autostart.py).
v2.5.4 adds two MCP-side defense-in-depth helpers, both in
``td_component/callbacks/_header.py``:

  * C-1 part B — ``_is_origin_allowed(origin)``: rejects non-loopback
    Origin headers from browser tabs. Sits BELOW the existing
    Sec-Fetch-Site check in ``router.py`` so curl + non-browser MCP
    clients (which don't send Origin) still pass.
  * M-1 — ``_redact_paths(s)``: strips ``$HOME`` + TDPilot config-dir
    paths from response strings (mainly tracebacks in the 500 path).
    Mirrors the chat-pipe-side ``tdpilot_api_config.redact_paths``.

The byte-equivalence baseline at
``tests/fixtures/mcp_webserver_callbacks_v1.8.2_baseline.py`` is
refreshed in the same PR — see test_composer_byte_equivalence.py.
"""

from __future__ import annotations

import os

import pytest
from _callbacks_loader import load_callbacks_module


@pytest.fixture(scope="module")
def cbk():
    return load_callbacks_module()


# ---------------------------------------------------------------------------
# _is_origin_allowed — C-1 part B
# ---------------------------------------------------------------------------


class TestIsOriginAllowed:
    def test_empty_origin_is_allowed(self, cbk):
        """Non-browser MCP clients (curl, npx tdpilot-dpsk4) don't send
        Origin — they MUST still pass."""
        assert cbk._is_origin_allowed("") is True
        assert cbk._is_origin_allowed(None) is True

    def test_null_origin_is_allowed(self, cbk):
        """file:// + sandboxed iframes emit 'null'. Treat as same-origin."""
        assert cbk._is_origin_allowed("null") is True

    def test_localhost_origins_allowed(self, cbk):
        for o in (
            "http://localhost",
            "http://localhost:9985",
            "http://127.0.0.1:9985",
            "https://127.0.0.1:9985",
            "http://[::1]:9985",
            "http://localhost:3000/foo",  # trailing path tolerated
        ):
            assert cbk._is_origin_allowed(o) is True, f"should allow {o!r}"

    def test_cross_origin_browser_tab_rejected(self, cbk):
        for o in (
            "https://attacker.example.com",
            "http://evil.local",
            "https://192.168.1.50:8080",
        ):
            assert cbk._is_origin_allowed(o) is False, f"should reject {o!r}"

    def test_malformed_origin_rejected(self, cbk):
        """Defensive: an Origin with no scheme but a foreign host is
        still rejected. A bracket-less IPv6-looking string with no
        closing bracket is rejected as malformed."""
        assert cbk._is_origin_allowed("[no-close") is False

    def test_case_insensitive_host_match(self, cbk):
        assert cbk._is_origin_allowed("http://LOCALHOST:9985") is True
        assert cbk._is_origin_allowed("HTTPS://127.0.0.1") is True


# ---------------------------------------------------------------------------
# _redact_paths — M-1
# ---------------------------------------------------------------------------


class TestRedactPaths:
    def test_home_path_redacted(self, cbk):
        home = os.path.expanduser("~")
        s = f"Traceback (most recent call last):\n  File '{home}/foo.py', line 12, in <module>"
        out = cbk._redact_paths(s)
        assert home not in out
        assert "~/foo.py" in out

    def test_tdpilot_dpsk4_config_dir_redacted(self, cbk):
        home = os.path.expanduser("~")
        target = os.path.join(home, ".tdpilot-dpsk4")
        s = f"Error reading {target}/config.json"
        out = cbk._redact_paths(s)
        assert "<TDPILOT_DPSK4_HOME>" in out
        assert ".tdpilot-dpsk4" not in out.replace("<TDPILOT_DPSK4_HOME>", "")

    def test_tdpilot_api_legacy_config_dir_redacted(self, cbk):
        home = os.path.expanduser("~")
        target = os.path.join(home, ".tdpilot-api")
        s = f"failed: {target}/snapshots/foo.scoped.json"
        out = cbk._redact_paths(s)
        assert "<TDPILOT_API_HOME>" in out
        assert ".tdpilot-api" not in out.replace("<TDPILOT_API_HOME>", "")

    def test_non_string_input_returned_unchanged(self, cbk):
        assert cbk._redact_paths(None) is None
        assert cbk._redact_paths(42) == 42
        assert cbk._redact_paths([]) == []

    def test_empty_string_returned_unchanged(self, cbk):
        assert cbk._redact_paths("") == ""

    def test_no_home_no_change(self, cbk):
        s = "Some error that doesn't mention the user home dir"
        assert cbk._redact_paths(s) == s

    def test_config_dirs_redacted_before_home(self, cbk):
        """The config-dir replacements run BEFORE the bare home
        redaction so the TDPILOT-specific placeholders win, giving a
        more useful error message in the response.
        """
        home = os.path.expanduser("~")
        s = os.path.join(home, ".tdpilot-dpsk4", "config.json")
        out = cbk._redact_paths(s)
        # Should be TDPILOT_DPSK4_HOME/config.json, not ~/.tdpilot-dpsk4/config.json
        assert out.startswith("<TDPILOT_DPSK4_HOME>")
