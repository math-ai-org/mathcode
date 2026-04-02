#!/usr/bin/env bash
# ============================================================================
# setup.sh — Set up Lean and Python for Math Code (binary release)
#
# Usage:
#   bash setup.sh
# ============================================================================
set -euo pipefail

DIST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Math Code setup"
echo ""

# --------------------------------------------------------------------------
# 1. .env
# --------------------------------------------------------------------------
if [[ ! -f "$DIST_DIR/.env" ]]; then
  cp "$DIST_DIR/.env.example" "$DIST_DIR/.env"
  echo "[OK] Created .env from template"
else
  echo "[OK] .env already exists"
fi

# --------------------------------------------------------------------------
# 2. Python venv for AUTOLEAN
# --------------------------------------------------------------------------
AUTOLEAN_DIR="$DIST_DIR/AUTOLEAN"
VENV_DIR="$AUTOLEAN_DIR/.venv"

if ! command -v python3 &>/dev/null; then
  echo "[!!] python3 not found. Please install Python 3.10+ and re-run."
  exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]; }; then
  echo "[!!] Python 3.10+ required (found $PY_VERSION)"
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating Python venv..."
  python3 -m venv "$VENV_DIR"
  echo "[OK] Created $VENV_DIR"
else
  echo "[OK] Python venv already exists"
fi

echo "==> Installing AUTOLEAN dependencies..."
"$VENV_DIR/bin/pip" install --quiet -e "$AUTOLEAN_DIR"
echo "[OK] AUTOLEAN installed"

# --------------------------------------------------------------------------
# 3. Lean toolchain
# --------------------------------------------------------------------------
LEAN_WS="$DIST_DIR/lean-workspace"

if command -v lean &>/dev/null; then
  echo "[OK] Lean found: $(lean --version 2>/dev/null | head -1)"
elif [[ -d "$DIST_DIR/.local/elan/bin" ]]; then
  echo "[OK] Local elan found at $DIST_DIR/.local/elan"
else
  echo "==> Installing elan (Lean version manager) locally..."
  export ELAN_HOME="$DIST_DIR/.local/elan"
  curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | \
    sh -s -- --default-toolchain none --no-modify-path -y
  export PATH="$ELAN_HOME/bin:$PATH"
  echo "[OK] elan installed to $ELAN_HOME"
fi

# Make sure lean is on PATH for the rest of the script
if [[ -d "$DIST_DIR/.local/elan/bin" ]]; then
  export ELAN_HOME="$DIST_DIR/.local/elan"
  export PATH="$ELAN_HOME/bin:$PATH"
fi

# --------------------------------------------------------------------------
# 4. Lean workspace / Mathlib cache
# --------------------------------------------------------------------------
if [[ ! -f "$LEAN_WS/lakefile.toml" ]]; then
  echo "[!!] lean-workspace not found. The release may be incomplete."
  exit 1
fi

echo "==> Building Lean workspace (this may take a while on first run)..."
cd "$LEAN_WS"

# Try to download Mathlib cache first
if command -v lake &>/dev/null; then
  if [[ "${MATHCODE_SKIP_MATHLIB_CACHE:-0}" != "1" ]]; then
    echo "    Downloading Mathlib cache..."
    lake exe cache get 2>/dev/null || echo "    (cache download skipped or failed, will build from source)"
  fi
  lake build || echo "[!!] lake build failed — Lean compilation may not work until resolved"
else
  echo "[!!] lake not found — skipping workspace build"
fi

cd "$DIST_DIR"

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
echo ""
echo "==> Setup complete!"
echo ""
echo "    To start Math Code:"
echo "      ./run"
echo ""
echo "    To authenticate:"
echo "      Start Math Code, then type /login"
echo ""
