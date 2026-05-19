"""TDPilot API — snapshots + transactional patch sessions.

Three safety mechanisms with very different blast radius:

  1. **Full project snapshots** (heavy): ``.toe`` files saved to
     ``~/.tdpilot-api/snapshots/``. ``snapshot_save`` is cheap; full
     restore is intentionally NOT exposed as a tool because
     ``project.load()`` would reload the entire project (destroying
     the agent COMP itself mid-call). Users restore manually via TD's
     File > Open.

  2. **Scoped snapshots** (NEW 2026-05-11, medium-weight): JSON
     manifest of a scope's nodes + params + connections, EXCLUDING the
     agent COMP. ``snapshot_save_scoped`` writes a small JSON file;
     ``snapshot_restore_scoped`` diffs the current scope against the
     manifest and applies create/delete/update operations to converge.
     Agent COMP survives because it's in the exclusion list. Captures
     structural shape + parameters. Does NOT capture: DAT text,
     extension code, geometry data, animation curves — for those use
     the full .toe snapshot.

  3. **Patch sessions** (lightweight, transactional): wrap a multi-
     step build in TD's native ``ui.undo.startBlock(name)`` /
     ``ui.undo.endBlock()`` pair. On commit, the block becomes one
     undo step in TD's stack. On rollback, we end the block and call
     ``project.undo()`` once — TD reverts the entire group atomically.

     Limitation: only valid WITHIN a single turn (TD's undo stack
     can't span a thread boundary safely). For cross-turn safety use
     a scoped snapshot.

State: ONE active patch at a time, stored in ``comp.storage`` so it
survives module reloads. Re-entry attempted while a patch is active
returns an error.

Exposed handlers:
    handle_snapshot_save           write current project state to a .toe
    handle_snapshot_list           enumerate saved snapshots (incl. scoped)
    handle_snapshot_save_scoped    write a JSON manifest of a scope (NEW)
    handle_snapshot_restore_scoped diff + apply a scoped manifest (NEW)
    handle_patch_begin             start an undo block + state tracking
    handle_patch_validate          td_get_errors on the scope path
    handle_patch_commit            close the undo block, discard state
    handle_patch_rollback          close + undo, discard state
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

# 2.1.3 — namespaced under ~/.tdpilot-dpsk4/api/snapshots with legacy fallback.
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    SNAPSHOTS_DIR = resolve_user_dir("snapshots")
except ImportError:
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
    """List saved snapshots (both ``.toe`` full snapshots and
    ``.scoped.json`` scoped manifests), newest first.
    """
    if not SNAPSHOTS_DIR.is_dir():
        return {"ok": True, "count": 0, "snapshots": []}

    entries: list[dict] = []
    # 2026-05-11 — include scoped .json manifests alongside .toe files.
    patterns = ["*.toe", "*.scoped.json"]
    candidates: list[Path] = []
    for pat in patterns:
        candidates.extend(SNAPSHOTS_DIR.glob(pat))
    for p in sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True):
        kind = "scoped" if p.suffix == ".json" else "toe"
        entries.append(
            {
                "filename": p.name,
                "path": str(p),
                "kind": kind,
                "size_bytes": p.stat().st_size,
                "modified": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(p.stat().st_mtime),
                ),
            }
        )
    return {"ok": True, "count": len(entries), "snapshots": entries}


# ---------------------------------------------------------------------------
# Scoped snapshot handlers (2026-05-11) — JSON manifests of a scope's
# structural shape, restorable mid-conversation because the agent COMP
# is excluded from both save and restore.
# ---------------------------------------------------------------------------

# Manifest schema version. Bump this if the manifest shape changes
# in a backwards-incompatible way. Restore refuses unknown versions.
_SCOPED_MANIFEST_VERSION = "tdpilot_api_snapshot_scoped_v1"

# Default scope/exclusions. The agent COMP path is computed dynamically
# from ``parent()`` at handler invocation time so we adapt to whatever
# the user named the container.
_DEFAULT_SCOPE = "/project1"
_DEFAULT_EXCLUDES_ALWAYS = ("/project1/tdpilot", "/project1/mcp_server")


def _agent_comp_path() -> str:
    """Return the absolute path of the agent's COMP, e.g.
    ``/project1/tdpilot_API``. Falls back to a sentinel string outside TD
    (test/unit-test environments without a parent())."""
    try:
        return parent().path  # type: ignore[name-defined]
    except Exception:
        return "/__no_agent_comp__"


def _excluded(path: str, excludes: list[str]) -> bool:
    """True if ``path`` is in ``excludes`` or is a descendant of any
    excluded prefix. Paths are TD absolute paths."""
    for ex in excludes:
        if path == ex or path.startswith(ex + "/"):
            return True
    return False


def _serialize_param(par) -> dict | None:
    """Capture one parameter as a JSON-safe dict, or None if the param
    should be skipped (default value standard param). Custom params are
    ALWAYS captured. Reference-style params (instanceop/material/camera/
    geometry/lights/etc.) are stored as their string-rendered path so
    restore can re-resolve them via td_set_params.

    Returned shape:
        {"mode": "constant"|"expression"|"export", "val": ..., "expr": ...}
    """
    try:
        mode = str(par.mode).split(".")[-1].lower()
    except Exception:
        return None
    entry: dict[str, Any] = {"mode": mode}
    # Capture expression text for non-constant modes
    if mode == "expression":
        try:
            entry["expr"] = str(par.expr or "")
        except Exception:
            entry["expr"] = ""
        return entry
    if mode == "export":
        try:
            # Exported params don't carry a meaningful local value; the
            # source is what matters. Restore will re-bind by setting
            # the same expression-like reference string.
            entry["expr"] = str(getattr(par, "expr", "") or "")
        except Exception:
            entry["expr"] = ""
        return entry
    # Constant mode — capture .val
    try:
        v = par.val
    except Exception:
        return None
    # Skip default standard params to keep the manifest small.
    try:
        is_custom = bool(par.isCustom)
    except Exception:
        is_custom = False
    if not is_custom:
        try:
            if v == par.default:
                return None
        except Exception:
            pass
    # JSON-serialize primitives; coerce TD-specific types to strings.
    if isinstance(v, (str, int, float, bool)) or v is None:
        entry["val"] = v
    else:
        entry["val"] = str(v)
    return entry


def _serialize_node(node) -> dict | None:
    """Capture one node as a manifest dict. Returns None for nodes that
    shouldn't be serialized (system/internal)."""
    try:
        node_path = node.path
        node_type = node.type  # canonical "noiseTOP" etc.
        family = node.family
    except Exception:
        return None
    params: dict[str, dict] = {}
    try:
        par_list = list(node.pars())
    except Exception:
        par_list = []
    for par in par_list:
        # Skip the standard "Common" page housekeeping params (cookmode,
        # display, render, viewer, etc.) that get reset on create — they
        # rarely encode user intent. Keep them if the user has explicitly
        # changed them away from default though (handled by the default-
        # comparison inside _serialize_param).
        try:
            name = par.name
        except Exception:
            continue
        entry = _serialize_param(par)
        if entry is not None:
            params[name] = entry
    try:
        nx, ny = float(node.nodeX), float(node.nodeY)
    except Exception:
        nx = ny = 0.0
    return {
        "path": node_path,
        "name": node.name,
        "parent_path": node.parent().path,
        "type": node_type,
        "family": family,
        "nodeX": nx,
        "nodeY": ny,
        "params": params,
    }


