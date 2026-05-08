"""PR-19 (F-18) — ``tdpilot_api_lookup`` helpers extraction.

Pre-1.8.1 every handler that reached the runtime open-coded the same
five-line walk:

    try:
        comp = parent()
    except NameError:
        return None
    ext = comp.op("tdpilot_api_extension").module.get_extension(comp)
    return ext.runtime.raw_dispatcher

Each callsite handled soft-failure differently (some returned None,
some raised, some printed). PR-19 centralises this in
``tdpilot_api_lookup`` so:
  * One module owns the walk.
  * Every step soft-fails to None — callers decide what to do.
  * A future shape change in extension/runtime needs only one edit.

These tests exercise the helpers using a stub COMP / extension /
runtime without touching TouchDesigner.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TD_COMP = REPO_ROOT / "td_component"


@pytest.fixture
def lookup_with_stub(monkeypatch):
    """Import ``tdpilot_api_lookup`` and stub TD's ``parent()``
    builtin to return a configurable fake COMP. Yields
    ``(module, ctx)`` where ``ctx`` is a mutable dict the test can
    populate with ``comp`` / ``ext`` / ``runtime`` / ``dispatcher``."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("tdpilot_api_lookup", None)
    ctx: dict = {"comp": None}
    monkeypatch.setattr(builtins, "parent", lambda: ctx.get("comp"), raising=False)
    mod = importlib.import_module("tdpilot_api_lookup")
    try:
        yield mod, ctx
    finally:
        sys.modules.pop("tdpilot_api_lookup", None)
        sys.path.remove(str(TD_COMP))


class FakeRuntime:
    def __init__(self, raw_dispatcher=None):
        self.raw_dispatcher = raw_dispatcher


class FakeExt:
    def __init__(self, runtime=None):
        self.runtime = runtime


class FakeExtDat:
    """Stub for ``op('tdpilot_api_extension')``. Has a ``.module``
    attribute that mimics the textDAT-as-module pattern; calling
    ``.module.get_extension(comp)`` returns the fake extension."""

    def __init__(self, ext):
        outer = self

        class _Mod:
            @staticmethod
            def get_extension(comp):
                return outer._ext

        self.module = _Mod()
        self._ext = ext


class FakeComp:
    """Stub TouchDesigner COMP. ``op(name)`` returns the configured
    DAT (typically the FakeExtDat for ``tdpilot_api_extension``)."""

    def __init__(self, op_table=None):
        self._op_table = op_table or {}

    def op(self, name):
        return self._op_table.get(name)


# ---------------------------------------------------------------------------
# get_comp
# ---------------------------------------------------------------------------


def test_get_comp_returns_parent_result(lookup_with_stub):
    mod, ctx = lookup_with_stub
    fake = FakeComp()
    ctx["comp"] = fake
    assert mod.get_comp() is fake


def test_get_comp_handles_parent_returning_none(lookup_with_stub):
    mod, ctx = lookup_with_stub
    ctx["comp"] = None
    assert mod.get_comp() is None


