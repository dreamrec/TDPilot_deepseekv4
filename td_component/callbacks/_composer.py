"""Compose the split source files into the textDAT body baked into the .tox.

The output of :func:`compose` is set as the ``mcp_webserver_callbacks``
textDAT's text inside the ``mcp_server`` COMP at build time — it must
exec cleanly inside TouchDesigner with TD globals in scope (``op``,
``parent``, etc.) and expose all the handlers + helpers the runtime
expects.

PR-16 contract: compose() output is byte-identical to the pre-split
``td_component/mcp_webserver_callbacks.py`` snapshot captured at v1.8.2.
The split files contain raw source slices, each ending with a newline,
so concatenation is the entire transformation. Adding cross-file imports
or whitespace transforms here would break the byte-equivalence test in
``tests/test_composer_byte_equivalence.py``.
"""

from __future__ import annotations

from pathlib import Path

# Order is load-bearing: a lot of helpers in later files reference module
# constants and helpers defined in earlier files. The runtime sees a single
# flat module, so order = file definition order = original god-module order.
COMPOSE_ORDER = (
    "_header.py",
    "router.py",
    "auth.py",
    "serializers.py",
    "handlers/nodes.py",
    "handlers/exec_and_custom_params.py",
    "handlers/exec_python.py",
    "handlers/inspect.py",
    "handlers/search.py",
    "handlers/lifecycle.py",
    "handlers/pulse.py",
    "handlers/monitor.py",
    "handlers/analyze_frame.py",
)


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def compose() -> str:
    """Return the composed textDAT body as a UTF-8 string."""
    return compose_bytes().decode("utf-8")


def compose_bytes() -> bytes:
    """Return the composed textDAT body as raw bytes.

    Build scripts and the .tox-source-hash gate prefer bytes (hash + write
    paths are byte-oriented), and reading the splits as bytes guarantees
    no platform-dependent newline translation slips in.
    """
    base = _package_dir()
    chunks: list[bytes] = []
    for rel in COMPOSE_ORDER:
        path = base / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing split source: {path}")
        chunks.append(path.read_bytes())
    return b"".join(chunks)


def source_paths() -> list[Path]:
    """Return absolute paths of the split files in compose order.

    Used by the .tox-source-hash gate so it hashes the splits, not the
    deleted god module. Hashing splits + COMPOSE_ORDER membership keeps
    the freshness check semantically equivalent to the pre-PR-16 hash:
    any byte change in any split bumps the hash and forces a .tox
    rebuild.
    """
    base = _package_dir()
    return [base / rel for rel in COMPOSE_ORDER]
