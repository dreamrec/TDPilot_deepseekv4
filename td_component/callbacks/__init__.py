"""TDPilot DPSK4 — TouchDesigner MCP webserver callbacks (split package).

The runtime artefact is a single textDAT named ``mcp_webserver_callbacks``
inside the ``mcp_server`` COMP. The build script (``td_component/
build_export_mcp_tox.py``) calls :func:`._composer.compose` once at
.tox-build time to concatenate the files in this package into that
textDAT's body. References between handlers, helpers and module-level
state therefore resolve in a SINGLE flat namespace at runtime — these
files MUST NOT add cross-module imports.

The split is purely a developer-facing concern: it gives focused git
history and reviewable diffs for handler-scoped changes (see PR-16,
audit finding F-14). The composer's output is byte-identical to the
pre-split god module ``td_component/mcp_webserver_callbacks.py`` (now
removed); :mod:`tests.test_composer_byte_equivalence` enforces that.
"""
