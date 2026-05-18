"""Tests for v2.5.2 — OCR subprocess sidecar.

Unit tests use a mock subprocess via a stub worker script so we don't
need paddleocr installed in the dev env. One end-to-end test runs the
real worker behind ``@pytest.mark.skipif(not paddleocr_installed)`` so
contributors who DO have extras get extra coverage.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from td_mcp.vision.ocr import (
    OcrManager,
    OcrResult,
    OcrTimeout,
    OcrUnavailable,
    get_global_manager,
    reset_global_manager,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_worker_script(tmp_path: Path) -> Path:
    """Write a stub worker that doesn't need paddleocr. Mimics the real
    worker's JSON-line protocol so the manager tests exercise the wire
    contract without the heavy dep."""
    script = tmp_path / "fake_worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    path = req["image_path"]
                    # Emit a deterministic fake response keyed on path.
                    response = {
                        "ok": True,
                        "text": f"FAKE OCR: {path}",
                        "boxes": [[[0, 0], [10, 0], [10, 10], [0, 10]]],
                        "confidence": [0.99],
                    }
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                sys.stdout.write(json.dumps(response) + "\\n")
                sys.stdout.flush()
            """
        ).strip()
    )
    return script


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    """A real on-disk file for the manager's existence check. Contents
    don't matter — the fake worker echoes the path."""
    p = tmp_path / "test_image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic only — enough to exist
    return p


@pytest.fixture(autouse=True)
def _reset_global_manager_each_test():
    """Module-level OCR manager is a singleton; reset between tests."""
    reset_global_manager()
    yield
    # Best-effort cleanup so a leaked worker doesn't survive into the
    # next test session.
    try:
        get_global_manager().shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pre-flight (no extras installed)
# ---------------------------------------------------------------------------


class TestPreflightWithoutExtras:
    def test_real_manager_raises_ocr_unavailable_without_paddleocr(self, image_file, monkeypatch):
        """The default manager pre-flights ``python -c 'import paddleocr'``
        before spawning. Without extras, that subprocess returns non-zero
        and we raise OcrUnavailable cleanly (not a vague crash)."""
        # Force pre-flight failure regardless of host env: patch sys.executable
        # to a Python that won't have paddleocr in its venv.
        # Simplest: run preflight against `sys.executable -c 'raise ImportError'`.
        # Patch subprocess.run inside the ocr module so we can simulate.
        from td_mcp.vision import ocr as ocr_mod

        def _fake_preflight(args, **kwargs):
            # Mimic a paddleocr-missing run.
            class _CompletedProcess:
                returncode = 1
                stderr = b"ModuleNotFoundError: No module named 'paddleocr'"

            return _CompletedProcess()

        monkeypatch.setattr(ocr_mod.subprocess, "run", _fake_preflight)
        mgr = ocr_mod.OcrManager()
        with pytest.raises(OcrUnavailable, match="paddleocr is not installed"):
            mgr.ocr_image(str(image_file))


# ---------------------------------------------------------------------------
# Manager + worker via the stub worker script
# ---------------------------------------------------------------------------


def _patch_preflight_pass(monkeypatch):
    """Make ``subprocess.run`` (used in pre-flight) report paddleocr-present."""
    from td_mcp.vision import ocr as ocr_mod

    def _fake_preflight(args, **kwargs):
        class _CompletedProcess:
            returncode = 0
            stderr = b""

        return _CompletedProcess()

    monkeypatch.setattr(ocr_mod.subprocess, "run", _fake_preflight)


class TestManagerHappyPath:
    def test_first_call_spawns_and_returns_result(self, fake_worker_script, image_file, monkeypatch):
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=fake_worker_script)
        assert mgr.is_running() is False
        result = mgr.ocr_image(str(image_file))
        assert isinstance(result, OcrResult)
        assert result.text == f"FAKE OCR: {image_file}"
        assert result.confidence == [0.99]
        assert mgr.is_running() is True
        mgr.shutdown()

    def test_second_call_reuses_running_worker(self, fake_worker_script, image_file, monkeypatch):
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=fake_worker_script)
        mgr.ocr_image(str(image_file))
        pid_first = mgr._proc.pid
        mgr.ocr_image(str(image_file))
        pid_second = mgr._proc.pid
        assert pid_first == pid_second, "manager should reuse existing worker"
        mgr.shutdown()

    def test_result_to_dict_shape(self, fake_worker_script, image_file, monkeypatch):
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=fake_worker_script)
        result = mgr.ocr_image(str(image_file))
        d = result.to_dict()
        assert set(d.keys()) == {"text", "boxes", "confidence", "elapsed_ms"}
        assert d["elapsed_ms"] >= 0
        mgr.shutdown()


