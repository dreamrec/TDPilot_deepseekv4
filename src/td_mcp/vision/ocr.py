"""OCR sidecar — v2.5.2.

Subprocess-based OCR using PaddleOCR. The worker lives in a separate
Python process so the ~400 MB OCR model never bloats the MCP server's
RAM footprint when the user isn't using OCR.

Lifecycle
---------
* Worker is spawned LAZILY on the first OCR request (~5 s warm-up).
* The worker stays alive after warm-up so subsequent calls return fast.
* If the worker is idle for ``IDLE_KILL_SECONDS`` (default 300 s = 5 min),
  it gets killed; the next request re-spawns.
* Per-request hard timeout: ``REQUEST_TIMEOUT_SECONDS`` (default 30 s).
* If the worker crashes, the manager restarts up to ``MAX_RESTARTS``
  times (with exponential backoff) before giving up for the session.

Wire protocol (JSON lines over stdin/stdout)
--------------------------------------------
Manager → Worker (one JSON object per line on stdin)::

    {"image_path": "/tmp/screen.png", "lang": "en"}

Worker → Manager (one JSON object per line on stdout)::

    {"ok": true, "text": "...", "boxes": [...], "confidence": [...]}
    {"ok": false, "error": "..."}

Optional dependency
-------------------
Install ``paddleocr`` (and ``paddlepaddle``) via the ``[ocr]`` extras::

    pip install -e .[ocr]

If extras are missing, ``OcrUnavailable`` is raised on first use. The
``td_ocr_image`` tool catches this and returns a clear advisory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Tuning knobs — env-overridable for tests + power users.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("TDPILOT_OCR_REQUEST_TIMEOUT", "30"))
IDLE_KILL_SECONDS = float(os.environ.get("TDPILOT_OCR_IDLE_KILL", "300"))
MAX_RESTARTS = int(os.environ.get("TDPILOT_OCR_MAX_RESTARTS", "3"))

# Backoff between restart attempts, in seconds.
_RESTART_BACKOFF = (1.0, 2.0, 4.0)


class OcrUnavailable(RuntimeError):
    """Raised when the [ocr] extras aren't installed (paddleocr is missing).

    The MCP tool layer catches this and returns a user-facing advisory.
    """


class OcrTimeout(RuntimeError):
    """A single OCR request exceeded ``REQUEST_TIMEOUT_SECONDS``."""


@dataclass
class OcrResult:
    """Structured OCR output for a single image."""

    text: str
    boxes: list[list[list[float]]] = field(default_factory=list)
    confidence: list[float] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "boxes": self.boxes,
            "confidence": self.confidence,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class OcrManager:
    """Singleton-like manager that owns the worker subprocess lifecycle.

    Not thread-safe for concurrent OCR requests — wrap with the lock if
    you need parallel calls. For v2.5.2 the MCP server processes one
    OCR call at a time per tool invocation, which the asyncio event
    loop serialises anyway, so the lock here is sufficient.
    """

    def __init__(self, worker_path: Path | None = None):
        self._worker_path = worker_path or _default_worker_path()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_use_ts: float = 0.0
        self._restart_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ocr_image(self, image_path: str, lang: str = "en") -> OcrResult:
        """Run OCR on ``image_path``. Spawns the worker if needed.

        Raises :class:`OcrUnavailable` if extras aren't installed,
        :class:`OcrTimeout` on hard timeout, or ``RuntimeError`` on
        worker crash beyond ``MAX_RESTARTS``.
        """
        if not Path(image_path).exists():
            raise FileNotFoundError(image_path)

        with self._lock:
            self._ensure_worker()
            start = time.monotonic()
            payload = {"image_path": str(image_path), "lang": lang}
            response = self._send_request(payload)
            elapsed_ms = int((time.monotonic() - start) * 1000)

        if not response.get("ok"):
            err = response.get("error", "unknown OCR error")
            if "ImportError" in err or "ModuleNotFoundError" in err:
                raise OcrUnavailable(err)
            raise RuntimeError(f"OCR worker error: {err}")

        return OcrResult(
            text=response.get("text", ""),
            boxes=response.get("boxes", []),
            confidence=response.get("confidence", []),
            elapsed_ms=elapsed_ms,
        )

    def shutdown(self) -> None:
        """Best-effort shutdown of the worker subprocess."""
        with self._lock:
            self._kill_worker()

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Spawn the worker if not running. Handles idle-kill + restart."""
        # Idle-kill: if the worker hasn't been used in IDLE_KILL_SECONDS,
        # tear it down so the spawn below produces a fresh one. Frees
        # ~400 MB of resident memory.
        if (
            self._proc is not None
            and self._proc.poll() is None
            and self._last_use_ts > 0
            and (time.monotonic() - self._last_use_ts) > IDLE_KILL_SECONDS
        ):
            self._kill_worker()

        if self._proc is None or self._proc.poll() is not None:
            self._spawn_worker()

    def _spawn_worker(self) -> None:
        """Start a new worker subprocess. Validates extras availability."""
        # Quick pre-flight: try importing paddleocr in a fresh subprocess
        # so we can raise OcrUnavailable cleanly instead of a vague
        # "worker died" message.
        try:
            preflight = subprocess.run(
                [sys.executable, "-c", "import paddleocr"],
                check=False,
                capture_output=True,
                timeout=10,
            )
            if preflight.returncode != 0:
                stderr = preflight.stderr.decode("utf-8", errors="replace")
                raise OcrUnavailable(
                    "paddleocr is not installed in the MCP server's Python "
                    "environment. Install via `pip install -e .[ocr]` "
                    "(or `pip install paddleocr paddlepaddle`). "
                    f"Pre-flight stderr: {stderr[:200]}"
                )
        except subprocess.TimeoutExpired as exc:
            raise OcrUnavailable(
                "paddleocr pre-flight import timed out (10 s). "
                "Likely cause: corrupt install or missing native libs."
            ) from exc

        # Spawn the worker. Use sys.executable so we land in the same
        # venv where extras are installed.
        self._proc = subprocess.Popen(
            [sys.executable, str(self._worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._last_use_ts = time.monotonic()

    def _send_request(self, payload: dict) -> dict:
        """Send one JSON-line request and read one JSON-line response.

        Restarts the worker on crash (up to ``MAX_RESTARTS`` times).
        """
        attempts = 0
        while attempts <= MAX_RESTARTS:
            if self._proc is None or self._proc.poll() is not None:
                if attempts >= MAX_RESTARTS:
                    raise RuntimeError(f"OCR worker died and {MAX_RESTARTS} restarts exhausted.")
                time.sleep(_RESTART_BACKOFF[min(attempts, len(_RESTART_BACKOFF) - 1)])
                self._spawn_worker()
                attempts += 1

            try:
                assert self._proc is not None
                assert self._proc.stdin is not None
                assert self._proc.stdout is not None
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()

                # Read one line with a timeout. ``readline`` itself blocks
                # without a timeout, so we use a thread-based timeout.
                response_line = _readline_with_timeout(self._proc.stdout, REQUEST_TIMEOUT_SECONDS)
                if response_line is None:
                    raise OcrTimeout(
                        f"OCR request exceeded {REQUEST_TIMEOUT_SECONDS}s. "
                        "Worker may be stuck loading a slow model."
                    )

                self._last_use_ts = time.monotonic()
                return json.loads(response_line.strip())
            except OcrTimeout:
                # Don't restart on timeout — return error to caller.
                raise
            except (BrokenPipeError, OSError) as exc:
                # Worker died. Restart and retry.
                self._kill_worker()
                attempts += 1
                if attempts > MAX_RESTARTS:
                    raise RuntimeError(
                        f"OCR worker crashed and exceeded {MAX_RESTARTS} restarts: {exc}"
                    ) from exc

        # Defensive — should be unreachable.
        raise RuntimeError("OCR send_request fell through retry loop")

    def _kill_worker(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self._proc = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_worker_path() -> Path:
    """Locate the worker script that ships next to this module."""
    return Path(__file__).parent / "ocr_worker.py"


def _readline_with_timeout(stream, timeout: float) -> str | None:
    """Read one line from ``stream`` with a hard timeout.

    Returns the line (with trailing newline) or ``None`` on timeout.
    Uses a background thread because ``stream.readline()`` blocks.
    """
    result: dict[str, Any] = {}

    def _reader():
        try:
            result["line"] = stream.readline()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None
    if "error" in result:
        raise result["error"]
    return result.get("line")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_GLOBAL_MANAGER: OcrManager | None = None


def get_global_manager() -> OcrManager:
    """Lazily construct the module-level OCR manager.

    Tests can call ``reset_global_manager()`` to get a fresh instance.
    """
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is None:
        _GLOBAL_MANAGER = OcrManager()
    return _GLOBAL_MANAGER


def reset_global_manager() -> OcrManager:
    """Test helper — rebuilds the singleton, shutting down the existing
    worker if any."""
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is not None:
        try:
            _GLOBAL_MANAGER.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _GLOBAL_MANAGER = OcrManager()
    return _GLOBAL_MANAGER


__all__ = [
    "IDLE_KILL_SECONDS",
    "MAX_RESTARTS",
    "OcrManager",
    "OcrResult",
    "OcrTimeout",
    "OcrUnavailable",
    "REQUEST_TIMEOUT_SECONDS",
    "get_global_manager",
    "reset_global_manager",
]
