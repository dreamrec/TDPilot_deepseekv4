#!/usr/bin/env python3
"""OCR worker subprocess — v2.5.2.

Standalone script. Reads JSON-line requests on stdin, writes JSON-line
responses on stdout. Stderr is for diagnostics only (the manager pipes
it but doesn't parse it).

The PaddleOCR import is lazy — only happens on the first request — so a
worker that never receives a request never pays the ~5 s model load
cost. After the first call, subsequent calls reuse the same in-memory
model.

Request shape::

    {"image_path": "/path/to/image.png", "lang": "en"}

Response shape (success)::

    {
      "ok": true,
      "text": "joined OCR text",
      "boxes": [[[x0,y0],[x1,y1],[x2,y2],[x3,y3]], ...],
      "confidence": [0.99, 0.95, ...]
    }

Response shape (error)::

    {"ok": false, "error": "stringified exception"}

Robustness
----------
* Any unhandled exception in OCR is caught and reported as a structured
  error, not a process crash — keeps the worker alive across bad
  inputs.
* SIGTERM exits cleanly via the natural EOF on stdin (parent closes).
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

# Lazily imported on first use.
_PADDLEOCR_INSTANCES: dict[str, Any] = {}


def _get_paddleocr(lang: str):
    """Cached PaddleOCR instance per language. Loaded on first use."""
    if lang in _PADDLEOCR_INSTANCES:
        return _PADDLEOCR_INSTANCES[lang]

    # Import here so the module isn't loaded until we actually need it.
    # PaddleOCR's import-time side effects (model download, GPU probing)
    # are expensive — we defer them past worker startup so the manager's
    # spawn-time stays minimal.
    from paddleocr import PaddleOCR  # type: ignore[import-not-found]

    instance = PaddleOCR(use_angle_cls=True, lang=lang, show_log=False)
    _PADDLEOCR_INSTANCES[lang] = instance
    return instance


def _ocr_image(image_path: str, lang: str) -> dict:
    """Run OCR on a single image. Returns a dict ready to JSON-encode."""
    ocr = _get_paddleocr(lang)
    raw = ocr.ocr(image_path, cls=True)

    # PaddleOCR's output shape: [[[box, (text, conf)], ...]] (a list per
    # image; we pass one image so we take [0]). Defensive against
    # empty/null pages.
    if not raw or not raw[0]:
        return {"ok": True, "text": "", "boxes": [], "confidence": []}

    boxes: list[list[list[float]]] = []
    texts: list[str] = []
    confidences: list[float] = []
    for entry in raw[0]:
        box, (text, conf) = entry
        boxes.append([[float(p[0]), float(p[1])] for p in box])
        texts.append(text)
        confidences.append(float(conf))

    return {
        "ok": True,
        "text": "\n".join(texts),
        "boxes": boxes,
        "confidence": confidences,
    }


def _handle_one_request(line: str) -> str:
    """Parse a single request line + return a response line."""
    try:
        request = json.loads(line)
        image_path = request["image_path"]
        lang = request.get("lang", "en")
        response = _ocr_image(image_path, lang)
    except KeyError as exc:
        response = {"ok": False, "error": f"Missing field: {exc}"}
    except (ImportError, ModuleNotFoundError) as exc:
        # Surface the import error verbatim so the manager can detect
        # it and raise OcrUnavailable for the user.
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc().splitlines()[-3:]
        response = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}\n" + "\n".join(tb),
        }
    return json.dumps(response)


def main() -> int:
    """Worker main loop. One request per stdin line; one response per
    stdout line. Exits cleanly on EOF (parent closed stdin)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        response_line = _handle_one_request(line)
        sys.stdout.write(response_line + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
