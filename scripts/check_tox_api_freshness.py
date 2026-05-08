#!/usr/bin/env python3
"""Fail if the committed ``td_component/tdpilot_API.tox`` is out of sync with source.

Parallel to ``scripts/check_tox_freshness.py`` (which guards
``tdpilot-dpsk4.tox``). The .tox is a binary TD artifact and can only
be rebuilt inside TouchDesigner. Pre-2.1.1 only the dpsk4 .tox had a
CI gate, so the API .tox went stale silently if a contributor edited
``tdpilot_api_runtime.py``, ``tdpilot_api_chat.html``, or any other
file under the API source tree without rebuilding inside TD. Users
installing the plugin would get an old binary while CI stayed green.

This guard compares the hash of the source files against the hash
recorded in ``td_component/.tox-api-source-hash.json`` at build time.
Mismatch -> "rebuild the API .tox in TD before pushing".

Runs in CI. Also runnable locally.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Must match _API_TOX_SOURCE_FILES in td_component/build_tdpilot_api_tox.py.
# Add new files in BOTH places — the build script's list drives the hash
# that gets written; this list drives the check that compares to it.
# A drift between the two lists shows up as a stable mismatch even with
# no source edits (different inputs -> different hashes).
SOURCE_FILES = (
    # Direct embeds — every file listed in the build script's _SOURCE_FILES
    # tuple as a real path (i.e. not the `<COMPOSE>` sentinel).
    "td_component/tdpilot_api_agent.py",
    "td_component/tdpilot_api_dispatcher.py",
    "td_component/tdpilot_api_config.py",
    "td_component/tdpilot_api_lookup.py",
    "td_component/tdpilot_api_schema_defs.py",
    "td_component/tdpilot_api_schema_map.py",
    "td_component/tdpilot_api_schema.py",
    "td_component/tdpilot_api_runtime.py",
    "td_component/tdpilot_api_extension.py",
    "td_component/tdpilot_api_bm25.py",
    "td_component/tdpilot_api_memory.py",
    "td_component/tdpilot_api_knowledge.py",
    "td_component/tdpilot_api_recipes.py",
    "td_component/tdpilot_api_skills.py",
    "td_component/tdpilot_api_patches.py",
    "td_component/tdpilot_api_user_tools.py",
    "td_component/tdpilot_api_subagents.py",
    "td_component/tdpilot_api_macros.py",
    "td_component/tdpilot_api_official_docs.py",
    "td_component/tdpilot_api_td2025.py",
    "td_component/tdpilot_api_introspect.py",
    "td_component/tdpilot_api_batch.py",
    "td_component/tdpilot_api_recovery.py",
    "td_component/tdpilot_api_tracing.py",
    "td_component/tdpilot_api_compaction.py",
    "td_component/tdpilot_api_chat.html",
    "td_component/tdpilot_api_web_callbacks.py",
    "td_component/tdpilot_api_executor.py",
    "td_component/tdpilot_api_parexec.py",
    # Composed mcp_webserver_callbacks textDAT — its body comes from the
    # callbacks/ split package (overlaps with check_tox_freshness.py by
    # design; both .tox files embed this composed content).
    "td_component/callbacks/_composer.py",
    "td_component/callbacks/__init__.py",
    "td_component/callbacks/_header.py",
    "td_component/callbacks/router.py",
    "td_component/callbacks/auth.py",
    "td_component/callbacks/serializers.py",
    "td_component/callbacks/handlers/__init__.py",
    "td_component/callbacks/handlers/nodes.py",
    "td_component/callbacks/handlers/exec_and_custom_params.py",
    "td_component/callbacks/handlers/exec_python.py",
    "td_component/callbacks/handlers/inspect.py",
    "td_component/callbacks/handlers/search.py",
    "td_component/callbacks/handlers/lifecycle.py",
    "td_component/callbacks/handlers/pulse.py",
    "td_component/callbacks/handlers/monitor.py",
    "td_component/callbacks/handlers/analyze_frame.py",
    # Build script bytes — same reasoning as the dpsk4 gate (any change to
    # how the .tox is laid out forces a rebuild signal even if no embedded
    # source changed).
    "td_component/build_tdpilot_api_tox.py",
)
HASH_FILE = ROOT / "td_component" / ".tox-api-source-hash.json"
TOX_FILE = ROOT / "td_component" / "tdpilot_API.tox"


def compute_current_hash() -> str:
    h = hashlib.sha256()
    for rel in SOURCE_FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


def main() -> int:
    if not TOX_FILE.exists():
        print("ERROR: " + str(TOX_FILE.relative_to(ROOT)) + " is missing.")
        print("       Rebuild it inside TouchDesigner via")
        print("       td_component/build_tdpilot_api_tox.py in the Textport.")
        return 1

    if not HASH_FILE.exists():
        print("ERROR: " + str(HASH_FILE.relative_to(ROOT)) + " is missing.")
        print("       Rebuild the API .tox in TouchDesigner — the rebuild writes this file.")
        return 1

    stored = json.loads(HASH_FILE.read_text())
    stored_hash = stored.get("tox_source_hash")
    current_hash = compute_current_hash()

    if stored_hash != current_hash:
        print("ERROR: tdpilot_API.tox is stale relative to td_component source.")
        print("  stored hash:  " + str(stored_hash))
        print("  current hash: " + current_hash)
        print("  built at:     " + str(stored.get("built_at")))
        print("")
        print("Rebuild the API .tox in TouchDesigner before pushing.")
        print("  See td_component/build_tdpilot_api_tox.py and the Textport recipe in")
        print("  feedback_td_tox_rebuild_recipe.md (single-line statements only).")
        print("  Then: git add td_component/tdpilot_API.tox td_component/.tox-api-source-hash.json")
        return 1

    print(
        "tdpilot_API.tox is fresh (hash "
        + current_hash[:16]
        + "..., built "
        + str(stored.get("built_at"))
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
