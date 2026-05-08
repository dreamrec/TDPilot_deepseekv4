#!/bin/bash
# ============================================================================
#  TDPilot DPSK4 — macOS Installer (DeepSeek v4 optimized)
#  Run: bash install.sh
# ============================================================================

set -euo pipefail

REPO_URL="https://github.com/dreamrec/TDPilot_deepseekv4.git"
REPO_DIR_NAME=".tdpilot-dpsk4"

echo ""
echo "  TDPilot DPSK4 — Installer for macOS (DeepSeek v4)"
echo "  ==================================================="
echo ""

# ---------- Step 1: Check / Install uv ----------

echo "[1/4] Checking for uv..."

UV_PINNED_VERSION="${TDPILOT_UV_VERSION:-0.6.10}"

if command -v uv &>/dev/null; then
    UV_PATH=$(which uv)
    echo "  Found uv: $(uv --version) at $UV_PATH"
else
    echo "  uv not found. Installing pinned version ${UV_PINNED_VERSION}..."
    if [ "$UV_PINNED_VERSION" = "latest" ]; then
        UV_INSTALL_URL="https://astral.sh/uv/install.sh"
    else
        UV_INSTALL_URL="https://astral.sh/uv/${UV_PINNED_VERSION}/install.sh"
    fi
    curl -LsSf "$UV_INSTALL_URL" | sh 2>&1

    export PATH="$HOME/.local/bin:$PATH"

    if command -v uv &>/dev/null; then
        UV_PATH=$(which uv)
        echo "  uv installed: $UV_PATH"
    else
        echo "  ERROR: uv installed but not found in PATH."
        echo "  Close this terminal, open a new one, and run this script again."
        exit 1
    fi
fi

UV_PATH=$(which uv)

# ---------- Step 2: Locate or clone the repo ----------

echo ""
echo "[2/4] Setting up repository..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    REPO_PATH="$SCRIPT_DIR"
    echo "  Running from repo: $REPO_PATH"
else
    INSTALL_DIR="$HOME/$REPO_DIR_NAME"

    if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
        REPO_PATH="$INSTALL_DIR"
        echo "  Found existing install: $REPO_PATH"
    else
        echo "  Cloning to: $INSTALL_DIR"
        if command -v git &>/dev/null; then
            git clone "$REPO_URL" "$INSTALL_DIR" 2>&1
            LATEST_TAG="$( cd "$INSTALL_DIR" && git describe --tags --abbrev=0 2>/dev/null || true )"
            if [ -n "$LATEST_TAG" ]; then
                if ( cd "$INSTALL_DIR" && git checkout "$LATEST_TAG" >/dev/null 2>&1 ); then
                    echo "  Pinned to $LATEST_TAG"
                else
                    echo "  WARN: Could not check out $LATEST_TAG; staying on main"
                fi
            else
                echo "  WARN: No release tag found upstream; staying on main"
            fi
        else
            echo "  git not found — downloading ZIP..."
            ZIP_URL="https://github.com/dreamrec/TDPilot_deepseekv4/archive/refs/heads/main.zip"
            ZIP_PATH="/tmp/td-mcp.zip"
            curl -L -o "$ZIP_PATH" "$ZIP_URL"
            unzip -q "$ZIP_PATH" -d /tmp/td-mcp-extract
            # v2.0.1 security audit fix: GitHub extracts the archive as
            # ``<github-repo-name>-<branch>``, i.e. ``TDPilot_deepseekv4-main``.
            # The previous code looked for ``${REPO_DIR_NAME}-main`` which
            # interpolates to ``.tdpilot-dpsk4-main`` (the LOCAL install
            # directory naming, not the GitHub archive convention). Anyone
            # without git in PATH hit a no-such-file error here. Use the
            # actual extracted dir name; fall back to a wildcard for forks.
            EXTRACTED_DIR="/tmp/td-mcp-extract/TDPilot_deepseekv4-main"
            if [ ! -d "$EXTRACTED_DIR" ]; then
                # Fork or future archive-naming change — pick the only dir.
                EXTRACTED_DIR="$(find /tmp/td-mcp-extract -mindepth 1 -maxdepth 1 -type d | head -1)"
            fi
            mv "$EXTRACTED_DIR" "$INSTALL_DIR"
            rm -f "$ZIP_PATH"
            rm -rf /tmp/td-mcp-extract
        fi
        REPO_PATH="$INSTALL_DIR"
        echo "  Downloaded to: $REPO_PATH"
    fi
fi

# ---------- Step 3: Configure MCP ----------

echo ""
echo "[3/4] Configuring MCP for DeepSeek v4 / Claude Code CLI..."

MCP_CONFIG="$REPO_PATH/.mcp.json"

# Backup existing config
if [ -f "$MCP_CONFIG" ]; then
    BACKUP_PATH="${MCP_CONFIG}.backup_$(date +%Y%m%d_%H%M%S)"
    cp "$MCP_CONFIG" "$BACKUP_PATH"
    echo "  Backed up config to: $BACKUP_PATH"
fi

python3 -c "
import json, os

config_path = '$MCP_CONFIG'
repo_path = '$REPO_PATH'
uv_path = '$UV_PATH'

config = {}
if os.path.exists(config_path):
    try:
        with open(config_path) as f:
            text = f.read().strip()
            config = json.loads(text) if text else {}
    except (json.JSONDecodeError, ValueError):
        print('  WARNING: Existing config has invalid JSON. Creating fresh config.')
        config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['touchdesigner-dpsk4'] = {
    'command': uv_path,
    'args': ['run', '--directory', repo_path, 'tdpilot-dpsk4'],
    'env': {
        'TD_MCP_HOST': '127.0.0.1',
        'TD_MCP_PORT': '9985',
        'TD_MCP_WS_PORT': '9986',
        'TD_MCP_EXEC_MODE': 'restricted',
        'TDPILOT_VARIANT': 'dpsk4',
    }
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)
os.chmod(config_path, 0o644)

print('  Config updated: ' + config_path)
"

# ---------- Step 4: Summary ----------

echo ""
echo "[4/4] Done!"
echo ""
echo "  ========================================"
echo "  INSTALL COMPLETE — TDPilot DPSK4"
echo "  ========================================"
echo ""
echo "  Repo location:   $REPO_PATH"
echo "  MCP config:      $REPO_PATH/.mcp.json"
echo "  uv path:         $UV_PATH"
echo ""
echo "  NEXT STEPS:"
echo "  1. Restart Claude Code CLI in this directory"
echo "  2. Open TouchDesigner and load the component (once per session):"
echo "     Option A: Drag td_component/tdpilot-dpsk4.tox into /local"
echo "     Option B: Run in Textport:"
echo "       exec(open(\"$REPO_PATH/setup_mcp_in_td.py\").read(), globals(), globals())"
echo "  3. Ask your AI client: 'What's in my TouchDesigner project?'"
echo ""
echo "  Installing into /local means TDPilot DPSK4 persists across project opens."
echo ""
