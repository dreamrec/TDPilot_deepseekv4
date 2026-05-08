"""PR-22 — fill genuine coverage gaps for previously uncovered error paths.

Targeted at error-handling code paths that have no existing tests but
*do* run in production when something goes sideways. The user's hard
rule: "Goal is finding regressions waiting to bite, not inflating the
coverage number." Every test here picks a specific gap that would
silently regress otherwise.

Modules touched:
  * ``tdpilot_api_agent._call_api``  — URLError / TimeoutError / OSError
    branches were missing coverage. HTTPError already covered upstream.
  * ``tdpilot_api_skills``           — non-dict YAML frontmatter,
    unreadable user-dir file, ``handle_skill_get`` not-found and
    missing-name branches, ``handle_skill_load`` activation flag.
  * ``tdpilot_api_runtime``          — wired callback chain: when the
    runtime's runtime-attached ``on_tool_result`` + ``on_turn_done``
    callbacks fire as the Agent would invoke them, the missing-validator
    hint must reach the chat queue. Ports the runtime-side intent of
    the live ``test_build_no_validation_emits_hint`` agent_eval without
    needing mock-DeepSeek (PR-20) infrastructure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from queue import Empty
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_skills as skills  # noqa: E402
from tdpilot_api_agent import Agent, AgentError  # noqa: E402

# ---------------------------------------------------------------------------
# _call_api error branches
#
# HTTPError is already covered in tests/test_tdpilot_api_agent.py. The
# URLError / TimeoutError / OSError branches were missing — these fire
# in production any time DeepSeek is unreachable (DNS, TLS handshake
# timeout, EHOSTUNREACH). The error message format becomes part of the
# user-facing chat error event, so a regression here is a UX regression.
# ---------------------------------------------------------------------------


def _agent_for_call_api():
    a = Agent(api_key="sk-fake", dispatcher=lambda *a, **k: None)
    a.add_user_message("anything")
    return a


def test_call_api_url_error_surfaces_as_agent_error():
    """``urllib.error.URLError`` (DNS failure, refused connection) must
    surface as an ``AgentError`` whose message includes the base URL —
    not bubble up as a raw URLError."""
    import urllib.error

    a = _agent_for_call_api()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS resolution failed")):
        with pytest.raises(AgentError) as exc:
            a.run_turn()
    # The message includes both the base URL and the reason — both are
    # useful for the user diagnosing a connectivity issue.
    assert "Network error" in str(exc.value)
    assert "DNS resolution failed" in str(exc.value)


def test_call_api_timeout_error_surfaces_as_agent_error():
    """``TimeoutError`` (request timeout against a slow / hung peer)
    must surface as an ``AgentError`` — not a raw TimeoutError."""
    a = _agent_for_call_api()
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(AgentError) as exc:
            a.run_turn()
    assert "I/O error" in str(exc.value)
    assert "timed out" in str(exc.value)


def test_call_api_os_error_surfaces_as_agent_error():
    """``OSError`` (e.g. EHOSTUNREACH, EBADF) must surface as an
    ``AgentError``. Easy to mishandle since OSError is a parent of
    ConnectionError + TimeoutError — the catch ordering matters."""
    a = _agent_for_call_api()
    with patch("urllib.request.urlopen", side_effect=OSError("Host is unreachable")):
        with pytest.raises(AgentError) as exc:
            a.run_turn()
    assert "I/O error" in str(exc.value)
    assert "Host is unreachable" in str(exc.value)


def test_call_api_http_error_with_undecodable_body_still_raises():
    """If ``HTTPError.read()`` itself raises (rare — e.g. underlying
    socket already closed), the inner try/except in ``_call_api`` must
    still surface an ``AgentError`` with the HTTP code, just without
    detail. Pre-fix, the bare except + missing detail lookup could
    have masked the original error."""
    import urllib.error

    class _BadFp:
        def read(self, *a, **k):
            raise OSError("body stream broken")

        def close(self) -> None:
            # HTTPError's _TemporaryFileCloser invokes close() during GC;
            # without this, pytest emits a benign Unraisable warning.
            return None

    def fail(*a, **k):
        raise urllib.error.HTTPError(url="x", code=502, msg="Bad Gateway", hdrs=None, fp=_BadFp())

    a = _agent_for_call_api()
    with patch("urllib.request.urlopen", side_effect=fail):
        with pytest.raises(AgentError) as exc:
            a.run_turn()
    assert "502" in str(exc.value)


# ---------------------------------------------------------------------------
# Skill loader — frontmatter type errors
#
# The YAML parser may successfully parse a frontmatter block whose top-
# level value is a list or string instead of a dict — pyyaml accepts
# any well-formed YAML document. Without the explicit isinstance check
# the loader would attempt ``meta.get("name")`` and crash. This branch
# was previously uncovered.
# ---------------------------------------------------------------------------


def test_parse_frontmatter_yaml_list_top_level_is_flagged():
    text = """---
- one
- two
- three
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("yaml mapping" in e.lower() or "mapping" in e.lower() for e in errors), (
        f"expected mapping error, got {errors!r}"
    )


def test_parse_frontmatter_yaml_string_top_level_is_flagged():
    text = """---
just-a-string-not-a-dict
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("yaml mapping" in e.lower() or "mapping" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Skill loader — _user_entries unreadable file
#
# A user-skill file with restrictive perms (or platform-specific
# encoding glitch) raises OSError on read. The loader must log + skip,
# never propagate. Pre-fix this branch was untested; a regression
# could turn a single bad file into a session-wide skill blackout.
# ---------------------------------------------------------------------------


def test_user_entries_skips_unreadable_file_without_raising(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)

    # Two skills — one readable, one that simulates an OS read failure.
    (tmp_path / "good.md").write_text(
        """---
