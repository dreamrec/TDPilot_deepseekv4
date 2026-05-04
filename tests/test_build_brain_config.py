"""Tests for ``scripts/build_brain.py`` config loading.

Phase 1.4 — pin the brain config schema so the documented community
template parses cleanly through the builder. Catches the historical
silent failure where the template said ``name:`` but the builder
expected ``brain_id:``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_build_brain_module():
    """Import scripts/build_brain.py as a module without polluting sys.path
    with the whole scripts dir.
    """
    spec = importlib.util.spec_from_file_location(
        "build_brain_module",
        REPO_ROOT / "scripts" / "build_brain.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_brain_module"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def build_brain():
    return _load_build_brain_module()


def test_template_yaml_parses_through_load_config(build_brain, tmp_path):
    """The documented community template must parse cleanly through the
    builder's load_config function — that's the whole contract Phase 1.4
    is restoring.
    """
    template = REPO_ROOT / "data" / "brains" / "_template_community.yaml"
    cfg = build_brain.load_config(template)
    assert cfg["brain_id"] == "my_source"
    assert cfg["display_name"] == "My Tutorial Source"
    assert cfg["content_selector"] == "body"


def test_load_config_rejects_legacy_name_field_with_helpful_message(build_brain, tmp_path, caplog):
    """If a contributor copied an older template (with ``name:``), the
    builder must point them at ``brain_id:`` instead of dying with a
    bare KeyError.
    """
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        'name: my_tutorials\ndisplay_name: "My Tutorials"\ncontent_selector: "body"\n',
        encoding="utf-8",
    )
    caplog.set_level("ERROR", logger=build_brain.logger.name)
    with pytest.raises(SystemExit):
        build_brain.load_config(legacy)
    msg = " ".join(r.getMessage() for r in caplog.records)
    # Migration message must mention the new field name AND quote the
    # value so the user knows exactly what to type.
    assert "brain_id" in msg
    assert "my_tutorials" in msg
    assert "name" in msg.lower()


def test_load_config_rejects_missing_brain_id(build_brain, tmp_path, caplog):
    """No ``brain_id`` and no ``name`` either — generic missing-key error."""
    minimal = tmp_path / "broken.yaml"
    minimal.write_text(
        'display_name: "Whatever"\ncontent_selector: "body"\n',
        encoding="utf-8",
    )
    caplog.set_level("ERROR", logger=build_brain.logger.name)
    with pytest.raises(SystemExit):
        build_brain.load_config(minimal)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "brain_id" in msg


def test_load_config_rejects_non_mapping(build_brain, tmp_path, caplog):
    """A YAML scalar (or list) at the top level isn't a config."""
    bad = tmp_path / "scalar.yaml"
    bad.write_text("just-a-string\n", encoding="utf-8")
    caplog.set_level("ERROR", logger=build_brain.logger.name)
    with pytest.raises(SystemExit):
        build_brain.load_config(bad)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "mapping" in msg.lower() or "yaml" in msg.lower()


def test_bundled_yamls_use_canonical_brain_id_field():
    """Phase 1.4 — every yaml under data/brains/ must use ``brain_id:``,
    not ``name:``. Locks in the canonical schema across the repo.
    """
    import yaml

    brains_dir = REPO_ROOT / "data" / "brains"
    yaml_files = sorted(brains_dir.glob("*.yaml"))
    assert yaml_files, "no brain yamls discovered"
    offenders: list[str] = []
    for path in yaml_files:
        cfg = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(cfg, dict):
            continue
        if "brain_id" not in cfg:
            offenders.append(f"{path.name}: missing brain_id")
        if "name" in cfg and "brain_id" not in cfg:
            offenders.append(f"{path.name}: still uses legacy 'name:'")
    assert not offenders, f"YAML schema drift: {offenders}"
