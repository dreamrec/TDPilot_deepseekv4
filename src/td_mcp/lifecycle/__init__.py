"""Lifecycle utilities for TDPilot DPSK4 — v2.5.7+.

Shipped:

* :mod:`td_mcp.lifecycle.update_check` (v2.5.7) — GitHub Releases API
  client + ``.tox`` source-hash drift detection. Read-only; full
  auto-apply (``td_self_update``) lives in the v2.7 plan.
"""

from __future__ import annotations

from td_mcp.lifecycle.update_check import (
    UpdateCheckResult,
    check_for_updates,
    clear_cache,
)

__all__ = [
    "UpdateCheckResult",
    "check_for_updates",
    "clear_cache",
]