name: good
description: works
---

body
""",
        encoding="utf-8",
    )
    bad_path = tmp_path / "bad.md"
    bad_path.write_text("placeholder", encoding="utf-8")

    real_read = Path.read_text

    def fake_read(self, *args, **kwargs):
        if self.name == "bad.md":
            raise OSError("Permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)

    entries = skills._user_entries()
    captured = capsys.readouterr()

    # The good entry survives, the bad one is dropped silently with a log line.
    names = [e["name"] for e in entries]
    assert "good" in names
    assert "bad" not in names
    assert "could not read bad.md" in captured.out


# ---------------------------------------------------------------------------
# Skill handlers — handle_skill_get / handle_skill_load
#
# Both handle_skill_get's "missing name" branch and "skill not found"
# branch were uncovered. The error messages are user-facing — a
# regression that swaps them silently would leave the user staring at
# a confusing chat response.
# ---------------------------------------------------------------------------


def test_handle_skill_get_missing_name_returns_error():
    out = skills.handle_skill_get({})
    assert "error" in out
    assert "name" in out["error"].lower()


def test_handle_skill_get_blank_name_returns_error():
    out = skills.handle_skill_get({"name": "   "})
    assert "error" in out
    assert "name" in out["error"].lower()


def test_handle_skill_get_unknown_skill_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])
    out = skills.handle_skill_get({"name": "does-not-exist"})
    assert "error" in out
    assert "not found" in out["error"].lower()
    assert "does-not-exist" in out["error"]


def test_handle_skill_load_sets_activated_flag(tmp_path, monkeypatch):
    """skill_load is the same data as skill_get *plus* an
    ``activated=True`` flag that signals to the model that the returned
    content is authoritative for the rest of the turn. The flag must
    be present on success and absent on error."""
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    (tmp_path / "active.md").write_text(
        """---
name: active-skill
description: x
---

body
""",
        encoding="utf-8",
    )

    ok = skills.handle_skill_load({"name": "active-skill"})
    assert ok.get("ok") is True
    assert ok.get("activated") is True

    # Error path: no activated flag should be set on a failed load.
    fail = skills.handle_skill_load({"name": "missing"})
    assert "error" in fail
    assert "activated" not in fail


# ---------------------------------------------------------------------------
# Runtime hint chain — full wired callback path
#
# The live agent_eval ``test_build_no_validation_emits_hint`` exercises
# the chain "model decides to skip validator → runtime emits hint". The
# unit-level pieces (``_record_tool_call``, ``_maybe_emit_validation_hint``)
# are individually tested. What was missing is the WIRED callback path:
# when the Agent invokes ``on_tool_result`` (which the runtime sets to
# call ``_record_tool_call`` + friends) followed by ``on_turn_done``
# (which the runtime sets to call ``_maybe_emit_validation_hint``), the
# EV_HINT must reach ``_events``. Mock-DeepSeek (PR-20) would cover this
# end-to-end via a fake API; pre-PR-20 we exercise the runtime's wired
# callbacks directly without spinning up an Agent.
# ---------------------------------------------------------------------------


def _build_runtime_for_chain_test(monkeypatch):
    """Build an AgentRuntime with the dispatcher stubbed and fetch_api_key
    overridden so it can construct without TD globals. Same pattern as
    the existing test_validation_hint_* helpers."""
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    return AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])


def test_runtime_callback_chain_emits_validation_hint(monkeypatch):
    """Simulate the Agent firing the runtime's wired callbacks for a
    high-severity tool followed by turn-done: EV_HINT must land on the
    chat queue with the documented payload shape. This is the end-to-end
    runtime check that the agent_eval was the only test for; here it
    runs without a live model in the loop."""
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_chain_test(monkeypatch)
    agent = rt._agent
    assert agent is not None, "runtime should have wired an Agent in __init__"

    # Fire the Agent-side callbacks the runtime attached. on_tool_result
    # signature is (name, result, is_error); on_turn_done is (final_text).
    agent.on_tool_result("td_create_node", {"path": "/project1/x"}, False)
    agent.on_turn_done("created the node")

    # Drain the runtime's event queue and look for the wired hint.
    events = []
    while True:
        try:
            events.append(rt._events.get_nowait())
        except Empty:
            break

    hint_payloads = [p for k, p in events if k == EV_HINT]
    assert len(hint_payloads) == 1, f"expected exactly one EV_HINT, got {hint_payloads!r}"
    payload = hint_payloads[0]
    assert payload["kind"] == "missing_validation"
    assert payload["tools"] == ["td_create_node"]
    assert "td_get_errors" in payload["message"]


def test_runtime_callback_chain_suppressed_when_validator_paired(monkeypatch):
    """Same wired-callback path, but this time the agent calls a
    validator after the high-severity tool. The hint must be suppressed
    end-to-end."""
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_chain_test(monkeypatch)
    agent = rt._agent

    agent.on_tool_result("td_create_node", {"path": "/project1/x"}, False)
    agent.on_tool_result("td_get_errors", {"errors": []}, False)
    agent.on_turn_done("validated and clean")

    events = []
    while True:
        try:
            events.append(rt._events.get_nowait())
        except Empty:
            break

    assert not any(k == EV_HINT for k, _ in events), (
        "validator paired with mutation must suppress hint via wired callbacks"
    )
