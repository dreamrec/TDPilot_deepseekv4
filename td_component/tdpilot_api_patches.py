"""TDPilot API — snapshots + transactional patch sessions.

Two safety mechanisms with very different blast radius:

  1. **Snapshots** (heavy): full ``.toe`` files saved to
     ``~/.tdpilot-api/snapshots/``. ``snapshot_save`` is cheap; restore
     is intentionally NOT exposed as a tool because it would reload the
     entire project (destroying the agent COMP itself mid-call).
     Users restore manually via TD's File > Open.

  2. **Patch sessions** (lightweight, transactional): wrap a multi-
     step build in TD's native ``ui.undo.startBlock(name)`` /
     ``ui.undo.endBlock()`` pair. On commit, the block becomes one
     undo step in TD's stack. On rollback, we end the block and call
     ``project.undo()`` once — TD reverts the entire group atomically.

     This is dramatically simpler than the ``td_patch_*`` family in the
     MCP variant (which built its own typed-op rollback engine on top
     of exec_python). TD's native undo is well-tested and covers every
     mutation that `parent.create()`, `node.par.X = ...`,
     `node.destroy()`, etc. produce.

State: ONE active patch at a time, stored in ``comp.storage`` so it
survives module reloads. Re-entry attempted while a patch is active
returns an error.

Exposed handlers:
    handle_snapshot_save     write current project state to a .toe
    handle_snapshot_list     enumerate saved snapshots
    handle_patch_begin       start an undo block + state tracking
    handle_patch_validate    td_get_errors on the scope path
    handle_patch_commit      close the undo block, discard state
    handle_patch_rollback    close + undo, discard state
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SNAPSHOTS_DIR = Path.home() / ".tdpilot-api" / "snapshots"
PATCH_STATE_KEY = "tdpilot_api_active_patch"


def _ensure_snapshots_dir() -> None:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "snap"


def _comp_for_state():
    """Return the parent COMP for storing patch state. Falls back to
    None outside TD (unit tests run without ``parent()`` defined)."""
    try:
        return parent()  # type: ignore[name-defined]
    except NameError:
        return None


def _get_patch_state() -> dict | None:
    comp = _comp_for_state()
    if comp is None:
        return None
    return comp.fetch(PATCH_STATE_KEY, None)


def _set_patch_state(state: dict | None) -> None:
    comp = _comp_for_state()
    if comp is None:
        return
    if state is None:
        try:
            comp.unstore(PATCH_STATE_KEY)
        except Exception:
            # fallback for TD versions without unstore
            comp.store(PATCH_STATE_KEY, None)
    else:
        comp.store(PATCH_STATE_KEY, state)


# ---------------------------------------------------------------------------
# Snapshot handlers
# ---------------------------------------------------------------------------


def handle_snapshot_save(body: dict) -> dict:
    """Save current project to ~/.tdpilot-api/snapshots/<slug>_<timestamp>.toe.

    Cheap to call — TD's ``project.save(path)`` writes a .toe. Use this
    BEFORE risky multi-step builds you can't easily reproduce. Users
    restore via TD's File > Open menu (we deliberately don't expose
    snapshot_restore as a tool because ``project.load()`` would
    destroy the agent COMP itself mid-execution).
    """
    name = (body.get("name") or "").strip()
    if not name:
        name = f"auto_{int(time.time())}"

    _ensure_snapshots_dir()
    slug = _slugify(name)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{slug}_{timestamp}.toe"
    filepath = SNAPSHOTS_DIR / filename

    try:
        # `project` is a TD global. Outside TD, this raises.
        save_external = bool(body.get("save_external_toxs", False))
        project.save(str(filepath), saveExternalToxs=save_external)  # type: ignore[name-defined]
    except NameError:
        return {"error": "project global not available — running outside TouchDesigner?"}
    except Exception as exc:
        return {"error": f"project.save failed: {type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "name": name,
        "filename": filename,
        "path": str(filepath),
        "size_bytes": filepath.stat().st_size if filepath.is_file() else 0,
    }


def handle_snapshot_list(body: dict) -> dict:
    """List saved snapshots, newest first."""
    if not SNAPSHOTS_DIR.is_dir():
        return {"ok": True, "count": 0, "snapshots": []}

    entries: list[dict] = []
    for p in sorted(SNAPSHOTS_DIR.glob("*.toe"), key=lambda x: x.stat().st_mtime, reverse=True):
        entries.append(
            {
                "filename": p.name,
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(p.stat().st_mtime),
                ),
            }
        )
    return {"ok": True, "count": len(entries), "snapshots": entries}


# ---------------------------------------------------------------------------
# Patch session handlers — leverage TD's ui.undo block API
# ---------------------------------------------------------------------------


def handle_patch_begin(body: dict) -> dict:
    """Start a transactional patch session.

    Opens a TD undo block (ui.undo.startBlock) — every operation
    performed before patch_commit/patch_rollback gets grouped into a
    single undo step. On rollback, project.undo() reverts the entire
    block atomically.

    Only one patch session can be active at a time. Re-entry without
    commit/rollback returns an error.
    """
    state = _get_patch_state()
    if state is not None:
        return {
            "error": "Another patch session is already active.",
            "active_patch": state.get("name"),
            "hint": "Commit or rollback the active patch before beginning a new one.",
        }

    name = (body.get("name") or "patch").strip()
    scope_path = (body.get("scope_path") or "/").strip()

    try:
        ui.undo.startBlock(name)  # type: ignore[name-defined]
    except NameError:
        return {"error": "ui.undo not available — running outside TouchDesigner?"}
    except Exception as exc:
        return {"error": f"ui.undo.startBlock failed: {type(exc).__name__}: {exc}"}

    new_state = {
        "name": name,
        "scope_path": scope_path,
        "started_at": time.time(),
        "step_count": 0,
    }
    _set_patch_state(new_state)
    return {"ok": True, "patch": new_state}


def handle_patch_validate(body: dict) -> dict:
    """Run td_get_errors on the patch session's scope_path. Use BETWEEN
    operations to confirm the network is still healthy before
    continuing or before committing."""
    state = _get_patch_state()
    if state is None:
        return {
            "error": "No active patch session.",
            "hint": "Call patch_begin first.",
        }

    scope_path = state.get("scope_path", "/")

    # Reach the dispatcher to call td_get_errors. We're on cook thread
    # already so use the raw dispatcher. PR-19 (F-18) — single-source
    # helper replaces the bespoke walk.
    try:
        from tdpilot_api_lookup import get_raw_dispatcher  # type: ignore[import-not-found]

        raw = get_raw_dispatcher()
    except ImportError as exc:
        return {"error": f"Could not access dispatcher: {exc}"}
    if raw is None:
        return {"error": "Raw dispatcher not available"}

    try:
        errors_result = raw("td_get_errors", {"path": scope_path, "recursive": True})
    except Exception as exc:
        return {"error": f"td_get_errors raised: {exc}"}

    return {
        "ok": True,
        "patch_name": state["name"],
        "scope_path": scope_path,
        "errors_result": errors_result,
    }


def handle_patch_commit(body: dict) -> dict:
    """Finalize the patch session — close the undo block. The whole
    sequence is now ONE step in TD's undo stack (manual Cmd+Z still
    reverts everything if the user wants)."""
    state = _get_patch_state()
    if state is None:
        return {"error": "No active patch session to commit."}

    try:
        ui.undo.endBlock()  # type: ignore[name-defined]
    except NameError:
        return {"error": "ui.undo not available"}
    except Exception as exc:
        return {"error": f"ui.undo.endBlock failed: {type(exc).__name__}: {exc}"}

    _set_patch_state(None)
    return {
        "ok": True,
        "committed": state["name"],
        "duration_seconds": round(time.time() - state.get("started_at", time.time()), 2),
    }


def handle_patch_rollback(body: dict) -> dict:
    """Roll back the entire patch session atomically.

    Closes the undo block and immediately calls project.undo() to
    revert the whole grouped sequence. Use after a failed step or
    when the user wants to abandon the build.
    """
    state = _get_patch_state()
    if state is None:
        return {"error": "No active patch session to rollback."}

    endblock_warning: str | None = None
    try:
        ui.undo.endBlock()  # type: ignore[name-defined]
    except NameError:
        return {"error": "ui.undo not available"}
    except Exception as exc:
        # endBlock failed — undo state is now ambiguous (the block may or
        # may not be closed). Continue with project.undo() because that
        # is still the user's best chance of reverting; surface the
        # warning in the result so the caller knows the rollback was not
        # clean and may want to inspect the project manually.
        endblock_warning = f"endBlock failed during rollback: {type(exc).__name__}: {exc}"
        print(f"[tdpilot_api_patches] {endblock_warning}")

    try:
        # ``project.undo()`` reverts one step. With the just-closed
        # block as the most recent step, this reverts everything done
        # since patch_begin.
        project.undo()  # type: ignore[name-defined]
    except NameError:
        _set_patch_state(None)
        return {"error": "project.undo not available"}
    except Exception as exc:
        _set_patch_state(None)
        return {"error": f"project.undo failed: {type(exc).__name__}: {exc}"}

    _set_patch_state(None)
    result = {
        "ok": True,
        "rolled_back": state["name"],
        "duration_seconds": round(time.time() - state.get("started_at", time.time()), 2),
    }
    if endblock_warning:
        result["warning"] = endblock_warning
    return result
