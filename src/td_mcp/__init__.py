"""TouchDesigner MCP Server — AI-powered control of TouchDesigner via MCP."""

__version__ = "2.0.1"

# Canonical .tox filename. The TD component's API_VERSION lives inside the
# .tox itself (see ``td_component/callbacks/_header.py`` since v1.8.3), so
# the filename doesn't carry a version marker. v2.0 (PR-26) removed the
# legacy ``tdpilot_v1_3.tox`` fallback that pre-v1.4.7 installs relied on;
# users on those vintage installs run ``npx tdpilot-dpsk4 install`` once to
# refresh.
TOX_FILENAME = "tdpilot-dpsk4.tox"


def normalize_transport(raw: str) -> str:
    """Normalize transport name: strip, lowercase, underscores to hyphens."""
    return raw.strip().lower().replace("_", "-")