class TestErrorPaths:
    def test_missing_image_raises_file_not_found(self, fake_worker_script, monkeypatch):
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=fake_worker_script)
        with pytest.raises(FileNotFoundError):
            mgr.ocr_image("/definitely/not/a/file.png")

    def test_worker_error_response_raises_runtime_error(self, tmp_path, image_file, monkeypatch):
        """If the worker emits ``{ok: false, error: "..."}``, the
        manager raises RuntimeError. Use an err-only stub."""
        script = tmp_path / "err_worker.py"
        script.write_text(
            textwrap.dedent(
                """
                import json, sys
                for line in sys.stdin:
                    sys.stdout.write(json.dumps({"ok": False, "error": "boom"}) + "\\n")
                    sys.stdout.flush()
                """
            ).strip()
        )
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=script)
        with pytest.raises(RuntimeError, match="boom"):
            mgr.ocr_image(str(image_file))
        mgr.shutdown()

    def test_worker_import_error_response_raises_unavailable(self, tmp_path, image_file, monkeypatch):
        script = tmp_path / "import_err_worker.py"
        script.write_text(
            textwrap.dedent(
                """
                import json, sys
                for line in sys.stdin:
                    response = {"ok": False, "error": "ModuleNotFoundError: paddleocr"}
                    sys.stdout.write(json.dumps(response) + "\\n")
                    sys.stdout.flush()
                """
            ).strip()
        )
        _patch_preflight_pass(monkeypatch)
        mgr = OcrManager(worker_path=script)
        with pytest.raises(OcrUnavailable):
            mgr.ocr_image(str(image_file))
        mgr.shutdown()


class TestSingleton:
    def test_global_manager_lazy_init(self):
        reset_global_manager()
        from td_mcp.vision.ocr import _GLOBAL_MANAGER

        # After reset_global_manager() the singleton IS already
        # constructed (reset returns a fresh manager). The shape we
        # care about: get_global_manager twice returns the same instance.
        a = get_global_manager()
        b = get_global_manager()
        assert a is b

    def test_reset_replaces_instance(self):
        a = get_global_manager()
        b = reset_global_manager()
        assert a is not b


# ---------------------------------------------------------------------------
# Real worker integration (skipped if paddleocr unavailable)
# ---------------------------------------------------------------------------


def _paddleocr_importable() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import paddleocr"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _paddleocr_importable(),
    reason="paddleocr not installed (install via `pip install -e .[ocr]`)",
)
class TestRealOcrIntegration:
    def test_real_worker_produces_text_from_fixture_png(self, tmp_path):
        """End-to-end with the real worker. Skipped unless extras
        installed in the dev env."""
        # Use the worker shipped with the package.
        from td_mcp.vision.ocr import _default_worker_path

        # Generate a tiny PNG with text via PIL if available, otherwise
        # skip — we want to avoid a fixture binary committed to the
        # repo since this whole test class is opt-in.
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            pytest.skip("Pillow not installed; cannot synthesize test image")

        img = Image.new("RGB", (200, 80), color="white")
        draw = ImageDraw.Draw(img)
        draw.text((10, 20), "HELLO TDPILOT", fill="black")
        image_path = tmp_path / "hello.png"
        img.save(image_path)

        mgr = OcrManager(worker_path=_default_worker_path())
        result = mgr.ocr_image(str(image_path))
        assert result.text  # non-empty
        # Don't assert exact match — OCR isn't perfect. Just sanity.
        assert any(c.isalpha() for c in result.text)
        mgr.shutdown()
