"""
tools/issue_token.py
=====================
VENDOR tooling. Mint a signed, offline, single-use credit token.

The customer pastes the printed string into KhaleejNode's "Add Prepaid Credits"
field. The client verifies it with the bundled public key (or HMAC fallback)
and credits the tamper-evident vault.

Usage:
    python tools/issue_token.py --package starter
    python tools/issue_token.py --credits 42
    python tools/issue_token.py --package enterprise --print-json

Token format:  base64url(payload_json) + "." + base64url(signature)
Payload:       {credits, nonce, package, issued, expires}
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running directly as `python tools/issue_token.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(payload_bytes: bytes) -> bytes:
    """Sign with Ed25519 private key if available, else HMAC fallback."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        priv = load_pem_private_key(config.VENDOR_PRIVATE_KEY_FILE.read_bytes(), password=None)
        return priv.sign(payload_bytes)
    except Exception:
        import hashlib
        import hmac

        return hmac.new(config.SECRET_SALT, payload_bytes, hashlib.sha256).digest()


def issue(credits: int, package: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "credits": int(credits),
        "nonce": secrets.token_hex(16),
        "package": package,
        "issued": now.isoformat(),
        "expires": (now + timedelta(days=config.TOKEN_VALIDITY_DAYS)).isoformat(),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = _sign(payload_bytes)
    return f"{_b64u(payload_bytes)}.{_b64u(signature)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Mint a KhaleejNode credit token.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--package", choices=sorted(config.CREDIT_PACKAGES), help="named package")
    group.add_argument("--credits", type=int, help="custom credit count")
    ap.add_argument("--print-json", action="store_true", help="also print the payload")
    args = ap.parse_args()

    if args.package:
        credits = config.CREDIT_PACKAGES[args.package]["credits"]
        package = args.package
    else:
        credits = args.credits
        package = "custom"

    token = issue(credits, package)

    print("\n=== KhaleejNode Credit Token ===")
    print(f"Package : {package}")
    print(f"Credits : {credits}")
    print("\nPaste this into 'Add Prepaid Credits':\n")
    print(token)
    print()

    if args.print_json:
        payload_b64 = token.split(".", 1)[0]
        pad = "=" * (-len(payload_b64) % 4)
        print("Payload:", base64.urlsafe_b64decode(payload_b64 + pad).decode())


if __name__ == "__main__":
    main()