def _walk_scope(scope_path: str, excludes: list[str]) -> list:
    """Return all non-excluded operator descendants of ``scope_path``.

    2026-05-11 — original implementation used ``findChildren(depth=999,
    includeNested=True)``. That call SILENTLY returned an empty list on
    TD 2025.32820 (the ``includeNested`` kwarg isn't recognised and the
    no-arg form raises ``TypeError: issubclass() arg 2 must be a class``
    internally). Switched to a manual BFS using ``op.children`` — works
    on every TD version, defends per-node against non-COMP nodes that
    don't expose ``.children``.

    The scope itself is NOT included in the list — only its descendants.
    Nodes under excluded prefixes are skipped entirely (children of an
    excluded COMP are NOT walked, even if the excluded COMP itself was
    only listed as ``/project1/tdpilot_API`` — the descendants are
    caught by the ``path.startswith(ex + "/")`` check in ``_excluded``).
    """
    root = op(scope_path)  # type: ignore[name-defined]
    if root is None:
        return []
    out: list = []
    try:
        stack = list(root.children)
    except Exception:
        return []
    while stack:
        n = stack.pop()
        if _excluded(n.path, excludes):
            continue
        out.append(n)
        # Recurse into anything that exposes .children (COMPs). Non-COMP
        # nodes either don't have the attribute or return an empty list;
        # be defensive either way.
        try:
            kids = list(n.children) if n.children else []
        except Exception:
            kids = []
        stack.extend(kids)
    return out


