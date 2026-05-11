"""Regression tests for SearchNodesInput class constants.

Pydantic v2 treats class-level annotated assignments as model fields unless
they're declared as ``ClassVar``. Pre-fix, ``LEGACY_SCOPES`` and ``NEW_SCOPES``
were declared as ``tuple[str, ...]`` without ``ClassVar``, so they were
absorbed as model fields and stripped from the class namespace. Any code
accessing ``SearchNodesInput.LEGACY_SCOPES`` (notably ``td_search_nodes`` in
registry/tools_data.py) then crashed with ``AttributeError: LEGACY_SCOPES``,
making the new-scope routing path completely broken at runtime.
"""

from __future__ import annotations

from td_mcp.models._legacy import SearchNodesInput


class TestSearchNodesInputScopeConstants:
    def test_legacy_scopes_accessible_as_class_attribute(self):
        assert SearchNodesInput.LEGACY_SCOPES == ("name", "type", "family", "all")

    def test_new_scopes_accessible_as_class_attribute(self):
        assert SearchNodesInput.NEW_SCOPES == ("dat_text", "param_exprs")

    def test_scope_constants_are_not_model_fields(self):
        # If Pydantic absorbs them as fields, the tool dispatch path crashes.
        assert "LEGACY_SCOPES" not in SearchNodesInput.model_fields
        assert "NEW_SCOPES" not in SearchNodesInput.model_fields

    def test_effective_scopes_default_expands_all(self):
        inp = SearchNodesInput(query="foo")
        assert inp.effective_scopes() == ["name", "type", "family"]

    def test_effective_scopes_new_scope_passes_through(self):
        inp = SearchNodesInput(query="foo", scopes=["dat_text"])
        assert inp.effective_scopes() == ["dat_text"]
