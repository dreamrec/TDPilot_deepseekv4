"""TDPilot API — per-turn observability traces (Phase 4.1).

Writes one JSONL line per completed turn to
``~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl``. Each record captures
the structural fingerprint of a turn: timing, tool calls + their
latencies + their success/error state, model tier used, outcome.
Raw user text and tool arguments are SHA-256-hashed (truncated to
12 hex chars) so the file never holds prompt contents — useful for
debugging behaviour regressions across runs without leaking
session detail.

Threading model:
  - The cook thread calls ``start_turn`` / ``record_tool`` /
    ``end_turn`` directly. These are O(1) — they just push events
    onto an in-memory queue.
  - A daemon writer thread drains the queue and appends to the
    day's JSONL file. The writer never blocks the cook thread.
  - Day-rollover is handled by re-opening the file when the
    current date no longer matches the open one.

Disk hygiene:
  - On Tracer init, prune files older than ``RETENTION_DAYS``
    (default 30). Keeps the traces dir bounded.

Disabling:
  - Construct the Tracer with ``enabled=False``, OR set
    ``config["trace_logging"] = False`` in the runtime config.
    Disabled tracers no-op all calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TRACES_DIR = Path.home() / ".tdpilot-api" / "traces"
RETENTION_DAYS = 30
HASH_PREFIX_LEN = 12


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _today_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"


def _hash_text(value: Any) -> str:
    """Truncated SHA-256 of any JSON-serialisable input. Stable across
    runs for the same input. Empty string for None / empty input.
    """
    if value is None:
        return ""
    try:
        data = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        data = str(value)
    if not data:
        return ""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:HASH_PREFIX_LEN]


class Tracer:
    """Per-runtime trace emitter.

    Lifecycle:
      tracer = Tracer(traces_dir=...)        # at AgentRuntime init
      tracer.start_turn(user_text, model_tier=...)  # one per start_turn
      tracer.record_tool(name, args, latency_ms, ok, error=...)  # per tool
      tracer.end_turn(outcome, total_tokens=...)    # one per end / error

    Each ``end_turn`` call enqueues the assembled record for the
    writer thread to flush.
    """

    def __init__(
        self,
        traces_dir: Path | None = None,
        *,
        enabled: bool = True,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        self.enabled = bool(enabled)
        self._traces_dir = Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR
        self._retention_days = retention_days

        # Per-session id, stable for the lifetime of the Tracer.
        self.session_id = uuid.uuid4().hex[:HASH_PREFIX_LEN]

        # Active-turn state. Touched only from the cook thread.
        self._current: dict[str, Any] | None = None

        # Writer-thread plumbing.
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None

        if self.enabled:
            self._ensure_dir()
            self._prune_old()
            self._writer = threading.Thread(
                target=self._writer_loop,
                name="tdpilot_api_tracer",
                daemon=True,
            )
            self._writer.start()

    # ------------------------------------------------------------------
    # Public API (called from the cook thread)
    # ------------------------------------------------------------------

    def start_turn(
        self,
        user_text: str,
        *,
        model_tier: str = "",
        model_used: str = "",
    ) -> str:
        """Open a new turn record. Returns the turn_id (so callers can
        correlate downstream events if they want).
        """
        if not self.enabled:
            return ""
        turn_id = uuid.uuid4().hex[:HASH_PREFIX_LEN]
        self._current = {
            "ts": _now_iso(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "user_text_hash": _hash_text(user_text),
            "model_tier": model_tier or "",
            "model_used": model_used or "",
            "tool_calls": [],
            "_started": time.monotonic(),
        }
        return turn_id

    def record_tool(
        self,
        name: str,
        args: Any,
        *,
        latency_ms: int,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Append one tool-call record to the current turn. No-op if
        ``start_turn`` hasn't been called yet (defensive — this can
        fire during teardown races).
        """
        if not self.enabled or self._current is None:
            return
        self._current["tool_calls"].append(
            {
                "name": name or "",
                "args_hash": _hash_text(args),
                "latency_ms": int(latency_ms),
                "ok": bool(ok),
                "error": error or "",
            }
        )

    def update_model(self, model_tier: str = "", model_used: str = "") -> None:
        """Update the model fields on the current turn after the
        agent's tier-routing fires. Cheap to call repeatedly; only
        the most recent values stick.
        """
        if not self.enabled or self._current is None:
            return
        if model_tier:
            self._current["model_tier"] = model_tier
        if model_used:
            self._current["model_used"] = model_used

    def end_turn(
        self,
        outcome: str,
        *,
        total_tokens: int = 0,
    ) -> None:
        """Finalise the current turn. Computes ``duration_ms`` from the
        start_turn timestamp, enqueues the record, clears active state.
        Re-entrant safe: end_turn followed by another end_turn (e.g.
        on_error fires after on_turn_done) is a no-op.
        """
        if not self.enabled or self._current is None:
            return
        rec = self._current
        self._current = None
        started = rec.pop("_started", time.monotonic())
        rec["duration_ms"] = int((time.monotonic() - started) * 1000)
        rec["outcome"] = outcome or "unknown"
        rec["total_tokens"] = int(total_tokens)
        try:
            self._queue.put_nowait(rec)
        except queue.Full:  # pragma: no cover — unbounded queue, defensive
            pass

    def shutdown(self, timeout: float = 1.5) -> None:
        """Stop the writer thread cleanly. Tests use this to flush
        deterministically before reading the file.
        """
        if not self.enabled:
            return
        self._stop.set()
        self._queue.put_nowait(None)  # poison pill
        if self._writer is not None:
            self._writer.join(timeout=timeout)

    # Compatibility aliases — convenient for tests that want to
    # synchronously flush without tearing down the Tracer.
    def flush(self, timeout: float = 1.0) -> None:
        if not self.enabled:
            return
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    # ------------------------------------------------------------------
    # Writer loop — daemon thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        current_filename: str | None = None
        fh = None
        try:
            while True:
                if self._stop.is_set() and self._queue.empty():
                    break
                try:
                    rec = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if rec is None:
                    break
                target = self._traces_dir / _today_filename()
                if str(target) != current_filename:
                    if fh is not None:
                        try:
                            fh.close()
                        except Exception:
                            pass
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fh = target.open("a", encoding="utf-8")
                    current_filename = str(target)
                try:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                except Exception as exc:  # noqa: BLE001
                    print(f"[tdpilot_API/tracing] writer error: {exc}")
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Disk hygiene
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        try:
            self._traces_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[tdpilot_API/tracing] could not create traces dir: {exc}")

    def _prune_old(self) -> None:
        """Delete trace files older than ``retention_days``. Best-effort —
        permission errors / missing-file races are swallowed.
        """
        if self._retention_days <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        try:
            for entry in self._traces_dir.glob("*.jsonl"):
                try:
                    stem = entry.stem  # YYYY-MM-DD
                    file_date = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        entry.unlink()
                    except OSError:
                        continue
        except OSError:
            return