def _walk_connections(nodes: list) -> list[dict]:
    """Walk all connections among the given node set. Returns
    ``[{from, from_index, to, to_index}, ...]``. Connections to/from
    nodes NOT in the set are dropped (typically because the peer is in
    the excludes list).
    """
    path_set = {n.path for n in nodes}
    out: list[dict] = []
    for src in nodes:
        try:
            out_conns = list(getattr(src, "outputConnectors", []) or [])
        except Exception:
            continue
        for out_idx, connector in enumerate(out_conns):
            try:
                conns = list(getattr(connector, "connections", []) or [])
            except Exception:
                continue
            for c in conns:
                try:
                    target = c.owner
                    in_idx = c.index
                except Exception:
                    continue
                if target.path not in path_set:
                    continue
                out.append(
                    {
                        "from": src.path,
                        "from_index": int(out_idx),
                        "to": target.path,
                        "to_index": int(in_idx),
                    }
                )
    return out


def handle_snapshot_save_scoped(body: dict) -> dict:
    """Save a JSON manifest of the scope's structural shape.

    Args (body):
        name: snapshot label (required, slugged into filename).
        scope: TD path to snapshot. Default ``/project1``.
        excludes: additional paths to exclude (agent COMP, classic
            tdpilot COMP, and mcp_server are ALWAYS excluded).

    Returns:
        {ok, name, filename, path, size_bytes, node_count,
         connection_count}
    """
    name = (body.get("name") or "").strip()
    if not name:
        name = f"scoped_{int(time.time())}"
    scope = (body.get("scope") or _DEFAULT_SCOPE).strip()
    user_excludes = body.get("excludes") or []
    if not isinstance(user_excludes, list):
        return {"error": "excludes must be a list of TD paths"}

    excludes = list(_DEFAULT_EXCLUDES_ALWAYS) + [_agent_comp_path()]
    for x in user_excludes:
        if isinstance(x, str) and x not in excludes:
            excludes.append(x)

    try:
        nodes = _walk_scope(scope, excludes)
    except NameError:
        return {"error": "op() global not available — running outside TouchDesigner?"}
    except Exception as exc:
        return {"error": f"scope walk failed: {type(exc).__name__}: {exc}"}

    node_entries: list[dict] = []
    for n in nodes:
        entry = _serialize_node(n)
        if entry is not None:
            node_entries.append(entry)
    connection_entries = _walk_connections(nodes)

    manifest = {
        "version": _SCOPED_MANIFEST_VERSION,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "name": name,
        "scope": scope,
        "excludes": excludes,
        "node_count": len(node_entries),
        "connection_count": len(connection_entries),
        "nodes": node_entries,
        "connections": connection_entries,
    }

    _ensure_snapshots_dir()
    slug = _slugify(name)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{slug}_{timestamp}.scoped.json"
    filepath = SNAPSHOTS_DIR / filename
    try:
        filepath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:
        return {"error": f"write failed: {type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "name": name,
        "filename": filename,
        "path": str(filepath),
        "size_bytes": filepath.stat().st_size,
        "node_count": len(node_entries),
        "connection_count": len(connection_entries),
        "scope": scope,
        "excludes": excludes,
    }


