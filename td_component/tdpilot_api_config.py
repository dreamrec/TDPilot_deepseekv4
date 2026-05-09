"""
TDPilot API — config resolution for the standalone (in-TD) agent.

Resolution order for the API key (first hit wins):
  1. Process env: DEEPSEEK_API_KEY  (or TDPILOT_API_KEY override)
  2. <CONFIG_DIR>/config.json       (chmod 600 expected; first-run UI writes here)
  3. <CONFIG_DIR>/.env              (KEY=VALUE format, mirrors .tdpilot-dpsk4.env)

The key MUST never be saved into the .toe file. The COMP's parameter for
the key is intended to hold an EXPRESSION that calls fetch_api_key() at
cook time, so saving the .toe persists the expression, not the value.

Storage roots
-------------
2.1.3 — chat-pipe storage moved under the dpsk4 variant root so the
distribution is self-contained. New default is::

    ~/.tdpilot-dpsk4/api/<subdir>

Pre-2.1.3 the chat-pipe wrote to ``~/.tdpilot-api/<subdir>``. To avoid
breaking existing user data, ``resolve_user_dir`` falls back to the
legacy path when it exists with content. Tools (``memory_save``,
``recipe_save``, etc.) that imported the path constants from this
module pick up the new location automatically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 2.1.3 — namespaced storage roots. The legacy `~/.tdpilot-api/` path
# remains supported for backwards compatibility. These constants are
# kept as module-level exports for cosmetic backwards-compat, but the
# canonical resolver is ``resolve_user_dir`` (which re-evaluates
# ``Path.home()`` on every call so test monkey-patches and dynamic
# HOME changes are honoured).
USER_BASE_NEW = Path.home() / ".tdpilot-dpsk4" / "api"
USER_BASE_LEGACY = Path.home() / ".tdpilot-api"


def resolve_user_dir(subdir: str = "") -> Path:
    """Return the storage directory for ``subdir`` (or the root when
    empty). Prefers the new ``~/.tdpilot-dpsk4/api/<subdir>`` location;
    falls back to ``~/.tdpilot-api/<subdir>`` when that legacy path
    already exists with content (so existing user data keeps working).

    Per-subdir resolution is deliberate — a user can have
    ``~/.tdpilot-api/memory/`` populated but no ``~/.tdpilot-api/recipes/``;
    memory continues to read/write the legacy location while recipes
    starts fresh under the new root. No bulk migration runs at import
    time (filesystem moves should be a user-driven choice).

    ``Path.home()`` is re-evaluated on every call so test monkey-
    patches (e.g. ``test_firstrun.py``'s ``_stub_pristine_home``) take
    effect even though this module was already imported.
    """
    home = Path.home()
    new_root = home / ".tdpilot-dpsk4" / "api"
    legacy_root = home / ".tdpilot-api"
    new = new_root / subdir if subdir else new_root
    legacy = legacy_root / subdir if subdir else legacy_root
    if legacy.exists():
        try:
            if any(legacy.iterdir()):
                return legacy
        except OSError:
            pass
    try:
        new.mkdir(parents=True, exist_ok=True)
    except OSError:
        # If we can't create the new dir (read-only home, weird perms),
        # fall back to whatever legacy returned. The caller may still
        # error on its first write, but at least we don't crash at
        # import time.
        return legacy
    return new


# CONFIG_DIR / CONFIG_JSON / ENV_FILE — stable names used across the
# codebase and tests. Resolved lazily-once at import time so existing
# callers don't need to change. The API key file naturally stays at
# the legacy path for users who set it up before 2.1.3.
CONFIG_DIR = resolve_user_dir("")
CONFIG_JSON = CONFIG_DIR / "config.json"
ENV_FILE = CONFIG_DIR / ".env"

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TURN_BUDGET = 10
DEFAULT_TEMPERATURE = 0.7

ENV_KEY_PRIMARY = "DEEPSEEK_API_KEY"
ENV_KEY_OVERRIDE = "TDPILOT_API_KEY"


def _read_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _read_config_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_api_key() -> str:
    """Resolve the DeepSeek API key from env > config.json > .env. Returns '' if not set."""
    for env_name in (ENV_KEY_OVERRIDE, ENV_KEY_PRIMARY):
        v = os.environ.get(env_name, "").strip()
        if v:
            return v
    cfg = _read_config_json(CONFIG_JSON)
    v = str(cfg.get("api_key", "")).strip()
    if v:
        return v
    env_data = _read_env_file(ENV_FILE)
    return env_data.get(ENV_KEY_PRIMARY, env_data.get(ENV_KEY_OVERRIDE, "")).strip()


def fetch_setting(name: str, default):
    """Resolve a non-secret setting from env (TDPILOT_API_<NAME>) > config.json > default."""
    env_name = f"TDPILOT_API_{name.upper()}"
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    cfg = _read_config_json(CONFIG_JSON)
    return cfg.get(name, default)


def save_api_key_to_config(key: str) -> None:
    """First-run helper. Writes the key to ~/.tdpilot-api/config.json with
    mode 0600 on POSIX. Windows ignores POSIX permissions; we log a
    warning instead of pretending the file is restricted."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = _read_config_json(CONFIG_JSON)
    cfg["api_key"] = key.strip()
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if os.name == "nt":
        # POSIX 0o600 is a no-op on NTFS; surface this once so users
        # know the API-key file is world-readable on Windows unless
        # they tighten ACLs themselves.
        print(
            "[tdpilot_api_config] note: file permissions not restricted on Windows; "
            f"consider hardening ACLs on {CONFIG_JSON} manually if shared machine"
        )
    else:
        os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_JSON)


