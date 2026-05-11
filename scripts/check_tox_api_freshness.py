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

# Pull the canonical source-file list directly from the build script — this is
# the single source of truth for what bytes feed tdpilot_API.tox. Pre-consolidation
# this file kept a parallel SOURCE_FILES tuple that had to be hand-mirrored
# against td_component/build_tdpilot_api_tox.py::_API_TOX_SOURCE_FILES; that
# pair drifted in PR #34 (v2.2.0 Phase 1.1) when `tdpilot_api_rollback.py` was
# added to the build script's list but not this one, breaking CI for two
# commits until the manual mirror caught up. Importing here makes drift
# structurally impossible.
#
# Note: build_tdpilot_api_tox auto-runs its build_and_export() at import only
# when ``__name__ != "build_tdpilot_api_tox"`` — our import keeps __name__ at
# the module's own name, so no build fires here.
sys.path.insert(0, str(ROOT / "td_component"))
from build_tdpilot_api_tox import _API_TOX_SOURCE_FILES as SOURCE_FILES  # noqa: E402

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
