# TDPilot DPSK4 — Roadmap v2.2.0 → v3.0

**Scope:** standalone API variant (`tdpilot_API.tox`) usability. Every phase below targets the in-TD chat experience first. MCP-side improvements may follow as derivatives but aren't the priority.

**Status:** drafted 2026-05-11. Source-of-truth for the v2.2.0→v3.0 implementation arc.

**Source of truth for execution:** this file. When in doubt, prefer this plan over scattered conversation notes. Updates land via PR like any other doc change.

---

## How to use this doc

1. **Picking up work in a new session?** Start with [`docs/NEW_SESSION_PROMPT.md`](./NEW_SESSION_PROMPT.md). It bootstraps a fresh agent with the right context.
2. **Operating rules / release flow?** See [`AGENTS.md`](../AGENTS.md) at the repo root.
3. **What just shipped?** See [`CHANGELOG.md`](../CHANGELOG.md).
4. **This doc:** the multi-month plan, phase by phase, with concrete file-level deliverables and test counts.

---

## North-star vision

A user opens TouchDesigner, drags `tdpilot_API.tox` into `/project1`, pastes a DeepSeek key, and is met with a chat panel that:

- **understands their existing project** (auto-explain),
- **answers questions with provable verification** (freshness indicator),
- **shows visual results inline** (inline screenshots),
- **proposes plans before destructive builds** (plan preview),
- **rolls back automatically if it breaks something** (auto-rollback on error regression),
- **accepts voice input during live shows** (Web Speech API),
- **costs cents per session via DeepSeek prefix-cache discipline**, and
- **never silently drops a message** (the v2.1.3-5 FIFO inbox + safety timer).

The chat feels less like a remote tool and more like a co-pilot embedded in the network — open-source, prefix-cache disciplined, with a broad feature mix.

---

## Strategic context

The plan below targets three axes of dpsk4 differentiation:

- **Cost discipline** — DeepSeek prefix-cache stability gives us a 50× input-token discount per cached turn. Every phase preserves byte-stable system prompts.
- **Reliability** — auto-rollback on error regression, cycle detection, freshness indicators, mid-turn integrity checks. The agent becomes safe to leave unsupervised on a complex build.
- **Open-source positioning** — every feature ships as inspectable source; no external SaaS dependency for the core agent loop. Optional integrations (vision, STT, local models) are pluggable but never required.

Phases 1–2 land reliability + visibility. Phases 3–4 expand the content surface (audio, GLSL, templates, voice). Phase 5 turns the agent into a senior pair-programmer. Phase 6 evolves the architecture (out-of-process workers, user-defined operator tools, graph memory, MCP-server mode, local-model support).

---

## Phase 0 — Foundation (week 0, ~3 days)

**Purpose:** enable safe iterative shipping of subsequent phases. No user-visible UX changes; lays plumbing the other phases need.

### Features

