"""
TDPilot API auto-load startup script for TouchDesigner.

Place in ~/Documents/Derivative/Startup/ — TD scans this directory at
launch, BEFORE the .toe loads. We use the same fast-path-or-rebuild
pattern as tdpilot_dpsk4_startup.py, but for the standalone API .tox
that runs entirely in-process (no MCP, no external CLI).

Source path resolution: ~/.tdpilot-api_path holds the repo root the
user installed from. If absent we silently no-op — TD users who don't
want the standalone variant aren't affected.

Coexistence with tdpilot-dpsk4: BOTH startup scripts can live side by
side in the Startup folder. Each loads its own COMP (tdpilot_dpsk4 vs
tdpilot_API) into /local. Their port usage doesn't overlap because the
API variant doesn't bind any port.
"""

import glob
import os

_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".tdpilot-api_path")
_HOME_ENV_FILE = os.path.join(os.path.expanduser("~"), ".tdpilot-api", ".env")
_TOX_RELATIVE = os.path.join("td_component", "tdpilot_API.tox")
_BUILD_SCRIPT_RELATIVE = os.path.join("td_component", "build_tdpilot_api_tox.py")
_COMP_NAMES = ("tdpilot_API", "tdpilot_api")
_SCAN_PARENTS = ("/local", "/project1")
_MARKER_FILES = [
    "pyproject.toml",
    os.path.join("td_component", "tdpilot_api_agent.py"),
    os.path.join("td_component", "mcp_webserver_callbacks.py"),
]
_SOURCE_GLOB = os.path.join("td_component", "tdpilot_api_*.py")
_HANDLERS_FILE = os.path.join("td_component", "mcp_webserver_callbacks.py")


def _read_config():
    if not os.path.isfile(_CONFIG_FILE):
        return None
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        return f.read().strip() or None


def _load_env_file(path):
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError as exc:
        print(f"[tdpilot_API] Could not read {path}: {exc}")


def _validate_repo(repo_root):
    return all(os.path.isfile(os.path.join(repo_root, m)) for m in _MARKER_FILES)


def _is_tox_stale(repo_root, tox_path):
    try:
        tox_mtime = os.path.getmtime(tox_path)
    except OSError:
        return True
    candidates = list(glob.glob(os.path.join(repo_root, _SOURCE_GLOB)))
    candidates.append(os.path.join(repo_root, _HANDLERS_FILE))
    for src in candidates:
        try:
            if os.path.getmtime(src) > tox_mtime:
                return True
        except OSError:
            continue
    return False


def _find_existing_comps():
    found = []
    for parent_path in _SCAN_PARENTS:
        parent = op(parent_path)
        if parent is None or not hasattr(parent, "op"):
            continue
        for name in _COMP_NAMES:
            cand = parent.op(name)
            if cand is not None and getattr(cand, "isCOMP", False):
                found.append((parent_path, cand.path))
    return found


def _load_tox_fast(tox_path):
    existing = _find_existing_comps()
    target_parent_path = existing[0][0] if existing else "/local"
    target_parent = op(target_parent_path)
    if target_parent is None or not getattr(target_parent, "isCOMP", False):
        print(f"[tdpilot_API] target parent {target_parent_path} not found")
        return False
    for _p, comp_path in existing:
        try:
            print(f"[tdpilot_API] destroying stale {comp_path}")
            op(comp_path).destroy()
        except Exception as e:
            print(f"[tdpilot_API] destroy failed {comp_path}: {e}")
    try:
        loaded = target_parent.loadTox(tox_path)
        if loaded is not None:
            print(f"[tdpilot_API] loaded into {loaded.path}")
            return True
    except Exception as e:
        print(f"[tdpilot_API] loadTox failed ({e}); falling back to rebuild")
    return False


def _rebuild_from_source(repo_root):
    build_script = os.path.join(repo_root, _BUILD_SCRIPT_RELATIVE)
    if not os.path.isfile(build_script):
        print(f"[tdpilot_API] build script not found: {build_script}")
        return False
    os.environ["TD_MCP_REPO_ROOT"] = repo_root
    os.environ.pop("TD_MCP_PARENT_PATH", None)
    print("[tdpilot_API] rebuilding from source...")
    with open(build_script, encoding="utf-8") as f:
        source = f.read()
    prev_file = globals().get("__file__", None)
    globals()["__file__"] = build_script
    try:
        exec(compile(source, build_script, "exec"), globals(), globals())  # noqa: S102
    finally:
        if prev_file is None:
            globals().pop("__file__", None)
        else:
            globals()["__file__"] = prev_file
    return True


def _startup():
    repo_root = _read_config()
    if repo_root is None:
        return  # not installed
    if not os.path.isdir(repo_root):
        print(f"[tdpilot_API] repo not found at {repo_root}")
        return
    if not _validate_repo(repo_root):
        print(f"[tdpilot_API] invalid repo at {repo_root}")
        return

    _load_env_file(_HOME_ENV_FILE)

    tox_path = os.path.join(repo_root, _TOX_RELATIVE)
    if os.path.isfile(tox_path) and not _is_tox_stale(repo_root, tox_path):
        if _load_tox_fast(tox_path):
            return
    _rebuild_from_source(repo_root)


if os.environ.get("TDPILOT_API_STARTUP_SKIP") != "1":
    try:
        _startup()
    except Exception as e:
        print(f"[tdpilot_API] startup error: {e}")
