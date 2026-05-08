"""PR-15 (F-11) — event_emitter migrates module globals to comp.storage.

Pre-1.8.1 the buffer + per-key last-emit + stats lived in module-level
globals. A textDAT module reload (build script edit, Reload Config
pulse, hot-edit) wiped all three — buffered events vanished silently
and stats counters reset mid-session.

This test exercises the migration without booting TD: we stub
``parent()`` / ``op()`` into the module's namespace at import time,
provide a fake COMP whose ``storage`` dict survives "module reloads",
then re-import the module and verify the buffer/last-emit/stats
references all point at the same underlying dict the previous
incarnation populated.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TD_COMP = REPO_ROOT / "td_component"


class FakeComp:
    """Minimal stand-in for a TouchDesigner COMP with comp.storage
    semantics. ``fetch(key, default)`` returns the stored value or
    default; ``store(key, value)`` saves it. The dict is the full
    backing store — survives module reloads because we hold the
    reference outside the module."""

    def __init__(self):
        self._storage: dict = {}

    def fetch(self, key, default):
        return self._storage.get(key, default)

    def store(self, key, value):
        self._storage[key] = value


class FakeWs:
    """WS DAT stub — accepts sendText, records what was sent, can
    be made to fail (returning False from _send_payload)."""

    def __init__(self, *, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def sendText(self, text):
        if self.fail:
            raise RuntimeError("ws not connected")
        self.sent.append(text)


@pytest.fixture
def emitter_with_comp(monkeypatch):
    """Import event_emitter with a stub ``parent()`` returning a
    ``FakeComp`` and ``op()`` returning a ``FakeWs``. Yields
    ``(module, comp, ws)``. Cleans up the import on teardown so
    other tests get a fresh module."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs()
    # TD injects ``parent``, ``op``, ``me`` into the module namespace.
    # We monkeypatch them into builtins so the import sees them.
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    mod = importlib.import_module("event_emitter")
    try:
        yield mod, comp, ws
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


@pytest.fixture
def emitter_no_comp(monkeypatch):
    """Import event_emitter with no ``parent()`` available — fallback
    storage path. Used to verify the module imports + works without
    a TD COMP context (CI / offline tests)."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)

    def _no_parent():
        raise NameError("parent")

    monkeypatch.setattr(builtins, "parent", _no_parent, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: None, raising=False)
    mod = importlib.import_module("event_emitter")
    # Reset fallback storage so each test starts clean.
    mod._FALLBACK_STORAGE.clear()
    try:
        yield mod
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


# ---------------------------------------------------------------------------
# Storage shape
# ---------------------------------------------------------------------------


def test_storage_keys_present_for_all_three_state_buckets(emitter_with_comp):
    mod, _comp, _ws = emitter_with_comp
    assert set(mod._STORAGE_KEYS.keys()) == {"buffer", "last_emit", "stats"}
    # Namespaced so they don't collide with other modules using comp.storage.
    for v in mod._STORAGE_KEYS.values():
        assert v.startswith("tdpilot_emitter_"), v


def test_buffer_lives_in_comp_storage(emitter_with_comp):
    mod, comp, _ws = emitter_with_comp
    buf = mod._buffer()
    buf.append({"type": "test", "data": 1})
    # The buffer object IS the same one the COMP has stored.
    assert comp._storage[mod._STORAGE_KEYS["buffer"]] is buf


def test_last_emit_lives_in_comp_storage(emitter_with_comp):
    mod, comp, _ws = emitter_with_comp
    last = mod._last_emit()
    last["k"] = 12345.0
    assert comp._storage[mod._STORAGE_KEYS["last_emit"]] is last


def test_stats_lives_in_comp_storage(emitter_with_comp):
    mod, comp, _ws = emitter_with_comp
    stats_dict = mod._stats()
    assert set(stats_dict.keys()) == {
        "sent",
        "buffered",
        "dropped_rate_limited",
        "dropped_buffer_overflow",
    }
    assert comp._storage[mod._STORAGE_KEYS["stats"]] is stats_dict


# ---------------------------------------------------------------------------
# Module reload survival — the bug F-11 fixed
# ---------------------------------------------------------------------------


def test_buffer_survives_module_reload(monkeypatch):
    """The killer test: pre-fix, the buffer dict was a module global;
    re-importing the module dropped its contents on the floor.
    Post-fix, the buffer lives in comp.storage, so the new import's
    ``_buffer()`` returns the SAME list the old import populated."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs(fail=True)  # force buffering — sendText raises
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        # Fill the buffer with a couple of events.
        mod.emit("scene_change", {"path": "/project1"}, rate_limit=0)
        mod.emit("param_change", {"path": "/p", "name": "x"}, rate_limit=0)
        assert len(mod._buffer()) == 2
        # Simulate a reload — drop the module from sys.modules and
        # re-import. The new module instance fetches the buffer from
        # comp.storage, which still holds the populated list.
        sys.modules.pop("event_emitter", None)
        mod2 = importlib.import_module("event_emitter")
        assert mod2 is not mod  # genuinely a fresh module object
        new_buf = mod2._buffer()
        assert len(new_buf) == 2, "buffer lost on module reload — comp.storage migration is broken"
        # Same underlying object — both refs point at the storage dict.
        assert new_buf is comp._storage[mod2._STORAGE_KEYS["buffer"]]
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


