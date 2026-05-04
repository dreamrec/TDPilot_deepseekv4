"""Shared pytest fixtures for TDPilot tests.

Centralizes the patterns that were previously re-defined in half a dozen
files: a fake TDClient that records requests, a ServiceContainer factory,
and a mock MCP Context wired to a lifespan state dict. Also handles
sys.path setup for the standalone tdpilot_API tests under
``td_component/`` and provides the canned-urlopen helper their tests use.

Keep this file small — specialized fakes (POP-inspect, brain-search, etc.)
still belong in the test files that own them. These fixtures cover the
shared ~80% case: "give me something that quacks like TDClient + Context".
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# --------------------------------------------------------------------------
# Standalone tdpilot_API path setup. Conftest is imported by pytest before
# any test module, so this fires early enough that
# ``from tdpilot_api_X import ...`` at the top of each test file resolves.
# Existing tests still inline their own sys.path.insert; this is additive.
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TD_COMPONENT = _REPO_ROOT / "td_component"
if str(_TD_COMPONENT) not in sys.path:
    sys.path.insert(0, str(_TD_COMPONENT))


@dataclass
class RecordingTDClient:
    """A TDClient-shaped stub that records every ``request()`` call.

    Configure ``responses`` as a dict of ``endpoint -> response`` to return
    canned payloads, or pass a ``handler`` callable for dynamic responses.
    Unconfigured endpoints return ``{}``.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    handler: Callable[[str, dict[str, Any]], Any] | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    last_body: dict[str, Any] | None = None
    last_endpoint: str | None = None

    async def request(self, endpoint: str, body: dict[str, Any] | None = None) -> Any:
        body = body or {}
        self.calls.append((endpoint, body))
        self.last_body = body
        self.last_endpoint = endpoint
        if self.handler is not None:
            return self.handler(endpoint, body)
        if endpoint in self.responses:
            return self.responses[endpoint]
        return {}

    @property
    def last_code(self) -> str | None:
        """For 'exec' endpoint bodies, return the 'code' field."""
        return self.last_body.get("code") if self.last_body else None

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def close(self) -> None:
        return None


@pytest.fixture
def td_client() -> RecordingTDClient:
    """Provide a fresh RecordingTDClient for each test."""
    return RecordingTDClient()


@pytest.fixture
def service_container(td_client: RecordingTDClient):
    """A ServiceContainer with just enough wiring for tool-registry tests.

    Built lazily so tests that don't touch services don't pay the import cost.
    """
    from td_mcp.services import ServiceContainer  # local import: avoids circulars

    return ServiceContainer(
        td_client=td_client,
        technique_store=None,
        preference_store=None,
    )


@pytest.fixture
def mcp_ctx(service_container):
    """A Context-shaped SimpleNamespace pointing at the ServiceContainer."""
    lifespan_state = {"services": service_container}
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=lifespan_state,
            lifespan_state=lifespan_state,
        )
    )


# --------------------------------------------------------------------------
# Standalone tdpilot_API helpers — canned ``urlopen()`` responses for
# tests that mock DeepSeek's /v1/messages endpoint.
# --------------------------------------------------------------------------


class _UrlopenCtxMgr:
    """Context-manager wrapper for canned ``urlopen()`` return values."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def mk_response():
    """Factory: pass a payload dict, get back a fake ``urlopen()``
    context-manager value. Mirrors the helper inlined in the older
    test files so newer tests can stop duplicating it."""

    def _build(payload: dict) -> _UrlopenCtxMgr:
        body = json.dumps(payload).encode("utf-8")
        fake = SimpleNamespace(read=lambda: body)
        return _UrlopenCtxMgr(fake)

    return _build


@pytest.fixture
def exec_client_factory():
    """Factory returning a RecordingTDClient pre-wired for the /exec endpoint.

    Usage:
        def test_foo(exec_client_factory):
            client = exec_client_factory({"path": "/comp", "issues": []})
            ...

    The exec endpoint wraps the dict in the JSON-string envelope TD returns.
    """
    import json as _json

    def _make(exec_result: Any) -> RecordingTDClient:
        return RecordingTDClient(
            responses={
                "exec": {"result": _json.dumps(exec_result)},
                "project/lifecycle": {"ok": True},
            }
        )

    return _make