| # | Item | Reason |
|---|---|---|
| 0.1 | **Feature-flag module** (`td_component/tdpilot_api_features.py`) — central registry reading COMP params + env vars + config.json. Default values per flag. Tests can monkey-patch. | Every Phase 1-5 feature needs an opt-in/opt-out flag so we can ship "experimental" → "preview" → "default-on" across releases. |
| 0.2 | **Latency-bench harness** (`scripts/bench_chat_pipe.py`) — runs N canonical chat turns against a mock-DeepSeek; reports per-tool latency, total turn time, token usage. | We need baselines before measuring impact of D2 cache, B4 mid-turn check, etc. |
| 0.3 | **Phase-PR test conventions** documented in `AGENTS.md` — every phase PR follows: source diff → unit tests → integration tests → request user tox rebuild → live verification. | Tox-rebuild friction means we batch source changes per phase; tests must catch as much as possible at PR time, not at runtime. |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_features.py` | **NEW** — `is_enabled(flag_name)`, `get(flag_name)`, declarative `FLAGS` dict |
| `td_component/build_tdpilot_api_tox.py` | Add `tdpilot_api_features.py` to `_API_TOX_SOURCE_FILES` tuple |
| `scripts/bench_chat_pipe.py` | **NEW** — uses `tests/_mock_deepseek.py` fixture machinery |
| `tests/test_v220_features_module.py` | **NEW** — pin flag defaults + override precedence |
| `AGENTS.md` | Add "Phase-PR test conventions" subsection |

### Tox-rebuild requirement
**API tox only.** Single rebuild at end of Phase 0.

### Effort
**2-3 days.** Mostly plumbing.

### Tests added
~6 tests pinning the flag module's precedence rules (COMP param > env var > config.json > default).

---

## Phase 1 — Reliability foundation (v2.2.0, ~1.5 weeks)

**Goal:** zero "agent broke my project" incidents. The agent becomes safe to leave unsupervised on a complex build.

### Features

| # | Feature | Description | Default state |
|---|---|---|---|
| 1.1 | **Auto-rollback on error regression** | At each tool batch's start, capture `td_get_errors` baseline + take an in-memory snapshot. After the batch, recheck errors. If new critical errors appear, restore the snapshot and emit `EV_HINT(auto_rollback: <N> new critical errors)`. | `enabled` (low risk; user can opt out via `TDPILOT_DISABLE_AUTO_ROLLBACK`) |
| 1.2 | **Cycle detection in tool chains** | Per-turn `(tool_name, args_hash)` ledger. After 3 identical calls, break the turn with `EV_ERROR(Cycle detected: <tool> ×3)`. | `enabled` |
| 1.3 | **Mid-turn integrity check** | Every K (default 5) tool calls, run `td_get_errors` recurse=True. If new errors found, surface via `EV_HINT` and offer rollback (reuses 1.1 infra). | `enabled` |
| 1.4 | **Answer freshness indicator** | Count `EV_TOOL_CALL` events per turn. If zero by `EV_DONE`, prepend `(cached state)` to assistant text in the chat bubble. | `enabled` |
| 1.5 | **Per-turn tool-result cache** | Wrap dispatcher; for idempotent reads (`td_get_*`, `td_chop_data`, `td_geometry_data`, `td_pop_inspect`), cache by `(tool, args_hash)` within a turn. | `enabled` |
| 1.6 | **Adaptive max_tokens** | Scale based on user prompt complexity. Short lookups: 1024. Builds: 4096. Heuristic in `_resolve_model`-adjacent helper. | `enabled` |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_agent.py` | + cycle detection in `_loop` (~30 lines). + adaptive max_tokens helper (~20 lines). |
| `td_component/tdpilot_api_runtime.py` | + baseline-error capture at `start_turn`. + mid-turn integrity check loop. + post-batch error re-check + rollback orchestration. + EV_TOOL_CALL counter for freshness indicator. (~100 lines) |
| `td_component/tdpilot_api_dispatcher.py` | + per-turn `_results_cache` dict; cache only idempotent reads. (~40 lines) |
| `td_component/tdpilot_api_extension.py` | + handle new `EV_HINT(auto_rollback)` in `_handle_event` (broadcast to chat UI). |
| `td_component/tdpilot_api_chat.html` | + render `(cached state)` prefix when `payload.cached === true`. + render auto-rollback hint with a "view rolled-back changes" expander. (~50 lines) |
| `td_component/tdpilot_api_features.py` | + register flags: `AUTO_ROLLBACK`, `CYCLE_DETECT`, `MID_TURN_CHECK`, `FRESHNESS_INDICATOR`, `TOOL_RESULT_CACHE`, `ADAPTIVE_MAX_TOKENS` |
| `td_component/callbacks/_header.py` | bump `API_VERSION = "2.2.0"` (forces dpsk4 tox rebuild too) |
| 7 other version manifests | bump to `2.2.0` |
| `CHANGELOG.md` | new `## 2.2.0 - YYYY-MM-DD` section with feature details |
| `README.md` | banner update |
| `tests/test_v220_reliability.py` | **NEW** — ~30 tests covering each feature |

### Tox-rebuild requirement
**BOTH .tox files** (`_header.py::API_VERSION` is in both source lists).

### Effort
**1.5 weeks** (~30 hours: 50% code, 30% tests, 20% docs/release flow).

### Risks
- **1.1 auto-rollback** could surprise users who WANT errors temporarily (e.g. mid-build with deferred wiring). Mitigation: define `_is_critical(error)` narrowly — only count engine-level errors (missing refs, type mismatches), not warnings.
- **1.5 cache** could mask staleness if TD state changes mid-turn outside the agent's awareness. Mitigation: cache lifetime is per-turn (cleared at turn end), and `td_set_params` / `td_create_node` / etc. invalidate by path prefix.

