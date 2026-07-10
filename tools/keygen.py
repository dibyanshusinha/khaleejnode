"""
tools/keygen.py
===============
VENDOR tooling. Generate the Ed25519 signing keypair used to mint offline
credit tokens.

  * PRIVATE key  -> tools/vendor_private.pem   (vendor-only, never ships)
  * PUBLIC  key  -> core/keys/vendor_public.pem (bundled with the client)

Idempotent: if a keypair already exists it is left untouched unless --force is
passed. If the `cryptography` package is not installed, this is a no-op because
the reference build then uses the HMAC fallback signer (see core/refill.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly as `python tools/keygen.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config


def generate(force: bool = False) -> bool:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception:
        print("  [keygen] 'cryptography' not installed -> using HMAC fallback signer.")
        return False

    config.KEYS_DIR.mkdir(parents=True, exist_ok=True)
    config.VENDOR_PRIVATE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)

    if (
        config.VENDOR_PRIVATE_KEY_FILE.exists()
        and config.VENDOR_PUBLIC_KEY_FILE.exists()
        and not force
    ):
        print("  [keygen] Existing vendor keypair found -> keeping it.")
        return True

    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    config.VENDOR_PRIVATE_KEY_FILE.write_bytes(priv_pem)
    config.VENDOR_PUBLIC_KEY_FILE.write_bytes(pub_pem)
    print(f"  [keygen] Wrote private key -> {config.VENDOR_PRIVATE_KEY_FILE}")
    print(f"  [keygen] Wrote public  key -> {config.VENDOR_PUBLIC_KEY_FILE}")
    return True


if __name__ == "__main__":
    generate(force="--force" in sys.argv)
