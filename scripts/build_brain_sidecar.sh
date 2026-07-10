#!/usr/bin/env bash
#
# scripts/build_brain_sidecar.sh
# ==============================
# Package the Python Brain (brain/cli.py) into a single self-contained binary
# with PyInstaller, then place it where Tauri expects a sidecar (with the
# target-triple suffix Tauri requires) so it ships inside the .app / installer.
#
# After running this, add to tauri.conf.json:
#
#   "bundle": { "externalBin": ["binaries/khaleej-brain"] }
#
# and Tauri bundles binaries/khaleej-brain-<triple> automatically. The Rust
# backend then launches it via KHALEEJNODE_BRAIN instead of python3.
#
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Ensure the Rust toolchain is reachable (needed for the target-triple suffix).
export PATH="$HOME/.cargo/bin:$PATH"

# Use the project venv.
if [ ! -x ".venv/bin/python" ]; then
  echo "  No .venv found. Run ./setup.sh --no-launch first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --quiet pyinstaller

echo "  Building khaleej-brain sidecar with PyInstaller…"
# pydantic v2 ships a compiled `pydantic_core` extension + package metadata that
# PyInstaller must be told to collect, or the frozen binary fails at import time.
pyinstaller \
  --onefile \
  --noconfirm \
  --name khaleej-brain \
  --distpath src-tauri/binaries \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  --collect-all pydantic \
  --collect-all pydantic_core \
  --collect-all pymupdf \
  --collect-all fitz \
  --collect-all spellchecker \
  --add-data "$ROOT_DIR/brain/compliance/data:brain/compliance/data" \
  brain/cli.py

# Tauri sidecars must be suffixed with the Rust target triple.
TRIPLE="$(rustc -Vv 2>/dev/null | sed -n 's/^host: //p')"
if [ -z "$TRIPLE" ]; then
  # Fallback if rustc is unavailable: derive from uname.
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) TRIPLE="aarch64-apple-darwin" ;;
    Darwin-x86_64) TRIPLE="x86_64-apple-darwin" ;;
    Linux-x86_64) TRIPLE="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;;
    *) echo "  Could not determine target triple; set it manually." >&2; exit 1 ;;
  esac
fi
SRC="src-tauri/binaries/khaleej-brain"
DST="src-tauri/binaries/khaleej-brain-${TRIPLE}"
if [ -f "$SRC" ]; then
  cp "$SRC" "$DST"
  echo "  Sidecar ready: $DST"
else
  echo "  PyInstaller did not produce $SRC" >&2
  exit 1
fi

echo ""
echo "  Next: add \"externalBin\": [\"binaries/khaleej-brain\"] to tauri.conf.json bundle,"
echo "  and the Rust backend will use it via KHALEEJNODE_BRAIN automatically."
