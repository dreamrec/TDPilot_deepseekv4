"""TDPilot API — conversation compaction (Phase 4.3).

When a session's message history exceeds the configured threshold,
the oldest portion gets summarised into one synthetic assistant
message and the most-recent ``keep_recent`` messages are kept
verbatim. Prevents unbounded context growth on long sessions.

Critical thinking-block contract
================================

DeepSeek's Anthropic-compatible endpoint REQUIRES that any
``type: thinking`` content blocks emitted by a previous turn be
echoed back in the next turn's message history. The error on
mismatch is::

    HTTP 400: The content[].thinking in the thinking mode must
    be passed back to the API.

The synthetic message we produce here is **text-only — no
``thinking`` block**. Why this is safe:

  * Compaction REPLACES the older turns entirely (the original
    user / tool_use / tool_result / assistant messages, including
    any thinking blocks, are sliced out of the history).
  * The retained recent messages still carry their ORIGINAL,
    API-issued thinking blocks (with valid ``signature`` fields).
    Those echo back unchanged.
  * The new synthetic message has no thinking block, so there's
    nothing for the API to validate.

The implementation plan's strawman called for "a synthesized
thinking block to satisfy the contract" — that's what we'd LIKE
but can't do safely. ``signature`` fields are signed by the API
itself; a fabricated signature would 400. Text-only is the
defensive path the plan's risk note explicitly anticipated.

Forensic preservation
=====================

Before each compaction, the to-be-removed messages are appended
to ``~/.tdpilot-api/history/<session_id>.jsonl`` (one JSON
record per compaction event, the record containing the entire
sliced batch). Forensic users can reload via ``read_history``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_DIR = Path.home() / ".tdpilot-api" / "history"
DEFAULT_THRESHOLD = 20
DEFAULT_KEEP_RECENT = 10

# Marker the synthesized message carries in its text body so future
# tooling can recognise compaction summaries (e.g. for re-expansion
# from the on-disk forensic file).
COMPACTION_MARKER = "[[TDPILOT_COMPACTED_HISTORY]]"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def needs_compaction(messages: list[dict], threshold: int = DEFAULT_THRESHOLD) -> bool:
    """``True`` when the message count is at or above the threshold AND
    the threshold is positive (zero/negative disables compaction).
    """
    if threshold <= 0:
        return False
    return len(messages) >= threshold


def compact(
    messages: list[dict],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> list[dict]:
    """Return a new message list = ``[synthetic_summary, *most_recent_N]``.

    Pure function — does NOT mutate the input. Does NOT touch the
    filesystem (forensic persistence is the caller's job; see
    ``persist_history_chunk`` below). Always preserves the most-recent
    ``keep_recent`` messages verbatim, including any original
    ``thinking`` content blocks they carry.

    Edge cases:
      - ``keep_recent`` >= ``len(messages)``: return a copy of
        ``messages`` unchanged (nothing to compact away).
      - ``len(messages) <= 1``: return a copy unchanged (degenerate).
      - The slice point lands mid tool-chain — i.e. the retained
        slice would START with a user ``tool_result`` block whose
        matching assistant ``tool_use`` was archived. The Anthropic
        API rejects that with ``messages.0: tool_result block
        without matching tool_use``. We advance the cut FORWARD
        past every leading tool_result so the retained slice starts
        on a clean boundary (typically the next user text message
        or the next assistant turn). This eats into ``keep_recent``
        — by design; an unsendable history is worse than a slightly
        smaller one. Phase 1.6.13 fix.
    """
    if keep_recent < 0:
        keep_recent = 0
    if len(messages) <= max(1, keep_recent):
        return list(messages)
    cut = len(messages) - keep_recent

    # Advance ``cut`` forward while the FIRST retained message is a
    # user message that contains ANY tool_result block. Without
    # repair, that tool_result references a tool_use_id from an
    # archived assistant turn and the API 400s on the next call.
    while cut < len(messages) and _starts_with_tool_result(messages[cut]):
        cut += 1
    if cut >= len(messages):
        # Pathological — every "recent" message was a tool_result
        # tail. Fall back to keeping nothing recent; the synthetic
        # summary stands alone.
        cut = len(messages)

    older = messages[:cut]
    recent = messages[cut:]
    summary_text = _summarise_old_turns(older)
    synthetic = {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": COMPACTION_MARKER + "\n\n" + summary_text,
            }
        ],
    }
    return [synthetic, *recent]


def _starts_with_tool_result(message: dict) -> bool:
    """True if this message is a user message whose first content
    block is a ``tool_result``. That's the marker for "this message
    is the tail of a previous turn's tool chain" — its
    ``tool_use_id`` references an assistant block that, after
    compaction, would no longer exist in the history.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    if not isinstance(first, dict):
        return False
    return first.get("type") == "tool_result"


