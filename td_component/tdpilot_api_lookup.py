"""TDPilot API — COMP/extension/runtime lookup helpers.

Phase 3 / F-18 — every handler that needs to reach the runtime
previously open-coded the same five-line walk:

    try:
        comp = parent()
    except NameError:
        return None
    if comp is None:
        return None
    ext_dat = comp.op("tdpilot_api_extension")
    if ext_dat is None:
        return None
    ext = ext_dat.module.get_extension(comp)
    return ext.runtime.raw_dispatcher

Across ``tdpilot_api_batch.py``, ``tdpilot_api_recipes.py``,
``tdpilot_api_patches.py``, ``tdpilot_api_macros.py``, the chat web
callbacks, and the ws callbacks, the boilerplate diverged subtly —
some return None on failure, some raise, some print, some try-except
around different segments. This module gives each of those callers
one line.

Soft semantics: every helper returns ``None`` rather than raising on
failure. Callers decide what to do with a missing piece (most just
report a clean error to the model).
"""

from __future__ import annotations

from typing import Any


def get_comp() -> Any | None:
    """Return the owning COMP via TD-injected ``parent()``.
    ``None`` outside a TD context (the name isn't defined) or if
    ``parent()`` itself returns None."""
    try:
        return parent()  # type: ignore[name-defined]
    except NameError:
        return None


def get_extension(comp: Any | None = None) -> Any | None:
    """Resolve the ``TDPilotAPIExt`` instance hanging off ``comp``
    (or the active comp if ``comp`` is None). Returns the extension
    object or ``None`` on any failure (missing DAT, missing module,
    missing get_extension)."""
    comp = comp if comp is not None else get_comp()
    if comp is None:
        return None
    try:
        ext_dat = comp.op("tdpilot_api_extension")
    except Exception:
        return None
    if ext_dat is None:
        return None
    try:
        return ext_dat.module.get_extension(comp)
    except Exception:
        return None


def get_runtime(comp: Any | None = None) -> Any | None:
    """Walk to ``ext.runtime`` (the public accessor added in PR-14).
    ``None`` if the extension or runtime isn't available."""
    ext = get_extension(comp)
    if ext is None:
        return None
    try:
        return ext.runtime
    except Exception:
        return None


def get_raw_dispatcher(comp: Any | None = None) -> Any | None:
    """Convenience for the most common callsite — handlers that need
    the cook-thread-bypass dispatcher. Equivalent to
    ``get_runtime(comp).raw_dispatcher`` with all the soft-failure
    handling baked in."""
    runtime = get_runtime(comp)
    if runtime is None:
        return None
    try:
        return runtime.raw_dispatcher
    except Exception:
        return None


def get_module(name: str, comp: Any | None = None) -> Any | None:
    """Resolve ``op(name).module`` — the textDAT-as-module pattern
    used throughout the standalone tox. ``None`` if the DAT is
    missing or doesn't expose a module attribute."""
    comp = comp if comp is not None else get_comp()
    if comp is None:
        return None
    try:
        dat = comp.op(name)
    except Exception:
        return None
    if dat is None:
        return None
    try:
        return dat.module
    except Exception:
        return None
