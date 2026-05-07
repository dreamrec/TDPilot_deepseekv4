"""Tests for ``scripts/doctor_live.py`` — Phase 5.1 contract.

Each individual check is a small pure-Python unit that returns a
``CheckResult``. Tests stub out network / filesystem so the suite
runs offline + cleanroom-friendly (no developer-machine state
leaks in).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "doctor_live.py"


def _load_doctor_module():
    spec = importlib.util.spec_from_file_location("doctor_live_module", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor_live_module"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def doctor():
    return _load_doctor_module()


# ---------------------------------------------------------------------------
# webserver_up
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes = b'{"ok":true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def test_webserver_up_pass(monkeypatch, doctor):
    monkeypatch.setattr(
        doctor.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(b'{"ok":true}'),
    )
    r = doctor._check_webserver_up("http://127.0.0.1:9987/")
    assert r.status == "pass"
    assert "OK" in r.message


def test_webserver_up_fail_on_connection_refused(monkeypatch, doctor):
    def boom(*a, **k):
        raise URLError("Connection refused")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
    r = doctor._check_webserver_up("http://127.0.0.1:9987/")
    assert r.status == "fail"
    assert "Could not reach" in r.message
    assert "Drag the tdpilot_API.tox" in r.fix


# ---------------------------------------------------------------------------
# api_key_set
# ---------------------------------------------------------------------------


def test_api_key_set_missing_config(tmp_path, doctor):
    r = doctor._check_api_key_set(tmp_path / "absent.json")
    assert r.status == "fail"
    assert "missing" in r.message.lower()


def test_api_key_set_empty_key(tmp_path, doctor):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": ""}), encoding="utf-8")
    r = doctor._check_api_key_set(cfg)
    assert r.status == "fail"
    assert "empty" in r.message


def test_api_key_set_pass_obscures_key(tmp_path, doctor):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-abcdefghij1234"}), encoding="utf-8")
    r = doctor._check_api_key_set(cfg)
    assert r.status == "pass"
    assert "...1234" in r.message
    assert "sk-abcdefghij" not in r.message


def test_api_key_set_unparseable_config(tmp_path, doctor):
    cfg = tmp_path / "config.json"
    cfg.write_text("not json {", encoding="utf-8")
    r = doctor._check_api_key_set(cfg)
    assert r.status == "fail"


# ---------------------------------------------------------------------------
# api_key_valid (deep)
# ---------------------------------------------------------------------------


def test_api_key_valid_passes_on_2xx(monkeypatch, tmp_path, doctor):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-test"}), encoding="utf-8")
    monkeypatch.setattr(
        doctor.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(b'{"id":"x"}'),
    )
    r = doctor._check_api_key_valid(cfg)
    assert r.status == "pass"
    assert "DeepSeek accepted" in r.message


def test_api_key_valid_fails_on_401(monkeypatch, tmp_path, doctor):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-bad"}), encoding="utf-8")

    def fail(*a, **k):
        raise HTTPError(
            url="x",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"no key"}'),
        )

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fail)
    r = doctor._check_api_key_valid(cfg)
    assert r.status == "fail"
    assert "401" in r.message


def test_api_key_valid_passes_on_other_4xx(monkeypatch, tmp_path, doctor):
    """A non-401 4xx still means the key was processed — auth passed,
    body shape just needs work."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_key": "sk-test"}), encoding="utf-8")

    def fail(*a, **k):
        raise HTTPError(
            url="x",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad shape"}'),
        )

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fail)
    r = doctor._check_api_key_valid(cfg)
    assert r.status == "pass"


def test_api_key_valid_skip_when_no_config(tmp_path, doctor):
    r = doctor._check_api_key_valid(tmp_path / "absent.json")
    assert r.status == "warn"


# ---------------------------------------------------------------------------
# external_brains
# ---------------------------------------------------------------------------


def test_external_brains_warns_when_none(tmp_path, doctor):
    r = doctor._check_external_brains((tmp_path,))
    assert r.status == "warn"
    assert "no external brains" in r.message


def test_external_brains_pass_with_sqlite(tmp_path, doctor):
    corpus = tmp_path / "derivative"
    corpus.mkdir()
    (corpus / "docsbrain.db").write_bytes(b"")
    r = doctor._check_external_brains((tmp_path,))
    assert r.status == "pass"
    assert "derivative(sqlite)" in r.message


