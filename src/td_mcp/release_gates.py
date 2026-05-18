"""Release-gate constants shared across tests, scripts, and runtime.

Any threshold that needs to be in sync between a test assertion and a release
script lives here. Bump once per release.
"""

from __future__ import annotations

# Minimum tool count enforced by contract tests, the registry smoke check,
# the full e2e suite, and the runtime stress matrix. Kept as a floor (not an
# exact match) so adding tools never breaks downstream checks.
# 2026-04-25: bumped 97 → 101 with td_knowledge_{save,recall,get,list}.
# 2026-05-02: dropped 101 → 99 (community brain tools removed from public repo).
# 2026-05-02: bumped 99 → 101 with td_get_focus + td_locations (v1.6.0 Phase 1).
# 2026-05-02: bumped 101 → 102 with td_get_hints (v1.6.0 Phase 2).
# 2026-05-02: bumped 102 → 103 with td_component_notes (v1.6.0 Phase 3).
# 2026-05-12: bumped 103 → 104 with td_midi_devices (v2.4 / Phase C.2).
# 2026-05-12: bumped 104 → 105 with td_get_capabilities_summary (v2.4 / Phase C.6).
# 2026-05-18: bumped 105 → 106 with td_get_activity_log (v2.5.1).
# 2026-05-19: bumped 106 → 108 with td_ocr_image (v2.5.2) + td_check_for_updates (v2.5.7).
# 2026-05-19: bumped 108 → 109 with td_get_traces (v2.5.8 — chat-pipe trace viewer).
EXPECTED_MIN_TOOL_COUNT: int = 109
