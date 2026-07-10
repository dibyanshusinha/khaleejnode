#!/usr/bin/env bash
#
# run_tauri.sh — launch the KhaleejNode Tauri desktop app in development.
#
# Backend  : native Rust Shield (core-rs) compiled into the Tauri binary.
# Brain    : Python (brain/cli.py) invoked out-of-process. In dev we point the
#            Rust backend at this repo's virtualenv Python so pydantic is found.
# Frontend : ui/ served directly by Tauri (no JS dev server needed).
#
# Prereqs (one time):
#   - Rust toolchain (rustup)        -> https://rustup.rs
#   - Node + npm (for the Tauri CLI) -> `npm install`
#   - Python venv with deps          -> `./setup.sh --no-launch`  (creates .venv)
#
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PATH="$HOME/.cargo/bin:$PATH"

# Point the Rust backend's Brain bridge at the venv Python if present.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  export KHALEEJNODE_PYTHON="$ROOT_DIR/.venv/bin/python"
fi
export KHALEEJNODE_BRAIN_CLI="$ROOT_DIR/brain/cli.py"

# Warn (don't block) if the local vision model server isn't reachable — the
# Brain will otherwise silently fall back to the mock simulator.
VISION_MODEL="${KHALEEJNODE_VISION_MODEL:-qwen2.5vl:7b}"
if curl -s -m 2 http://localhost:11434/api/tags 2>/dev/null | grep -q "$VISION_MODEL"; then
  echo "  Vision       : ${VISION_MODEL} reachable (real extraction) ✓"
else
  echo "  Vision       : ⚠  Ollama/${VISION_MODEL} not reachable — Brain will use the MOCK."
  echo "                 Start it with: brew services start ollama && ollama pull ${VISION_MODEL}"
fi

echo "  Launching KhaleejNode (Tauri dev)…"
echo "  Brain Python : ${KHALEEJNODE_PYTHON:-python3}"
echo ""

exec npx tauri dev