def persist_history_chunk(
    messages: list[dict],
    session_id: str,
    *,
    history_dir: Path | None = None,
) -> Path:
    """Append one JSONL record to ``<history_dir>/<session_id>.jsonl``
    capturing the to-be-compacted messages. Returns the file path.

    The record is one JSON line: ``{ts, session_id, message_count,
    messages}``. Each call appends; the file grows monotonically so a
    long session with multiple compactions has a readable timeline.

    Best-effort: filesystem errors print a one-line warning and
    return the path that WOULD have been used. The caller never
    blocks on persistence failure — context window is the priority.
    """
    target_dir = Path(history_dir) if history_dir else DEFAULT_HISTORY_DIR
    target_path = target_dir / f"{session_id}.jsonl"
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": session_id,
        "message_count": len(messages),
        "messages": messages,
    }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"[tdpilot_API/compaction] history persist failed: {exc}")
    return target_path


def read_history(
    session_id: str,
    *,
    history_dir: Path | None = None,
) -> list[dict]:
    """Re-read every persisted compaction chunk for ``session_id``.

    Returns a list of records (each with ``ts`` /
    ``message_count`` / ``messages``). Empty list if no file or any
    parse error — never raises. Used by future ``td_history_recall``
    tools / debug UIs to reconstruct full session detail after
    compaction.
    """
    target_dir = Path(history_dir) if history_dir else DEFAULT_HISTORY_DIR
    target_path = target_dir / f"{session_id}.jsonl"
    if not target_path.is_file():
        return []
    out: list[dict] = []
    try:
        with target_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (TypeError, ValueError):
                    continue
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# Internal — local-heuristic summarisation
# ---------------------------------------------------------------------------


def _summarise_old_turns(messages: list[dict]) -> str:
    """Build a text summary of the sliced-away messages.

    Pure local heuristic — no LLM call. Captures:
      - User-message count + a sample of the first / last user goals.
      - Tool calls invoked, deduplicated and sorted (most-frequent
        first), with per-tool counts. Token-frugal: just names.
      - Notable error events.

    The model reading this gets enough signal to maintain rough
    continuity without paying full-fidelity context cost. If higher
    fidelity is needed mid-session, the agent can call a future
    ``td_history_recall(query)`` tool that hits the on-disk archive.
    """
    user_texts: list[str] = []
    tool_counts: dict[str, int] = {}
    error_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        if role == "user":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text and not text.startswith("[[TDPILOT_"):
                        user_texts.append(text)
                elif block.get("type") == "tool_result":
                    if block.get("is_error"):
                        error_count += 1
        elif role == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name:
                        tool_counts[name] = tool_counts.get(name, 0) + 1

    parts: list[str] = []
    parts.append(f"This summary replaces {len(messages)} compacted messages from earlier in the session.")
    parts.append(f"User prompts: {len(user_texts)}.")
    if user_texts:
        first = user_texts[0]
        if len(first) > 200:
            first = first[:197] + "..."
        parts.append(f"First user goal: {first!r}")
        if len(user_texts) > 1:
            last = user_texts[-1]
            if len(last) > 200:
                last = last[:197] + "..."
            parts.append(f"Latest user goal before this point: {last!r}")
    if tool_counts:
        ranked = sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        rendered = ", ".join(f"{n}×{c}" if c > 1 else n for n, c in ranked)
        parts.append(f"Tools used (deduped): {rendered}.")
    else:
        parts.append("No tool calls in this slice (text-only conversation).")
    if error_count:
        parts.append(f"Tool errors encountered: {error_count}.")
    parts.append(
        "Recent turns are kept verbatim below. If you need detail from "
        "the compacted slice, call td_history_recall (when available) "
        "or ask the user to re-state the relevant context."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Compactor — pairs compact() with persist_history_chunk + tracking
# ---------------------------------------------------------------------------


class Compactor:
    """Per-session compactor. Owns threshold + keep_recent + the
    on-disk archive path, and exposes a ``maybe_compact`` method the
    Agent calls at the top of each ``_loop`` iteration.

    Re-entrant safe — repeated calls with the same messages are
    a no-op once below the threshold.

    Stats:
      - ``compactions_run``: count of times this Compactor reduced
        the history. Surfaced in observability traces (Phase 4.1).
    """

    def __init__(
        self,
        *,
        session_id: str,
        threshold: int = DEFAULT_THRESHOLD,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        history_dir: Path | None = None,
        persist: bool = True,
    ) -> None:
        self.session_id = session_id
        self.threshold = max(0, int(threshold))
        self.keep_recent = max(0, int(keep_recent))
        self.history_dir = Path(history_dir) if history_dir else DEFAULT_HISTORY_DIR
        self.persist = bool(persist)
        self.compactions_run = 0
        self.last_compaction_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.threshold > 0 and self.keep_recent < self.threshold

    def maybe_compact(self, messages: list[dict]) -> list[dict]:
        """Return a possibly-compacted copy of ``messages``. If no
        compaction was needed, returns the input unchanged
        (same-object identity preserved for caller-friendliness).
        """
        if not self.enabled:
            return messages
        if not needs_compaction(messages, self.threshold):
            return messages
        cut = len(messages) - self.keep_recent
        if cut <= 0:
            return messages
        older = messages[:cut]
        if self.persist and older:
            persist_history_chunk(older, self.session_id, history_dir=self.history_dir)
        compacted = compact(messages, keep_recent=self.keep_recent)
        self.compactions_run += 1
        self.last_compaction_ts = time.monotonic()
        return compacted
