"""H-1 regression — ``_find_scoped_manifest`` sandbox to ``SNAPSHOTS_DIR``.

PR-#53 added the path-traversal sandbox to
``td_component/tdpilot_api_patches.py:_find_scoped_manifest`` but
shipped without regression tests — flagged as audit finding N-2 in
the post-PR-#53 fresh audit. These tests close that gap.

The pre-fix vulnerability: a prompt-injected agent (or any caller
bypassing the v2.5.3 approval gate via ``Authmode=open`` or
``TDPILOT_DISABLE_TOOL_APPROVAL=1``) could pass an absolute path
like ``/Users/<user>/.ssh/some.json`` as ``path`` and have the file
parsed as JSON. The manifest-version check on the path limited
mutation, but the parsed JSON still surfaced in error messages →
file-existence probing + arbitrary-JSON read.

These tests pin the new contract: absolute-path inputs must resolve
(symlinks followed) to within ``SNAPSHOTS_DIR``, OR they return
``None`` (which the caller surfaces as "no scoped manifest found"
without leaking the parsed content).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import tdpilot_api_patches as patches


@pytest.fixture
def tmp_snapshots_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level ``SNAPSHOTS_DIR`` at a fresh tmp dir.

    The module reads ``SNAPSHOTS_DIR`` at module load time via
    ``resolve_user_dir`` / a ``Path.home()`` fallback. Tests need a
    clean dir per case so the slug-based lookup doesn't collide.
    """
    snapdir = tmp_path / "snapshots"
    snapdir.mkdir()
    monkeypatch.setattr(patches, "SNAPSHOTS_DIR", snapdir)
    return snapdir


class TestAbsolutePathSandbox:
    def test_absolute_path_inside_snapshots_dir_resolves(self, tmp_snapshots_dir: Path) -> None:
        """A legitimate absolute path inside SNAPSHOTS_DIR with the right
        suffix should be returned as-is (after resolve)."""
        target = tmp_snapshots_dir / "legit_2026-05-19.scoped.json"
        target.write_text('{"version": "scoped/v1", "scope": [], "nodes": []}')
        got = patches._find_scoped_manifest(str(target))
        assert got is not None
        assert got.resolve() == target.resolve()

    def test_absolute_path_outside_snapshots_dir_rejected(
        self, tmp_snapshots_dir: Path, tmp_path: Path
    ) -> None:
        """The canonical attack: an absolute path outside SNAPSHOTS_DIR.
        Pre-fix this would parse + leak the JSON content; post-fix
        returns None and the caller emits a generic 'not found' error."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "leak.scoped.json"
        target.write_text('{"sensitive": "should-not-leak"}')
        assert patches._find_scoped_manifest(str(target)) is None

    def test_etc_hosts_style_attack_rejected(self, tmp_snapshots_dir: Path, tmp_path: Path) -> None:
        """Regression: the canonical /etc/passwd-style attack. The path
        doesn't even have a .scoped.json suffix — pre-fix the lookup
        would still try to read it. Post-fix returns None at the
        relative_to gate."""
        # We can't write to /etc on CI, so use a tmp file that has the
        # shape of the attack: an absolute path outside SNAPSHOTS_DIR
        # that exists but isn't a snapshot.
        fake = tmp_path / "looks_like_passwd"
        fake.write_text("root:x:0:0::/root:/bin/bash\n")
        assert patches._find_scoped_manifest(str(fake)) is None

    def test_nonexistent_absolute_path_falls_through_to_slug_lookup(
        self, tmp_snapshots_dir: Path, tmp_path: Path
    ) -> None:
        """A missing absolute path doesn't raise — the resolve(strict=True)
        FileNotFoundError is caught and we fall through to slug-based
        lookup, which itself returns None for an empty SNAPSHOTS_DIR."""
        missing = tmp_path / "does_not_exist.scoped.json"
        # No file written.
        assert patches._find_scoped_manifest(str(missing)) is None


class TestSymlinkAttacks:
    def test_symlink_in_snapshots_pointing_outside_rejected(
        self, tmp_snapshots_dir: Path, tmp_path: Path
    ) -> None:
        """The classic bypass attempt: create a symlink INSIDE the
        sandbox that points OUTSIDE. ``resolve(strict=True)`` follows
        the symlink BEFORE the ``relative_to(SNAPSHOTS_DIR)`` check,
        so the resolved real path falls outside the sandbox and the
        lookup returns None."""
        secret = tmp_path / "secret_outside.scoped.json"
        secret.write_text('{"sensitive": "should-not-leak"}')
        link = tmp_snapshots_dir / "decoy.scoped.json"
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        assert patches._find_scoped_manifest(str(link)) is None

    def test_symlink_in_snapshots_pointing_inside_accepted(self, tmp_snapshots_dir: Path) -> None:
        """Defense-in-depth: a symlink whose target IS inside the
        sandbox should still resolve. (Useful for legitimate setups
        where snapshots are stored under a symlinked directory.)"""
        real = tmp_snapshots_dir / "real.scoped.json"
        real.write_text('{"version": "scoped/v1", "scope": [], "nodes": []}')
        link = tmp_snapshots_dir / "via-link.scoped.json"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        got = patches._find_scoped_manifest(str(link))
        assert got is not None
        assert got.resolve() == real.resolve()


class TestSlugFallback:
    def test_slug_lookup_inside_snapshots_dir_works(self, tmp_snapshots_dir: Path) -> None:
        """The non-absolute-path code path (slug-based) should still
        function — verify we didn't break it while adding the
        absolute-path sandbox. ``_slugify("mysnap")`` → ``"mysnap"`` so
        the glob ``mysnap_*.scoped.json`` matches the on-disk file."""
        target = tmp_snapshots_dir / "mysnap_2026-05-19.scoped.json"
        target.write_text('{"version": "scoped/v1", "scope": [], "nodes": []}')
        got = patches._find_scoped_manifest("mysnap")
        assert got is not None
        assert got.resolve() == target.resolve()

    def test_slug_lookup_with_no_matches_returns_none(self, tmp_snapshots_dir: Path) -> None:
        assert patches._find_scoped_manifest("nothing-like-this") is None