def test_external_brains_pass_with_jsonl(tmp_path, doctor):
    corpus = tmp_path / "popx"
    corpus.mkdir()
    (corpus / "pages.jsonl").write_text("{}\n", encoding="utf-8")
    r = doctor._check_external_brains((tmp_path,))
    assert r.status == "pass"
    assert "popx(jsonl)" in r.message


# ---------------------------------------------------------------------------
# memory_dir
# ---------------------------------------------------------------------------


def test_memory_dir_missing_is_pass(tmp_path, doctor):
    r = doctor._check_memory_dir(tmp_path / "absent")
    assert r.status == "pass"


def test_memory_dir_present_with_files(tmp_path, doctor):
    d = tmp_path / "memory"
    d.mkdir()
    (d / "feedback_x.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (d / "feedback_y.md").write_text("---\nname: y\n---\n", encoding="utf-8")
    r = doctor._check_memory_dir(d)
    assert r.status == "pass"
    assert "2 memory file" in r.message


def test_memory_dir_is_a_file_not_a_dir(tmp_path, doctor):
    p = tmp_path / "stray"
    p.write_text("oops", encoding="utf-8")
    r = doctor._check_memory_dir(p)
    assert r.status == "fail"


# ---------------------------------------------------------------------------
# user_tools
# ---------------------------------------------------------------------------


def test_user_tools_missing_dir_is_pass(tmp_path, doctor):
    r = doctor._check_user_tools(tmp_path / "absent")
    assert r.status == "pass"


def test_user_tools_clean_compile_pass(tmp_path, doctor):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (d / "b.py").write_text("x = 1\n", encoding="utf-8")
    r = doctor._check_user_tools(d)
    assert r.status == "pass"
    assert "2 user tool" in r.message


def test_user_tools_syntax_error_fails(tmp_path, doctor):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "broken.py").write_text("def (oops:\n", encoding="utf-8")
    r = doctor._check_user_tools(d)
    assert r.status == "fail"
    assert "broken.py" in r.message


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_no_failures(monkeypatch, doctor, capsys):
    """Stub every check to "pass" — main exits 0."""

    def stub_check():
        return doctor.CheckResult(name="stub", status="pass", message="OK")

    monkeypatch.setattr(
        doctor,
        "_build_checks",
        lambda url, deep: [stub_check],
    )
    rc = doctor.main(["--url", "http://x"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "summary: 0 fail" in out


def test_main_returns_one_on_failure(monkeypatch, doctor, capsys):
    def fail_check():
        return doctor.CheckResult(name="x", status="fail", message="bad", fix="do thing")

    monkeypatch.setattr(doctor, "_build_checks", lambda url, deep: [fail_check])
    rc = doctor.main(["--url", "http://x"])
    assert rc == 1


def test_main_json_mode(monkeypatch, doctor, capsys):
    def pass_check():
        return doctor.CheckResult(name="x", status="pass", message="OK")

    monkeypatch.setattr(doctor, "_build_checks", lambda url, deep: [pass_check])
    doctor.main(["--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list) and parsed[0]["name"] == "x"


def test_main_deep_flag_includes_key_probe(monkeypatch, doctor):
    """``--deep`` adds one more check than the default registry."""
    base = doctor._build_checks("http://x", deep=False)
    deep = doctor._build_checks("http://x", deep=True)
    assert len(deep) == len(base) + 1


# ---------------------------------------------------------------------------
# run_all_checks tolerates exceptions
# ---------------------------------------------------------------------------


def test_run_all_checks_isolates_check_exceptions(monkeypatch, doctor):
    def boom():
        raise RuntimeError("kaboom")

    def good():
        return doctor.CheckResult(name="good", status="pass", message="OK")

    monkeypatch.setattr(doctor, "_build_checks", lambda url, deep: [boom, good])
    results = doctor.run_all_checks()
    statuses = [r.status for r in results]
    assert "fail" in statuses
    assert "pass" in statuses
    # The error message references the exception type so debugging is possible.
    fail_msg = next(r.message for r in results if r.status == "fail")
    assert "RuntimeError" in fail_msg
