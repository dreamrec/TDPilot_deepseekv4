"""TDPilot API — TD 2025 native introspection (Tier 1 port from CLI).

Seven read-only tools that probe TouchDesigner's runtime state. All
execute in single-digit milliseconds on the cook thread — no streaming,
no polling, no IPC. Output is small (a few KB at most) so chat UI
rendering stays cheap.

Tools:
  td_python_env_status     sys.version, paths, top-level installed modules
  td_threading_status      active threads, daemon flags, main thread name
  td_logger_status         project cookLogger state + recent message count
  td_tdresources_inspect   TDResources categories + sample contents
  td_component_standardize audit a COMP for naming/color/tag standards
  td_color_pipeline        project color management settings
  td_audit_project         recursive audit of a subtree, flag issues

All assume TD globals (``op``, ``project``, ``ui``, ``tdu``) are
available — the loader injects them, same as ``tdpilot_api_user_tools``.
Outside TD (unit tests) the handlers return ``{"error": "running
outside TouchDesigner"}`` rather than crashing.
"""

from __future__ import annotations

import sys
from typing import Any


def _outside_td(reason: str) -> dict:
    """Standard error response when a TD global isn't available."""
    return {"error": f"{reason} — running outside TouchDesigner?"}


# ---------------------------------------------------------------------------
# Python environment probes
# ---------------------------------------------------------------------------


def handle_python_env_status(body: dict) -> dict:
    """Return Python version, executable, sys.path, and a sampled list of
    top-level installed modules. Useful for diagnosing "ImportError"
    issues inside TD."""
    import platform

    info: dict[str, Any] = {
        "python_version": sys.version,
        "python_implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
        "sys_path_entries": len(sys.path),
        "sys_path": sys.path[:25],  # cap to keep response small
    }
    if len(sys.path) > 25:
        info["sys_path_truncated"] = True

    # Sampled top-level modules — names only, no introspection. Filter
    # to user-visible modules (skip leading underscore).
    modules = sorted(
        name for name in sys.modules if "." not in name and not name.startswith("_") and len(name) > 1
    )
    info["loaded_module_count"] = len(modules)
    info["loaded_modules_sample"] = modules[:50]
    if len(modules) > 50:
        info["loaded_modules_truncated"] = True

    return {"ok": True, **info}


def handle_threading_status(body: dict) -> dict:
    """Return active threads, daemon status, current/main thread names.
    Useful for diagnosing "why is my callback not firing" thread issues."""
    import threading

    threads: list[dict] = []
    for t in threading.enumerate():
        threads.append(
            {
                "name": t.name,
                "ident": t.ident,
                "daemon": t.daemon,
                "alive": t.is_alive(),
            }
        )
    return {
        "ok": True,
        "active_count": threading.active_count(),
        "main_thread": threading.main_thread().name,
        "current_thread": threading.current_thread().name,
        "threads": threads,
    }


# ---------------------------------------------------------------------------
# TD-specific probes (require TD globals)
# ---------------------------------------------------------------------------


def handle_logger_status(body: dict) -> dict:
    """Return TD's project cookLogger state and recent message count.

    Reads ``project.cookLogger`` if present. Some TD versions don't
    expose it — return a structured "not available" rather than failing.
    """
    try:
        proj = project  # type: ignore[name-defined]
    except NameError:
        return _outside_td("project global not available")

    logger = getattr(proj, "cookLogger", None)
    if logger is None:
        return {
            "ok": True,
            "logger_available": False,
            "hint": "project.cookLogger not exposed in this TD build.",
        }

    out: dict[str, Any] = {"ok": True, "logger_available": True}
    for attr in ("active", "logCount", "maxLogCount"):
        val = getattr(logger, attr, None)
        if val is not None:
            out[attr] = val
    return out


def handle_tdresources_inspect(body: dict) -> dict:
    """Inspect TDResources — list categories and sample contents.

    TDResources is TD's bundled-asset registry (palette components,
    examples, defaults). Useful for ``td_recommend_official_component``
    and ``td_find_official_example`` to know what's installed.

    Falls back gracefully when the registry isn't exposed (TD versions
    differ on whether ``ui.tdResources`` exists, and older builds don't
    have it at all). Returns ``ok=true`` with ``available=false`` rather
    than an error so the agent can continue without a "tool failed"
    message in the transcript.
    """
    try:
        ui_ref = ui  # type: ignore[name-defined]
    except NameError:
        return _outside_td("ui global not available")

    # Probe a few attribute-name variants to handle TD build differences.
    res = None
    accessor_used = None
    for attr in ("tdResources", "TDResources"):
        candidate = getattr(ui_ref, attr, None)
        if candidate is not None:
            res = candidate
            accessor_used = f"ui.{attr}"
            break

    if res is None:
        return {
            "ok": True,
            "available": False,
            "hint": (
                "ui.tdResources / ui.TDResources not exposed in this TD build. "
                "TDResources is a TD 2025-class registry; on older builds, fall "
                "back to td_lookup_palette_component (knowledge corpus) for the "
                "same kind of information."
            ),
        }

    # TDResources exposes attribute-based access to top-level groups.
    # We sample a known subset so the response stays small.
    sample_groups = ("Palette", "Examples", "TDResources", "Builtin")
    inspected: dict[str, Any] = {}
    for group_name in sample_groups:
        group = getattr(res, group_name, None)
        if group is None:
            continue
        # Most groups expose a `.children` collection.
        children = getattr(group, "children", None)
        if children is None:
            inspected[group_name] = {"present": True, "children": "n/a"}
            continue
        try:
            names = [c.name for c in children][:30]
        except Exception:
            names = []
        inspected[group_name] = {"present": True, "child_count": len(children), "sample": names}

    return {
        "ok": True,
        "available": True,
        "accessor": accessor_used,
        "groups_inspected": list(inspected.keys()),
        "groups": inspected,
    }


