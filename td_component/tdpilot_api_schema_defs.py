"""TDPilot API — Anthropic-format tool schemas (Sprint 0–4 surface).

Split off from tdpilot_api_schema.py during the 2026-05-04 audit. See
tdpilot_api_schema_map.py for the matching name → handler routing.
Re-exported via tdpilot_api_schema.py for backward compatibility.
"""

from __future__ import annotations

from typing import Any

# Tool schemas (Anthropic /v1/messages format)
# ---------------------------------------------------------------------------
# Hand-curated for the standalone agent. Schema shape:
# {"name": str, "description": str, "input_schema": JSON-Schema dict}.

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "td_get_info",
        "description": "Get TouchDesigner version, FPS, project info. No parameters.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_get_nodes",
        "description": "List child nodes of a COMP path. Returns name, type, family for each child.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to a COMP, e.g. '/project1'."},
                "family": {
                    "type": "string",
                    "description": "Optional filter — TOP, CHOP, SOP, DAT, MAT, COMP, PANEL.",
                },
                "type": {
                    "type": "string",
                    "description": "Optional filter on full op type, e.g. 'noiseTOP'.",
                },
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_get_node_detail",
        "description": "Get detailed info for a single node: type, parameters, connections, errors.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "td_get_params",
        "description": "Get parameter values for a node. Optionally filter by page or by names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute node path."},
                "page": {
                    "type": "string",
                    "description": "Optional. Restrict to one parameter page, e.g. 'Common'.",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional. List of specific parameter names to fetch.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_set_params",
        "description": "Set one or more parameter values on a node.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "params": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Map of param_name -> new value.",
                },
            },
            "required": ["path", "params"],
        },
    },
    {
        "name": "td_create_node",
        "description": (
            "Create a new operator inside a parent COMP. Returns the new node's path. "
            "CRITICAL: op_type MUST include the family suffix — TouchDesigner has no "
            "type called 'box' or 'sphere' or 'noise' on its own. The correct names are "
            "'boxSOP', 'sphereSOP', 'noiseTOP', 'levelTOP', 'constantCHOP', 'textDAT', "
            "'phongMAT', 'geometryCOMP', etc. The suffix tells TD which family the "
            "operator belongs to (TOP=textures, CHOP=channels, SOP=geometry, "
            "DAT=data/text, MAT=materials, COMP=components)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_path": {
                    "type": "string",
                    "description": "Absolute path to the parent COMP, e.g. '/project1'.",
                },
                "op_type": {
                    "type": "string",
                    "description": (
                        "Full type name WITH family suffix. "
                        "Examples: 'boxSOP' (cube), 'sphereSOP', 'gridSOP', "
                        "'noiseTOP', 'levelTOP', 'constantTOP', 'constantCHOP', "
                        "'lfoCHOP', 'textDAT', 'tableDAT', 'phongMAT', "
                        "'geometryCOMP', 'cameraCOMP', 'lightCOMP'. "
                        "Never just 'box' or 'sphere' — always include the suffix."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "RECOMMENDED: pass a meaningful name (e.g. 'noise1', "
                        "'audio_level', 'main_render'). If omitted, TD assigns "
                        "the type's default (noise1, noise2, ...). Do NOT pass "
                        "an empty string or null — pass a real name or omit "
                        "the field entirely."
                    ),
                },
                "nodeX": {
                    "type": "integer",
                    "description": "Optional X position in the network editor. Use to lay out nodes left-to-right (e.g. 0, 200, 400, 600).",
                },
                "nodeY": {
                    "type": "integer",
                    "description": "Optional Y position. Use to group related nodes by row.",
                },
            },
            "required": ["parent_path", "op_type"],
        },
    },
    {
        "name": "td_delete_node",
        "description": "Delete a node by absolute path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "td_connect_nodes",
        "description": (
            "Wire the output of a source node into an input of a target node. "
            "Indices default to 0 (first output -> first input)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path of the upstream node providing output.",
                },
                "target_path": {
                    "type": "string",
                    "description": "Path of the downstream node receiving input.",
                },
                "source_index": {"type": "integer", "default": 0, "minimum": 0},
                "target_index": {"type": "integer", "default": 0, "minimum": 0},
            },
            "required": ["source_path", "target_path"],
        },
    },
    {
        "name": "td_get_errors",
        "description": (
            "Return errors and warnings under a path. Critical for verification "
            "after any multi-step mutation (create / connect / set_params)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/", "description": "Root to scan from."},
                "recurse": {"type": "boolean", "default": True, "description": "Include descendants."},
                "max_depth": {"type": "integer", "minimum": 1, "description": "Optional depth limit."},
            },
        },
    },
    {
        "name": "td_exec_python",
        "description": (
            "Execute Python in the TD process. Subject to the COMP's exec mode "
            "(restricted by default — denies file/process/network access). "
            "Use this only when no dedicated tool fits — prefer td_create_node, "
            "td_set_params, td_connect_nodes etc. for normal operations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source. TD globals (op, parent, me) available.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "default": 5000,
                    "description": "Soft timeout in milliseconds.",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "td_screenshot",
        "description": "Capture a TOP as a JPEG. Returns base64. Use sparingly — high token cost. Note: DeepSeek's Anthropic-compat layer doesn't accept image content blocks, so the returned base64 is for the user to decode/inspect, not for the model to view. For visual analysis use td_analyze_frame instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a TOP, e.g. '/project1/render1'."},
            },
            "required": ["path"],
        },
    },
    # ---------------------------------------------------------------------
    # The "easy 7" — handler already exists, only the schema was missing.
    # Audit confirmed by reading mcp_webserver_callbacks.py 2026-05-04.
    # ---------------------------------------------------------------------
    {
        "name": "td_project_lifecycle",
        "description": (
            "Save, undo, redo, or reload the TD project. CRITICAL for safety on "
            "multi-step builds — call before risky changes (snapshot via save) and "
            "use undo to roll back if a step breaks the network. Use sparingly: "
            "every save writes the .toe to disk, which can be slow on big projects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "save", "undo", "redo", "reload"],
                    "default": "status",
                    "description": "status = read state; save = write .toe; undo/redo = step the TD undo stack; reload = re-open the file.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional. Save-as path (only for save action). Defaults to the current .toe path.",
                },
                "save_external_toxs": {
                    "type": "boolean",
                    "default": False,
                    "description": "Optional. Also save externally-linked .tox children. Save-only.",
                },
            },
        },
    },
    {
        "name": "td_custom_parameters",
        "description": (
            "Declaratively author custom parameters on a COMP. Creates pages and "
            "params in one call (no need for td_exec_python). Each spec is a dict: "
            "{name, kind: Str|Int|Float|Toggle|Pulse|Menu|Header, label?, default?, "
            "min?, max?, options?(for Menu)}. Use this instead of td_exec_python "
            "for parameter authoring — more reliable and TD-version safe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the COMP whose parameters to create/update.",
                },
                "page": {"type": "string", "description": "Custom page name (created if missing)."},
                "params": {
                    "type": "array",
                    "description": "List of parameter specs. Each: {name, kind, label?, default?, min?, max?, options?}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["Str", "Int", "Float", "Toggle", "Pulse", "Menu", "Header"],
                            },
                            "label": {"type": "string"},
                            "default": {},
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "For Menu kind only: the menu choices.",
                            },
                        },
                        "required": ["name", "kind"],
                    },
                },
            },
            "required": ["path", "page", "params"],
        },
    },
    {
        "name": "td_pop_inspect",
        "description": (
            "Inspect a POP's metadata, bounds, and per-vertex attribute samples. "
            "First-class introspection for POP work — returns attribute names/types, "
            "bounding box, and sampled values. Required reading before modifying a "
            "POP via td_exec_python so the agent knows what fields exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a POP node."},
                "include_bounds": {"type": "boolean", "default": True},
                "include_attributes": {"type": "boolean", "default": True},
                "start": {
                    "type": "integer",
                    "default": 0,
                    "minimum": 0,
                    "description": "Starting vertex index for samples.",
                },
                "count": {
                    "type": "integer",
                    "default": 32,
                    "minimum": 1,
                    "maximum": 2048,
                    "description": "Number of samples.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_geometry_data",
        "description": (
            "Read geometry data (points, primitives) from a SOP or POP. Returns "
            "actual coordinates and attributes — useful for the agent to reason "
            "about real mesh content rather than guessing. Limit defaults to 500 "
            "to keep token cost reasonable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a SOP or POP."},
                "include_points": {"type": "boolean", "default": True},
                "include_prims": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_subscribe",
        "description": (
            "Subscribe to monitor events on a node — creates monitor DATs that "
            "track state changes (parameters, errors, cooks). Pair with "
            "td_unsubscribe when done so you don't leak monitor DATs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to monitor."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_unsubscribe",
        "description": "Remove the monitor created by td_subscribe for a path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_analyze_frame",
        "description": (
            "Analyze pixel data of a TOP using numpy — replaces the 'show me what "
            "this looks like' flow that vision would have provided. Modes: "
            "histogram (color distribution), luminance (brightness stats), "
            "alpha_coverage (% non-zero alpha), color_dominant (top color clusters), "
            "roi_diff (compare two regions). Returns numeric summaries the model "
            "can reason about — DeepSeek's compat layer doesn't accept image "
            "content blocks, so this is the visual-feedback substitute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a TOP node."},
                "modes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["histogram", "luminance", "alpha_coverage", "color_dominant", "roi_diff"],
                    },
                    "default": ["histogram", "luminance"],
                },
            },
            "required": ["path"],
        },
    },
    # ---------------------------------------------------------------------
    # The "missing 15" — tools that had dispatcher entries from the start
    # but were never given schemas, so the model couldn't see them. Audit
    # 2026-05-04 caught this when the agent listed only 18 tools instead
    # of the dispatcher's 33. Fix: schema parity with TOOL_TO_HANDLER.
    # ---------------------------------------------------------------------
    {
        "name": "td_list_families",
        "description": (
            "List operator families and concrete op types under a path. Use to "
            "discover the right type name when td_create_node returns 'Unknown "
            "operator type' — the family suffix is case-sensitive and varies "
            "between TD versions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/",
                    "description": "Scope. Defaults to root.",
                },
            },
        },
    },
    {
        "name": "td_disconnect",
        "description": (
            "Disconnect one of a node's inputs OR outputs. Use connector_type "
            "to choose direction; index to choose which connector."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Node whose connector to disconnect."},
                "connector_type": {
                    "type": "string",
                    "enum": ["input", "output"],
                    "default": "input",
                },
                "index": {"type": "integer", "default": 0, "minimum": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_get_connections",
        "description": "List inputs and outputs of a node, including connected peers and indices.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "td_get_content",
        "description": (
            "Read the text or table content of a DAT. Returns text body for textDAT, "
            "list-of-rows for tableDAT. Use before td_set_content to know what's there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to a DAT."}},
            "required": ["path"],
        },
    },
    {
        "name": "td_set_content",
        "description": (
            "Write text or table content to a DAT. Pass `text` for textDAT, "
            "`table` (list of lists of strings) for tableDAT. Exactly one of "
            "`text` or `table` should be set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string", "description": "For textDAT-style content."},
                "table": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "description": "For tableDAT — list of rows, each a list of strings.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_copy_node",
        "description": "Duplicate a node into the same or a different parent COMP.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Node to copy."},
                "dest_parent": {
                    "type": "string",
                    "description": "Optional. Where to put the copy. Defaults to the source's parent.",
                },
                "new_name": {"type": "string", "description": "Optional. New name."},
                "nodeX": {"type": "integer"},
                "nodeY": {"type": "integer"},
            },
            "required": ["source_path"],
        },
    },
    {
        "name": "td_rename_node",
        "description": "Rename a node in place. Both `path` and `new_name` are required.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["path", "new_name"],
        },
    },
    {
        "name": "td_chop_data",
        "description": (
            "Read channel sample data from a CHOP. Returns per-channel numeric "
            "samples. Use `channels` to filter by name; `range` to limit sample "
            "indices [start, end]. Without filters, returns all channels and "
            "all samples — be careful on CHOPs with many samples."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to a CHOP."},
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of channel names to include.",
                },
                "range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Optional [start, end] sample-index range.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "td_search_nodes",
        "description": (
            "Search for nodes by name, type, family, or all three. Useful when "
            "you don't remember the path of something the user mentioned by "
            "name. Returns matching paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string."},
                "path": {
                    "type": "string",
                    "default": "/",
                    "description": "Subtree to search under. Defaults to root.",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["name", "type", "family", "all"],
                    "default": "name",
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
        },
    },
    {
        "name": "td_python_help",
        "description": (
            "Return TouchDesigner Python help() text for an attribute. Useful "
            "for discovering API surface (e.g. `td.OP`, `tdu.Color`). Restricted "
            "to dotted identifiers — no arbitrary expressions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Dotted identifier, e.g. 'td.OP', 'tdu', 'op'.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "td_python_classes",
        "description": "List all classes available in the `td` module. No parameters.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_timeline",
        "description": "Read the project timeline state — current frame, FPS, play state, range. No parameters.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_timeline_set",
        "description": "Control timeline playback (play / pause / jump to frame / change FPS).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "frame"],
                    "description": "play = start, pause = stop, frame = jump to a specific frame.",
                },
                "frame": {"type": "integer", "description": "Required when action=frame."},
                "fps": {"type": "number", "description": "Optional. Set the project FPS."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "td_cooking_info",
        "description": (
            "Get cook-time stats for a node (or recursively under a path) — "
            "great for diagnosing 'why is my project slow' questions. Each "
            "row now includes cookTime (total wall), cpuCookTime, gpuCookTime "
            "(v2.4: GPU-only — 0 for non-TOP operators), cookFrame, and (TOPs "
            "only) cudaMemoryBytes — per-TOP VRAM footprint via cudaMemory(). "
            "Use sort_by='gpuCookTime' to surface GLSL / feedback-loop hot "
            "spots, sort_by='cudaMemoryBytes' to find VRAM hogs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/", "description": "Node or scope."},
                "recurse": {"type": "boolean", "default": False},
                "sort_by": {
                    "type": "string",
                    "enum": [
                        "cookTime",
                        "cpuCookTime",
                        "gpuCookTime",  # v2.4 / Phase A.4
                        "cudaMemoryBytes",  # v2.4 / Phase A.4
                    ],
                    "default": "cookTime",
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "td_pulse_param",
        "description": (
            "Pulse a pulse-type parameter on a node — equivalent to clicking the "
            "param's button. Both `path` and `param` are required."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "param": {"type": "string", "description": "Pulse param name (e.g. 'reset')."},
            },
            "required": ["path", "param"],
        },
    },
    # ---------------------------------------------------------------------
    # Memory tools — persistent markdown notes in ~/.tdpilot-dpsk4/api/memory/.
    # Pattern matches Claude Code's memory system; files survive across
    # sessions and are auto-indexed via MEMORY.md (loaded into the
    # system prompt at session start).
    # ---------------------------------------------------------------------
    {
        "name": "memory_save",
        "description": (
            "Save a memory file to ~/.tdpilot-dpsk4/api/memory/ for future sessions. "
            "Use whenever you learn something non-obvious about the user, the "
            "project, or how to do something — corrections, preferences, "
            "validated approaches, decisions, key references. Each memory is a "
            "small markdown file with frontmatter; the index (MEMORY.md) is "
            "auto-loaded into your system prompt next session.\n\n"
            "Memory types:\n"
            "  user      — user role, preferences, knowledge\n"
            "  feedback  — corrections OR confirmations (record both — only "
            "saving corrections drifts away from validated approaches)\n"
            "  project   — current work context (deadlines, decisions, "
            "ongoing initiatives)\n"
            "  reference — pointers to external systems, dashboards, repos\n\n"
            "For feedback/project entries, structure the body as: rule/fact, "
            "then **Why:** line, then **How to apply:** line — knowing the "
            "*why* lets future-you judge edge cases instead of blindly "
            "following the rule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable title (also used as filename slug).",
                },
                "description": {
                    "type": "string",
                    "description": "One-line hook used to decide relevance later. Be specific.",
                },
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                },
                "content": {
                    "type": "string",
                    "description": "Markdown body of the memory.",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["instruction", "reference", "fact"],
                    "description": (
                        "How pre-turn BM25 retrieval should treat this entry. "
                        "Default 'reference' — entry freely surfaces on any "
                        "matching query. Pick 'instruction' for step lists, "
                        "recipes, 'how to do X' procedures — those entries "
                        "are then HIDDEN from generic queries and surface "
                        "only when the user explicitly names them. "
                        "(Prevents drive-by tool execution from short prompts "
                        "matching instruction-shaped memories.) Pick 'fact' "
                        "for assertions / static knowledge."
                    ),
                },
            },
            "required": ["name", "type", "content"],
        },
    },
    {
        "name": "memory_get",
        "description": (
            "Read the full content of a saved memory. Use this when MEMORY.md "
            "(loaded into your system prompt) shows a relevant entry and you "
            "need the details. The `name` argument can be either the filename "
            "shown in the index (e.g. `feedback_terse_responses.md`) or the "
            "bare slug (`feedback_terse_responses`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename or bare slug."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "memory_list",
        "description": (
            "Enumerate all memories with their metadata. Optionally filter by "
            "type. Use this to scan everything you've stored when MEMORY.md's "
            "index isn't specific enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": "Optional type filter.",
                },
            },
        },
    },
    {
        "name": "memory_recall",
        "description": (
            "BM25 search over all memory files. Returns ranked matches with "
            "snippets. Call when you suspect there's a relevant memory but the "
            "index entry isn't specific enough to pick a single file. "
            "Combine with memory_get to read full content of the top match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 20,
                },
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": "Optional type filter (e.g. only feedback).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_delete",
        "description": (
            "Remove a memory file. Use when a memory is superseded by newer "
            "information OR turned out to be wrong. The `name` argument can "
            "be a filename or a bare slug, same as memory_get."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    # ---------------------------------------------------------------------
    # Knowledge tools — bundled TD reference corpus + user additions in
    # ~/.tdpilot-dpsk4/api/knowledge/. BM25 search over both pools combined.
    # ---------------------------------------------------------------------
    {
        "name": "knowledge_search",
        "description": (
            "BM25 search the TouchDesigner knowledge corpus — bundled "
            "reference docs (operator families, Python idioms, common "
            "pitfalls), user-added .md files in ~/.tdpilot-dpsk4/api/knowledge/, "
            "AND any external docsbrain corpora found at "
            "~/.tdpilot/data/normalized/<name>/pages.jsonl (e.g. 'popx' "
            "= 58 pages of POPx library docs, 'derivative' = 2478 pages "
            "of official TD docs). The system-prompt index lists local "
            "entries by name + announces external corpora by page "
            "count. Use this BEFORE guessing on TD/POPx specifics. "
            "Returns ranked snippets — call knowledge_get on a top hit "
            "to read the full document. Pass corpus='popx' to limit to "
            "POPx docs only, or corpus='derivative' for TD docs only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 20,
                },
                "category": {
                    "type": "string",
                    "description": "Optional filter (e.g. 'reference', 'guide', 'catalog').",
                },
                "corpus": {
                    "type": "string",
                    "description": (
                        "Optional corpus filter: 'popx', 'derivative', "
                        "'bundled', 'user'. Default = search everything."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "knowledge_get",
        "description": (
            "Read the full content of a knowledge entry by its `name` "
            "(the human-readable title from frontmatter). Use after "
            "knowledge_search returns a relevant hit and you need the "
            "details. Falls back to filename match if `name` doesn't "
            "match any title."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "knowledge_list",
        "description": (
            "List all available knowledge entries with their metadata. "
            "Optionally filter by category. Useful for discovering what "
            "reference material is available before searching."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
            },
        },
    },
    {
        "name": "knowledge_add",
        "description": (
            "Add a new knowledge entry to the user pool (~/.tdpilot-dpsk4/api/"
            "knowledge/). Bundled entries are read-only — to override a "
            "bundled entry, save a user entry with the same `name` field "
            "and it will take precedence. Use this to teach the agent "
            "project-specific reference material that should persist "
            "across sessions and complement the bundled TD docs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable title."},
                "description": {"type": "string", "description": "One-line hook."},
                "category": {
                    "type": "string",
                    "default": "reference",
                    "description": "reference / guide / catalog / tutorial / project.",
                },
                "content": {"type": "string", "description": "Markdown body."},
                "content_type": {
                    "type": "string",
                    "enum": ["instruction", "reference", "fact"],
                    "description": (
                        "How pre-turn BM25 retrieval should treat this entry. "
                        "Default 'reference' — entry surfaces freely on matching "
                        "queries (the right pick for descriptive knowledge: "
                        "operator docs, API surfaces, conventions). Pick "
                        "'instruction' for step-list guides — those then surface "
                        "only when the user explicitly names them, avoiding "
                        "drive-by tool execution from incidental short-prompt "
                        "matches."
                    ),
                },
            },
            "required": ["name", "content"],
        },
    },
    # ---------------------------------------------------------------------
    # Recipe tools — saved replayable sequences of tool calls.
    # The user's flow: agent builds something complex this session,
    # saves it as a recipe via recipe_save with the exact tool-call
    # sequence; next session, the user says "do that thing again" and
    # recipe_recall + recipe_replay reproduces the build.
    # ---------------------------------------------------------------------
    {
        "name": "recipe_save",
        "description": (
            "Save a recipe — a replayable sequence of tool calls that "
            "reproduces a creative technique. Call this AFTER successfully "
            "completing a multi-step build (>3 tool calls) that the user "
            "might want to reproduce later. Pass the full list of tool "
            "calls (each {tool: name, args: dict}) so recipe_replay can "
            "execute the same sequence. Add tags so recipe_recall can find "
            "it later by keyword (e.g. 'audio', 'feedback', 'particles')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable title (becomes filename slug).",
                },
                "description": {
                    "type": "string",
                    "description": "One-line hook for what the recipe builds.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lowercase tags for recall. e.g. ['audio', 'reactive', 'top'].",
                },
                "goal": {
                    "type": "string",
                    "description": "Markdown describing what the recipe achieves.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional human-readable step list (one per tool call ideally).",
                },
                "replay": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string"},
                            "args": {"type": "object"},
                        },
                        "required": ["tool"],
                    },
                    "description": "List of {tool, args} dicts — the executable replay sequence.",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["instruction", "reference", "fact"],
                    "description": (
                        "How pre-turn BM25 retrieval should treat this recipe. "
                        "Default 'instruction' — recipes are step lists by "
                        "design, so they surface ONLY when the user explicitly "
                        "names them (avoids drive-by replay from incidental "
                        "matches). Pass 'reference' for documentation-style "
                        "recipes the user should be able to search by content."
                    ),
                },
            },
            "required": ["name", "replay"],
        },
    },
    {
        "name": "recipe_get",
        "description": (
            "Read the full content of a saved recipe — frontmatter, "
            "human-readable steps, and the parsed replay JSON. Use after "
            "recipe_recall returns a candidate to confirm it's the right "
            "one before calling recipe_replay."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Recipe name or filename."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "recipe_list",
        "description": "List all saved recipes with metadata. Optionally filter by tag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Optional tag filter (case-insensitive)."},
            },
        },
    },
    {
        "name": "recipe_recall",
        "description": (
            "BM25 search across all saved recipes by name, description, "
            "tags, and content. Use when the user asks for 'that thing' or "
            "'the technique we did before' — recall a likely match, then "
            "recipe_get to confirm, then recipe_replay to reproduce."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recipe_replay",
        "description": (
            "Execute a recipe's saved tool-call sequence in order. Two "
            "modes:\n"
            "  * transactional=false (default) — best-effort. If step N "
            "fails, ok=false, completed=N-1, earlier steps STAY in the "
            "network.\n"
            "  * transactional=true — wraps the whole replay in a TD "
            "undo block. On any failure, every step rolls back atomically. "
            "On success, the entire sequence is one undo step (manual "
            "Cmd+Z still reverts everything if the user wants).\n"
            "Use dry_run=true first to preview the planned sequence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Recipe name or filename."},
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, returns the plan without executing.",
                },
                "transactional": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, wrap the replay in a TD undo block — "
                        "all-or-nothing. RECOMMENDED for important recipes."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    # ---------------------------------------------------------------------
    # Skills tools — on-demand behaviour modulators. Each skill is a
    # markdown doc with discipline rules for a specific workflow (POPs,
    # performance optimization, glsl, etc.). Bundled defaults plus user
    # additions in ~/.tdpilot-dpsk4/api/skills/.
    # ---------------------------------------------------------------------
    {
        "name": "skill_list",
        "description": (
            "List all available skills (bundled + user). The Skills Index "
            "in your system prompt shows them too — use this tool when you "
            "need fresh metadata or are inside a flow where the index "
            "isn't enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "skill_get",
        "description": (
            "Read the full content of a skill by name. Returns the markdown "
            "body that contains the skill's discipline rules and protocols. "
            "Use when you want to consult a skill but aren't committing to "
            "follow it for the rest of the turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "skill_load",
        "description": (
            "Activate a skill — load its full content and treat it as "
            "AUTHORITATIVE behaviour guidance for the rest of the current "
            "turn. Use when the user's task fits a skill's triggers (e.g. "
            "user mentions POPs → skill_load('popx-mode'); user reports "
            "lag → skill_load('performance-mode')). Skills are layered on "
            "top of your base discipline — they don't replace it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "skill_validate",
        "description": (
            "Re-validate skill frontmatter (YAML schema) and surface any "
            "broken skills. Pre-1.7.2 invalid skills were silently skipped; "
            "now they're listed with specific error messages. Pass `name` to "
            "validate one skill, omit to get the list of all currently "
            "invalid skills. Use after editing a skill in "
            "~/.tdpilot-dpsk4/api/skills/ to confirm it parses correctly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional skill name to validate. If omitted, returns all invalid skills.",
                },
            },
            "additionalProperties": False,
        },
    },
    # ---------------------------------------------------------------------
    # Snapshot + Patch session tools — safety mechanisms for risky builds.
    # Snapshots are full .toe saves to ~/.tdpilot-dpsk4/api/snapshots/ (heavy).
    # Patch sessions wrap operations in TD's native undo block API
    # (lightweight, atomic rollback). Sprint 3.3.
    # ---------------------------------------------------------------------
    {
        "name": "snapshot_save",
        "description": (
            "Save the current TD project to a .toe file in "
            "~/.tdpilot-dpsk4/api/snapshots/. Use BEFORE risky multi-step builds "
            "you can't easily reproduce. Snapshot restore is NOT exposed "
            "as a tool (project.load() would destroy the agent COMP "
            "mid-call) — users restore manually via TD's File > Open. "
            "For lightweight transactional rollback within a turn, use "
            "patch_begin/patch_rollback or recipe_replay(transactional=true) "
            "instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional human-readable name. Defaults to auto_<unix_timestamp>.",
                },
                "save_external_toxs": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also save externally-linked .tox children.",
                },
            },
        },
    },
    {
        "name": "snapshot_list",
        "description": (
            "List saved snapshots in ~/.tdpilot-dpsk4/api/snapshots/, newest "
            "first. Includes both full ``.toe`` snapshots and scoped JSON "
            "manifests (.scoped.json). Each entry has a ``kind`` field: "
            "``toe`` (full project, manual restore via File>Open only) or "
            "``scoped`` (JSON manifest, restorable via snapshot_restore_scoped)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "snapshot_save_scoped",
        "description": (
            "Save a JSON manifest of a TD scope's structural shape — nodes, "
            "parameters, and connections — EXCLUDING the agent's own COMP and "
            "any user-listed paths. Use BEFORE risky multi-step builds where "
            "you want to be able to restore mid-conversation (unlike "
            "snapshot_save which writes a full .toe and can only be restored "
            "by closing the project).\n\n"
            "Captures: node tree (path, type, family, position, custom params, "
            "non-default standard params, expressions), connections within the "
            "scope.\n"
            "Does NOT capture: DAT text contents, extension Python, geometry "
            "data, animation curves, custom-python on operators. For those use "
            "snapshot_save (full .toe) and restore manually.\n\n"
            "Pair with snapshot_restore_scoped to converge the scope back to "
            "the saved state mid-conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Human-readable label (slugged into the filename). "
                        "Defaults to scoped_<unix_timestamp>."
                    ),
                },
                "scope": {
                    "type": "string",
                    "default": "/project1",
                    "description": "Absolute TD path to snapshot. Default /project1.",
                },
                "excludes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Additional TD paths to exclude. The agent's own COMP, "
                        "the classic tdpilot COMP, and mcp_server are ALWAYS "
                        "excluded."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "snapshot_restore_scoped",
        "description": (
            "Restore a scope from a previously saved JSON manifest. "
            "Computes a diff between the current scope state and the manifest, "
            "then applies create / delete / param-update / connect / "
            "disconnect operations to converge the scope back to the snapshot. "
            "The agent's own COMP is excluded from the restore so the "
            "operation is safe to call mid-conversation.\n\n"
            "Pair with snapshot_save_scoped. Use ``dry_run=true`` first to "
            "preview the diff before applying.\n\n"
            "Returns a structured report: counts of created / deleted / "
            "params_updated / connected / disconnected operations plus a "
            "post-restore td_get_errors result. Read this carefully — partial "
            "restores can leave the scope in an inconsistent state if some "
            "operations failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Snapshot name (same slugged form used by save). "
                        "Newest matching .scoped.json is used. Alternative "
                        "to ``path``."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": ("Absolute path to a .scoped.json manifest. Alternative to ``name``."),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, report the diff (to_create / to_delete / "
                        "to_update) without applying changes."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_begin",
        "description": (
            "Start a transactional patch session. Wraps subsequent "
            "operations in a TD undo block — on patch_rollback, the "
            "entire sequence reverts atomically. On patch_commit, the "
            "block becomes ONE step in TD's undo stack (manual Cmd+Z "
            "still reverts everything if the user wants). Only ONE patch "
            "session can be active at a time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable label for this patch session.",
                },
                "scope_path": {
                    "type": "string",
                    "default": "/",
                    "description": "Path used by patch_validate for error checking.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "patch_validate",
        "description": (
            "Run td_get_errors on the active patch session's scope_path. "
            "Use BETWEEN operations to confirm the network is healthy "
            "before continuing or before patch_commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_commit",
        "description": (
            "Finalize the patch session — close the undo block. The "
            "whole sequence becomes one step in TD's undo stack. Idempotent "
            "in a sense: errors after this won't roll the patch back."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_rollback",
        "description": (
            "Roll back the active patch session — closes the undo block "
            "and immediately calls project.undo(), reverting the entire "
            "grouped sequence atomically. Use after a failed step or "
            "when abandoning the build mid-way."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    # ---------------------------------------------------------------------
    # User-pluggable tools (Sprint 4.2)
    # The agent's tool surface can be extended at runtime by dropping
    # Python files in ~/.tdpilot-dpsk4/api/tools/<name>.py with a SCHEMA dict
    # + handle(args) function. These two MANAGEMENT tools let the user
    # see what's loaded and validate edits before reload.
    # ---------------------------------------------------------------------
    {
        "name": "tool_list_user",
        "description": (
            "List user-pluggable tools loaded from "
            "~/.tdpilot-dpsk4/api/tools/. Returns metadata for each — both "
            "successfully active tools AND files that failed to load "
            "(with the validation error so the user can fix them). "
            "Useful for diagnosing 'I dropped a tool but it's not in "
            "the agent's surface' issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "tool_validate",
        "description": (
            "Dry-validate a Python file as a user tool WITHOUT "
            "registering it. Use this to check schema validity before "
            "the user pulses Reload Config. Path can be absolute OR "
            "relative to ~/.tdpilot-dpsk4/api/tools/. Returns ok=true with "
            "schema summary, or ok=false with the specific error "
            "(missing SCHEMA, missing handle(), bad name regex, "
            "wrong input_schema shape, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File path. Absolute or relative to ~/.tdpilot-dpsk4/api/tools/. e.g. 'my_tool.py'."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    # ---------------------------------------------------------------------
    # Subagents (Sprint 4.1) — fork off child agents for parallel work.
    # Up to 3 concurrent. Children CANNOT spawn grandchildren (depth=2).
    # Children default to deepseek-v4-flash for speed; turn budget=20.
    # Result aggregation via polling (subagent_status / subagent_wait),
    # not streaming — matches Anthropic batch-jobs pattern.
    # ---------------------------------------------------------------------
    {
        "name": "spawn_subagent",
        "description": (
            "Fork off a child agent for an independent sub-task. Use ONLY "
            "when the parent task naturally decomposes into 2-4 parallel "
            "sub-goals each requiring 3+ tool calls (e.g. 'inspect 5 "
            "children of /project1', 'try 3 noise variations and pick "
            "best'). Each subagent gets the parent's full tool surface, "
            "shares the same dispatcher, runs on its own thread.\n\n"
            "Returns a subagent_id immediately (non-blocking). Poll "
            "status via subagent_status, OR block via subagent_wait. "
            "Up to 3 subagents may run concurrently; over that, spawn "
            "queues or rejects. Children CANNOT spawn further "
            "subagents (depth=2 hard cap)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The sub-task instruction. Be specific and "
                        "self-contained — children don't see the parent's "
                        "conversation context."
                    ),
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "Optional override for the child's system prompt. Default is a terse subagent prompt."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "subagent_status",
        "description": (
            "Read a subagent's current state. Returns alive/result/"
            "error/tool_calls/duration_ms/partial_text. Non-blocking. "
            "Call repeatedly to poll; or use subagent_wait to block."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
            },
            "required": ["subagent_id"],
        },
    },
    {
        "name": "subagent_wait",
        "description": (
            "Block until the subagent finishes OR timeout_seconds "
            "elapses. Returns the same fields as subagent_status. "
            "Default timeout is 30s; max 600s."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
                "timeout_seconds": {
                    "type": "number",
                    "default": 30,
                    "minimum": 0.1,
                    "maximum": 600,
                },
            },
            "required": ["subagent_id"],
        },
    },
    {
        "name": "subagent_cancel",
        "description": (
            "Cooperatively cancel a running subagent. The child checks "
            "its cancel_event between API calls and exits gracefully — "
            "in-flight HTTP request to DeepSeek finishes before exit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subagent_id": {"type": "string"},
            },
            "required": ["subagent_id"],
        },
    },
    {
        "name": "subagent_list",
        "description": (
            "List all subagents in this session (active + completed) "
            "with their state. Useful for diagnostics — see what's "
            "still running, how many tool calls each consumed, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    # ---------------------------------------------------------------------
    # Macros (Sprint 4.4) — parametrized network templates.
    # 5 bundled: feedback_loop, post_processing, audio_reactive,
    # particle_gpu, feedback_displacement. User templates in
    # ~/.tdpilot-dpsk4/api/macros/*.json layer over bundled.
    # ---------------------------------------------------------------------
    {
        "name": "macro_list",
        "description": (
            "List available macro templates. Macros are parametrized "
            "network patterns the agent can instantiate with one call "
            "(macro_run). 5 bundled: feedback_loop, post_processing, "
            "audio_reactive, particle_gpu, feedback_displacement. "
            "Users can add custom macros as JSON files in "
            "~/.tdpilot-dpsk4/api/macros/ which override bundled by name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "macro_get",
        "description": (
            "Inspect a macro's parameter schema, node count, and "
            "structure. Use BEFORE macro_run to discover what overrides "
            "are available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "macro_type": {
                    "type": "string",
                    "description": "e.g. 'feedback_loop' or 'audio_reactive'.",
                },
            },
            "required": ["macro_type"],
        },
    },
    {
        "name": "macro_run",
        "description": (
            "Instantiate a macro inside parent_path. Creates all the "
            "nodes, wires connections, applies parameters/expressions/"
            "node-references in one call. Returns the created node "
            "paths (logical_name → path map) and the entry/exit nodes "
            "for chaining further work. Use overrides to customize "
            "param values; defaults from the macro's param_schema "
            "apply otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "macro_type": {"type": "string", "description": "Name of the macro template."},
                "parent_path": {
                    "type": "string",
                    "description": "Where to instantiate (e.g. '/project1').",
                },
                "name_prefix": {
                    "type": "string",
                    "description": (
                        "Optional prefix prepended to each node's logical "
                        "name. Lets you instantiate the same macro multiple "
                        "times under one parent without name collisions."
                    ),
                },
                "node_x": {
                    "type": "integer",
                    "default": 0,
                    "description": "X-offset for the macro's origin in the network editor.",
                },
                "node_y": {
                    "type": "integer",
                    "default": 0,
                    "description": "Y-offset for the macro's origin.",
                },
                "overrides": {
                    "type": "object",
                    "description": (
                        "Param overrides (matching the macro's param_schema). "
                        "e.g. {feedback_opacity: 0.85, blur_size: 8}."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["macro_type", "parent_path"],
        },
    },
    # ===========================================================================
    # Memory advanced (export / import / favorite)
    # ===========================================================================
    {
        "name": "memory_export",
        "description": (
            "Dump every memory file as a JSON object. Used for backup, "
            "cross-machine sharing, or archiving before destructive changes. "
            "Returns {ok, count, memories: {filename: {meta, body}}}."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_import",
        "description": (
            "Restore memories from an export dump. By default skips files "
            "that already exist (preserving local edits); set overwrite=true "
            "to force-overwrite. Returns counts of written/skipped/errored."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memories": {
                    "type": "object",
                    "description": "Dict of {filename: {meta, body}} from memory_export.",
                    "additionalProperties": True,
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, replace existing files. When false, skip them.",
                },
            },
            "required": ["memories"],
        },
    },
    {
        "name": "memory_favorite",
        "description": (
            "Mark a memory as favorite and/or rate it 0-5. The flag and rating "
            "are stored in the memory's frontmatter. Provide at least one of "
            "favorite (bool) or rating (int 0-5)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Memory name or filename."},
                "favorite": {"type": "boolean", "description": "Set favorite flag."},
                "rating": {"type": "integer", "minimum": 0, "maximum": 5, "description": "0-5 rating."},
            },
            "required": ["name"],
        },
    },
    # ===========================================================================
    # Recipe validation
    # ===========================================================================
    {
        "name": "td_validate_recipe",
        "description": (
            "Pre-save sanity check for a recipe. Validates that 'replay' is a "
            "list of {tool, args} dicts and every tool name is in the live "
            "TOOL_TO_HANDLER (built-in or user-pluggable). Use BEFORE "
            "recipe_save to catch authoring errors that would only fail at "
            "replay time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional recipe name for error messages."},
                "replay": {
                    "type": "array",
                    "description": "List of {tool, args} dicts to validate.",
                    "items": {"type": "object"},
                },
            },
            "required": ["replay"],
        },
    },
    # ===========================================================================
    # Official-docs lookup (Tier 1 + 2 — derivative corpus + recommendations)
    # ===========================================================================
    {
        "name": "td_search_official_docs",
        "description": (
            "Search TouchDesigner's official documentation corpus by query. "
            "Returns BM25-ranked excerpts with source URLs. Requires the "
            "'derivative' corpus at ~/.tdpilot/data/normalized/derivative/pages.jsonl; "
            "without it returns a hint to install. Optional doc_type filter: "
            "operator, parameter, snippet, palette, guide, release-note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "doc_type": {
                    "type": "string",
                    "description": "Filter by doc category (operator/parameter/snippet/palette/guide/release-note).",
                },
                "top_k": {"type": "integer", "default": 5, "description": "Max results."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "td_get_operator_doc",
        "description": (
            "Fetch full documentation for a specific operator type (e.g. "
            "'noiseTOP', 'feedbackTOP'). Tries an exact-name match first, "
            "falls back to BM25 search restricted to operator-typed pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op_type": {"type": "string", "description": "Operator type name (e.g. 'noiseTOP')."},
            },
            "required": ["op_type"],
        },
    },
    {
        "name": "td_get_param_help",
        "description": (
            "Look up parameter-level help for an operator. Targeted BM25 "
            "search using op_type + param_name. Without the derivative "
            "corpus, suggests td_get_params on a live op as fallback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op_type": {"type": "string", "description": "Operator type name."},
                "param": {"type": "string", "description": "Parameter name."},
            },
            "required": ["op_type", "param"],
        },
    },
    {
        "name": "td_lookup_snippets",
        "description": (
            "Find code snippets by topic. Searches doc_type=snippet entries "
            "in the derivative corpus (or any installed corpus with snippet pages)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Snippet topic (e.g. 'audio reactive level')."},
                "top_k": {"type": "integer", "default": 5, "description": "Max results."},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "td_lookup_palette_component",
        "description": (
            "Look up a TouchDesigner Palette component by name or topic. "
            "Searches doc_type=palette pages first; falls back to a "
            "name-substring BM25 query across all corpora."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Palette component name or topic."},
                "top_k": {"type": "integer", "default": 5, "description": "Max results."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "td_recommend_official_component",
        "description": (
            "Given a goal description, surface relevant official Palette "
            "components, operator-level docs, and code snippets the agent "
            "can reason over before building from scratch. Returns three "
            "ranked lists (palette_components, operators, snippets)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "What you're trying to build (e.g. 'audio-reactive feedback loop').",
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "td_find_official_example",
        "description": (
            "Find TouchDesigner-shipped examples for a topic. Searches the "
            "'examples' corpus first if installed, else derivative entries "
            "tagged as examples."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Example topic (e.g. 'particle simulation')."},
                "top_k": {"type": "integer", "default": 5, "description": "Max results."},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "td_explain_better_way",
        "description": (
            "Given the user's current approach + their goal, surface "
            "canonical alternatives from the docs corpus. Constructs two "
            "queries (best-practice and alternative) and merges results "
            "deduped by name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_approach": {
                    "type": "string",
                    "description": "What they're doing now (e.g. 'looping audioCHOP into mathCHOP for envelope').",
                },
                "goal": {
                    "type": "string",
                    "description": "What they want (e.g. 'beat-aligned amplitude envelope').",
                },
            },
            "required": ["goal"],
        },
    },
    # ===========================================================================
    # TD 2025 native introspection
    # ===========================================================================
    {
        "name": "td_python_env_status",
        "description": (
            "Return Python version, executable, sys.path, and a sampled "
            "list of top-level installed modules. Useful for diagnosing "
            "ImportError issues inside TD."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_threading_status",
        "description": (
            "Return active threads, daemon flags, and main/current thread "
            "names. Useful for diagnosing 'why is my callback not firing' "
            "thread issues."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_logger_status",
        "description": (
            "Return TD's project cookLogger state (active flag, log count). "
            "Returns logger_available=false on TD builds that don't expose it."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_tdresources_inspect",
        "description": (
            "Inspect TDResources — TD's bundled-asset registry. Lists "
            "Palette/Examples/Builtin groups with sample child names so the "
            "agent knows what's installed before recommending components."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_color_pipeline",
        "description": (
            "Return the project's color management settings (mode, gamma, "
            "viewer color space, OCIO config). Useful for diagnosing "
            "'why does my render look different' colorspace issues."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_component_standardize",
        "description": (
            "Audit a COMP for project standards: naming convention, color "
            "coding, tag policy, comment presence, child-count limits. "
            "Returns a list of issues with severity. Pure read — never "
            "mutates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/project1",
                    "description": "Absolute COMP path to audit.",
                },
            },
        },
    },
    {
        "name": "td_audit_project",
        "description": (
            "Recursively audit a project subtree — collects errors, "
            "warnings, and standards-compliance issues across every COMP "
            "descendant. Aggregates so the agent gets one summary instead "
            "of N tool calls. Capped to max_depth (default 5)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/project1",
                    "description": "Subtree root.",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 5,
                    "description": "Recursion limit (1-20).",
                },
            },
        },
    },
    # ===========================================================================
    # Server introspection (Tier 2)
    # ===========================================================================
    {
        "name": "td_get_server_metrics",
        "description": (
            "Return process-level metrics for the standalone agent: uptime, "
            "RSS memory, CPU%, thread count, and runtime counters (turn "
            "count, tool call count, queue depth). Best-effort — missing "
            "fields just don't appear in the response."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_describe_surface",
        "description": (
            "Compact view of every tool the agent has access to — built-in "
            "+ user-pluggable. Categorizes by name prefix. Useful when the "
            "model needs to answer 'what can you do?' without enumerating "
            "every schema."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_get_recent_traces",
        "description": (
            "Return the most recent per-turn observability traces "
            "written to ~/.tdpilot-dpsk4/api/traces/. Each trace is a dict "
            "with ts, session_id, turn_id, user_text_hash, model_tier, "
            "model_used, total_tokens, tool_calls (list of {name, "
            "args_hash, latency_ms, ok, error}), outcome, "
            "duration_ms. User text and tool args are hashed (12-char "
            "SHA-256 prefix) so the file never holds raw prompt "
            "content. Use this for debugging behaviour regressions, "
            "or as input to eval scripts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max records to return, newest first. Default 10, max 200.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 200,
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "td_get_capabilities",
        "description": (
            "Report which features are wired in this build of the "
            "standalone (memory/knowledge/recipes/skills/patches/user_tools/"
            "subagents/macros/official_docs/td2025_native/introspect/bm25). "
            "Plus active model tier, exec mode, and bundled corpus counts."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "td_get_capabilities_summary",
        "description": (
            "v2.4 / Phase C.6 — return a grouped human-readable capability "
            "index with example prompts per group. Use when the user asks "
            "'what can you do?' or to render UI affordances (the chat HTML "
            "renders 5–6 'featured prompt' chips from `featured_prompts` "
            "on first load). Pure data — no live TD calls."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "tool_batch",
        "description": (
            "Run multiple TDPilot tool calls in one round trip. Saves "
            "LLM round-trip cost when you need several independent "
            "lookups (e.g. info + errors + capabilities) — submit them "
            "all here instead of issuing N separate tool_use blocks. "
            "Each sub-call's result is returned in `results[i]` with "
            "the same shape as a normal tool result. A failed sub-call "
            "does NOT abort the batch — the failure is reported in "
            "results[i].error and the rest still run. Max 8 sub-calls "
            "per batch; nested tool_batch is rejected. Sub-calls "
            "execute serially on the cook thread (TD's API isn't "
            "thread-safe), so the win is round-trip latency, not "
            "per-tool latency."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "description": "List of tool calls to dispatch.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": (
                                    "Name of an existing TDPilot tool (must be "
                                    "in TOOL_SCHEMAS — tool_batch itself is rejected)."
                                ),
                            },
                            "args": {
                                "type": "object",
                                "description": "Arguments dict for that tool. May be empty.",
                            },
                        },
                        "required": ["tool"],
                    },
                },
            },
            "required": ["calls"],
            "additionalProperties": False,
        },
    },
]


def supported_tool_names() -> list[str]:
    return [t["name"] for t in TOOL_SCHEMAS]
