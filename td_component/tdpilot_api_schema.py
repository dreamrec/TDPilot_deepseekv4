"""TDPilot API — schema/handler re-export shim.

Historical home of TOOL_SCHEMAS + TOOL_TO_HANDLER. After the 2026-05-04
audit the two big tables live in their own modules; this file just
re-exports them so older imports

    from tdpilot_api_schema import TOOL_SCHEMAS
    from tdpilot_api_schema import TOOL_TO_HANDLER

continue to resolve unchanged. Keep this shim thin — new code should
prefer importing from the underlying modules directly.
"""

from __future__ import annotations

from tdpilot_api_schema_defs import TOOL_SCHEMAS, supported_tool_names  # noqa: F401
from tdpilot_api_schema_map import TOOL_TO_HANDLER  # noqa: F401

__all__ = ["TOOL_SCHEMAS", "TOOL_TO_HANDLER", "supported_tool_names"]