### Tests
- `test_b1_auto_rollback_restores_on_new_critical_error`
- `test_b1_auto_rollback_no_op_when_errors_unchanged`
- `test_b1_auto_rollback_respects_disable_flag`
- `test_b2_cycle_detection_breaks_on_third_identical_call`
- `test_b2_cycle_detection_does_not_break_on_different_args`
- `test_b4_mid_turn_check_fires_every_k_calls`
- `test_b7_freshness_indicator_present_when_no_tool_calls`
- `test_b7_freshness_indicator_absent_when_tool_called`
- `test_d2_cache_serves_repeated_read`
- `test_d2_cache_invalidated_on_mutation`
- `test_d2_cache_cleared_at_turn_end`
- `test_d3_adaptive_max_tokens_short_prompt`
- `test_d3_adaptive_max_tokens_long_build_prompt`

---

## Phase 2 — Visual feedback loop (v2.2.1, ~1.5 weeks)

**Goal:** the user sees what the agent does without leaving the chat. The chat panel becomes a primary surface, not a passive log.

### Features

| # | Feature | Description | Default state |
|---|---|---|---|
| 2.1 | **Inline screenshots in chat** | After every `td_screenshot` tool result, emit an `image` event over WS with base64 JPEG. Chat HTML renders inline as `<img>` in the assistant's reply bubble (not as a tool-result blob). Max 5 inline screenshots per turn (clip oldest). | `enabled` |
| 2.2 | **Live metrics in status bar** | Add channels: **FPS** (from `me.time.frame` delta), **GPU%** (from system CHOP — TD exposes it), **cook queue depth** (from CookThreadDispatcher), **last-API-latency** (from agent loop). Surface in the status bar alongside model badge + token meter. | `enabled` |
| 2.3 | **Cost tracker** | Track DeepSeek input/output/cache tokens × current price. Display per-turn and session-total in status bar. Configurable price via env (`TDPILOT_DEEPSEEK_PRICE_INPUT_PER_M`, etc.). | `enabled` |
| 2.4 | **Subagent activity panel** | Below main chat, collapsible panel listing live subagents: `sa_<id> → <task summary> → <elapsed>s → <last tool>`. Updates via existing `EV_SUB_TOOL` / `EV_SUB_DONE` events. | `enabled` |
| 2.5 | **Better tool-call rendering** | Collapsible by default; expand to show full args (pretty-printed JSON), result preview (truncate large), duration, retry button. | `enabled` |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_runtime.py` | + new `EV_METRIC` event type. + `_emit_metrics` cook-thread helper called periodically (every N frames). + image-event emission after `td_screenshot` tool result. |
| `td_component/tdpilot_api_extension.py` | + handle `EV_METRIC` in `_handle_event` (broadcast to chat). + handle image-event piggyback on `EV_TOOL_RESULT`. |
| `td_component/tdpilot_api_web_callbacks.py` | + serialize image-events with base64 + content-type metadata. |
| `td_component/tdpilot_api_chat.html` | + inline image renderer (with `loading=lazy`, max-width, click-to-fullscreen). + status-bar metric channels (4 new cells). + subagent panel (collapsible `<details>`). + tool-call expander (replace current `<details>` with richer renderer). (~250 lines net) |
| `td_component/tdpilot_api_agent.py` | + emit per-call latency via on_usage callback. |
| `td_component/tdpilot_api_features.py` | + flags: `INLINE_SCREENSHOTS`, `LIVE_METRICS`, `COST_TRACKER`, `SUBAGENT_PANEL` |
| `tests/test_v221_visual_feedback.py` | **NEW** — ~15 tests (chat HTML structural + agent event-emission) |

### Tox-rebuild requirement
**API tox only.** No `_header.py` change if we don't bump the version.

**Recommendation:** ship Phase 1 as v2.2.0, Phase 2 as v2.2.1. Each requires one tox rebuild.

### Effort
**1.5 weeks.** Chat HTML changes are the bulk; most of it is markup + CSS + small JS additions.

### Risks
- **Inline screenshots inflate chat HTML size** — 5 × 50KB JPEGs per turn = 250KB per turn. Mitigation: lazy-load, click-to-expand for high-res, downscale to 800px width by default.
- **Live metrics cooking cost** — `_emit_metrics` runs every N frames. Make N configurable (default 30 = 0.5Hz at 60fps). Negligible cook cost at 0.5Hz.

---

## Phase 3 — Audio + GLSL + templates (v2.3.0, ~3 weeks)

**Goal:** cover TD's #1 use case (audio-reactive visuals) and #2 use case (custom shaders) end-to-end. Provide curated, production-grade templates so users can start from working scaffolds.

### Features

| # | Feature | Description | Default state |
|---|---|---|---|
| 3.1 | **`td_audio_analyze(path, [features])`** | Wraps `audiofileinCHOP → audioAnalysisCHOP → ...` into one call. Returns `{bpm, key, energy_timeline, beat_onsets, fft_bands, rms}`. Optional `keep_chain=True` to preserve the nodes for live reaction. | `enabled` |
| 3.2 | **GLSL pattern library** | 16-20 named patterns. New tools `td_glsl_pattern_list()`, `td_glsl_pattern_get(name)`, `td_glsl_pattern_apply(name, target_path)`. Patterns: vignette, chromatic-aberration, bloom, fbm, voronoi, ray-march-sphere, ray-march-sdf, tonemap, color-grade, vhs-glitch, scanlines, datamosh, kaleidoscope, displace-noise, ripple, edge-detect, posterize, halftone, dot-grid, gradient-map. | `enabled` |
| 3.3 | **GLSL safety** | When agent generates GLSL: pre-compile in a stub `glslTOP` (or `glslMAT`) with `compileonly=True`; capture compile errors; retry with errors fed back to the model OR reject if 2× failed. | `enabled` |
| 3.4 | **Template library** | 10-12 templates as recipes. New tool `td_template_list()`, `td_template_apply(name, parent_path)`. Templates: `audio_reactive_shell`, `particle_field_pop`, `feedback_chain_canonical`, `video_mixer`, `projection_mapping_rig`, `generative_shader_starter`, `gltf_orbit_camera`, `instancing_grid`, `vj_setup_4output`, `dmx_perform_starter`, `webcam_pipeline_starter`, `osc_responder`. | `enabled` |
| 3.5 | **Python API class search** | Ingest TD's official Python docs (TDFunctions, tdu, td.Vector, COMP, OP, Par, TDStoreTools, etc.) into docsbrain. New tools `td_python_class_get(name)`, `td_python_class_search(query)`, `td_python_class_methods(class_name)`. ~70 classes, ~1500 methods. | `enabled` |
| 3.6 | **PBR material macro** | `td_create_pbr_material({diffuse, roughness, metallic, normal_map, ...})` collapses 10+ param sets to one call. | `enabled` |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_audio.py` | **NEW** — `handle_audio_analyze` + helpers (~250 lines) |
| `td_component/tdpilot_api_glsl.py` | **NEW** — pattern library + safety checker + handlers (~400 lines including bundled patterns) |
| `td_component/tdpilot_api_templates.py` | **NEW** — template registry + `handle_template_*` (~300 lines) |
| `td_component/tdpilot_api_python_docs.py` | **NEW** — Python class search handlers (~200 lines, the docs themselves are data) |
| `td_component/tdpilot_api_pbr.py` | **NEW** — PBR material macro (~100 lines) |
| `td_component/tdpilot_api_schema_defs.py` | + ~15 new tool schemas |
| `td_component/tdpilot_api_schema_map.py` | + ~15 new tool→handler mappings |
| `td_component/tdpilot_api_extension.py` | + register 5 new handler modules in `_build_runtime` |
| `td_component/build_tdpilot_api_tox.py` | + 5 new files in `_API_TOX_SOURCE_FILES` |
| **Data files** (NEW, in repo): | |
| `td_component/data/glsl_patterns/*.glsl` | 16-20 GLSL pattern files |
| `td_component/data/templates/*.json` | 10-12 template recipe files |
| `td_component/data/python_classes.jsonl` | ingested Python API (1 line per class) |
| `scripts/build_python_docs_corpus.py` | **NEW** — generator: scrape derivative.ca/UserGuide/ + bundle into jsonl |
| `tests/test_v230_audio.py` | **NEW** — ~10 tests |
| `tests/test_v230_glsl.py` | **NEW** — ~15 tests (pattern catalog, safety check) |
| `tests/test_v230_templates.py` | **NEW** — ~12 tests |
| `tests/test_v230_python_docs.py` | **NEW** — ~8 tests |