def test_stats_counters_survive_module_reload(monkeypatch):
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs()  # successful sends — stats["sent"] increments
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        mod.emit("a", {"path": "/x"}, rate_limit=0)
        mod.emit("b", {"path": "/y"}, rate_limit=0)
        snapshot = mod.stats()
        assert snapshot["sent"] == 2
        # Reload.
        sys.modules.pop("event_emitter", None)
        mod2 = importlib.import_module("event_emitter")
        assert mod2.stats()["sent"] == 2
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


def test_last_emit_survives_module_reload(monkeypatch):
    """Rate-limit dedupe state must also persist — without it, a
    reload window would let a flood of events through that the
    previous module would have rate-limited."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs()
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        mod.emit("chop_change", {"path": "/c1", "channel": "x", "name": "v"})
        # Same event again immediately — should be rate-limited.
        accepted = mod.emit("chop_change", {"path": "/c1", "channel": "x", "name": "v"})
        assert accepted is False
        sys.modules.pop("event_emitter", None)
        mod2 = importlib.import_module("event_emitter")
        # Reload — the last-emit timestamp must still be there, so a
        # third emit immediately after reload is also rate-limited.
        accepted2 = mod2.emit("chop_change", {"path": "/c1", "channel": "x", "name": "v"})
        assert accepted2 is False
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


# ---------------------------------------------------------------------------
# Functional behaviour parity — pre-fix tests must still pass
# ---------------------------------------------------------------------------


def test_emit_sends_when_ws_available(emitter_with_comp):
    mod, _comp, ws = emitter_with_comp
    mod.emit("scene_change", {"path": "/project1"}, rate_limit=0)
    assert len(ws.sent) == 1
    assert "scene_change" in ws.sent[0]


def test_emit_buffers_when_ws_fails(monkeypatch):
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs(fail=True)
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        mod.emit("scene_change", {"path": "/p1"}, rate_limit=0)
        assert mod.stats()["buffered"] == 1
        assert mod.stats()["sent"] == 0
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


def test_emit_rate_limit_drops_repeats(emitter_with_comp):
    mod, _comp, _ws = emitter_with_comp
    accepted_first = mod.emit("foo", {"path": "/x"}, rate_limit=0.5)
    accepted_second = mod.emit("foo", {"path": "/x"}, rate_limit=0.5)
    assert accepted_first is True
    assert accepted_second is False
    assert mod.stats()["dropped_rate_limited"] == 1


def test_buffer_overflow_drops_oldest(monkeypatch):
    """Buffer is capped at MAX_BUFFER; oldest event drops on overflow."""
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs(fail=True)
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        # Reduce the cap for the test by mutating the constant.
        mod.MAX_BUFFER = 3
        # Push 4 events with distinct dedupe keys so rate limit doesn't fire.
        for i in range(4):
            mod.emit(f"e{i}", {"path": f"/n{i}"}, rate_limit=0)
        s = mod.stats()
        assert s["buffer_depth"] == 3
        assert s["dropped_buffer_overflow"] == 1
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


def test_flush_pending_drains_buffer(monkeypatch):
    sys.path.insert(0, str(TD_COMP))
    sys.modules.pop("event_emitter", None)
    comp = FakeComp()
    ws = FakeWs(fail=True)
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "op", lambda path: ws, raising=False)
    try:
        mod = importlib.import_module("event_emitter")
        for i in range(5):
            mod.emit(f"e{i}", {"path": f"/n{i}"}, rate_limit=0)
        assert mod.stats()["buffer_depth"] == 5
        # Flip the WS to "succeed" by replacing it.
        ws.fail = False
        sent_count = mod.flush_pending(limit=10)
        assert sent_count == 5
        assert mod.stats()["buffer_depth"] == 0
        assert len(ws.sent) == 5
    finally:
        sys.modules.pop("event_emitter", None)
        sys.path.remove(str(TD_COMP))


# ---------------------------------------------------------------------------
# Fallback storage — module imports cleanly without a TD COMP
# ---------------------------------------------------------------------------


def test_module_imports_without_comp(emitter_no_comp):
    """No ``parent()``, no error — the fallback storage path lets the
    module import in offline / unit-test contexts."""
    mod = emitter_no_comp
    assert callable(mod.emit)
    # The fallback storage holds the same buckets.
    buf = mod._buffer()
    assert isinstance(buf, list)


def test_fallback_storage_isolates_state(emitter_no_comp):
    """Without a COMP, mutations should still flow through the
    fallback dict so emit / stats / buffer all see consistent state."""
    mod = emitter_no_comp
    mod._buffer().append({"type": "x"})
    assert mod._buffer() == [{"type": "x"}]
    mod._stats()["sent"] = 7
    assert mod.stats()["sent"] == 7
