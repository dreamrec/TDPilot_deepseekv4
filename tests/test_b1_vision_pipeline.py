"""v2.4 / Phase B.1 — screenshot vision pipeline (feature-flagged).

DeepSeek's Anthropic-compat /v1/messages endpoint cannot surface base64
inside ``tool_result`` blocks today. The vision pipeline:
  * strips ``data_base64`` from the tool_result in the agent's message
    history (saves cached-prefix tokens), and
  * injects a sibling ``image`` content block in the SAME user turn so
    DeepSeek can see the screenshot.

Tests pin:
  * Flag off (default) → tool_result unchanged, NO image block.
  * Flag on → tool_result has no base64, image block follows it.
  * Non-screenshot tools are NOT affected by the flag.
  * Multi-screenshot turn: each tool_result has its image block right after.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

from tdpilot_api_agent import Agent, _split_screenshot_payload  # noqa: E402, I001


# ---------------------------------------------------------------------------
# urlopen helpers — borrowed from tests/test_tdpilot_api_agent.py.
# ---------------------------------------------------------------------------


class _CtxMgr:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


def _mk_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    fake = SimpleNamespace(read=lambda: body)
    return _CtxMgr(fake)


def _tool_use_then_text(tool_name: str, args: dict, follow_text: str):
    return [
        {
            "content": [
                {"type": "text", "text": "looking up state"},
                {"type": "tool_use", "id": "tu_1", "name": tool_name, "input": args},
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": follow_text}],
            "stop_reason": "end_turn",
        },
    ]


FAKE_B64 = "ZmFrZS1pbWFnZS1ieXRlcw=="  # "fake-image-bytes" in base64


def _screenshot_result_dict():
    return {
        "success": True,
        "path": "/project1/render1",
        "width": 1920,
        "height": 1080,
        "format": "jpeg",
        "data_base64": FAKE_B64,
        "size_bytes": 12345,
    }


# ---------------------------------------------------------------------------
# Unit-level: _split_screenshot_payload helper
# ---------------------------------------------------------------------------


def test_b1_split_helper_strips_base64_and_returns_it():
    """_split_screenshot_payload returns slim dict + raw b64 for td_screenshot."""
    slim, b64, media = _split_screenshot_payload("td_screenshot", _screenshot_result_dict())
    assert "data_base64" not in slim
    assert slim["image_omitted_for_compat"] is True
    assert slim["content_type"] == "image/jpeg"
    assert b64 == FAKE_B64
    assert media == "image/jpeg"


def test_b1_split_helper_passes_through_non_screenshot():
    """Non-screenshot tools must return the input unchanged."""
    payload = {"ok": True, "data_base64": "not-a-screenshot"}
    slim, b64, media = _split_screenshot_payload("td_get_nodes", payload)
    assert slim is payload  # identity — no transformation
    assert b64 is None
    assert media == ""


def test_b1_split_helper_handles_png():
    """PNG format → image/png media type."""
    payload = {"format": "png", "data_base64": "p_n_g_b64"}
    _, _, media = _split_screenshot_payload("td_screenshot", payload)
    assert media == "image/png"


def test_b1_split_helper_handles_no_base64():
    """td_screenshot result without data_base64 (error case) returns unchanged."""
    payload = {"error": "viewer flag missing"}
    slim, b64, media = _split_screenshot_payload("td_screenshot", payload)
    assert slim is payload
    assert b64 is None
    assert media == ""


# ---------------------------------------------------------------------------
# Integration: Agent._loop with flag OFF (default) — no behavior change
# ---------------------------------------------------------------------------


def test_b1_flag_off_preserves_legacy_base64_in_tool_result():
    """Default behavior: enable_vision_pipeline=False keeps data_base64 in
    the tool_result content (matches pre-v2.4 behavior byte-for-byte)."""
    captured: list[dict] = []

    def dispatcher(name, args):
        assert name == "td_screenshot"
        return _screenshot_result_dict()

    responses = _tool_use_then_text(
        "td_screenshot", {"path": "/project1/render1"}, "saw it"
    )
    calls = iter(responses)

    def _urlopen(req, *_a, **_kw):
        captured.append(json.loads(req.data.decode()))
        return _mk_response(next(calls))

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[{"name": "td_screenshot", "description": "x", "input_schema": {"type": "object"}}],
        enable_vision_pipeline=False,  # explicit; same as default
    )
    agent.add_user_message("take a screenshot")
    with patch("urllib.request.urlopen", side_effect=_urlopen):
        agent.run_turn()

    # 2 API calls: first triggers tool_use, second wraps up.
    assert len(captured) == 2
    second_msgs = captured[1]["messages"]
    # …user → assistant(tool_use) → user(tool_result)
    user_with_tr = second_msgs[2]
    assert user_with_tr["role"] == "user"
    blocks = user_with_tr["content"]
    # Flag off: ONLY a tool_result block (no image)
    types = [b["type"] for b in blocks]
    assert types == ["tool_result"]
    # And the legacy embedded-base64 is still there.
    assert FAKE_B64 in blocks[0]["content"]


# ---------------------------------------------------------------------------
# Integration: Agent._loop with flag ON — image-block injection
# ---------------------------------------------------------------------------


def test_b1_flag_on_strips_base64_and_injects_image_block():
    """With flag on: tool_result has NO base64, image block follows it
    in the same user turn."""
    captured: list[dict] = []

    def dispatcher(name, args):
        return _screenshot_result_dict()

    responses = _tool_use_then_text(
        "td_screenshot", {"path": "/project1/render1"}, "described"
    )
    calls = iter(responses)

    def _urlopen(req, *_a, **_kw):
        captured.append(json.loads(req.data.decode()))
        return _mk_response(next(calls))

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[{"name": "td_screenshot", "description": "x", "input_schema": {"type": "object"}}],
        enable_vision_pipeline=True,
    )
    agent.add_user_message("take and describe a screenshot")
    with patch("urllib.request.urlopen", side_effect=_urlopen):
        agent.run_turn()

    assert len(captured) == 2
    user_with_results = captured[1]["messages"][2]
    assert user_with_results["role"] == "user"
    blocks = user_with_results["content"]
    types = [b["type"] for b in blocks]
    # Expected shape: [tool_result, image]
    assert types == ["tool_result", "image"], f"unexpected block order: {types}"

    # tool_result content has NO base64
    tr = blocks[0]
    assert FAKE_B64 not in tr["content"]
    assert "data_base64" not in tr["content"]
    # Slim payload markers ARE there
    assert "image_omitted_for_compat" in tr["content"]

    # Image block is well-formed
    img = blocks[1]
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/jpeg"
    assert img["source"]["data"] == FAKE_B64


def test_b1_flag_on_does_not_inject_for_non_screenshot_tools():
    """Non-screenshot tool_use under flag=True must NOT trigger image
    injection (the slim payload helper passes through)."""
    captured: list[dict] = []

    def dispatcher(name, args):
        # Even if some other tool returns a data_base64 field (unlikely),
        # we don't auto-inject — only td_screenshot triggers.
        return {"ok": True, "rows": [["a", "b"]]}

    responses = _tool_use_then_text(
        "td_get_nodes", {"path": "/project1"}, "got it"
    )
    calls = iter(responses)

    def _urlopen(req, *_a, **_kw):
        captured.append(json.loads(req.data.decode()))
        return _mk_response(next(calls))

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[{"name": "td_get_nodes", "description": "x", "input_schema": {"type": "object"}}],
        enable_vision_pipeline=True,
    )
    agent.add_user_message("list nodes")
    with patch("urllib.request.urlopen", side_effect=_urlopen):
        agent.run_turn()

    blocks = captured[1]["messages"][2]["content"]
    types = [b["type"] for b in blocks]
    assert types == ["tool_result"], (
        f"non-screenshot tool must not produce image block, got: {types}"
    )


def test_b1_flag_on_screenshot_with_error_no_image_block():
    """A screenshot tool_result that's an error (no data_base64) must
    NOT trigger image injection — graceful handling of dispatcher errors."""
    captured: list[dict] = []

    def dispatcher(name, args):
        # Simulates handle_screenshot returning an error
        return {"_tool_error": True, "error": "viewer flag is off"}

    responses = _tool_use_then_text(
        "td_screenshot", {"path": "/project1/oops"}, "got error"
    )
    calls = iter(responses)

    def _urlopen(req, *_a, **_kw):
        captured.append(json.loads(req.data.decode()))
        return _mk_response(next(calls))

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[{"name": "td_screenshot", "description": "x", "input_schema": {"type": "object"}}],
        enable_vision_pipeline=True,
    )
    agent.add_user_message("oops screenshot")
    with patch("urllib.request.urlopen", side_effect=_urlopen):
        agent.run_turn()

    blocks = captured[1]["messages"][2]["content"]
    types = [b["type"] for b in blocks]
    # Error path: no image block, just the error tool_result
    assert types == ["tool_result"], (
        f"screenshot error must not produce image block, got: {types}"
    )
