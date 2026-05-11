"""Regression tests for new-scope search exec mode (v2.3.1+).

Pre-fix the ``_exec_dat_text_scope`` and ``_exec_param_exprs_scope`` handlers
passed ``exec_mode=_tr._current_exec_mode()`` to TD's ``/api/exec`` endpoint,
which defaults to ``"restricted"`` per the env var. But the code templates
they generate reference ``Exception`` and ``isinstance`` by name — both
absent from restricted-mode globals — so every new-scope call crashed with::

    NameError: name 'Exception' is not defined

The templates are fully internal: user inputs are repr()-escaped Python
literals and no user code runs. They are safe to execute in ``"full"`` mode.
The fix pins ``exec_mode="full"`` on both handlers regardless of the global
env-var default.
"""

from __future__ import annotations

import pytest

# Import the top-level tool_registry first so the circular-import graph
# between ``tools_data`` and ``tool_registry`` finishes resolving before
# we reach into ``tools_data`` for the private helpers under test.
from td_mcp import tool_registry as registry
from td_mcp.registry import tools_data

_exec_dat_text_scope = tools_data._exec_dat_text_scope
_exec_param_exprs_scope = tools_data._exec_param_exprs_scope


@pytest.fixture
def patched_client(monkeypatch, td_client, mcp_ctx):
    """Bypass _get_client's isinstance(TDClient) check so RecordingTDClient
    can stand in. tools_data calls ``_tr._get_client(ctx)`` via the
    tool_registry module, so monkey-patching the registry attr is enough.
    """
    monkeypatch.setattr(registry, "_get_client", lambda _ctx: td_client)
    return mcp_ctx


@pytest.mark.asyncio
async def test_dat_text_scope_pins_full_exec_mode(patched_client, td_client):
    await _exec_dat_text_scope(patched_client, query="foo", path="/", limit=10)
    assert td_client.last_endpoint == "exec"
    assert td_client.last_body is not None
    assert td_client.last_body.get("exec_mode") == "full", (
        "dat_text scope must pin exec_mode='full' — restricted mode "
        "blocks Exception / isinstance / hasattr at runtime."
    )


@pytest.mark.asyncio
async def test_param_exprs_scope_pins_full_exec_mode(patched_client, td_client):
    await _exec_param_exprs_scope(patched_client, query="foo", path="/", limit=10)
    assert td_client.last_endpoint == "exec"
    assert td_client.last_body is not None
    assert td_client.last_body.get("exec_mode") == "full"


@pytest.mark.asyncio
async def test_dat_text_scope_passes_through_query_and_path(patched_client, td_client):
    """Sanity: the generated code embeds the query + path verbatim (repr-escaped),
    so callers can trust the wiring isn't dropping arguments."""
    await _exec_dat_text_scope(patched_client, query="needle", path="/project1", limit=5)
    code = td_client.last_body.get("code", "")
    assert "'needle'" in code
    assert "'/project1'" in code