### Tox-rebuild requirement
**API tox only.** dpsk4 tox unaffected.

### Effort
**3 weeks.**
- Audio analyze: 3-4 days
- GLSL pattern library + safety: 1 week (content + tests)
- Template library: 1 week (curating templates is mostly content authoring)
- Python API ingestion: 3-4 days (scraper + chunker + tests)
- PBR macro: 1-2 days

### Risks
- **Template content quality** — bad templates undermine trust. Mitigation: each template ships with an explainer + a screenshot of expected output; we test that each template's recipe replays cleanly in a `/sandbox` test COMP.
- **Python API corpus drift** — TD's docs update each build. Mitigation: re-run scraper per TD release; CHANGELOG tracks which build the corpus was built against.
- **GLSL safety false-positives** — `compileonly=True` doesn't catch all runtime issues. Acceptable; we still surface "compiled OK" as a positive signal, not a correctness proof.

---

## Phase 4 — Voice + mobile (v2.4.0, ~1.5 weeks)

**Goal:** hands-free chat for live performance + phone-browser support for "controlling TD from across the room."

### Features

| # | Feature | Description | Default state |
|---|---|---|---|
| 4.1 | **Voice input (STT)** | Mic button in chat. Uses Web Speech API (browser-native, Chrome/Edge/Safari). On click: starts recording, shows live transcript in input field, on stop sends as a normal `/send`. | `enabled` |
| 4.2 | **Voice output (TTS)** (optional) | Toggle in settings. Browser-native Web Speech Synthesis. Reads each assistant text reply aloud. Skips tool_call/tool_result content. | `disabled` by default |
| 4.3 | **Mobile-responsive chat HTML** | Media queries: chat reflows for ≤768px width. Touch-friendly button sizes (44px min). Keyboard appears predictably on focus. | `enabled` |
| 4.4 | **Push-to-talk shortcut** | Hold space-bar to record, release to send (when input is empty). | `enabled` |
| 4.5 | **Network-discovery for phone access** | Chat HTML detects whether it's loaded from `127.0.0.1` vs LAN IP; surfaces "open on phone: http://<lan-ip>:9987/" QR code on a settings panel. Pairs with existing `TDPILOT_API_INSECURE` / token model. | `enabled` |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_chat.html` | + mic button + Web Speech API integration (~150 lines). + TTS toggle + speech synthesis (~80 lines). + media queries for responsive layout (~50 lines). + push-to-talk handler (~30 lines). + QR-code rendering for LAN URL (~40 lines, use a small QR library inlined). |
| `td_component/tdpilot_api_features.py` | + flags: `VOICE_INPUT`, `VOICE_OUTPUT`, `MOBILE_LAYOUT`, `PUSH_TO_TALK` |
| `td_component/tdpilot_api_web_callbacks.py` | + optionally relax origin allowlist when `TDPILOT_API_INSECURE=1` for LAN access (already kind-of supported; document the safe pattern) |
| `tests/test_v240_voice_mobile.py` | **NEW** — ~12 tests (structural HTML assertions) |

