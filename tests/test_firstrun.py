"""Tests for ``firstrun_status`` (Phase 5.2).

The function reports whether a fresh-install user has yet:
  - pasted an API key,
  - saved any memory,
  - installed any external brain.

Every other test should monkeypatch ``HOME`` to a clean tmp dir so
the developer machine's real ``~/.tdpilot-api/`` doesn't leak in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_introspect as introspect  # noqa: E402


def _stub_pristine_home(monkeypatch, tmp_path: Path) -> None:
    """Pin every relevant filesystem root inside ``tmp_path`` so the
    test sees a clean cleanroom regardless of the developer machine.

    Two layers of patching are needed:
      1. ``Path.home()`` — used lazily inside ``firstrun_status`` for
         the memory/brain checks, so a runtime patch suffices.
      2. ``tdpilot_api_config.CONFIG_JSON`` — frozen at module load,
         so we have to monkeypatch the module-level constant
         directly.
    """
    import tdpilot_api_config as cfg_mod

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(str(fake_home))))
    # The config module's CONFIG_JSON / CONFIG_DIR were resolved at
    # import time — re-point them at the fake home.
    fake_cfg_dir = fake_home / ".tdpilot-api"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", fake_cfg_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_JSON", fake_cfg_dir / "config.json")
    monkeypatch.setattr(cfg_mod, "ENV_FILE", fake_cfg_dir / ".env")


def test_firstrun_clean_install_reports_first_run(monkeypatch, tmp_path):
    _stub_pristine_home(monkeypatch, tmp_path)
    state = introspect.firstrun_status()
    assert state["is_first_run"] is True
    assert state["has_api_key"] is False
    assert state["has_memory"] is False
    assert state["has_brains"] is False
    # Three steps surfaced in canonical order.
    step_names = [s["name"] for s in state["next_steps"]]
    assert "paste_api_key" in step_names
    assert "install_brain" in step_names
    assert "first_memory" in step_names


def test_firstrun_with_api_key_no_longer_first_run(monkeypatch, tmp_path):
    _stub_pristine_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "home" / ".tdpilot-api"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"api_key": "sk-test-12345"}),
        encoding="utf-8",
    )
    state = introspect.firstrun_status()
    assert state["has_api_key"] is True
    assert state["is_first_run"] is False
    # paste_api_key step must NOT show up since it's done.
    assert all(s["name"] != "paste_api_key" for s in state["next_steps"])


def test_firstrun_with_saved_memory_no_longer_first_run(monkeypatch, tmp_path):
    _stub_pristine_home(monkeypatch, tmp_path)
    mem_dir = tmp_path / "home" / ".tdpilot-api" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "feedback_test.md").write_text("---\nname: test\n---\nbody\n", encoding="utf-8")
    state = introspect.firstrun_status()
    assert state["has_memory"] is True
    assert state["is_first_run"] is False
    # first_memory step must NOT show up since the user already has memories.
    assert all(s["name"] != "first_memory" for s in state["next_steps"])


def test_firstrun_with_brain_no_longer_first_run(monkeypatch, tmp_path):
    _stub_pristine_home(monkeypatch, tmp_path)
    brain_dir = tmp_path / "home" / ".tdpilot" / "data" / "normalized" / "derivative"
    brain_dir.mkdir(parents=True)
    (brain_dir / "docsbrain.db").write_bytes(b"")
    state = introspect.firstrun_status()
    assert state["has_brains"] is True
    assert state["is_first_run"] is False
    assert all(s["name"] != "install_brain" for s in state["next_steps"])


def test_firstrun_tolerates_pages_jsonl_too(monkeypatch, tmp_path):
    """Either ``*brain.db`` OR ``pages.jsonl`` counts as a brain."""
    _stub_pristine_home(monkeypatch, tmp_path)
    brain_dir = tmp_path / "home" / ".tdpilot" / "data" / "normalized" / "popx"
    brain_dir.mkdir(parents=True)
    (brain_dir / "pages.jsonl").write_text("{}\n", encoding="utf-8")
    state = introspect.firstrun_status()
    assert state["has_brains"] is True


def test_firstrun_empty_api_key_is_falsy(monkeypatch, tmp_path):
    """An ``api_key: ""`` entry doesn't count as having pasted a key."""
    _stub_pristine_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "home" / ".tdpilot-api"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"api_key": "   "}), encoding="utf-8")
    state = introspect.firstrun_status()
    assert state["has_api_key"] is False
    assert state["is_first_run"] is True


def test_firstrun_state_in_get_capabilities(monkeypatch, tmp_path):
    """``td_get_capabilities`` returns must include the first-run dict."""
    _stub_pristine_home(monkeypatch, tmp_path)
    out = introspect.handle_get_capabilities({})
    assert "first_run" in out
    assert isinstance(out["first_run"], dict)
    assert "is_first_run" in out["first_run"]
    assert "next_steps" in out["first_run"]


def test_firstrun_handles_unparseable_config_gracefully(monkeypatch, tmp_path):
    _stub_pristine_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "home" / ".tdpilot-api"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("not json {", encoding="utf-8")
    # Should still return a dict — never raise.
    state = introspect.firstrun_status()
    assert state["has_api_key"] is False  # bad config = no key


def test_firstrun_step_optional_flag_on_brain_step(monkeypatch, tmp_path):
    """The brain-install step is optional — the agent works fine
    against bundled knowledge with no external corpora.
    """
    _stub_pristine_home(monkeypatch, tmp_path)
    state = introspect.firstrun_status()
    brain_step = next((s for s in state["next_steps"] if s["name"] == "install_brain"), None)
    assert brain_step is not None
    assert brain_step.get("optional") is True


def test_firstrun_completed_state_yields_empty_next_steps(monkeypatch, tmp_path):
    """All three boxes ticked → no next_steps; chat UI dismisses the wizard."""
    _stub_pristine_home(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "home" / ".tdpilot-api"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"api_key": "sk-x"}), encoding="utf-8")
    mem_dir = tmp_path / "home" / ".tdpilot-api" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "any.md").write_text("body", encoding="utf-8")
    brain_dir = tmp_path / "home" / ".tdpilot" / "data" / "normalized" / "derivative"
    brain_dir.mkdir(parents=True)
    (brain_dir / "docsbrain.db").write_bytes(b"")

    state = introspect.firstrun_status()
    assert state["has_api_key"] is True
    assert state["has_memory"] is True
    assert state["has_brains"] is True
    assert state["is_first_run"] is False
    assert state["next_steps"] == []
