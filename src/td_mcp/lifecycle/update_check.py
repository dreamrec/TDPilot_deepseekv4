"""Update check — v2.5.7.

Read-only update awareness. Compares the running MCP server version
against the latest GitHub Release on ``dreamrec/TDPilot_deepseekv4``
AND checks whether the bundled ``.tox`` source-hash matches the
current source tree.

Why both checks?

* The MCP server can be upgraded via ``npx tdpilot-dpsk4@latest`` or
  ``pip install -U tdpilot-dpsk4``, but the ``.tox`` files are baked
  binaries that can only be rebuilt inside a running TouchDesigner
  session.
* When the server and ``.tox`` versions drift, the chat-pipe COMP
  silently runs old code. The freshness check surfaces that drift to
  the agent BEFORE the user reports "I rebuilt but it still doesn't
  work".

Full auto-apply (download + atomic swap) lives in v2.7's
``td_self_update``. v2.5.7 is read-only — surfaces the advisory; user
runs the upgrade commands.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from td_mcp import __version__ as CURRENT_VERSION

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_API_URL = "https://api.github.com/repos/dreamrec/TDPilot_deepseekv4/releases/latest"
USER_AGENT = f"tdpilot-dpsk4/{CURRENT_VERSION} (update-check)"

# In-memory cache TTL. GitHub's API has generous rate limits (60 req/h
# anonymous) but we still cache to keep td_check_for_updates cheap.
CACHE_TTL_SECONDS = 3600.0  # 1 hour

# Network timeout. Don't block the agent for long on a stuck request.
HTTP_TIMEOUT_SECONDS = 5.0

# Path to the .tox source-hash files, relative to repo root.
TOX_HASH_FILES = (
    "td_component/.tox-source-hash.json",  # MCP-side .tox
    "td_component/.tox-api-source-hash.json",  # API-side .tox
)


@dataclass
class UpdateCheckResult:
    """Structured update-check output."""

    server: dict[str, Any] = field(default_factory=dict)
    tox: dict[str, Any] = field(default_factory=dict)
    advice: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def clear_cache() -> None:
    """Test helper — invalidates the in-memory cache."""
    _cache["data"] = None
    _cache["ts"] = 0.0


def _cached_or_fetch() -> tuple[dict | None, str | None]:
    """Return (release_dict, error_str). Either is set, never both."""
    now = time.monotonic()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["data"], None

    try:
        req = urllib.request.Request(GITHUB_API_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return None, f"GitHub API HTTP {resp.status}"
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"HTTPError {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"URLError: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"network error: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"GitHub API returned non-JSON: {exc}"

    _cache["data"] = data
    _cache["ts"] = now
    return data, None


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _parse_version(tag: str) -> tuple[int, ...]:
    """Best-effort semver-ish tuple parse. ``v2.5.0`` → ``(2, 5, 0)``.

    Non-numeric segments collapse to 0 so pre-release suffixes don't
    crash the compare. ``v2.5.0-alpha.1`` → ``(2, 5, 0, 0, 1)``.
    """
    tag = tag.lstrip("vV").strip()
    out: list[int] = []
    for chunk in tag.replace("-", ".").split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            out.append(0)
    return tuple(out)


def _compare_versions(current: str, latest: str) -> str:
    """Return ``"older"``, ``"same"``, or ``"newer"`` relative to latest."""
    a = _parse_version(current)
    b = _parse_version(latest)
    if a < b:
        return "older"
    if a > b:
        return "newer"
    return "same"


# ---------------------------------------------------------------------------
# Tox freshness
# ---------------------------------------------------------------------------


def _check_tox_freshness(repo_root: Path) -> dict[str, Any]:
    """For each .tox, check whether the stored source-hash matches the
    live source files.

    Reuses the existing scripts/check_tox_freshness.py / check_tox_api_freshness.py
    machinery — we don't re-implement the hash; we just READ the
    sidecar hash file and compare against the live computation.

    Returns a dict keyed by .tox filename with ``hash_matches`` (bool),
    ``rebuild_needed`` (bool), and ``reason`` (str | None).
    """
    out: dict[str, Any] = {}
    for rel_hash_file in TOX_HASH_FILES:
        hash_file = repo_root / rel_hash_file
        # Filename in the stored hash, used as the dict key.
        if "api" in hash_file.name:
            tox_name = "tdpilot_API.tox"
        else:
            tox_name = "tdpilot-dpsk4.tox"

        if not hash_file.exists():
            out[tox_name] = {
                "hash_matches": False,
                "rebuild_needed": True,
                "reason": f"sidecar hash file missing: {rel_hash_file}",
            }
            continue

        try:
            stored = json.loads(hash_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out[tox_name] = {
                "hash_matches": False,
                "rebuild_needed": True,
                "reason": f"sidecar hash unreadable: {exc}",
            }
            continue

        # The hash file's `source_files` list tells us which files
        # contribute to the .tox. We recompute their hash over the
        # current tree and compare.
        # NOTE: stored field is ``tox_source_hash`` (not just ``hash``)
        # — matches the format written by
        # build_export_mcp_tox._write_tox_source_hash and
        # build_tdpilot_api_tox._write_api_tox_source_hash.
        stored_hash = stored.get("tox_source_hash")
        source_files = stored.get("source_files", [])
        if not stored_hash or not source_files:
            out[tox_name] = {
                "hash_matches": False,
                "rebuild_needed": True,
                "reason": "sidecar hash malformed (missing fields)",
            }
            continue

        live_hash = _compute_source_hash(repo_root, source_files)
        matches = live_hash == stored_hash
        out[tox_name] = {
            "hash_matches": matches,
            "rebuild_needed": not matches,
            "reason": None
            if matches
            else f"source files modified since last build (live={live_hash[:16]}..., stored={stored_hash[:16]}...)",
        }
    return out


def _compute_source_hash(repo_root: Path, source_files: list[str]) -> str:
    """Mirror the byte-stream scheme used by both build scripts.

    See ``td_component/build_tdpilot_api_tox.py::_compute_api_tox_source_hash``
    and ``td_component/build_export_mcp_tox.py::_compute_tox_source_hash``.

    Scheme (NOT sorted — order from the source_files tuple matters)::

        sha = sha256()
        for rel in source_files:
            sha.update(rel.encode("utf-8"))
            sha.update(b"\\x00")
            sha.update(file_bytes(rel))
            sha.update(b"\\x00")
        return sha.hexdigest()

    NUL separators keep filename and content boundaries unambiguous
    without quoting. The build scripts iterate ``_API_TOX_SOURCE_FILES``
    in declared order, so we use the order from the stored manifest.
    """
    import hashlib

    sha = hashlib.sha256()
    for rel in source_files:
        path = repo_root / rel
        if not path.is_file():
            continue  # matches build script's `if not os.path.isfile(path): continue`
        sha.update(rel.encode("utf-8"))
        sha.update(b"\x00")
        sha.update(path.read_bytes())
        sha.update(b"\x00")
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Repo-root detection
# ---------------------------------------------------------------------------


def _detect_repo_root() -> Path:
    """Find the repository root containing ``td_component/`` and
    ``pyproject.toml``. Walks up from this module's location."""
    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        if (ancestor / "td_component").is_dir() and (ancestor / "pyproject.toml").is_file():
            return ancestor
    # Defensive default — return the immediate ancestor of `src/`.
    return here.parents[2]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_for_updates(repo_root: Path | None = None) -> UpdateCheckResult:
    """Return a structured ``UpdateCheckResult`` for the current install.

    Cached for 1 hour to keep ``td_check_for_updates`` cheap.
    """
    repo_root = repo_root or _detect_repo_root()
    release, error = _cached_or_fetch()

    if release is None:
        return UpdateCheckResult(
            server={"current": CURRENT_VERSION, "check_failed": True, "reason": error},
            tox=_check_tox_freshness(repo_root),
            advice=(
                f"Could not check GitHub Releases ({error}). "
                "Try again later, or check manually at "
                "https://github.com/dreamrec/TDPilot_deepseekv4/releases. "
                "Tox freshness was checked locally and is reported below."
            ),
            checked_at=time.time(),
        )

    latest_tag = release.get("tag_name", "").lstrip("vV")
    release_url = release.get("html_url", "")
    cmp = _compare_versions(CURRENT_VERSION, latest_tag)
    has_update = cmp == "older"

    tox_status = _check_tox_freshness(repo_root)
    stale_tox = [name for name, info in tox_status.items() if info["rebuild_needed"]]

    # Compose actionable advice.
    advice_parts: list[str] = []
    if has_update:
        advice_parts.append(
            f"Server update available: {CURRENT_VERSION} → {latest_tag}. "
            "Run `npx tdpilot-dpsk4@latest` (npm install) or "
            "`pip install -U tdpilot-dpsk4` (pip install)."
        )
    if stale_tox:
        advice_parts.append(
            "Rebuild stale .tox file(s) in TouchDesigner: "
            + ", ".join(stale_tox)
            + ". Use the Textport rebuild recipe from "
            "feedback_td_tox_rebuild_recipe (single-line statements; "
            "set TD_MCP_REPO_ROOT first)."
        )
    if not advice_parts:
        advice_parts.append("Up to date. No action required.")

    return UpdateCheckResult(
        server={
            "current": CURRENT_VERSION,
            "latest": latest_tag,
            "has_update": has_update,
            "comparison": cmp,
            "url": release_url,
        },
        tox=tox_status,
        advice=" ".join(advice_parts),
        checked_at=time.time(),
    )