### Tox-rebuild requirement
**API tox only** (chat.html in API tox source list).

### Effort
**1.5 weeks.** Mostly chat HTML changes. The Web Speech API is well-documented; the bulk of work is UX polish on the mic button states (idle / recording / processing / error).

### Risks
- **Browser compatibility** — Web Speech API has gaps in Firefox. Mitigation: graceful degradation — mic button hidden if API unavailable, with a tooltip.
- **LAN access security** — exposing the chat to a phone over LAN expands the attack surface. Mitigation: enforce the per-launch token even for LAN; emit a warning if `TDPILOT_API_INSECURE=1` is also set (insecure + LAN = anyone on Wi-Fi can drive TD).

---

## Phase 5 — Agent-IDE features (v2.5.0, ~4 weeks)

**Goal:** the agent feels like a senior pair-programmer who already knows the project. Auto-explain, auto-troubleshoot, sandboxed execution.

### Features

| # | Feature | Description | Default state |
|---|---|---|---|
| 5.1 | **Auto-explain project** | New tool `td_explain_project(path="/project1", depth=3)`. Surveys nodes under path → identifies signal flow → classifies COMPs by role → returns Markdown writeup with section headers. Uses existing primitives. | `enabled` |
| 5.2 | **Auto-troubleshoot** | New tool `td_audit_and_fix(path, dry_run=True)`. Scans errors recursively → groups by root cause → proposes a fix plan with snapshots → if `dry_run=False`, applies with user confirmation per group. | `enabled` |
| 5.3 | **Multi-turn plan preview** | Before any tool batch ≥5 calls, agent emits `EV_PLAN` with the proposed steps; chat UI shows yes/no/edit modal. User can edit individual steps or skip plan preview entirely. | `disabled` by default (opt-in via COMP param `Planpreview`); will promote to default in v2.6.0 after feedback |
| 5.4 | **Sandbox-first mode** | Toggleable via COMP param `Sandboxmode`. When on: all `td_create_node` / `td_set_params` / `td_connect_nodes` calls get their `parent_path` rewritten from `/project1/<X>` to `/project1/sandbox/<X>`. User reviews; `td_promote_sandbox()` moves from sandbox to project root. | `disabled` by default; opt-in |
| 5.5 | **Destructive-op confirmation** | For `td_delete_node` calls that would delete ≥5 nodes OR touch any /project1 top-level COMP, dispatch is gated by `EV_CONFIRM` event; chat UI renders "Confirm? Yes / No" inline. | `enabled` |
| 5.6 | **Stale-state detection on session resume** | On chat init (WS first connect after `/reset` or page load), compute a project-state hash. Compare against last-saved hash. If differs significantly, surface "since last session: +N nodes, M new errors" in the welcome message. | `enabled` |
| 5.7 | **Cook-thread health monitor** | Dispatcher queue-depth + pump-latency tracked over a rolling window. If queue depth grows past N or median pump latency past M ms, surface `EV_HINT(td_slow: ...)` in chat. | `enabled` |

