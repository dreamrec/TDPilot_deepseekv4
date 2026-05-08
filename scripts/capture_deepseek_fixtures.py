"""Capture real DeepSeek responses to JSON fixtures for PR-20.

Usage::

    # One scenario, single user prompt:
    uv run python scripts/capture_deepseek_fixtures.py \\
        --scenario inspect_basic_fps \\
        --prompt "What's the current FPS of the project?"

    # Multi-turn — feed extra prompts after the first response:
    uv run python scripts/capture_deepseek_fixtures.py \\
        --scenario memory_save_and_recall \\
        --prompt "Save a memory called X with content 'hello'" \\
        --prompt "Now recall X and quote the content"

The recorder:
  1. Spawns a localhost HTTP server that forwards every POST to
     https://api.deepseek.com/anthropic/v1/messages.
  2. Constructs the standalone ``Agent`` (from
     ``td_component/tdpilot_api_agent.py``) pointing at the proxy.
  3. Uses a stub dispatcher that returns realistic-shaped TD tool
     results — matches the shape real TD's MCP handlers return so
     the model's behavior tracks production.
  4. Every (request, response) pair is appended to
     ``tests/fixtures/deepseek/<scenario>.json`` (pretty-printed,
     sorted keys, ASCII-safe).

Note: this script costs DeepSeek API credits. Each fixture is a
small handful of API calls (typically 2-5 turns × $0.01-0.03).
The captured fixtures live in the repo so subsequent CI runs are
free.

API key resolution mirrors ``tdpilot_api_config.fetch_api_key``:
  TDPILOT_API_KEY env > ~/.tdpilot-api/config.json > .env file.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

# Capture and replay use the SAME stub dispatcher so a fixture
# captured against version N replays cleanly against version N+1
# unless the agent's tool-use logic itself changes.
from _mock_dispatcher import default_tools_for_capture, stub_dispatcher  # type: ignore[import-not-found]
from tdpilot_api_agent import Agent, AgentError  # type: ignore[import-not-found]
from tdpilot_api_config import fetch_api_key  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Recording proxy server
# ---------------------------------------------------------------------------


class RecordingProxy:
    """HTTP proxy that forwards POSTs to real DeepSeek and records both
    sides for fixture writing. Single-threaded — captures are
    sequential and we don't want concurrent overlap.
    """

    def __init__(self, real_base_url: str, api_key: str) -> None:
        self.real_base_url = real_base_url.rstrip("/")
        self.api_key = api_key
        self.exchanges: list[dict] = []
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("not started")
        host, port = self._httpd.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}/anthropic"

    def start(self) -> None:
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                owner._handle_post(self)

        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="RecordingProxy",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> RecordingProxy:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def _handle_post(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length", "0") or "0")
        body = h.rfile.read(length) if length else b""
        try:
            body_dict = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            body_dict = {}

        if not h.path.endswith("/v1/messages"):
            h.send_response(404)
            h.end_headers()
            return

        # Forward to real DeepSeek using the agent's actual headers.
        # We set x-api-key from our stored key; the agent's outbound
        # header (sk-mock) is intentionally replaced — the real
        # endpoint needs the real key.
        target_url = f"{self.real_base_url}/v1/messages"
        req = urllib.request.Request(
            url=target_url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            data=body,
        )
        try:
            with urllib.request.urlopen(req, timeout=120.0) as resp:
                resp_body = resp.read()
                resp_status = resp.status
        except urllib.error.HTTPError as exc:
            resp_body = exc.read()
            resp_status = exc.code
        except Exception as exc:
            err_body = json.dumps({"error": {"message": f"proxy upstream: {exc}"}}).encode()
            h.send_response(502)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(err_body)))
            h.end_headers()
            h.wfile.write(err_body)
            return

        try:
            resp_dict = json.loads(resp_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp_dict = {}

        # Record (request, response) for the fixture.
        self.exchanges.append({"request": body_dict, "response": resp_dict})

        # Pass through to the agent.
        h.send_response(resp_status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(resp_body)))
        h.end_headers()
        h.wfile.write(resp_body)


# ---------------------------------------------------------------------------
# Capture orchestrator
# ---------------------------------------------------------------------------


def capture(
    scenario: str,
    prompts: list[str],
    *,
    real_base_url: str = "https://api.deepseek.com/anthropic",
    model: str = "deepseek-v4-pro",
    model_tier: str = "auto",
    out_dir: Path | None = None,
    system_prompt: str = "",
    tools: list[dict] | None = None,
) -> Path:
    """Run the agent against the recording proxy and write the fixture
    file. Returns the on-disk path of the written fixture.
    """
    api_key = fetch_api_key()
    if not api_key:
        raise SystemExit(
            "No DeepSeek API key found. Set TDPILOT_API_KEY or "
            "configure ~/.tdpilot-api/config.json before running."
        )

    out_dir = out_dir or REPO_ROOT / "tests" / "fixtures" / "deepseek"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scenario}.json"

    text_chunks: list[str] = []
    tool_calls: list[tuple[str, dict]] = []
    tool_results: list[tuple[str, Any, bool]] = []

    proxy = RecordingProxy(real_base_url=real_base_url, api_key=api_key)
    proxy.start()
    try:
        agent = Agent(
            api_key="sk-recorder-bypass",  # not actually used; proxy injects real key
            dispatcher=stub_dispatcher,
            tools=tools or [],
            system_prompt=system_prompt,
            base_url=proxy.base_url,
            model=model,
            model_tier=model_tier,
            on_text=text_chunks.append,
            on_tool_call=lambda n, a: tool_calls.append((n, a)),
            on_tool_result=lambda n, r, e: tool_results.append((n, r, e)),
        )
        for prompt in prompts:
            agent.add_user_message(prompt)
            try:
                agent.run_turn()
            except AgentError as exc:
                print(f"[capture] AgentError on prompt {prompt!r}: {exc}", file=sys.stderr)
                break
    finally:
        proxy.stop()

    fixture = {
        "scenario": scenario,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "model_tier": model_tier,
        "prompts": list(prompts),
        "tool_calls_observed": [{"name": n, "args": a} for n, a in tool_calls],
        "final_text_concatenated": "\n".join(text_chunks),
        "exchanges": proxy.exchanges,
    }
    out_path.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, sort_keys=False),
        encoding="utf-8",
    )
    print(
        f"[capture] wrote {out_path} — {len(proxy.exchanges)} exchange(s), "
        f"{len(tool_calls)} tool call(s), {len(text_chunks)} text chunk(s)"
    )
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", required=True, help="fixture name (file stem)")
    p.add_argument(
        "--prompt",
        action="append",
        required=True,
        help="user prompt (repeat for multi-turn)",
    )
    p.add_argument(
        "--model-tier",
        default="auto",
        choices=("auto", "flash", "pro"),
        help="model routing tier",
    )
    p.add_argument(
        "--model",
        default="deepseek-v4-pro",
        help="model name when tier=pro",
    )
    p.add_argument(
        "--system-prompt",
        default="You are TDPilot, an assistant inside TouchDesigner. Use tools when needed.",
        help="system prompt for the capture session",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="override the fixture output directory (default: tests/fixtures/deepseek)",
    )
    args = p.parse_args()

    capture(
        scenario=args.scenario,
        prompts=list(args.prompt),
        model=args.model,
        model_tier=args.model_tier,
        system_prompt=args.system_prompt,
        out_dir=args.out_dir,
        tools=default_tools_for_capture(),
    )


if __name__ == "__main__":
    main()