def redact(s: str) -> str:
    """Replace any occurrence of the live API key in s with [REDACTED]. For log/UI safety."""
    key = fetch_api_key()
    if key and key in s:
        return s.replace(key, "[REDACTED]")
    return s


def redact_paths(s: str) -> str:
    """Strip home-directory and config-dir paths from a string. Used on
    tracebacks before they're returned to the model — the model doesn't
    need to know the user's home path, and leaking it into the chat
    transcript / DeepSeek logs is a soft information leak.

    Replaces (in order):
      * The session-token file path (if present in the message body).
      * The TDPilot config dir (``<CONFIG_DIR>``).
      * The user's home dir (``$HOME``).

    Each replacement uses a stable placeholder so the redacted message
    is still useful for debugging.
    """
    if not isinstance(s, str) or not s:
        return s
    out = s
    home = str(Path.home()) if Path else ""
    # Replace BOTH the active config dir AND the legacy one — even after
    # 2.1.3 a user may still have files in ~/.tdpilot-api/, and a leaked
    # absolute path would fail to round-trip cleanly without this pair.
    for src, token in (
        (str(CONFIG_DIR), "<CONFIG_DIR>"),
        (str(USER_BASE_NEW), "<TDPILOT_API_HOME>"),
        (str(USER_BASE_LEGACY), "<TDPILOT_API_HOME>"),
    ):
        if src and src in out:
            out = out.replace(src, token)
    if home and home in out:
        out = out.replace(home, "~")
    return out


def resolved_config() -> dict:
    """Bundle for the agent. Never includes the api_key — caller fetches that separately."""
    return {
        "model": str(fetch_setting("model", DEFAULT_MODEL)),
        "base_url": str(fetch_setting("base_url", DEFAULT_BASE_URL)).rstrip("/"),
        "max_tokens": int(fetch_setting("max_tokens", DEFAULT_MAX_TOKENS)),
        "turn_budget": int(fetch_setting("turn_budget", DEFAULT_TURN_BUDGET)),
        "temperature": float(fetch_setting("temperature", DEFAULT_TEMPERATURE)),
    }
