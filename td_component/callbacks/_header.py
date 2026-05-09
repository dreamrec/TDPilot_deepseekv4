"""
TouchDesigner MCP WebServer DAT Callbacks
==========================================
Paste this into the callbacks DAT attached to your WebServer DAT.
This is the TD-side router that receives HTTP requests from the
FastMCP server and executes operations using the TD Python API.

Setup:
  1. Create a Base COMP named 'mcp_server'
  2. Inside it, create a WebServer DAT (port 9981, Active=On)
  3. Attach this script as the callbacks DAT
  4. Optionally add a Movie File Out TOP named 'mcp_screenshot' for captures

Compatible with TouchDesigner 2025.30000+
"""

import base64
import json
import os
import re
import sys
import time
import traceback

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

API_VERSION = "2.1.2"
SCREENSHOT_TEMP_PATH = os.path.join(os.environ.get('TEMP', os.environ.get('TMP', '/tmp')), 'td_mcp_screenshot.jpg')

# Auth + policy env is read at CALL TIME, not import time — otherwise TD's
# compiled-callback module cache pins stale values for the whole session, and
# env changes (e.g. swapping the shared secret) silently take no effect. See
# audit A-1. Module-level aliases remain below for backward compatibility with
# external callers and tests that inspect them.

def _current_shared_secret():
    """Read TD_MCP_SHARED_SECRET fresh from the environment."""
    return os.environ.get('TD_MCP_SHARED_SECRET', '').strip()


def _current_require_auth():
    """Read TD_MCP_REQUIRE_AUTH fresh from the environment.

    Auth policy:
      - If SHARED_SECRET is set: always required.
      - If TD_MCP_REQUIRE_AUTH=1 (default in production): refuse when secret is empty.
      - If TD_MCP_REQUIRE_AUTH=0: legacy permissive mode. Use only for local dev.
    """
    return os.environ.get('TD_MCP_REQUIRE_AUTH', '1').strip() not in ('0', 'false', 'no', '')


def _current_cors_origin():
    """Read TD_MCP_CORS_ORIGIN fresh from the environment.

    CORS policy:
      - Default: no CORS header (browsers are rejected by same-origin). MCP
        stdio/http clients don't need CORS.
      - TD_MCP_CORS_ORIGIN can be set to an exact origin (e.g. http://localhost:3000).
      - Wildcard '*' is NEVER used.
    """
    return os.environ.get('TD_MCP_CORS_ORIGIN', '').strip()


def _current_exec_mode():
    """Read TD_MCP_EXEC_MODE fresh from the environment."""
    mode = os.environ.get('TD_MCP_EXEC_MODE', 'restricted').strip().lower()
    if mode not in ('off', 'restricted', 'standard', 'full'):
        return 'restricted'
    return mode


# Module-level aliases (evaluated at import time) — kept so existing tests
# that do `module.SHARED_SECRET` still work. The live value used by auth
# checks comes from _current_shared_secret() etc., NOT these aliases.
SHARED_SECRET = _current_shared_secret()
REQUIRE_AUTH = _current_require_auth()
CORS_ORIGIN = _current_cors_origin()
DEFAULT_EXEC_MODE = _current_exec_mode()
RESTRICTED_IMPORT_RE = re.compile(r'(?:^|;)\s*(import|from)\s+\w+', re.MULTILINE)
RESTRICTED_TOKENS = (
    '__import__',
    'open\x28',
    'compile\x28',
    'input\x28',
    'subprocess',
    'socket',
    'requests',
    'httpx',
    'urllib',
    'pathlib',
    'shutil',
    'os.system',
    'os.popen',
    '__subclasses__',
    '__bases__',
    '__mro__',
    '__class__',
)
STANDARD_ALLOWED_IMPORTS = frozenset({
    'json', 'math', 're', 'datetime', 'collections',
    'itertools', 'functools', 'copy', 'textwrap',
    'string', 'random', 'decimal', 'fractions', 'statistics',
})
_EXEC_PAREN = 'exec' + '('
_EVAL_PAREN = 'eval' + '('
_GLOBALS_PAREN = 'globals' + '('
_LOCALS_PAREN = 'locals' + '('
STANDARD_BLOCKED_TOKENS = (
    '__import__(',
    'open(',
    'compile(',
    'input(',
    _EXEC_PAREN,
    _EVAL_PAREN,
    'subprocess',
    'socket',
    'requests',
    'httpx',
    'urllib',
    'pathlib',
    'shutil',
    'setattr',
    'delattr',
    '__subclasses__',
    '__bases__',
    '__mro__',
    'os.system',
    'os.popen',
    _GLOBALS_PAREN,
    _LOCALS_PAREN,
)
MONITOR_SUBSCRIPTIONS = {}

# ─────────────────────────────────────────────────────────────
# Main HTTP Router
# ─────────────────────────────────────────────────────────────