def _find_scoped_manifest(name_or_path: str) -> Path | None:
    """Resolve a scoped snapshot by name OR path. Returns the newest
    matching ``.scoped.json`` file, or None if nothing matches.

    H-1 audit fix (2026-05-19): absolute-path inputs are sandboxed to
    ``SNAPSHOTS_DIR``. A prompt-injected agent (or any caller bypassing
    the v2.5.3 approval gate via ``Authmode=open`` /
    ``TDPILOT_DISABLE_TOOL_APPROVAL=1``) used to be able to pass
    ``path='/Users/<user>/.ssh/some.json'`` and coerce-read+parse the
    file. The manifest-version check on line 528 limited mutation, but
    the parsed JSON still surfaced in error messages — file-existence
    probing + arbitrary-JSON read. Symlinks are followed BEFORE the
    root check, so a symlink-in-SNAPSHOTS_DIR pointing at
    ``~/.ssh/known_hosts`` is rejected.
    """
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            # RuntimeError catches symlink loops; missing files fall
            # through to the slug-based lookup below.
            resolved = None
        if resolved is not None and resolved.is_file():
            try:
                snapshots_root = SNAPSHOTS_DIR.resolve()
            except FileNotFoundError:
                snapshots_root = SNAPSHOTS_DIR
            try:
                resolved.relative_to(snapshots_root)
            except ValueError:
                # Path is outside SNAPSHOTS_DIR — refuse instead of
                # silently reading. Returning None lets the caller emit
                # a "No scoped manifest found" error which is still
                # informative without leaking the parsed JSON.
                return None
            return resolved
    # Try as a name: find any .scoped.json starting with the slug
    if not SNAPSHOTS_DIR.is_dir():
        return None
    slug = _slugify(name_or_path)
    matches = sorted(
        SNAPSHOTS_DIR.glob(f"{slug}_*.scoped.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if matches:
        return matches[0]
    # Last-resort: any json snapshot that contains the name substring
    matches = sorted(
        SNAPSHOTS_DIR.glob("*.scoped.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for m in matches:
        if slug in m.stem:
            return m
    return None


def handle_snapshot_restore_scoped(body: dict) -> dict:
    """Diff current scope state against a saved manifest and apply
    changes to converge.

    Args (body):
        name: snapshot name OR full path (alternative to path).
        path: full path to a .scoped.json file (alternative to name).
        dry_run: if True, report the diff without applying. Default False.

    Returns:
        {ok, applied: {created, deleted, params_updated, connected,
         disconnected}, errors_after, manifest_meta}
    """
    name = (body.get("name") or "").strip()
    path_arg = (body.get("path") or "").strip()
    dry_run = bool(body.get("dry_run", False))

    lookup_key = path_arg or name
    if not lookup_key:
        return {"error": "Provide either 'name' or 'path' for the snapshot to restore."}
    manifest_path = _find_scoped_manifest(lookup_key)
    if manifest_path is None:
        return {
            "error": f"No scoped manifest found for {lookup_key!r}.",
            "hint": "Call snapshot_list to see available snapshots.",
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"manifest read failed: {type(exc).__name__}: {exc}"}
    if manifest.get("version") != _SCOPED_MANIFEST_VERSION:
        return {
            "error": (
                f"manifest version {manifest.get('version')!r} not supported "
                f"by this build (expected {_SCOPED_MANIFEST_VERSION})."
            ),
        }

    scope = manifest.get("scope", _DEFAULT_SCOPE)
    excludes = list(manifest.get("excludes") or [])
    # Always re-add current agent COMP path even if manifest predates
    # the current COMP rename — defensive in case the agent moves.
    agent_path = _agent_comp_path()
    if agent_path not in excludes:
        excludes.append(agent_path)

    manifest_nodes_by_path = {n["path"]: n for n in manifest.get("nodes", [])}
    manifest_conns = manifest.get("connections", [])

    # Walk current state
    try:
        current_nodes = _walk_scope(scope, excludes)
    except NameError:
        return {"error": "op() global not available — running outside TouchDesigner?"}
    current_paths = {n.path for n in current_nodes}
    manifest_paths = set(manifest_nodes_by_path.keys())

    # Compute diff
    to_create = list(manifest_paths - current_paths)
    to_delete = list(current_paths - manifest_paths)
    to_update = list(manifest_paths & current_paths)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "manifest": {
                "path": str(manifest_path),
                "name": manifest.get("name"),
                "ts": manifest.get("ts"),
                "node_count": manifest.get("node_count"),
                "connection_count": manifest.get("connection_count"),
            },
            "diff": {
                "to_create": to_create,
                "to_delete": to_delete,
                "to_update_params": to_update,
            },
        }

    # Reach the dispatcher for tool calls.
    try:
        from tdpilot_api_lookup import get_raw_dispatcher  # type: ignore[import-not-found]

        raw = get_raw_dispatcher()
    except ImportError as exc:
        return {"error": f"dispatcher import failed: {exc}"}
    if raw is None:
        return {"error": "raw dispatcher not available"}

    applied = {
        "created": [],
        "deleted": [],
        "params_updated": [],
        "params_failed": [],
        "connected": [],
        "disconnected": [],
        "errors": [],
    }

    # Step 1: delete extras FIRST (avoids name collisions when creating).
    for p in to_delete:
        if _excluded(p, excludes):
            continue
        try:
            r = raw("td_delete_node", {"path": p})
            if isinstance(r, dict) and r.get("success") is False:
                applied["errors"].append({"op": "delete", "path": p, "result": r})
            else:
                applied["deleted"].append(p)
        except Exception as exc:
            applied["errors"].append({"op": "delete", "path": p, "exc": str(exc)})

    # Step 2: create missing nodes.
    for p in to_create:
        spec = manifest_nodes_by_path[p]
        try:
            r = raw(
                "td_create_node",
                {
                    "parent_path": spec["parent_path"],
                    "op_type": spec["type"],
                    "name": spec["name"],
                    "node_x": spec.get("nodeX"),
                    "node_y": spec.get("nodeY"),
                },
            )
            if isinstance(r, dict) and r.get("success") is False:
                applied["errors"].append({"op": "create", "path": p, "result": r})
            else:
                applied["created"].append(p)
        except Exception as exc:
            applied["errors"].append({"op": "create", "path": p, "exc": str(exc)})

    # Step 3: set params on everything in manifest (create + update).
    for p in to_create + to_update:
        spec = manifest_nodes_by_path[p]
        params = spec.get("params") or {}
        if not params:
            continue
        # Translate manifest entry shape -> td_set_params shape
        set_payload: dict[str, Any] = {}
        for pname, entry in params.items():
            if not isinstance(entry, dict):
                continue
            mode = entry.get("mode")
            if mode == "expression":
                set_payload[pname] = {"expr": entry.get("expr", "")}
            elif mode == "export":
                # Best-effort: same expression text restores the binding
                set_payload[pname] = {"expr": entry.get("expr", "")}
            else:
                # constant
                if "val" in entry:
                    set_payload[pname] = {"val": entry["val"]}
        if not set_payload:
            continue
        try:
            r = raw("td_set_params", {"path": p, "params": set_payload})
            if isinstance(r, dict) and r.get("success") is False:
                applied["params_failed"].append({"path": p, "result": r})
            else:
                applied["params_updated"].append(p)
        except Exception as exc:
            applied["params_failed"].append({"path": p, "exc": str(exc)})

    # Step 4: connections. Strategy — disconnect all current connections
    # among the manifest's node set, then create the manifest connections
    # fresh. This is simpler and safer than computing a connection diff
    # because connection identity in TD is positional.
    current_nodes_after = _walk_scope(scope, excludes)
    current_conns = _walk_connections(current_nodes_after)
    # Disconnect any current connection NOT in the manifest set.
    manifest_conn_keys = {(c["from"], c["from_index"], c["to"], c["to_index"]) for c in manifest_conns}
    for c in current_conns:
        key = (c["from"], c["from_index"], c["to"], c["to_index"])
        if key in manifest_conn_keys:
            continue
        try:
            r = raw(
                "td_disconnect",
                {
                    "path": c["to"],
                    "connector_type": "input",
                    "index": c["to_index"],
                },
            )
            if isinstance(r, dict) and r.get("success") is False:
                applied["errors"].append({"op": "disconnect", "edge": key, "result": r})
            else:
                applied["disconnected"].append(key)
        except Exception as exc:
            applied["errors"].append({"op": "disconnect", "edge": key, "exc": str(exc)})
    # Create manifest connections not currently present.
    current_conn_keys = {(c["from"], c["from_index"], c["to"], c["to_index"]) for c in current_conns}
    for c in manifest_conns:
        key = (c["from"], c["from_index"], c["to"], c["to_index"])
        if key in current_conn_keys:
            continue
        try:
            r = raw(
                "td_connect_nodes",
                {
                    "source_path": c["from"],
                    "source_output": c["from_index"],
                    "target_path": c["to"],
                    "target_input": c["to_index"],
                },
            )
            if isinstance(r, dict) and r.get("success") is False:
                applied["errors"].append({"op": "connect", "edge": key, "result": r})
            else:
                applied["connected"].append(key)
        except Exception as exc:
            applied["errors"].append({"op": "connect", "edge": key, "exc": str(exc)})

    # Step 5: post-restore error check.
    try:
        errors_after = raw("td_get_errors", {"path": scope, "recurse": True})
    except Exception as exc:
        errors_after = {"error": str(exc)}

    return {
        "ok": True,
        "manifest": {
            "path": str(manifest_path),
            "name": manifest.get("name"),
            "ts": manifest.get("ts"),
            "scope": scope,
        },
        "applied": {
            "created_count": len(applied["created"]),
            "deleted_count": len(applied["deleted"]),
            "params_updated_count": len(applied["params_updated"]),
            "connected_count": len(applied["connected"]),
            "disconnected_count": len(applied["disconnected"]),
            "error_count": len(applied["errors"]),
            "details": applied,
        },
        "errors_after": errors_after,
    }


# ---------------------------------------------------------------------------
# Patch session handlers — leverage TD's ui.undo block API
# ---------------------------------------------------------------------------


def handle_patch_begin(body: dict) -> dict:
    """Start a transactional patch session.

    Opens a TD undo block (ui.undo.startBlock) — every operation
    performed before patch_commit/patch_rollback gets grouped into a
    single undo step. On rollback, ``ui.undo.undo()`` reverts the
    entire block atomically.

    Only one patch session can be active at a time. Re-entry without
    commit/rollback returns an error — UNLESS the prior session is
    older than 5 minutes (almost certainly orphaned by a failed
    commit/rollback) OR the caller passes ``force=True``, in which
    case we auto-clean and start fresh.
    """
    name = (body.get("name") or "patch").strip()
    scope_path = (body.get("scope_path") or "/").strip()
    force = bool(body.get("force", False))

    state = _get_patch_state()
    recovered_from = None
    if state is not None:
        # v2.0.1: detect orphaned sessions. patch_commit / patch_rollback
        # used to leave state set when their underlying TD calls failed,
        # so the next patch_begin would refuse forever. We now auto-clear
        # in those handlers, but ALSO defend here for sessions left over
        # from older .tox builds. Stale = older than 5 minutes.
        age_s = time.time() - state.get("started_at", time.time())
        STALE_AFTER_S = 300
        if not force and age_s < STALE_AFTER_S:
            return {
                "error": "Another patch session is already active.",
                "active_patch": state.get("name"),
                "active_age_seconds": round(age_s, 1),
                "hint": (
                    "Commit or rollback the active patch before beginning "
                    "a new one. If the prior session was orphaned (commit "
                    "or rollback raised), retry with force=True to clear "
                    "and start fresh."
                ),
            }
        # Either stale or force=True. Defensively close any lingering
        # undo block before we open a new one.
        try:
            ui.undo.endBlock()  # type: ignore[name-defined]
        except Exception:
            pass
        recovered_from = {
            "name": state.get("name"),
            "age_seconds": round(age_s, 1),
            "reason": "force" if force else "stale",
        }
        print(f"[tdpilot_api_patches] auto-cleared {recovered_from}")

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
    result: dict = {"ok": True, "patch": new_state}
    if recovered_from is not None:
        result["recovered_from"] = recovered_from
    return result


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
    reverts everything if the user wants).

    v2.0.1: state is cleared regardless of whether endBlock succeeds.
    The previous behavior left state set when endBlock raised, which
    meant the next patch_begin saw a phantom "already active" session
    and refused. The undo block close is best-effort — if TD's undo
    stack is in a weird state (block already closed by something else,
    block never opened cleanly), the operations performed during the
    session are still applied to the network and the agent should be
    able to start a fresh session right after.
    """
    state = _get_patch_state()
    if state is None:
        return {"error": "No active patch session to commit."}

    endblock_warning: str | None = None
    try:
        ui.undo.endBlock()  # type: ignore[name-defined]
    except NameError:
        # No TD env — clear state defensively so unit tests don't get stuck.
        _set_patch_state(None)
        return {"error": "ui.undo not available"}
    except Exception as exc:
        endblock_warning = f"ui.undo.endBlock failed: {type(exc).__name__}: {exc}"
        print(f"[tdpilot_api_patches] {endblock_warning}")

    # ALWAYS clear state, regardless of endBlock outcome. This is the
    # core v2.0.1 fix — without it, a single endBlock failure orphans
    # the session forever.
    _set_patch_state(None)

    result: dict = {
        "ok": True,
        "committed": state["name"],
        "duration_seconds": round(time.time() - state.get("started_at", time.time()), 2),
    }
    if endblock_warning:
        result["warning"] = endblock_warning
        result["note"] = (
            "Undo block close raised but state has been cleared so a new "
            "patch_begin will succeed. Operations performed during this "
            "session remain applied to the network — manual Cmd+Z in TD "
            "may revert them as separate steps if needed."
        )
    return result


def handle_patch_rollback(body: dict) -> dict:
    """Roll back the entire patch session atomically.

    Closes the undo block and immediately calls ``ui.undo.undo()`` to
    revert the whole grouped sequence. Use after a failed step or
    when the user wants to abandon the build.

    v2.0.1: switched from ``project.undo()`` to ``ui.undo.undo()``.
    The ``project`` global in TD 2025 is a Project instance with no
    ``.undo()`` method; the undo machinery lives on ``ui.undo``.
    Pre-v2.0.1 every rollback raised
    ``AttributeError: 'td.Project' object has no attribute 'undo'``.

    State is cleared regardless of whether either underlying TD call
    succeeds, so a failed rollback doesn't orphan the session.
    """
    state = _get_patch_state()
    if state is None:
        return {"error": "No active patch session to rollback."}

    endblock_warning: str | None = None
    try:
        ui.undo.endBlock()  # type: ignore[name-defined]
    except NameError:
        _set_patch_state(None)
        return {"error": "ui.undo not available"}
    except Exception as exc:
        endblock_warning = f"endBlock failed during rollback: {type(exc).__name__}: {exc}"
        print(f"[tdpilot_api_patches] {endblock_warning}")

    undo_warning: str | None = None
    try:
        # ui.undo.undo() reverts one step. With the just-closed block as
        # the most recent step, this reverts everything done since
        # patch_begin. Replaces the broken pre-v2.0.1 project.undo() call.
        ui.undo.undo()  # type: ignore[name-defined]
    except NameError:
        _set_patch_state(None)
        return {"error": "ui.undo not available"}
    except Exception as exc:
        undo_warning = f"ui.undo.undo failed: {type(exc).__name__}: {exc}"
        print(f"[tdpilot_api_patches] {undo_warning}")

    _set_patch_state(None)
    result: dict = {
        "ok": True,
        "rolled_back": state["name"],
        "duration_seconds": round(time.time() - state.get("started_at", time.time()), 2),
    }
    if endblock_warning:
        result["warning_endblock"] = endblock_warning
    if undo_warning:
        result["warning_undo"] = undo_warning
    if endblock_warning or undo_warning:
        result["note"] = (
            "Rollback signalled but TD's undo machinery may not have fully "
            "reverted the session. Inspect the project manually and use "
            "Cmd+Z in TD if needed. State has been cleared so a new "
            "patch_begin will succeed."
        )
    return result