def handle_color_pipeline(body: dict) -> dict:
    """Return project color management settings.

    Reads project-level color params (color management mode, gamma,
    display LUTs). Useful for diagnosing "why does my render look
    different" colorspace issues.
    """
    try:
        proj = project  # type: ignore[name-defined]
    except NameError:
        return _outside_td("project global not available")

    info: dict[str, Any] = {"ok": True}
    # These params are exposed on the project object in TD 2025; missing
    # ones get None (not an error).
    for attr in (
        "colorManagementMode",
        "colorRendering",
        "displayColorSpace",
        "viewerColorSpace",
        "ocioConfig",
        "useGammaInRender",
    ):
        val = getattr(proj, attr, None)
        if val is None:
            par = getattr(getattr(proj, "par", None), attr, None)
            if par is not None:
                try:
                    val = par.eval()
                except Exception:
                    val = None
        if val is not None:
            info[attr] = str(val)
    return info


def handle_component_standardize(body: dict) -> dict:
    """Audit a COMP for project standards: naming convention, color
    coding, tag policy, comment presence. Returns a list of issues
    with severity (error / warning / info).

    Pure read — never mutates. The agent then decides whether to fix.
    """
    path = (body.get("path") or "/project1").strip()
    try:
        comp = op(path)  # type: ignore[name-defined]
    except NameError:
        return _outside_td("op global not available")
    if comp is None:
        return {"error": f"Path not found: {path}"}
    if not getattr(comp, "isCOMP", False):
        return {"error": f"Path is not a COMP: {path}"}

    issues: list[dict] = []

    # Check naming: lowercase + underscore preferred for COMP children.
    name = comp.name
    if not name.replace("_", "").islower() and not name.startswith("project"):
        issues.append(
            {
                "severity": "info",
                "rule": "naming_convention",
                "message": f"COMP name '{name}' uses mixed case; prefer lowercase_underscore for non-stock COMPs.",
            }
        )

    # Check color coding (default grey suggests no thought given).
    try:
        r, g, b = comp.color
        if (r, g, b) == (0.55, 0.55, 0.55):
            issues.append(
                {
                    "severity": "info",
                    "rule": "color_coding",
                    "message": "COMP uses default grey color. Color-coding by role aids navigation.",
                }
            )
    except Exception:
        pass

    # Check tags presence (tags help organization).
    tags = list(getattr(comp, "tags", set()) or set())
    if not tags:
        issues.append(
            {
                "severity": "info",
                "rule": "tags",
                "message": "No tags set. Tags ('shader', 'ui', 'audio', etc.) make subnets searchable.",
            }
        )

    # Check comment.
    comment = getattr(comp, "comment", "") or ""
    if not comment.strip():
        issues.append(
            {
                "severity": "info",
                "rule": "comment",
                "message": "No comment set. A one-line purpose statement helps future-you.",
            }
        )

    # Check child count — overstuffed COMPs hurt readability.
    children = list(getattr(comp, "children", []))
    if len(children) > 50:
        issues.append(
            {
                "severity": "warning",
                "rule": "child_count",
                "message": f"COMP has {len(children)} children — consider splitting into sub-COMPs.",
            }
        )

    return {
        "ok": True,
        "path": path,
        "name": name,
        "tags": tags,
        "child_count": len(children),
        "issues": issues,
        "issue_count": len(issues),
    }


def handle_audit_project(body: dict) -> dict:
    """Recursively audit a project subtree — collect errors, warnings,
    and standards-compliance issues across every COMP descendant.

    Stops at ``max_depth``. Aggregates results so the agent gets one
    actionable summary instead of N tool calls.
    """
    path = (body.get("path") or "/project1").strip()
    try:
        max_depth = max(1, min(int(body.get("max_depth", 5) or 5), 20))
    except (TypeError, ValueError):
        max_depth = 5

    try:
        root = op(path)  # type: ignore[name-defined]
    except NameError:
        return _outside_td("op global not available")
    if root is None:
        return {"error": f"Path not found: {path}"}

    # Collect issues across the subtree. Per-node we look at:
    #   - .errors (TD's compile/cook errors)
    #   - .warnings
    #   - standardize-style soft issues (only on COMPs)
    summary: dict[str, int] = {"errors": 0, "warnings": 0, "info": 0}
    findings: list[dict] = []

    def _walk(node, depth: int) -> None:
        if depth > max_depth:
            return
        node_path = node.path
        try:
            errs = getattr(node, "errors", "") or ""
        except Exception:
            errs = ""
        if errs:
            summary["errors"] += 1
            findings.append({"path": node_path, "severity": "error", "message": str(errs).strip()[:300]})
        try:
            warns = getattr(node, "warnings", "") or ""
        except Exception:
            warns = ""
        if warns:
            summary["warnings"] += 1
            findings.append({"path": node_path, "severity": "warning", "message": str(warns).strip()[:300]})

        # Soft standards on COMPs.
        if getattr(node, "isCOMP", False):
            try:
                std = handle_component_standardize({"path": node_path})
            except Exception:
                std = {"issues": []}
            for iss in std.get("issues", []):
                summary["info"] += 1
                findings.append({"path": node_path, **iss})

        # Recurse into children.
        children = getattr(node, "children", None)
        if children:
            for child in children:
                _walk(child, depth + 1)

    _walk(root, 0)

    return {
        "ok": True,
        "path": path,
        "max_depth": max_depth,
        "summary": summary,
        "total_findings": len(findings),
        "findings": findings[:200],  # cap response size
        "truncated": len(findings) > 200,
    }
