"""PR-16 regression oracle for the mcp_webserver_callbacks split.

Before v1.8.3 the .tox baked a single 3149-line ``mcp_webserver_callbacks.py``
into a textDAT. PR-16 splits that file into ``td_component/callbacks/`` for git
diffability (audit finding F-14) and uses
:func:`td_component.callbacks._composer.compose_bytes` to reconstitute the textDAT
body at .tox-build time.

The contract these tests enforce:

1. **Byte-equivalence to the captured baseline** — the v1.8.2 god module is
   frozen at ``tests/fixtures/mcp_webserver_callbacks_v1.8.2_baseline.py``.
   The composer must reproduce it verbatim *except* for the API_VERSION
   line, which scripts/check_versions.py forces to track __version__. The
   test patches the baseline's API_VERSION to the current package version
   before comparing, so this stays a strong oracle for "no OTHER byte
   drifted" without breaking on the legitimate per-release bump.
2. **Symbol parity at exec-time** — exec'ing the composed source produces
   exactly the same module-level names as exec'ing the baseline (no
   accidental drops, no shadowing).
3. **Module-constant value parity** — ``RESTRICTED_TOKENS``,
   ``STANDARD_BLOCKED_TOKENS``, etc. carry forward identically. This
   catches whitespace/encoding drift that bytes-level identity already
   implies but is the audit's explicit acceptance gate per the PR-16
   brief. ``API_VERSION`` is excluded from this set for the same reason
   as (1).
4. **COMPOSE_ORDER stability** — drift in the slice ordering would change
   the textDAT layout silently; the test pins the canonical order.

Future PRs are free to break (1) and (2) the moment a split file changes;
when that happens, refresh the baseline fixture in the same PR. The tests
exist to detect *unintentional* drift.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / "tests" / "fixtures" / "mcp_webserver_callbacks_v1.8.2_baseline.py"
# The single line scripts/check_versions.py forces to track __version__.
# Patching this in the baseline before comparing lets us keep the rest of
# the byte-level oracle intact across version bumps.
_BASELINE_API_VERSION_LINE = b'API_VERSION = "1.8.2"\n'


def _current_package_version() -> str:
    """Return the version recorded in ``pyproject.toml``."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("could not parse version from pyproject.toml")


def _patched_baseline_bytes() -> bytes:
    """Baseline bytes with API_VERSION rewritten to the current __version__."""
    raw = BASELINE.read_bytes()
    new_line = f'API_VERSION = "{_current_package_version()}"\n'.encode()
    if _BASELINE_API_VERSION_LINE not in raw:
        raise RuntimeError(
            f"baseline fixture missing the expected API_VERSION line "
            f"({_BASELINE_API_VERSION_LINE!r}); refresh the fixture"
        )
    return raw.replace(_BASELINE_API_VERSION_LINE, new_line, 1)


def _load_composer():
    """Load the composer without going through td_component.__init__."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from td_component.callbacks import _composer  # noqa: PLC0415

        return _composer
    finally:
        if str(REPO_ROOT) in sys.path:
            sys.path.remove(str(REPO_ROOT))


def _exec_to_namespace(source: str) -> dict:
    """Exec ``source`` in a sandbox namespace that mocks TD globals.

    The original god module uses ``op``, ``project``, ``app`` etc. only
    INSIDE function bodies. Module-level execution touches stdlib + the
    env-reader helpers, neither of which need TD. So an empty namespace
    suffices — anything else is a regression.
    """
    ns: dict = {"__name__": "td_callbacks_under_test"}
    exec(compile(source, "<composed>", "exec"), ns)
    return ns


def test_baseline_fixture_exists():
    """The v1.8.2 baseline must be checked in — without it the suite is moot."""
    assert BASELINE.is_file(), f"missing baseline fixture: {BASELINE}"
    assert BASELINE.stat().st_size > 100_000, "baseline fixture is suspiciously small"


def test_compose_bytes_matches_baseline():
    """compose() output must be byte-identical to the patched v1.8.2 baseline.

    "Patched" means the API_VERSION line is rewritten to match the current
    pyproject.toml version — that's the one expected difference per
    scripts/check_versions.py. Any OTHER byte drift fails the test.
    """
    composer = _load_composer()
    composed = composer.compose_bytes()
    baseline = _patched_baseline_bytes()
    assert len(composed) == len(baseline), (
        f"composer length drift: composed={len(composed)} baseline={len(baseline)}"
    )
    assert composed == baseline, "composer output diverged from patched v1.8.2 baseline"


def test_compose_str_round_trips_through_utf8():
    """compose() must return the same content as compose_bytes() decoded as UTF-8."""
    composer = _load_composer()
    assert composer.compose().encode("utf-8") == composer.compose_bytes()


def test_compose_order_pins_split_layout():
    """Slice order is part of the .tox contract — pin it to detect drift."""
    composer = _load_composer()
    assert composer.COMPOSE_ORDER == (
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


def test_source_paths_resolve_to_real_files():
    composer = _load_composer()
    paths = composer.source_paths()
    assert len(paths) == len(composer.COMPOSE_ORDER)
    for path in paths:
        assert path.is_file(), f"source_paths() lists missing file: {path}"


def test_split_files_concatenate_to_compose_bytes():
    """source_paths() concat must equal compose_bytes() — no hidden transforms."""
    composer = _load_composer()
    concat = b"".join(p.read_bytes() for p in composer.source_paths())
    assert concat == composer.compose_bytes()


# ---------------------------------------------------------------------------
# Symbol parity — exec composed source vs baseline source, compare namespaces
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline_namespace(monkeypatch_module):  # noqa: ARG001 (monkeypatch handled via os.environ)
    return _exec_to_namespace(BASELINE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def composed_namespace(monkeypatch_module):  # noqa: ARG001
    composer = _load_composer()
    return _exec_to_namespace(composer.compose())


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped env hygiene so module-level aliases (SHARED_SECRET etc.)
    capture the same values for both baseline + composed loads."""
    saved = {
        k: os.environ.get(k)
        for k in (
            "TD_MCP_SHARED_SECRET",
            "TD_MCP_REQUIRE_AUTH",
            "TD_MCP_CORS_ORIGIN",
            "TD_MCP_EXEC_MODE",
        )
    }
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _public_names(ns: dict) -> set[str]:
    return {k for k in ns if not k.startswith("__")}


