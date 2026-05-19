"""Path-sandbox tests for ``td_ocr_image`` (H-3 audit fix, 2026-05-19).

The OCR tool accepts an image path straight from the agent. Without an
allowlist, a prompt-injected agent could OCR ``~/.ssh/known_hosts`` or
screenshot caches that happen to contain credentials. These tests pin
the two-layer defense:

1. Extension allowlist — must be a known image suffix.
2. Root allowlist     — resolved path must live under a known
   screenshot-like directory, OR under a path supplied via
   ``TDPILOT_OCR_ALLOWED_ROOTS``.

Symlinks are resolved BEFORE the root check so an allowed-dir symlink
pointing at ``/etc/passwd`` is rejected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from td_mcp.vision.ocr import (
    _ALLOWED_IMAGE_EXTS,
    PathNotAllowed,
    _resolve_and_check_path,
)


@pytest.fixture
def sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``tmp_path`` the *only* allowed root via env override."""
    monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(tmp_path))
    return tmp_path


def _make_png(path: Path) -> Path:
    """Create a 1-byte file with a .png suffix (content irrelevant for sandbox)."""
    path.write_bytes(b"\x00")
    return path


class TestExtensionAllowlist:
    def test_png_accepted(self, sandbox_env: Path) -> None:
        target = _make_png(sandbox_env / "shot.png")
        assert _resolve_and_check_path(str(target)) == target.resolve()

    def test_jpg_accepted(self, sandbox_env: Path) -> None:
        target = sandbox_env / "shot.jpg"
        target.write_bytes(b"\x00")
        assert _resolve_and_check_path(str(target)) == target.resolve()

    def test_case_insensitive_extension(self, sandbox_env: Path) -> None:
        target = sandbox_env / "SHOT.PNG"
        target.write_bytes(b"\x00")
        assert _resolve_and_check_path(str(target)) == target.resolve()

    def test_txt_rejected(self, sandbox_env: Path) -> None:
        target = sandbox_env / "secret.txt"
        target.write_bytes(b"\x00")
        with pytest.raises(PathNotAllowed) as exc:
            _resolve_and_check_path(str(target))
        assert "extension" in str(exc.value).lower()

    def test_no_extension_rejected(self, sandbox_env: Path) -> None:
        target = sandbox_env / "noext"
        target.write_bytes(b"\x00")
        with pytest.raises(PathNotAllowed):
            _resolve_and_check_path(str(target))

    def test_known_image_exts_present(self) -> None:
        """Pin the allowlist so future trims are explicit."""
        assert ".png" in _ALLOWED_IMAGE_EXTS
        assert ".jpg" in _ALLOWED_IMAGE_EXTS
        assert ".jpeg" in _ALLOWED_IMAGE_EXTS
        assert ".webp" in _ALLOWED_IMAGE_EXTS


class TestRootAllowlist:
    def test_path_outside_default_roots_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path that's NOT under the default roots and has no env override
        should be refused, even if the extension is fine."""
        # No env override.
        monkeypatch.delenv("TDPILOT_OCR_ALLOWED_ROOTS", raising=False)
        # Force the default roots to be empty so we don't accidentally
        # include /tmp (which may well be the parent of tmp_path).
        monkeypatch.setattr(
            "td_mcp.vision.ocr._default_allowed_roots",
            lambda: [],
        )
        target = _make_png(tmp_path / "shot.png")
        with pytest.raises(PathNotAllowed) as exc:
            _resolve_and_check_path(str(target))
        assert "allowed roots" in str(exc.value).lower()

    def test_env_override_extends_sandbox(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force defaults empty so ONLY the env override applies.
        monkeypatch.setattr("td_mcp.vision.ocr._default_allowed_roots", lambda: [])
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(tmp_path))
        target = _make_png(tmp_path / "shot.png")
        assert _resolve_and_check_path(str(target)) == target.resolve()

    def test_env_override_multiple_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("td_mcp.vision.ocr._default_allowed_roots", lambda: [])
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", f"{a}{os.pathsep}{b}")
        target = _make_png(b / "shot.png")
        assert _resolve_and_check_path(str(target)) == target.resolve()

    def test_sibling_outside_allowed_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("td_mcp.vision.ocr._default_allowed_roots", lambda: [])
        allowed = tmp_path / "allowed"
        elsewhere = tmp_path / "elsewhere"
        allowed.mkdir()
        elsewhere.mkdir()
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(allowed))
        target = _make_png(elsewhere / "shot.png")
        with pytest.raises(PathNotAllowed):
            _resolve_and_check_path(str(target))


class TestSymlinkResolution:
    def test_symlink_inside_allowed_to_outside_target_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The classic bypass attempt: drop a symlink in an allowed dir
        that points at /etc/passwd or ~/.ssh/known_hosts. resolve() must
        follow the link BEFORE the root check, so the resolved real
        path falls outside the sandbox."""
        monkeypatch.setattr("td_mcp.vision.ocr._default_allowed_roots", lambda: [])
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(allowed))

        real_target = _make_png(outside / "real.png")
        link = allowed / "link.png"
        try:
            link.symlink_to(real_target)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")

        with pytest.raises(PathNotAllowed):
            _resolve_and_check_path(str(link))

    def test_symlink_inside_allowed_to_inside_target_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("td_mcp.vision.ocr._default_allowed_roots", lambda: [])
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(allowed))

        real_target = _make_png(allowed / "real.png")
        link = allowed / "link.png"
        try:
            link.symlink_to(real_target)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")

        # Both link and target are under `allowed`, so it resolves.
        assert _resolve_and_check_path(str(link)) == real_target.resolve()


class TestMissingFile:
    def test_missing_file_raises_filenotfound(self, sandbox_env: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _resolve_and_check_path(str(sandbox_env / "does_not_exist.png"))

    def test_directory_rejected_as_filenotfound(self, sandbox_env: Path) -> None:
        """``resolve(strict=True)`` succeeds on a directory; the explicit
        ``is_file()`` check rejects it."""
        (sandbox_env / "subdir").mkdir()
        with pytest.raises(FileNotFoundError):
            _resolve_and_check_path(str(sandbox_env / "subdir"))


class TestRegression:
    def test_dotssh_known_hosts_not_image_ext(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: the canonical attack — ``~/.ssh/known_hosts`` —
        should fail on the extension check even before the root check
        gets a chance to engage. Pin this so a future "be lenient
        on no-extension files" change can't open the hole again."""
        ssh_dir = tmp_path / ".ssh-fake"
        ssh_dir.mkdir()
        target = ssh_dir / "known_hosts"
        target.write_bytes(b"\x00")
        monkeypatch.setenv("TDPILOT_OCR_ALLOWED_ROOTS", str(ssh_dir))
        with pytest.raises(PathNotAllowed) as exc:
            _resolve_and_check_path(str(target))
        assert "extension" in str(exc.value).lower()
