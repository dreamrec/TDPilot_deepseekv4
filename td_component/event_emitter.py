"""Buffered event emitter for TD -> MCP WebSocket delivery.

Usage from other DAT callbacks:
    mod('event_emitter').emit('chop_change', {...}, rate_limit=0.016)
    mod('event_emitter').flush_pending()

State storage (Phase 3 / F-11):
    Pre-1.8.1 the buffer + per-key last-emit map + stats counters
    lived in module-level globals (``_BUFFER``, ``_LAST_EMIT``,
    ``_STATS``). When the textDAT module reloads — which happens any
    time a build script edits ``event_emitter.text`` or the user
    pulses Reload Config — the globals reset and any in-flight
    buffered events vanish silently. The 1.7.1 ``_ws_clients``
    migration in ``tdpilot_api_web_callbacks.py`` solved the same
    class of bug for the standalone variant; this module mirrors
    that approach.

    Now: state lives in ``parent().storage`` keyed by stable strings.
    A reload re-imports the module but the COMP's storage dict is
    untouched, so the in-memory buffer + counters survive. Outside a
    TD context (e.g. unit tests where ``parent()`` is undefined) we
    fall back to a process-local dict so the module still imports
    cleanly without a live COMP.
"""

import json
import time

WS_DAT_CANDIDATES = (
    "/project1/mcp_server/ws_client",
    "ws_client",
)
MAX_BUFFER = 1000
DEFAULT_RATE_LIMIT = 0.016

# Storage keys — namespaced so they don't collide with other modules
# that use the same comp.storage dict (e.g. the standalone variant's
# ``tdpilot_api_*`` keys, the WS client registry).
_STORAGE_KEYS = {
    "buffer": "tdpilot_emitter_buffer",
    "last_emit": "tdpilot_emitter_last",
    "stats": "tdpilot_emitter_stats",
}

# Fallback storage when no COMP context is available (unit tests,
# offline imports). Keyed by the same logical names as _STORAGE_KEYS;
# fresh per-process so each test gets a clean slate after a reset.
_FALLBACK_STORAGE: dict = {}


def _resolve_comp():
    """Owning COMP. ``parent()`` is injected into the module's
    namespace by TD when this textDAT is loaded inside a COMP. Outside
    that context (test imports) the name is undefined; return None
    and let callers fall back to ``_FALLBACK_STORAGE``."""
    try:
        return parent()  # type: ignore[name-defined]
    except NameError:
        return None


def _get_storage(key, default_factory):
    """Return the live storage object for ``key``, creating it via
    ``default_factory()`` on first access. Comp.storage holds a
    reference to the same object across module reloads so mutations
    on the returned dict/list propagate to the next reload."""
    comp = _resolve_comp()
    if comp is None:
        if key not in _FALLBACK_STORAGE:
            _FALLBACK_STORAGE[key] = default_factory()
        return _FALLBACK_STORAGE[key]
    storage_key = _STORAGE_KEYS[key]
    try:
        v = comp.fetch(storage_key, None)
    except Exception:
        v = None
    if v is None:
        v = default_factory()
        try:
            comp.store(storage_key, v)
        except Exception:
            # Best-effort — if the store call fails we still return
            # the factory result so the current call has something
            # to mutate. Next call will retry.
            pass
    return v


def _buffer():
    return _get_storage("buffer", list)


def _last_emit():
    return _get_storage("last_emit", dict)


def _stats():
    return _get_storage(
        "stats",
        lambda: {
            "sent": 0,
            "buffered": 0,
            "dropped_rate_limited": 0,
            "dropped_buffer_overflow": 0,
        },
    )


def _resolve_ws_dat():
    for path in WS_DAT_CANDIDATES:
        ws = op(path)  # type: ignore[name-defined]
        if ws is not None:
            return ws
    return None


def _send_payload(payload):
    ws = _resolve_ws_dat()
    if ws is None or not hasattr(ws, "sendText"):
        return False

    try:
        ws.sendText(json.dumps(payload, separators=(",", ":")))
        _stats()["sent"] += 1
        return True
    except Exception:
        return False


def _append_to_buffer(payload):
    buf = _buffer()
    stats_dict = _stats()
    if len(buf) >= MAX_BUFFER:
        # Drop oldest to keep most-recent context for AI subscribers.
        buf.pop(0)
        stats_dict["dropped_buffer_overflow"] += 1
    buf.append(payload)
    stats_dict["buffered"] += 1


def emit(event_type, data, rate_limit=DEFAULT_RATE_LIMIT, dedupe_key=None):
    """Emit an event with optional per-key rate limiting.

    Returns True when accepted for send/buffer, False when dropped by rate limit.
    """
    now = time.time()
    key = dedupe_key
    if key is None:
        path = data.get("path", "") if isinstance(data, dict) else ""
        channel = data.get("channel", "") if isinstance(data, dict) else ""
        name = data.get("name", "") if isinstance(data, dict) else ""
        key = f"{event_type}:{path}:{channel}:{name}"

    last_emit_dict = _last_emit()
    stats_dict = _stats()
    last = last_emit_dict.get(key, 0.0)
    if rate_limit and (now - last) < float(rate_limit):
        stats_dict["dropped_rate_limited"] += 1
        return False

    last_emit_dict[key] = now
    payload = {
        "type": event_type,
        "timestamp": now,
        "data": data,
    }

    if not _send_payload(payload):
        _append_to_buffer(payload)
    return True


def emit_event(payload):
    """Emit a preconstructed event payload dict."""
    if not _send_payload(payload):
        _append_to_buffer(payload)


def flush_pending(limit=200):
    """Try sending queued events.

    Returns number of events flushed.
    """
    sent = 0
    buf = _buffer()
    while buf and sent < limit:
        payload = buf[0]
        if not _send_payload(payload):
            break
        buf.pop(0)
        sent += 1
    return sent


def stats():
    """Return current emitter stats and queue depth."""
    result = dict(_stats())
    result["buffer_depth"] = len(_buffer())
    result["dedupe_keys"] = len(_last_emit())
    return result