# ---------------------------------------------------------------------------
# Read helpers — used by the ``td_get_recent_traces`` tool handler
# ---------------------------------------------------------------------------


def _read_last_n_lines(path: Path, limit: int) -> list[str]:
    """Return the last ``limit`` non-empty lines of ``path``.

    Reads the whole file (each daily file caps at a few thousand
    lines, well under a megabyte) and slices. Robust against
    partially-written final lines from an in-progress writer.
    """
    if not path.is_file() or limit <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
    except OSError:
        return []
    return lines[-limit:]


def _iter_recent_files(traces_dir: Path, days: int = 7) -> Iterable[Path]:
    """Yield the trace files for the last ``days`` days, newest first."""
    today = datetime.now(timezone.utc)
    for offset in range(days):
        d = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = traces_dir / f"{d}.jsonl"
        if path.is_file():
            yield path


def get_recent_traces(limit: int = 10, *, traces_dir: Path | None = None) -> list[dict]:
    """Return up to ``limit`` most recent trace records (parsed dicts).

    Walks today's file first, then yesterday's, etc., until ``limit``
    is reached or 7 days have been scanned.
    """
    limit = max(1, min(int(limit or 10), 200))
    base = Path(traces_dir) if traces_dir else DEFAULT_TRACES_DIR
    if not base.is_dir():
        return []
    out: list[dict] = []
    for path in _iter_recent_files(base, days=7):
        for line in reversed(_read_last_n_lines(path, limit)):
            try:
                rec = json.loads(line)
            except (TypeError, ValueError):
                continue
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------------------
# Tool handler — registered as ``td_get_recent_traces`` in the schema map
# ---------------------------------------------------------------------------


def handle_get_recent_traces(body: dict) -> dict:
    """Return the most recent JSONL trace records.

    Args (all optional):
      - ``limit`` (int, default 10, max 200): how many records to
        return, newest first.

    Returns ``{"ok": True, "count": N, "traces": [...]}``. The trace
    records are JSON dicts with the fields documented at the top of
    this module — no raw user text or args (those are hashed at
    write time for privacy).
    """
    limit = body.get("limit", 10) if isinstance(body, dict) else 10
    try:
        n = int(limit or 10)
    except (TypeError, ValueError):
        n = 10
    traces = get_recent_traces(limit=n)
    return {"ok": True, "count": len(traces), "traces": traces}
