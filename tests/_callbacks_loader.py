"""Loader shim for tests that previously imported the god module directly.

Pre-v1.8.3, ``td_component/mcp_webserver_callbacks.py`` existed as a single
3149-line file and tests loaded it via ``importlib.spec_from_file_location``
or read its source as text. PR-16 split that file into
``td_component/callbacks/``; the runtime concatenates the splits into a textDAT
body at .tox-build time via :func:`td_component.callbacks._composer.compose`.

This helper produces the same composed string so tests can keep using
their existing patterns. Two entry points:

- :func:`callbacks_source` — returns the composed Python source as a string.
  Use this in tests that read the source as text (regex/keyword scans).
- :func:`load_callbacks_module` — execs the composed source as a synthetic
  module. Use this in tests that introspect attributes / call helper
  functions like ``module._check_auth_error``.

Both are cached per process (the source bytes don't change mid-session).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]

_CACHED_SOURCE: str | None = None


def callbacks_source() -> str:
    """Return the composed mcp_webserver_callbacks Python source."""
    global _CACHED_SOURCE
    if _CACHED_SOURCE is not None:
        return _CACHED_SOURCE

    added = False
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
        added = True
    try:
        from td_component.callbacks._composer import compose

        _CACHED_SOURCE = compose()
        return _CACHED_SOURCE
    finally:
        if added:
            sys.path.remove(str(REPO_ROOT))


def load_callbacks_module(module_name: str = "td_cb_under_test") -> ModuleType:
    """Exec the composed source as a fresh synthetic module.

    Always returns a NEW module instance — many tests mutate
    ``module.op``, ``module.project`` etc. in their setup, and a shared
    instance would leak that state between tests.
    """
    source = callbacks_source()
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = "<composed mcp_webserver_callbacks>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module