### File-level inventory

| File | Change |
|---|---|
| `td_component/tdpilot_api_explain.py` | **NEW** — `handle_explain_project` + helpers (~300 lines) |
| `td_component/tdpilot_api_audit.py` | **NEW** — `handle_audit_and_fix` + error grouping + fix planning (~400 lines) |
| `td_component/tdpilot_api_sandbox.py` | **NEW** — sandbox mode wrapper + `handle_promote_sandbox` (~250 lines) |
| `td_component/tdpilot_api_planpreview.py` | **NEW** — `EV_PLAN` event + agent-loop integration (~150 lines) |
| `td_component/tdpilot_api_dispatcher.py` | + destructive-op gate (~50 lines). + sandbox-mode path rewrite (~30 lines). |
| `td_component/tdpilot_api_runtime.py` | + cook-thread health monitor (~80 lines). + stale-state detection on `start_turn` first-fire (~40 lines). |
| `td_component/tdpilot_api_extension.py` | + handle 3 new event types (EV_PLAN, EV_CONFIRM, EV_HINT(td_slow)). + COMP params (Planpreview, Sandboxmode). |
| `td_component/tdpilot_api_chat.html` | + plan-preview modal UI (~120 lines). + destructive-op confirm modal (~80 lines). + sandbox-mode indicator in status bar (~30 lines). + stale-state delta in welcome (~30 lines). |
| `td_component/tdpilot_api_schema_defs.py` | + schemas for `td_explain_project`, `td_audit_and_fix`, `td_promote_sandbox`, `td_sandbox_status` |
| `td_component/tdpilot_api_schema_map.py` | + handler mappings |
| `td_component/tdpilot_api_features.py` | + flags for each feature |
| `td_component/build_tdpilot_api_tox.py` | + 4 new files in `_API_TOX_SOURCE_FILES` |
| `tests/test_v250_agent_ide.py` | **NEW** — ~35 tests |

### Tox-rebuild requirement
**API tox + dpsk4 tox** (version bump → `_header.py::API_VERSION`).

### Effort
**4 weeks.**
- Auto-explain: 1 week (orchestration + Markdown rendering + tests)
- Auto-troubleshoot: 1.5 weeks (error-grouping heuristics + fix-plan generation + dry-run/apply semantics)
- Plan preview: 1 week (event flow + chat modal + agent loop integration)
- Sandbox-first mode: 1 week (path rewriting is tricky; promotion is destructive — needs careful design)
- Destructive-op confirm: 3 days
- Stale-state + cook-thread monitor: 3 days

