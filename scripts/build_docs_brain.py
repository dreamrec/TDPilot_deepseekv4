#!/usr/bin/env python3
"""Build the TDPilot docs brain from scraped HTML files.

Usage:
    python scripts/build_docs_brain.py --source /path/to/docs.derivative.ca/
    python scripts/build_docs_brain.py  # uses TDPILOT_DOCS_SCRAPE_PATH env var
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Shared Chunk Schema v1 helpers — see docs/CHUNK_SCHEMA.md.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _chunk_schema_v1 import (  # noqa: E402
    DEFAULT_TRUST_TIER,
    build_v1_fts_index,
    enrich_to_v1,
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from td_mcp.knowledge.docsbrain.chunker import chunk_page, write_chunks_jsonl

# build_index from td_mcp.knowledge.docsbrain.indexer is replaced by the v1
# wrapper below (build_fts_index → build_v1_fts_index). Other files in
# src/td_mcp may still import build_index directly; only this script's call
# site is migrated.
from td_mcp.knowledge.docsbrain.normalizer import normalize_directory, write_pages_jsonl
from td_mcp.knowledge.docsbrain.release_parser import build_release_artifacts

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def build_fts_index(
    chunks_path: Path, db_path: Path, brain_id: str, *, trust_tier: str = DEFAULT_TRUST_TIER
) -> int:
    """Thin wrapper around the shared v1 indexer.

    See ``scripts/_chunk_schema_v1.build_v1_fts_index`` for the table
    layout. Kept as a function in this module so existing call sites
    don't break and so a future schema-v2 migration can be staged here
    without touching consumers.
    """
    return build_v1_fts_index(
        chunks_path,
        db_path,
        brain_id=brain_id,
        trust_tier=trust_tier,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the TDPilot docs brain")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Path to scraped docs.derivative.ca HTML files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "normalized" / "derivative",
        help="Output directory for generated files",
    )
    args = parser.parse_args()

    # Hard-coded identity for this single-purpose builder.
    brain_id = "derivative"
    trust_tier = "official"

    # Resolve source path
    source = args.source
    if source is None:
        env_path = os.environ.get("TDPILOT_DOCS_SCRAPE_PATH")
        if env_path:
            source = Path(env_path)
        else:
            logger.error("No source path. Use --source or set TDPILOT_DOCS_SCRAPE_PATH")
            sys.exit(1)

    if not source.is_dir():
        logger.error("Source directory not found: %s", source)
        sys.exit(1)

    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    t0 = time.time()

    # Stage 1: Normalize
    logger.info("Stage 1: Normalizing HTML files from %s", source)
    pages_path = output / "pages.jsonl"
    pages = normalize_directory(source)
    page_count = write_pages_jsonl(pages, pages_path)
    logger.info("  → %d pages normalized", page_count)

    # Stage 2: Chunk
    logger.info("Stage 2: Chunking pages")
    all_chunks: list[dict] = []
    with open(pages_path, encoding="utf-8") as f:
        for line in f:
            page = json.loads(line)
            # Reconstruct the HTML path from page URL
            page_name = page["url"].replace("https://docs.derivative.ca/", "")
            html_path = source / f"{page_name}.html"
            if html_path.exists():
                chunks = chunk_page(page, html_path)
                all_chunks.extend(chunks)

    # Enrich every chunk to Chunk Schema v1 before writing JSONL.
    # The td_mcp chunker emits v0-shaped dicts; enrich_to_v1 fills in
    # trust_tier, text_hash, schema_version, title, url, source.
    # enrich_to_v1 is idempotent — safe to call on already-v1 chunks.
    all_chunks = [
        enrich_to_v1(chunk, trust_tier=trust_tier, source="html", brain_id=brain_id) for chunk in all_chunks
    ]

    chunks_path = output / "chunks.jsonl"
    chunk_count = write_chunks_jsonl(all_chunks, chunks_path)
    logger.info("  → %d chunks created", chunk_count)

    # Stage 3: Index — Chunk Schema v1 (see docs/CHUNK_SCHEMA.md).
    logger.info("Stage 3: Building FTS5 index (Chunk Schema v1, trust_tier=%s)", trust_tier)
    db_path = output / "docsbrain.db"
    indexed = build_fts_index(chunks_path, db_path, brain_id, trust_tier=trust_tier)
    logger.info("  → %d chunks indexed", indexed)

    # Stage 4: Release notes
    logger.info("Stage 4: Building release note artifacts")
    manifest, changelog = build_release_artifacts(chunks_path, output)
    logger.info(
        "  → %d builds, %d operators with changelog",
        len(manifest.get("builds", [])),
        len(changelog),
    )

    elapsed = time.time() - t0
    logger.info("Done in %.1fs. Output: %s", elapsed, output)


if __name__ == "__main__":
    main()
