"""TDPilot API — markdown-corpus baker for the .tox build script.

Extracted from build_tdpilot_api_tox.py during the 2026-05-04 audit. The
build script previously inlined two near-identical functions
(``_create_skills_corpus`` and ``_create_knowledge_corpus``) that
walked a directory of ``.md`` files and baked each as a textDAT inside
a baseCOMP child. The shared logic now lives in ``bake_md_corpus`` and
the build script just supplies the per-corpus knobs.

Build-time only — this file is NOT baked into the .tox; it's imported
at build time when ``build_tdpilot_api_tox.build_and_export()`` runs
inside TouchDesigner's Textport.
"""

from __future__ import annotations

import glob as _glob
import os
from typing import Any


def bake_md_corpus(
    parent_comp: Any,
    legacy: Any,
    repo_root: str,
    src_subdir: str,
    container_name: str,
    dat_prefix: str,
    node_xy: tuple[int, int] = (0, 0),
    safe_stem: bool = False,
) -> Any | None:
    """Walk ``<repo_root>/td_component/<src_subdir>/*.md`` and bake each
    file into a textDAT named ``<dat_prefix><stem>`` inside a baseCOMP
    child of ``parent_comp`` named ``<container_name>``.

    Parameters
    ----------
    parent_comp:
        TD COMP that the new baseCOMP will be created under.
    legacy:
        The build_export_mcp_tox legacy helper module — supplies
        ``_create_with_fallback``. Passed in (rather than imported) so
        this module stays decoupled from the build-time bootstrap.
    repo_root:
        Absolute path to the repo root.
    src_subdir:
        Subdirectory under ``td_component/`` to scan for ``.md`` files
        (e.g. ``"skills"`` or ``"knowledge"``).
    container_name:
        Name for the baseCOMP that holds the baked textDATs.
    dat_prefix:
        Prefix for each baked textDAT — e.g. ``"skill_"`` produces
        ``skill_popx_mode`` for ``popx-mode.md``.
    node_xy:
        ``(nodeX, nodeY)`` for the new baseCOMP. Errors swallowed because
        the position is cosmetic.
    safe_stem:
        When True, replace ``-`` with ``_`` in the file stem so the DAT
        name is a valid Python identifier. Skills need this; knowledge
        entries already use snake_case filenames.

    Returns the baseCOMP container, or ``None`` when the source dir
    doesn't exist or has no ``.md`` files.
    """
    src_dir = os.path.join(repo_root, "td_component", src_subdir)
    if not os.path.isdir(src_dir):
        print(f"[tdpilot_API] {src_subdir}/ dir not found at {src_dir} — skipping bundle")
        return None

    md_files = sorted(_glob.glob(os.path.join(src_dir, "*.md")))
    if not md_files:
        print(f"[tdpilot_API] no .md files in {src_dir} — skipping bundle")
        return None

    container = legacy._create_with_fallback(parent_comp, ("baseCOMP",), container_name)
    try:
        container.nodeX, container.nodeY = node_xy
    except Exception:
        # Cosmetic — the layout coords aren't load-bearing for runtime
        # behaviour. Some TD versions disallow direct assignment when the
        # parent isn't fully realized yet.
        pass

    baked = 0
    for md_path in md_files:
        stem = os.path.splitext(os.path.basename(md_path))[0]
        if safe_stem:
            stem = stem.replace("-", "_")
        dat_name = f"{dat_prefix}{stem}"
        try:
            with open(md_path, encoding="utf-8") as f:
                text = f.read()
        except Exception as exc:
            print(f"[tdpilot_API] {src_subdir} read failed for {md_path}: {exc}")
            continue
        try:
            dat = container.create("textDAT", dat_name)
            dat.text = text
            baked += 1
        except Exception as exc:
            print(f"[tdpilot_API] {src_subdir} bake failed for {dat_name}: {exc}")

    print(f"[tdpilot_API] {src_subdir} corpus baked: {baked} entries")
    return container