### Risks
- **Sandbox-mode is the highest-risk feature.** Path rewriting can break referential params (an expression at `/project1/foo` referencing `/project1/bar` doesn't work if bar moves to `/project1/sandbox/bar`). Mitigation: only rewrite for `parent_path` parameters; do NOT rewrite parameter values that look like paths. Test exhaustively.
- **Auto-troubleshoot proposing wrong fixes** undermines trust. Mitigation: dry-run by default; show fix plan; require user "apply" before any destructive action.
- **Plan preview adds latency** to every multi-tool turn. Mitigation: only fires for batches ≥5; skippable per-turn via "skip preview this once" in the modal.

---

## Phase 6 — Architecture evolution (v3.0, ~10 weeks)

**Goal:** evolve the architecture. Out-of-process workers (OOP-worker), user-defined tools as TD operators, graph memory, and an MCP-server mode that wraps the chat-pipe agent loop so external clients can drive it.

### Features (research-first; spike each before committing)

| # | Feature | Description | Risk |
|---|---|---|---|
| 6.1 | **OOP-worker process** for heavy work (vision, embeddings, STT, image-gen) | Out-of-process Python worker. IPC over local Unix socket / Windows pipe. Heavy LLM-adjacent work (e.g. screenshot analysis with a vision model, embedding generation, Whisper STT) runs outside TD's process. | High — new IPC layer, process lifecycle, error propagation |
| 6.2 | **Operator-registered tools** | Let any TD operator declare itself as an agent tool via a contract (e.g. a custom-parameter page with name "TDPilotTool" + role / args / handler attached as a custom Python class). Agent's tool registry scans the network at runtime + auto-loads. | High — composition pattern needs careful spec |
| 6.3 | **Graph-based memory** | Replace BM25 docsbrain with a graph schema (nodes = entries, edges = "related-to", "supersedes", "contradicts"). Use SQLite + a simple graph schema or DuckDB graph extension. | Medium — migration story from current BM25 |
| 6.4 | **MCP-server mode** | Let external agents (Cursor, Claude Code) drive the chat-pipe. Add an MCP-server mode that wraps the agent loop as MCP tools. The chat-pipe webserver could serve both HTML chat AND MCP-over-WS simultaneously. | Medium |
| 6.5 | **Local model support** | Configurable base URL + auth pattern that accepts Ollama / LM Studio / llama.cpp endpoints — keep a local-first option available for users without DeepSeek access. | Low — mostly config + docs |
| 6.6 | **Reorganize chat-pipe codebase into a packaged module** | Currently 30+ `tdpilot_api_*.py` flat files. Move to `tdpilot_api/` package with submodules. Long-overdue cleanup; enables better testing. | Low |

### Effort
**8-10 weeks.** Each feature is multi-week. Plan: 4 weeks OOP-worker + operator-registered tools (the two architecture-evolution features), 3 weeks graph memory + MCP-server mode, 1-2 weeks codebase reorg + local-model support.

### Tox-rebuild requirement
**Both tox files**, multiple times during the phase as the codebase reorganizes.

### Recommendation
Do NOT commit to all of Phase 6 in one release. Spike the OOP-worker with a single workload (e.g. STT via Whisper.cpp) first; ship if it works; expand. Same for operator-registered tools — spike with one user-defined tool type.

---

## Cross-cutting concerns

### Testing strategy

Every phase follows this discipline:

1. **Unit tests** for pure-Python logic (most things). Use existing patterns from `tests/test_tdpilot_api_*.py`.
2. **Structural tests** for chat HTML (regex/grep). Same pattern as `tests/test_chat_html_state.py`, `tests/test_v214_codex_followups.py`.
3. **Mock-DeepSeek integration tests** for agent loop behaviour. Use existing `tests/_mock_deepseek.py`.
4. **Live-TD integration tests** marked `@pytest.mark.agent_eval` (already deselected from default `pytest`). Only run when TD is open and a key is configured.
5. **Static name-pin tests** added to `tests/test_release_critical_names.py` for every new tool that has a stable user-visible name.

Test additions per phase target ≥1.5× the test count growth in code lines. So if a phase adds 1000 LOC, expect ~30-50 new tests.

### Feature-flag pattern

Every new feature ships behind a flag with this lifecycle:

```
v(N).0       — flag exists, default OFF        (experimental)
v(N+1).0     — default ON, opt-out via flag    (preview)
v(N+2).0     — flag removed, always on         (default)
```

Implementation in `td_component/tdpilot_api_features.py`:

```python
FLAGS = {
    "AUTO_ROLLBACK":         {"default": True,  "since": "2.2.0"},
    "CYCLE_DETECT":          {"default": True,  "since": "2.2.0"},
    "PLAN_PREVIEW":          {"default": False, "since": "2.5.0"},
    "SANDBOX_MODE":          {"default": False, "since": "2.5.0"},
    "OOP_WORKER_VISION":     {"default": False, "since": "3.0.0"},
    # ...
}

def is_enabled(flag_name: str) -> bool:
    # Precedence: COMP param > env var > config.json > FLAGS[default]
    ...
```

### Release flow per phase

```
1. Branch off origin/main:                claude/v<X.Y.Z>-phase-<N>-<slug>
2. Add feature-flag entries first         (Phase 0 plumbing)
3. Land source changes (feature-flag-gated)
4. Add unit + structural tests
5. Bump 7 version manifests (incl. API_VERSION → forces tox rebuild)
6. CHANGELOG entry + README banner update
7. Local sweep: pytest + ruff + check_versions + check_tox_freshness_*
8. User rebuilds both .tox files via Textport recipe (per AGENTS.md)
9. Local sweep again, this time freshness gates pass
10. Commit (HEREDOC + Co-Authored-By), push, gh pr create
11. Watch 6 CI checks; merge when green
12. Tag + gh release create (per AGENTS.md "12-step release ritual")
13. Verify npm publish + GitHub release assets attached
```

### Documentation discipline

For every new tool / feature:
- Schema docstring in `tdpilot_api_schema_defs.py` — load-bearing because it goes to DeepSeek
- AGENTS.md section if it changes contributor workflow
- Skill update in `skills/tdpilot-dpsk4-core/SKILL.md` if it's a runtime-visible pattern (e.g. new tool the agent should know about)
- CHANGELOG entry with file:line citations for traceability
- README banner update if user-facing
- `tests/test_release_critical_names.py` pin if it has a load-bearing name

### Backward compatibility

- **Storage migrations** use the `resolve_user_dir` pattern from v2.1.3 — never break existing user data. If a feature introduces a new storage subdir, fall back to legacy locations transparently.
- **Tool deprecations** follow a 2-release cycle: deprecation warning in vX.Y, removal in vX.Y+2.
- **COMP parameter changes** add new params; never rename existing ones. If a rename is genuinely needed, ship both for one release with a console deprecation note.

---

## Release cadence summary

| Version | Phase | Time | User-visible delta |
|---|---|---|---|
| v2.2.0 | Phase 1 — Reliability foundation | week 1.5 | Auto-rollback, cycle detect, freshness indicator, cache, adaptive max_tokens |
| v2.2.1 | Phase 2 — Visual feedback loop | week 3 | Inline screenshots, live metrics, cost tracker, subagent panel |
| v2.3.0 | Phase 3 — Audio + GLSL + templates | week 6 | `td_audio_analyze`, 20 GLSL patterns + safety, 12 templates, Python API search |
| v2.4.0 | Phase 4 — Voice + mobile | week 7.5 | Voice STT, mobile-responsive chat, push-to-talk, LAN QR access |
| v2.5.0 | Phase 5 — Agent-IDE features | week 11.5 | Auto-explain, auto-troubleshoot, plan preview, sandbox mode, destructive-op confirm |
| **v2.5.x** | Polish + Codex follow-ups | weeks 12-13 | Whatever Codex catches in v2.2-v2.5 reviews |
| v3.0.0 | Phase 6 — Architecture evolution | weeks 14-23 | OOP-worker, operator-registered tools, graph memory, MCP-server mode, local models |

**Total: ~6 months to v3.0** with disciplined cadence. v2.5.0 is the realistic ship target if you want a "competitive parity" release; v3.0 is the "we lead the architecture" release.

---

## Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Tox-rebuild friction slows iteration | All phases | Batch source changes per phase; rigorous static + unit tests at PR time; rebuild gate only at end of phase |
| Auto-rollback false-positives | Phase 1 | Define `_is_critical(error)` narrowly; user can disable via flag |
| Live metrics cost > 0 | Phase 2 | Configurable polling rate; default 0.5Hz |
| Template quality undermines trust | Phase 3 | Each template ships with a sample screenshot + a recipe-replay round-trip test |
| Voice STT browser support gaps | Phase 4 | Graceful degradation; hide mic if API unavailable |
| Sandbox-mode path rewriting breaks expressions | Phase 5 | Only rewrite `parent_path`; never rewrite param values that look like paths; exhaustive tests |
| Codex reviews catch real bugs (good!) | All phases | Ship Codex follow-up patches per the established pattern (PR #31 as reference) |
| Architecture evolution destabilizes existing users | Phase 6 | Feature-flag everything; default OFF for new architecture; promote only after preview cycle |

---

## Quick-start for the next session

If you're picking this up in a fresh agent session:

1. Read [`AGENTS.md`](../AGENTS.md) (release flow, naming pins, .tox rebuild discipline, DeepSeek rules).
2. Read [`docs/NEW_SESSION_PROMPT.md`](./NEW_SESSION_PROMPT.md) (the copy-pasteable starter prompt that bootstraps you).
3. Read the latest CHANGELOG.md entry (what just shipped).
4. Start on **Phase 0**: feature-flag module + latency-bench harness + phase-PR conventions in AGENTS.md.
5. When Phase 0 ships, propose Phase 1 scope to the user before coding.
6. Always: pytest + ruff + check_versions + check_tox_freshness before every commit. Use `gh` for git ops. Squash-merge. After tag push, `gh release create`.

That's the whole roadmap. Single source of truth lives here.
