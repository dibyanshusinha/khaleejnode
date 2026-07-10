#!/usr/bin/env bash
#
# setup.sh — one-command initializer for KhaleejNode.
#
# Creates a local virtualenv, installs dependencies, generates the vendor
# keypair, mints a node-locked license for THIS machine, seeds the token vault,
# and launches the offline dashboard.
#
# Usage:
#   ./setup.sh              # full setup + launch
#   ./setup.sh --no-launch  # set up only, do not start the server
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PY="${PYTHON:-python3}"
VENV_DIR=".venv"

echo ""
echo "==================================================="
echo "  KhaleejNode — Enterprise TradeTech Node  setup"
echo "==================================================="
echo ""

# 1. Python check --------------------------------------------------------
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "  ERROR: python3 not found on PATH. Install Python 3.9+ and retry." >&2
  exit 1
fi
echo "  Using interpreter: $("$PY" --version 2>&1)"

# 2. Virtualenv ----------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
  echo "  Creating virtualenv in $VENV_DIR ..."
  "$PY" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 3. Dependencies --------------------------------------------------------
echo "  Installing dependencies (pydantic, cryptography) ..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || {
  echo "  WARNING: dependency install failed; app will use built-in fallbacks." >&2
}

# 4. Bootstrap + launch --------------------------------------------------
if [ "${1:-}" = "--no-launch" ]; then
  echo "  Bootstrapping (no launch) ..."
  python - <<'PYEOF'
from run import bootstrap
bootstrap()
print("\n  Setup complete. Launch later with:  python run.py")
PYEOF
else
  echo "  Launching KhaleejNode ..."
  echo ""
  exec python run.py
fi
