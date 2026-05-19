"""Bidirectional schema↔handler parity for the chat-pipe surface.

The chat-pipe agent's tool surface is defined by two side-by-side tables
in ``td_component/``:

  * ``TOOL_SCHEMAS``    (in ``tdpilot_api_schema_defs``) — what the LLM SEES
  * ``TOOL_TO_HANDLER`` (in ``tdpilot_api_schema_map``)  — how a call is ROUTED

When the two drift, tools become either invisible (handler without schema)
or unroutable (schema without handler).

The existing ``handler_table_consistent`` check in
``tdpilot_api_introspect.handle_describe_surface`` is **one-directional**:
it asserts every schema name has a matching handler key, but does NOT
assert the inverse. That gap is exactly what allowed v2.5.1's
``td_get_traces`` chat-pipe alias to ship missing: the handler existed
under ``td_get_recent_traces`` but no ``td_get_traces`` schema entry was
present, so the LLM could not call it.

These tests pin BOTH directions and catch the regression pre-merge.

Source-of-truth references:
  - CHANGELOG.md v2.5.1 entry
  - td_component/tdpilot_api_schema_map.py:215-220 (the as-shipped fix)
  - audit 2026-05-19, code-quality report
"""

from __future__ import annotations

import pytest

# These modules live in td_component/ and are on sys.path via conftest.
from tdpilot_api_schema_defs import TOOL_SCHEMAS  # type: ignore[import-not-found]
from tdpilot_api_schema_map import (  # type: ignore[import-not-found]
    INTERNAL_ONLY_TOOL_NAMES,
    TOOL_TO_HANDLER,
)


def _schema_names() -> set[str]:
    return {s.get("name") for s in TOOL_SCHEMAS if s.get("name")}


def _llm_visible_handler_names() -> set[str]:
    """Handler keys the LLM is expected to see — drops the auto-rollback
    internal-only routes that the AutoRollbackGuard invokes directly.
    """
    return set(TOOL_TO_HANDLER.keys()) - set(INTERNAL_ONLY_TOOL_NAMES)


class TestBidirectionalParity:
    def test_every_schema_has_a_handler(self) -> None:
        """Forward parity: each TOOL_SCHEMAS entry must be routable.

        Mirrors the existing ``handler_table_consistent`` check; pinned
        here so the test isn't lost if introspect.handle_describe_surface
        is ever refactored.
        """
        schemas = _schema_names()
        handlers = set(TOOL_TO_HANDLER.keys())  # full handler set — schemas may
        # legitimately resolve to internal-only handlers too.
        orphan_schemas = schemas - handlers
        assert not orphan_schemas, (
            "Schemas with no matching handler (LLM-callable but unroutable): "
            f"{sorted(orphan_schemas)}.\n"
            "Add a TOOL_TO_HANDLER entry in "
            "td_component/tdpilot_api_schema_map.py for each."
        )

    def test_every_visible_handler_has_a_schema(self) -> None:
        """Inverse parity (v2.5.1 regression class): every LLM-visible
        ``TOOL_TO_HANDLER`` entry must be advertised via a ``TOOL_SCHEMAS``
        entry. ``INTERNAL_ONLY_TOOL_NAMES`` is excluded (those are routed
        by the AutoRollbackGuard, not by the LLM).

        Without this, a handler can be wired but the LLM never sees it —
        the chat-pipe ``td_get_traces`` gap from v2.5.0 to v2.5.1.
        Catching this pre-merge would have saved the v2.5.1 hotfix.
        """
        schemas = _schema_names()
        visible_handlers = _llm_visible_handler_names()
        invisible_handlers = visible_handlers - schemas
        assert not invisible_handlers, (
            "Handlers with no matching schema (invisible to the LLM): "
            f"{sorted(invisible_handlers)}.\n"
            "Either add a TOOL_SCHEMAS entry in "
            "td_component/tdpilot_api_schema_defs.py for each, mark the "
            "handler as internal via INTERNAL_ONLY_TOOL_NAMES in "
            "td_component/tdpilot_api_schema_map.py, or delete the orphan."
        )

    def test_llm_visible_surfaces_set_equal(self) -> None:
        """Belt-and-braces: schema name set == LLM-visible handler set.

        Explicit equality gives a faster, more readable diff in CI when
        both directions break at once (e.g. a rename in only one file).
        """
        assert _schema_names() == _llm_visible_handler_names()


class TestInternalOnlyAllowlist:
    def test_internal_only_names_are_registered_handlers(self) -> None:
        """Every name in ``INTERNAL_ONLY_TOOL_NAMES`` must be a real
        ``TOOL_TO_HANDLER`` key — otherwise the allowlist is shielding
        a dangling reference rather than a legitimate internal route.
        """
        handlers = set(TOOL_TO_HANDLER.keys())
        stale = set(INTERNAL_ONLY_TOOL_NAMES) - handlers
        assert not stale, (
            f"INTERNAL_ONLY_TOOL_NAMES references unregistered handlers: "
            f"{sorted(stale)}. Remove these from INTERNAL_ONLY_TOOL_NAMES "
            "or add the handler back."
        )

    def test_no_td_prefixed_name_is_internal_only(self) -> None:
        """A ``td_``-prefixed name suggests a public tool; refuse to
        let one hide under the internal-only allowlist. Trips if a
        future maintainer tries to silence the parity test by adding a
        public-looking handler to INTERNAL_ONLY_TOOL_NAMES.
        """
        leaks = {n for n in INTERNAL_ONLY_TOOL_NAMES if n.startswith("td_")}
        assert not leaks, (
            "Public-looking td_-prefixed names in INTERNAL_ONLY_TOOL_NAMES: "
            f"{sorted(leaks)}. Internal-only routes should NOT use the td_ "
            "namespace; the parity test refuses to silently approve."
        )


class TestNonEmpty:
    def test_schemas_table_not_empty(self) -> None:
        """Pin the smoke: TOOL_SCHEMAS must hold at least one tool.

        If this fails, the chat-pipe surface is empty — likely an import
        error in tdpilot_api_schema_defs that's swallowing the whole list.
        """
        assert len(TOOL_SCHEMAS) > 0

    def test_handler_table_not_empty(self) -> None:
        assert len(TOOL_TO_HANDLER) > 0
