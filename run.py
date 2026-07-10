#!/usr/bin/env python3
"""
run.py
======
Single-command bootstrapper + launcher for KhaleejNode.

On first run it:
  1. ensures the runtime state/ directory exists,
  2. generates the vendor Ed25519 keypair (idempotent; HMAC fallback if the
     `cryptography` package is absent),
  3. mints a node-locked license.key bound to THIS machine's hardware so the
     app boots UNLOCKED for the builder,
  4. initializes the tamper-evident token vault with a free credit grant,
  5. opens the dashboard in the default browser and serves the loopback API.

Every subsequent run is idempotent: an existing valid license and vault are
reused untouched. Nothing here touches the network.

Usage:
    python run.py            # bootstrap + launch on :8787
    python run.py --port 9000
    python run.py --no-browser
    python run.py --reset    # wipe state/ and re-bootstrap from scratch
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
import webbrowser

from core import config, license as licensing, vault
from core.hardware import resolve_identity
from server import app as server_app
from tools import keygen


def bootstrap() -> str:
    """Prepare license + vault so the app is immediately usable. Returns hw id."""
    config.ensure_state_dir()

    # (2) Vendor keypair for offline token verification.
    keygen.generate(force=False)

    ident = resolve_identity()
    print(f"  Hardware ID : {ident.hardware_id}")
    print(f"  Motherboard : {ident.motherboard_uuid}")
    print(f"  Primary MAC : {ident.mac_address}")

    # (3) Node-locked license bound to THIS machine.
    status = licensing.verify_license()
    if status.activated:
        print("  License     : valid (existing) ✓")
    else:
        licensing.generate_license(ident.hardware_id)
        print("  License     : generated & node-locked to this machine ✓")

    # (4) Tamper-evident vault with a starter grant.
    try:
        state = vault.read_state(ident.hardware_id)
        print(f"  Vault       : existing, balance {state.balance} ✓")
    except vault.VaultError:
        state = vault.initialize(ident.hardware_id, config.FIRST_ACTIVATION_GRANT)
        print(f"  Vault       : initialized with {state.balance} free credits ✓")

    return ident.hardware_id


def reset_state() -> None:
    if config.STATE_DIR.exists():
        shutil.rmtree(config.STATE_DIR)
        print("  Wiped state/ — will re-bootstrap from scratch.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bootstrap and launch KhaleejNode.")
    ap.add_argument("--port", type=int, default=server_app.DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reset", action="store_true", help="wipe state/ first")
    args = ap.parse_args()

    print("\n=== KhaleejNode bootstrap ===")
    if args.reset:
        reset_state()

    try:
        bootstrap()
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    url = f"http://{server_app.HOST}:{args.port}"
    if not args.no_browser:
        def _open():
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    print("=============================\n")
    server_app.serve(args.port)


if __name__ == "__main__":
    main()
