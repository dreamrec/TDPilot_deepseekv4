"""TDPilot API — macro engine port (Sprint 4.4).

Macros are parametrized templates: predefined network patterns with
named inputs that the agent can instantiate with one tool call. Where
recipes (Sprint 3.1) replay an exact tool sequence verbatim, macros
generate a fresh sequence on the fly from a template + user-supplied
overrides.

Example: ``macro_run({macro_type: 'feedback_loop', parent_path:
'/project1', overrides: {feedback_opacity: 0.85}})`` creates a
feedback → decay → composite → null chain wired correctly, with the
opacity baked in.

Ported from src/td_mcp/macros/ (engine + loader + models + templates,
~759 LoC) — same data model as the dpsk4 variant so user JSON
templates are portable. Two changes vs the upstream:

  1. Synchronous (no asyncio). The dpsk4 variant's MacroEngine awaits
     an HTTP client; we call the cook-thread dispatcher directly.
  2. User template dir is ``~/.tdpilot-api/macros/`` not
     ``~/.tdpilot-dpsk4/templates/``.

Bundled templates (5): feedback_loop, post_processing, audio_reactive,
particle_gpu, feedback_displacement. Same as dpsk4 — copying the
template definitions verbatim keeps their behaviour identical.

Tool surface:
  macro_list           — enumerate available macros + warnings
  macro_get            — show a macro's parameter schema + structure
  macro_run            — instantiate the macro at parent_path with
                         optional name_prefix, position, overrides
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# ===========================================================================
# Models — frozen dataclasses for template definitions.
# Ported verbatim from src/td_mcp/macros/models.py.
# ===========================================================================


@dataclass(frozen=True)
class ParamSpec:
    type: str
    default: Any
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "default": self.default,
            "min": self.min_value,
            "max": self.max_value,
            "description": self.description,
        }


@dataclass
class NodeSpec:
    node_type: str
    name: str
    dx: int = 0
    dy: int = 0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionSpec:
    source: str
    target: str
    source_index: int = 0
    target_index: int = 0


@dataclass(frozen=True)
class ExpressionSpec:
    node: str
    param: str
    expr: str


@dataclass(frozen=True)
class ParamTarget:
    node: str
    param: str
    mode: str = "value"  # value | expr
    template: str | None = None


@dataclass(frozen=True)
class NodeRefParam:
    node: str
    param: str
    target_node: str


@dataclass
class MacroTemplate:
    name: str
    description: str
    nodes: list[NodeSpec]
    connections: list[ConnectionSpec]
    expressions: list[ExpressionSpec] = field(default_factory=list)
    node_references: list[NodeRefParam] = field(default_factory=list)
    param_schema: dict[str, ParamSpec] = field(default_factory=dict)
    param_targets: dict[str, list[ParamTarget]] = field(default_factory=dict)
    entry_node: str | None = None
    exit_node: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "node_count": len(self.nodes),
            "connection_count": len(self.connections),
            "entry_node": self.entry_node,
            "exit_node": self.exit_node,
            "params": {k: v.to_dict() for k, v in self.param_schema.items()},
        }


# ===========================================================================
# Bundled template library.
# Ported verbatim from src/td_mcp/macros/templates.py.
# 5 templates: feedback_loop, post_processing, audio_reactive,
# particle_gpu, feedback_displacement.
# ===========================================================================


def _build_default_templates() -> dict[str, MacroTemplate]:
    templates: dict[str, MacroTemplate] = {}

    templates["feedback_loop"] = MacroTemplate(
        name="feedback_loop",
        description="Classic feedback chain: feedback -> level -> composite -> out.",
        nodes=[
            NodeSpec("feedbackTOP", "feedback", dx=0, dy=0),
            NodeSpec("levelTOP", "decay", dx=220, dy=0, params={"opacity": 0.95}),
            NodeSpec("compositeTOP", "merge", dx=440, dy=0, params={"operand": "over"}),
            NodeSpec("nullTOP", "out", dx=660, dy=0),
        ],
        connections=[
            ConnectionSpec("feedback", "decay"),
            ConnectionSpec("decay", "merge", source_index=0, target_index=0),
            ConnectionSpec("merge", "out"),
        ],
        node_references=[
            NodeRefParam(node="feedback", param="top", target_node="out"),
        ],
        param_schema={
            "feedback_opacity": ParamSpec(
                type="float",
                default=0.95,
                min_value=0.0,
                max_value=1.0,
                description="Trail persistence in level TOP opacity.",
            ),
        },
        param_targets={
            "feedback_opacity": [ParamTarget(node="decay", param="opacity", mode="value")],
        },
        entry_node="merge",
        exit_node="out",
    )

    templates["post_processing"] = MacroTemplate(
        name="post_processing",
        description="Simple post-FX chain: level -> blur -> out.",
        nodes=[
            NodeSpec("levelTOP", "grade", dx=0, dy=0, params={"brightness1": 1.0}),
            NodeSpec("blurTOP", "blur", dx=220, dy=0, params={"filtersize": 4}),
            NodeSpec("nullTOP", "out", dx=440, dy=0),
        ],
        connections=[
            ConnectionSpec("grade", "blur"),
            ConnectionSpec("blur", "out"),
        ],
        param_schema={
            "brightness": ParamSpec(
                type="float",
                default=1.0,
                min_value=0.0,
                max_value=3.0,
                description="Overall gain (levelTOP brightness1).",
            ),
            "blur_size": ParamSpec(
                type="int",
                default=4,
                min_value=0,
                max_value=128,
                description="Blur kernel size.",
            ),
        },
        param_targets={
            "brightness": [ParamTarget(node="grade", param="brightness1", mode="value")],
            "blur_size": [ParamTarget(node="blur", param="filtersize", mode="value")],
        },
        entry_node="grade",
        exit_node="out",
    )

    templates["audio_reactive"] = MacroTemplate(
        name="audio_reactive",
        description="Audio signal preprocessing chain with gain stage and null output.",
        nodes=[
            NodeSpec("audiodeviceinCHOP", "audio_in", dx=0, dy=0),
            NodeSpec("analyzeCHOP", "audio_level", dx=220, dy=0),
            NodeSpec("mathCHOP", "gain", dx=440, dy=0, params={"mult": 1.0}),
            NodeSpec("nullCHOP", "out", dx=660, dy=0),
        ],
        connections=[
            ConnectionSpec("audio_in", "audio_level"),
            ConnectionSpec("audio_level", "gain"),
            ConnectionSpec("gain", "out"),
        ],
        param_schema={
            "gain": ParamSpec(
                type="float",
                default=1.0,
                min_value=0.0,
                max_value=10.0,
                description="Math CHOP multiplier.",
            ),
        },
        param_targets={
            "gain": [ParamTarget(node="gain", param="mult", mode="value")],
        },
        entry_node="audio_in",
        exit_node="out",
    )

    templates["particle_gpu"] = MacroTemplate(
        name="particle_gpu",
        description="Minimal POP chain: particle -> noise -> render.",
        nodes=[
            NodeSpec("particlePOP", "particles", dx=0, dy=0),
            NodeSpec("noisePOP", "noise", dx=220, dy=0),
            NodeSpec("renderPOP", "render", dx=440, dy=0),
            NodeSpec("nullTOP", "out", dx=660, dy=0),
        ],
        connections=[
            ConnectionSpec("particles", "noise"),
            ConnectionSpec("noise", "render"),
            ConnectionSpec("render", "out"),
        ],
        param_schema={},
        entry_node="particles",
        exit_node="out",
    )

    templates["feedback_displacement"] = MacroTemplate(
        name="feedback_displacement",
        description="Feedback displacement loop with source noise and composite merge.",
        nodes=[
            NodeSpec("noiseTOP", "source", dx=0, dy=0, params={"type": "simplex3d"}),
            NodeSpec("feedbackTOP", "feedback", dx=220, dy=0),
            NodeSpec("levelTOP", "decay", dx=440, dy=0, params={"opacity": 0.95}),
            NodeSpec("displaceTOP", "displace", dx=660, dy=0, params={"weightx": 0.05, "weighty": 0.05}),
            NodeSpec("compositeTOP", "merge", dx=880, dy=0, params={"operand": "over"}),
            NodeSpec("nullTOP", "out", dx=1100, dy=0),
        ],
        connections=[
            ConnectionSpec("feedback", "decay"),
            ConnectionSpec("source", "displace", source_index=0, target_index=0),
            ConnectionSpec("decay", "displace", source_index=0, target_index=1),
            ConnectionSpec("source", "merge", source_index=0, target_index=0),
            ConnectionSpec("displace", "merge", source_index=0, target_index=1),
            ConnectionSpec("merge", "out"),
        ],
        node_references=[
            NodeRefParam(node="feedback", param="top", target_node="out"),
        ],
        expressions=[
            ExpressionSpec(node="source", param="tz", expr="absTime.seconds * 0.3"),
        ],
        param_schema={
            "feedback_opacity": ParamSpec(
                type="float",
                default=0.95,
                min_value=0.0,
                max_value=1.0,
                description="Feedback level opacity.",
            ),
            "displacement_weight": ParamSpec(
                type="float",
                default=0.05,
                min_value=0.0,
                max_value=1.0,
                description="Displace weight (x and y).",
            ),
        },
        param_targets={
            "feedback_opacity": [ParamTarget(node="decay", param="opacity", mode="value")],
            "displacement_weight": [
                ParamTarget(node="displace", param="weightx", mode="value"),
                ParamTarget(node="displace", param="weighty", mode="value"),
            ],
        },
        entry_node="source",
        exit_node="out",
    )

    return templates


# ===========================================================================
# User template loader.
# Ported from src/td_mcp/macros/loader.py with default dir adjusted.
# ===========================================================================


# 2.1.3 — namespaced under ~/.tdpilot-dpsk4/api/macros with legacy fallback.
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    USER_MACROS_DIR = resolve_user_dir("macros")
except ImportError:
    USER_MACROS_DIR = Path.home() / ".tdpilot-api" / "macros"


def _load_user_templates(directory: str | Path | None = None) -> tuple[dict[str, MacroTemplate], list[str]]:
    template_dir = Path(directory).expanduser() if directory else USER_MACROS_DIR
    if not template_dir.exists() or not template_dir.is_dir():
        return {}, []

    templates: dict[str, MacroTemplate] = {}
    warnings: list[str] = []

    for file_path in sorted(template_dir.glob("*.json")):
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"{file_path.name}: failed to parse JSON ({exc})")
            continue

        entries: list[dict[str, Any]]
        if isinstance(raw, dict) and isinstance(raw.get("templates"), list):
            entries = [e for e in raw["templates"] if isinstance(e, dict)]
        elif isinstance(raw, dict):
            entries = [raw]
        else:
            warnings.append(f"{file_path.name}: expected object or {{'templates': [...]}}")
            continue

        for index, entry in enumerate(entries):
            try:
                t = _parse_template(entry)
            except Exception as exc:
                warnings.append(f"{file_path.name}[{index}]: {exc}")
                continue
            templates[t.name] = t

    return templates, warnings


def _parse_template(data: dict[str, Any]) -> MacroTemplate:
    name = _as_str(data.get("name"), field="name")
    description = _as_str(data.get("description", ""), field="description")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("'nodes' must be a non-empty list")
    nodes = [_parse_node(item) for item in raw_nodes]

    raw_connections = data.get("connections")
    if not isinstance(raw_connections, list):
        raise ValueError("'connections' must be a list")
    connections = [_parse_connection(item) for item in raw_connections]

    raw_expressions = data.get("expressions", [])
    if not isinstance(raw_expressions, list):
        raise ValueError("'expressions' must be a list when provided")
    expressions = [_parse_expression(item) for item in raw_expressions]

    raw_schema = data.get("param_schema", {})
    if not isinstance(raw_schema, dict):
        raise ValueError("'param_schema' must be an object when provided")
    param_schema = {k: _parse_param_spec(k, v) for k, v in raw_schema.items()}

    raw_targets = data.get("param_targets", {})
    if not isinstance(raw_targets, dict):
        raise ValueError("'param_targets' must be an object when provided")
    param_targets: dict[str, list[ParamTarget]] = {}
    for k, target_list in raw_targets.items():
        if not isinstance(target_list, list):
            raise ValueError(f"param_targets.{k} must be a list")
        param_targets[k] = [_parse_param_target(item) for item in target_list]

    raw_node_refs = data.get("node_references", [])
    if not isinstance(raw_node_refs, list):
        raise ValueError("'node_references' must be a list when provided")
    node_references = [_parse_node_ref(item) for item in raw_node_refs]

    entry_node = data.get("entry_node")
    if entry_node is not None and not isinstance(entry_node, str):
        raise ValueError("'entry_node' must be a string when provided")
    exit_node = data.get("exit_node")
    if exit_node is not None and not isinstance(exit_node, str):
        raise ValueError("'exit_node' must be a string when provided")

    return MacroTemplate(
        name=name,
        description=description,
        nodes=nodes,
        connections=connections,
        expressions=expressions,
        node_references=node_references,
        param_schema=param_schema,
        param_targets=param_targets,
        entry_node=entry_node,
        exit_node=exit_node,
    )


def _parse_node(data: Any) -> NodeSpec:
    if not isinstance(data, dict):
        raise ValueError("node item must be an object")
    return NodeSpec(
        node_type=_as_str(data.get("node_type"), field="node_type"),
        name=_as_str(data.get("name"), field="name"),
        dx=int(data.get("dx", 0)),
        dy=int(data.get("dy", 0)),
        params=dict(data.get("params", {})) if isinstance(data.get("params", {}), dict) else {},
    )


def _parse_connection(data: Any) -> ConnectionSpec:
    if not isinstance(data, dict):
        raise ValueError("connection item must be an object")
    return ConnectionSpec(
        source=_as_str(data.get("source"), field="source"),
        target=_as_str(data.get("target"), field="target"),
        source_index=int(data.get("source_index", 0)),
        target_index=int(data.get("target_index", 0)),
    )


def _parse_expression(data: Any) -> ExpressionSpec:
    if not isinstance(data, dict):
        raise ValueError("expression item must be an object")
    return ExpressionSpec(
        node=_as_str(data.get("node"), field="node"),
        param=_as_str(data.get("param"), field="param"),
        expr=_as_str(data.get("expr"), field="expr"),
    )


def _parse_param_spec(key: str, data: Any) -> ParamSpec:
    if not isinstance(data, dict):
        raise ValueError(f"param_schema.{key} must be an object")
    if "default" not in data:
        raise ValueError(f"param_schema.{key} requires 'default'")
    return ParamSpec(
        type=str(data.get("type", "any")),
        default=data.get("default"),
        min_value=_optional_float(data.get("min")),
        max_value=_optional_float(data.get("max")),
        description=str(data.get("description", "")),
    )


def _parse_param_target(data: Any) -> ParamTarget:
    if not isinstance(data, dict):
        raise ValueError("param target item must be an object")
    mode = str(data.get("mode", "value"))
    if mode not in {"value", "expr"}:
        raise ValueError("param target mode must be 'value' or 'expr'")
    template = data.get("template")
    if template is not None and not isinstance(template, str):
        raise ValueError("param target template must be a string")
    return ParamTarget(
        node=_as_str(data.get("node"), field="node"),
        param=_as_str(data.get("param"), field="param"),
        mode=mode,
        template=template,
    )


def _parse_node_ref(data: Any) -> NodeRefParam:
    if not isinstance(data, dict):
        raise ValueError("node_references item must be an object")
    return NodeRefParam(
        node=_as_str(data.get("node"), field="node"),
        param=_as_str(data.get("param"), field="param"),
        target_node=_as_str(data.get("target_node"), field="target_node"),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string")
    return value


# ===========================================================================
# Sync engine — sequentially executes a template using the cook-thread
# raw_dispatcher (same access pattern as recipe_replay).
# Ported from src/td_mcp/macros/engine.py with await/async stripped and
# td_client.request swapped to dispatcher(tool, body).
# ===========================================================================


# Per-runtime template cache + load warnings. Built on first call;
# rebuilt whenever load_user_templates picks up new files.
_TEMPLATES: dict[str, MacroTemplate] | None = None
_TEMPLATE_SOURCES: dict[str, str] = {}
_LOAD_WARNINGS: list[str] = []


def _ensure_templates() -> dict[str, MacroTemplate]:
    """Lazy-load templates. Bundled first; user templates layer over."""
    global _TEMPLATES, _TEMPLATE_SOURCES, _LOAD_WARNINGS
    if _TEMPLATES is not None:
        return _TEMPLATES
    templates = _build_default_templates()
    sources = {name: "built_in" for name in templates}
    user_templates, warnings = _load_user_templates()
    if user_templates:
        templates.update(user_templates)
        for name in user_templates:
            sources[name] = "user"
    _TEMPLATES = templates
    _TEMPLATE_SOURCES = sources
    _LOAD_WARNINGS = list(warnings)
    return _TEMPLATES


def reload_templates() -> None:
    """Force a reload from disk and rebuild the cache atomically.

    Why: a previous version cleared the globals and relied on the next
    `_ensure_templates()` call to repopulate them. A reader hitting the
    module between the clear and the next ensure call saw transiently
    empty state. Doing the rebuild inline closes that race.
    """
    global _TEMPLATES, _TEMPLATE_SOURCES, _LOAD_WARNINGS
    _TEMPLATES = None
    _TEMPLATE_SOURCES = {}
    _LOAD_WARNINGS = []
    _ensure_templates()


def _validate_param_ranges(template: MacroTemplate, values: dict[str, Any]) -> None:
    for key, value in values.items():
        spec = template.param_schema[key]
        if spec.min_value is not None and value < spec.min_value:
            raise ValueError(f"{key}={value} below minimum {spec.min_value}")
        if spec.max_value is not None and value > spec.max_value:
            raise ValueError(f"{key}={value} above maximum {spec.max_value}")


def _apply_param_targets(
    template: MacroTemplate, values: dict[str, Any]
) -> tuple[list[NodeSpec], list[ExpressionSpec]]:
    nodes = [
        NodeSpec(
            node_type=n.node_type,
            name=n.name,
            dx=n.dx,
            dy=n.dy,
            params=dict(n.params),
        )
        for n in template.nodes
    ]
    node_lookup = {n.name: n for n in nodes}
    extra_exprs: list[ExpressionSpec] = []

    for key, targets in template.param_targets.items():
        if key not in values:
            continue
        value = values[key]
        for target in targets:
            node = node_lookup.get(target.node)
            if not node:
                continue
            if target.mode == "expr":
                if not target.template:
                    continue
                extra_exprs.append(
                    ExpressionSpec(
                        node=target.node,
                        param=target.param,
                        expr=target.template.replace("{value}", str(value)),
                    )
                )
            else:
                node.params[target.param] = value

    return nodes, extra_exprs


def _get_dispatcher() -> Any:
    """Reach the parent runtime's RAW dispatcher. Same pattern as
    recipe_replay — handlers run on the cook thread; we bypass the
    cook-thread wrapper to avoid deadlock. PR-19 (F-18) — delegates
    to ``tdpilot_api_lookup.get_raw_dispatcher`` and converts the
    soft-failure ``None`` into the same exceptions callers expected."""
    try:
        from tdpilot_api_lookup import get_raw_dispatcher  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("tdpilot_api_lookup unavailable — running outside TouchDesigner?") from exc
    raw = get_raw_dispatcher()
    if raw is None:
        raise RuntimeError("agent runtime / dispatcher not available")
    return raw


def _create_macro(
    template: MacroTemplate,
    parent_path: str,
    name_prefix: str | None,
    node_x: int,
    node_y: int,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    unknown = [k for k in overrides if k not in template.param_schema]
    if unknown:
        raise ValueError(f"Unknown macro params for '{template.name}': {', '.join(sorted(unknown))}")

    resolved = {k: spec.default for k, spec in template.param_schema.items()}
    resolved.update(overrides)
    _validate_param_ranges(template, resolved)

    nodes, extra_expressions = _apply_param_targets(template, resolved)
    dispatcher = _get_dispatcher()

    created: list[dict] = []
    logical_to_path: dict[str, str] = {}
    warnings: list[str] = []

    for node in nodes:
        final_name = f"{name_prefix}_{node.name}" if name_prefix else node.name
        body = {
            "parent_path": parent_path,
            "op_type": node.node_type,
            "name": final_name,
            "nodeX": node_x + node.dx,
            "nodeY": node_y + node.dy,
        }
        result = dispatcher("td_create_node", body)
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(f"td_create_node failed for {node.name} ({node.node_type}): {result['error']}")
        node_info = result.get("node", {}) if isinstance(result, dict) else {}
        path = node_info.get("path") or f"{parent_path.rstrip('/')}/{final_name}"
        logical_to_path[node.name] = path
        created.append(
            {
                "logical_name": node.name,
                "name": node_info.get("name", final_name),
                "path": path,
                "type": node_info.get("type", node.node_type),
            }
        )
        if node.params:
            r = dispatcher("td_set_params", {"path": path, "params": node.params})
            if isinstance(r, dict) and "error" in r:
                warnings.append(f"set_params failed on {path}: {r['error']}")

    connection_results: list[dict] = []
    for c in template.connections:
        sp = logical_to_path.get(c.source)
        tp = logical_to_path.get(c.target)
        if not sp or not tp:
            warnings.append(f"Skipped connection {c.source}->{c.target}: missing node.")
            continue
        r = dispatcher(
            "td_connect_nodes",
            {
                "from_path": sp,
                "to_path": tp,
                "from_index": c.source_index,
                "to_index": c.target_index,
            },
        )
        if isinstance(r, dict) and "error" in r:
            warnings.append(f"connect failed {c.source}->{c.target}: {r['error']}")
        else:
            connection_results.append(
                {
                    "source": sp,
                    "target": tp,
                    "source_index": c.source_index,
                    "target_index": c.target_index,
                }
            )

    for ref in template.node_references:
        np = logical_to_path.get(ref.node)
        tp = logical_to_path.get(ref.target_node)
        if not np or not tp:
            warnings.append(f"Skipped node ref {ref.node}.{ref.param}->{ref.target_node}: missing node.")
            continue
        target_name = tp.rsplit("/", 1)[-1]
        r = dispatcher("td_set_params", {"path": np, "params": {ref.param: target_name}})
        if isinstance(r, dict) and "error" in r:
            warnings.append(f"node ref failed {ref.node}.{ref.param}: {r['error']}")

    all_expressions = [*template.expressions, *extra_expressions]
    for expr in all_expressions:
        path = logical_to_path.get(expr.node)
        if not path:
            warnings.append(f"Skipped expression {expr.node}.{expr.param}: node missing.")
            continue
        # Expression-mode set_params — handler accepts {expr: ...}
        r = dispatcher(
            "td_set_params",
            {
                "path": path,
                "params": {expr.param: {"expr": expr.expr}},
            },
        )
        if isinstance(r, dict) and "error" in r:
            warnings.append(f"expression failed {path}.{expr.param}: {r['error']}")

    return {
        "ok": True,
        "macro_type": template.name,
        "parent_path": parent_path,
        "created_nodes": created,
        "connections": connection_results,
        "entry_node": logical_to_path.get(template.entry_node) if template.entry_node else None,
        "exit_node": logical_to_path.get(template.exit_node) if template.exit_node else None,
        "resolved_params": resolved,
        "warnings": warnings,
    }


# ===========================================================================
# Tool handlers
# ===========================================================================


def handle_macro_list(body: dict) -> dict:
    templates = _ensure_templates()
    summaries = []
    for name, template in templates.items():
        s = template.summary()
        s["source"] = _TEMPLATE_SOURCES.get(name, "built_in")
        summaries.append(s)
    return {
        "ok": True,
        "count": len(templates),
        "macros": summaries,
        "load_warnings": list(_LOAD_WARNINGS),
    }


def handle_macro_get(body: dict) -> dict:
    macro_type = (body.get("macro_type") or body.get("name") or "").strip()
    if not macro_type:
        return {"error": "Missing required field: macro_type"}
    templates = _ensure_templates()
    template = templates.get(macro_type)
    if template is None:
        return {
            "error": f"Unknown macro_type: {macro_type}",
            "available": sorted(templates.keys()),
        }
    return {
        "ok": True,
        "macro_type": macro_type,
        "description": template.description,
        "source": _TEMPLATE_SOURCES.get(macro_type, "built_in"),
        "params": {k: v.to_dict() for k, v in template.param_schema.items()},
        "node_count": len(template.nodes),
        "connection_count": len(template.connections),
        "entry_node": template.entry_node,
        "exit_node": template.exit_node,
    }


def handle_macro_run(body: dict) -> dict:
    macro_type = (body.get("macro_type") or "").strip()
    parent_path = (body.get("parent_path") or "").strip()
    if not macro_type:
        return {"error": "Missing required field: macro_type"}
    if not parent_path:
        return {"error": "Missing required field: parent_path"}

    templates = _ensure_templates()
    template = templates.get(macro_type)
    if template is None:
        return {
            "error": f"Unknown macro_type: {macro_type}",
            "available": sorted(templates.keys()),
        }

    name_prefix = body.get("name_prefix")
    if name_prefix is not None:
        name_prefix = str(name_prefix)

    try:
        node_x = int(body.get("node_x", 0))
        node_y = int(body.get("node_y", 0))
    except (TypeError, ValueError):
        node_x, node_y = 0, 0

    overrides = body.get("overrides") or {}
    if not isinstance(overrides, dict):
        return {"error": "'overrides' must be an object"}

    try:
        return _create_macro(template, parent_path, name_prefix, node_x, node_y, overrides)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
