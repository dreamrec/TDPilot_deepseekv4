"""OCR tool — v2.5.2.

Exposes ``td_ocr_image`` so the agent can extract text from a screenshot
or any image on disk. Pairs naturally with the v2.4 Phase B vision
pipeline: capture → OCR → LLM gets BOTH the image AND a transcript so
small-font or cluttered-numerical content doesn't depend on vision-side
inference.

Side-effect-imported from ``tool_registry.py`` like every other ``tools_*``
submodule.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field

# Intentional cycle — see registry/__init__.py.
from td_mcp import tool_registry as _tr  # noqa: E402
from td_mcp.tool_registry import mcp  # noqa: E402
from td_mcp.vision.ocr import (
    OcrTimeout,
    OcrUnavailable,
    PathNotAllowed,
    get_global_manager,
)


@mcp.tool(name="td_ocr_image")
async def td_ocr_image(
    ctx: Context,
    path: Annotated[
        str,
        Field(description="Filesystem path to the image (PNG/JPEG/etc.) to OCR."),
    ],
    lang: Annotated[
        str,
        Field(
            description=(
                "PaddleOCR language code. Default 'en'. Common: 'en', 'ch' "
                "(Chinese simplified), 'japan', 'korean', 'german', 'french', "
                "'spanish'."
            )
        ),
    ] = "en",
) -> str:
    """Run OCR on an image file. Returns text + bounding boxes + confidence.

    Pairs with ``td_screenshot``: capture → save → OCR. The text you get
    back is what's visibly written on screen — error dialogs, parameter
    values in spinners, status bars, viewer captions, etc. Reading
    those from the image directly (via LLM vision) is unreliable on
    small fonts; OCR is the dedicated path.

    Args:
        path: filesystem path to the image (PNG/JPEG/etc.)
        lang: PaddleOCR language code, default 'en'. Common: 'en',
            'ch' (Chinese simplified), 'japan', 'korean', 'german',
            'french', 'spanish'.

    Returns: JSON dict with ``text`` (joined recognized strings),
    ``boxes`` (per-string quadrilateral coordinates), ``confidence``
    (per-string confidence 0.0-1.0), and ``elapsed_ms`` (latency).

    Optional dep: requires the ``[ocr]`` extras (paddleocr +
    paddlepaddle). Without them, returns a clear advisory.
    """
    finish = _tr._start_tool(ctx, "td_ocr_image")
    try:
        manager = get_global_manager()
        result = manager.ocr_image(path, lang=lang)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "path": path,
            "lang": lang,
            **result.to_dict(),
        }
        return _tr._as_json_output(payload)
    except OcrUnavailable as exc:
        _tr._record_tool_error(ctx, "td_ocr_image")
        return _tr._as_json_output(
            {
                "error": "ocr_extras_not_installed",
                "advice": (
                    "OCR requires the [ocr] optional extras. Install via "
                    "`pip install -e .[ocr]` in the tdpilot-dpsk4 install "
                    "directory (or `pip install paddleocr paddlepaddle` "
                    "into the same venv as the MCP server)."
                ),
                "details": str(exc),
            }
        )
    except FileNotFoundError as exc:
        _tr._record_tool_error(ctx, "td_ocr_image")
        return _tr._as_json_output({"error": "file_not_found", "path": str(exc), "advice": "Check the path."})
    except PathNotAllowed as exc:
        # H-3 sandbox refusal (audit 2026-05-19): path is outside the
        # OCR sandbox (extension or root allowlist).
        _tr._record_tool_error(ctx, "td_ocr_image")
        return _tr._as_json_output(
            {
                "error": "ocr_path_not_allowed",
                "details": str(exc),
                "advice": (
                    "td_ocr_image only reads images under the OCR sandbox "
                    "(system tmp + ~/Downloads + ~/Desktop + ~/Pictures + "
                    "~/.tdpilot-dpsk4 by default). To extend the sandbox, "
                    "set TDPILOT_OCR_ALLOWED_ROOTS to a colon-separated "
                    "list of directories in the MCP server's environment."
                ),
            }
        )
    except OcrTimeout as exc:
        _tr._record_tool_error(ctx, "td_ocr_image")
        return _tr._as_json_output(
            {
                "error": "ocr_timeout",
                "details": str(exc),
                "advice": (
                    "OCR took longer than the configured request timeout. "
                    "Set TDPILOT_OCR_REQUEST_TIMEOUT to a larger value if "
                    "your images are unusually dense, or check the worker "
                    "for a stuck state."
                ),
            }
        )
    except Exception as exc:
        _tr._record_tool_error(ctx, "td_ocr_image")
        from td_mcp.errors import format_tool_error

        return format_tool_error(exc)
    finally:
        finish()