def test_module_level_symbols_match(baseline_namespace, composed_namespace):
    only_in_composed = _public_names(composed_namespace) - _public_names(baseline_namespace)
    only_in_baseline = _public_names(baseline_namespace) - _public_names(composed_namespace)
    assert not only_in_composed, f"composed gained unexpected symbols: {sorted(only_in_composed)}"
    assert not only_in_baseline, f"composed lost expected symbols: {sorted(only_in_baseline)}"


@pytest.mark.parametrize(
    "constant",
    [
        # API_VERSION intentionally excluded — it's allowed (and required by
        # scripts/check_versions.py) to track the package version and so will
        # legitimately diverge from the v1.8.2 baseline at every release.
        "RESTRICTED_IMPORT_RE",
        "RESTRICTED_TOKENS",
        "STANDARD_ALLOWED_IMPORTS",
        "STANDARD_BLOCKED_TOKENS",
        "MONITOR_SUBSCRIPTIONS",
    ],
)
def test_module_constants_carry_forward(baseline_namespace, composed_namespace, constant):
    base_val = baseline_namespace[constant]
    comp_val = composed_namespace[constant]
    if hasattr(base_val, "pattern"):  # compiled regex
        assert comp_val.pattern == base_val.pattern
    else:
        assert comp_val == base_val


def test_api_version_tracks_package_version(composed_namespace):
    """API_VERSION must equal the current pyproject.toml version.

    This used to live in tests/test_startup_sweep.py; surface it here too
    so the byte-equivalence module captures the FULL contract for the
    composer's output (the one place where API_VERSION is allowed to
    diverge from the baseline).
    """
    assert composed_namespace["API_VERSION"] == _current_package_version()


@pytest.mark.parametrize(
    "handler",
    [
        # router + auth helpers
        "onHTTPRequest",
        "_extract_headers",
        "_check_auth_error",
        "_constant_time_equals",
        "_send_json",
        # serializers
        "_serialize_op",
        "_serialize_params",
        # node CRUD
        "handle_health",
        "handle_info",
        "handle_get_nodes",
        "handle_get_node_detail",
        "handle_get_params",
        "handle_set_params",
        "handle_create_node",
        "handle_delete_node",
        "handle_connect_nodes",
        "handle_disconnect_nodes",
        "handle_get_connections",
        "handle_get_errors",
        "handle_get_content",
        "handle_set_content",
        "handle_copy_node",
        "handle_rename_node",
        # exec + custom params
        "handle_custom_parameters",
        "_normalize_for_check",
        "_restricted_exec_violation",
        "_standard_exec_violation",
        "_build_exec_globals",
        "_json_safe_value",
        "_serialize_exec_result",
        "_safe_td_call",
        "_create_custom_parameter",
        "handle_exec_python",
        # inspection
        "handle_screenshot",
        "handle_chop_data",
        "handle_geometry_data",
        "handle_pop_inspect",
        "handle_cooking_info",
        "handle_analyze_frame",
        # search + python help
        "handle_search_nodes",
        "handle_list_families",
        "handle_python_help",
        "handle_python_classes",
        # lifecycle + pulse
        "handle_project_lifecycle",
        "handle_timeline",
        "handle_timeline_set",
        "handle_pulse_param",
        # monitor
        "handle_monitor_subscribe",
        "handle_monitor_unsubscribe",
        "_render_chop_callback",
        "_render_chop_poll_callback",
        "_render_par_callback",
        "_render_runtime_callback",
        "_provision_monitor_nodes",
    ],
)
def test_handler_or_helper_is_callable(composed_namespace, handler):
    assert callable(composed_namespace.get(handler)), f"composed module is missing callable: {handler}"


def test_route_table_keys_match_baseline(baseline_namespace, composed_namespace):
    """The /api/* route map is built inside onHTTPRequest. We can't run the
    function (it needs TD globals + a real request), so we extract the
    route literals from the source text instead. If a handler is dropped
    from the route table, audit visibility goes to zero."""
    baseline_src = BASELINE.read_text(encoding="utf-8")
    composer = _load_composer()
    composed_src = composer.compose()

    def _extract_routes(src: str) -> set[str]:
        import re

        return set(re.findall(r"'(/api/[a-z/_-]+)':", src))

    base_routes = _extract_routes(baseline_src)
    comp_routes = _extract_routes(composed_src)
    assert base_routes, "baseline contained no /api/* routes — extraction is broken"
    assert comp_routes == base_routes