def test_get_comp_handles_parent_undefined(monkeypatch):
    """Outside a TD context — ``parent`` isn't even bound — the
    helper returns None rather than raising NameError."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("tdpilot_api_lookup", None)

    def _no_parent():
        raise NameError("parent")

    monkeypatch.setattr(builtins, "parent", _no_parent, raising=False)
    try:
        mod = importlib.import_module("tdpilot_api_lookup")
        assert mod.get_comp() is None
    finally:
        sys.modules.pop("tdpilot_api_lookup", None)
        sys.path.remove(str(TD_COMP))


# ---------------------------------------------------------------------------
# get_extension
# ---------------------------------------------------------------------------


def test_get_extension_walks_through_dat_module(lookup_with_stub):
    mod, ctx = lookup_with_stub
    rt = FakeRuntime(raw_dispatcher=lambda n, a: {"ok": True})
    ext = FakeExt(runtime=rt)
    comp = FakeComp({"tdpilot_api_extension": FakeExtDat(ext)})
    ctx["comp"] = comp
    assert mod.get_extension() is ext


def test_get_extension_returns_none_when_dat_missing(lookup_with_stub):
    """``op('tdpilot_api_extension')`` returns None — extension dat
    not installed, fresh COMP. Helper soft-fails."""
    mod, ctx = lookup_with_stub
    comp = FakeComp(op_table={})
    ctx["comp"] = comp
    assert mod.get_extension() is None


def test_get_extension_returns_none_when_comp_missing(lookup_with_stub):
    mod, ctx = lookup_with_stub
    ctx["comp"] = None
    assert mod.get_extension() is None


def test_get_extension_uses_explicit_comp_arg(lookup_with_stub):
    """Caller can override the auto-resolved COMP — useful for tests
    or for handlers that already have the COMP in hand."""
    mod, ctx = lookup_with_stub
    rt = FakeRuntime()
    ext = FakeExt(runtime=rt)
    other = FakeComp({"tdpilot_api_extension": FakeExtDat(ext)})
    ctx["comp"] = None  # auto-lookup would fail
    assert mod.get_extension(comp=other) is ext


def test_get_extension_handles_dat_module_raise(lookup_with_stub):
    """A `.module.get_extension` that raises — soft-fail to None
    rather than propagating."""
    mod, ctx = lookup_with_stub

    class _BadDat:
        @property
        def module(self):
            raise RuntimeError("module not loaded")

    comp = FakeComp({"tdpilot_api_extension": _BadDat()})
    ctx["comp"] = comp
    assert mod.get_extension() is None


# ---------------------------------------------------------------------------
# get_runtime + get_raw_dispatcher
# ---------------------------------------------------------------------------


def test_get_runtime_returns_extension_runtime(lookup_with_stub):
    mod, ctx = lookup_with_stub
    rt = FakeRuntime()
    ext = FakeExt(runtime=rt)
    ctx["comp"] = FakeComp({"tdpilot_api_extension": FakeExtDat(ext)})
    assert mod.get_runtime() is rt


def test_get_runtime_returns_none_when_extension_missing(lookup_with_stub):
    mod, ctx = lookup_with_stub
    ctx["comp"] = FakeComp({})
    assert mod.get_runtime() is None


def test_get_runtime_handles_runtime_attr_raise(lookup_with_stub):
    mod, ctx = lookup_with_stub

    class _BadExt:
        @property
        def runtime(self):
            raise RuntimeError("not initialised")

    ctx["comp"] = FakeComp({"tdpilot_api_extension": FakeExtDat(_BadExt())})
    assert mod.get_runtime() is None


def test_get_raw_dispatcher_returns_runtime_attr(lookup_with_stub):
    mod, ctx = lookup_with_stub
    sentinel = object()
    rt = FakeRuntime(raw_dispatcher=sentinel)
    ext = FakeExt(runtime=rt)
    ctx["comp"] = FakeComp({"tdpilot_api_extension": FakeExtDat(ext)})
    assert mod.get_raw_dispatcher() is sentinel


def test_get_raw_dispatcher_returns_none_when_runtime_missing(lookup_with_stub):
    mod, ctx = lookup_with_stub
    ctx["comp"] = FakeComp({})
    assert mod.get_raw_dispatcher() is None


# ---------------------------------------------------------------------------
# get_module
# ---------------------------------------------------------------------------


def test_get_module_returns_dat_module_attr(lookup_with_stub):
    mod, ctx = lookup_with_stub

    class _Dat:
        module = "the-module-object"

    ctx["comp"] = FakeComp({"some_dat": _Dat()})
    assert mod.get_module("some_dat") == "the-module-object"


def test_get_module_returns_none_when_dat_missing(lookup_with_stub):
    mod, ctx = lookup_with_stub
    ctx["comp"] = FakeComp({})
    assert mod.get_module("missing_dat") is None


def test_get_module_returns_none_when_module_attr_raises(lookup_with_stub):
    mod, ctx = lookup_with_stub

    class _BadDat:
        @property
        def module(self):
            raise RuntimeError("not loaded")

    ctx["comp"] = FakeComp({"bad_dat": _BadDat()})
    assert mod.get_module("bad_dat") is None


# ---------------------------------------------------------------------------
# Source-level: callers wired through the helpers
# ---------------------------------------------------------------------------


def test_batch_uses_get_raw_dispatcher():
    """``tdpilot_api_batch._resolve_raw_dispatcher`` should now
    delegate to the lookup helper rather than open-coding the walk."""
    src = (TD_COMP / "tdpilot_api_batch.py").read_text()
    assert "from tdpilot_api_lookup import get_raw_dispatcher" in src
    # The bespoke walk should be gone (no parent()/.op('tdpilot_api_extension') chain).
    import re

    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    assert '.op("tdpilot_api_extension")' not in code, "batch.py still has the bespoke COMP walk"


def test_recipes_uses_get_raw_dispatcher():
    src = (TD_COMP / "tdpilot_api_recipes.py").read_text()
    assert "from tdpilot_api_lookup import get_raw_dispatcher" in src


def test_patches_uses_get_raw_dispatcher():
    src = (TD_COMP / "tdpilot_api_patches.py").read_text()
    assert "from tdpilot_api_lookup import get_raw_dispatcher" in src


def test_macros_uses_get_raw_dispatcher():
    src = (TD_COMP / "tdpilot_api_macros.py").read_text()
    assert "from tdpilot_api_lookup import get_raw_dispatcher" in src


def test_lookup_module_in_extension_bind_list():
    """The textDAT must be bound into sys.modules early enough that
    handlers can ``from tdpilot_api_lookup import ...`` at any time."""
    src = (TD_COMP / "tdpilot_api_extension.py").read_text()
    assert '"tdpilot_api_lookup"' in src


def test_lookup_module_in_standalone_build_source_list():
    """And the build script must include the file so the .tox carries
    a textDAT for it."""
    src = (TD_COMP / "build_tdpilot_api_tox.py").read_text()
    assert '"tdpilot_api_lookup"' in src
    assert '"td_component/tdpilot_api_lookup.py"' in src
