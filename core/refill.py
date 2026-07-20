"""
core/refill.py
==============
Offline credit refill via asymmetric (public-key) signatures.

Flow (fully offline, no network):
  1. The VENDOR holds an Ed25519 PRIVATE key. When a customer buys a package,
     the vendor runs tools/issue_token.py to mint a signed token string:

         base64url(payload_json) + "." + base64url(signature)

     where payload = {credits, nonce, package, issued, expires}.

  2. The customer pastes that string into the KhaleejNode UI ("Add Prepaid
     Credits"). The client verifies the signature with the bundled Ed25519
     PUBLIC key. Only the vendor's private key could have produced a signature
     that validates -- the client cannot forge one, even though it can *read*
     the public key.

  3. On success the token's nonce is recorded so the SAME token cannot be
     redeemed twice (replay protection), then the vault balance is credited.

If the `cryptography` package is unavailable, we transparently fall back to an
HMAC "detached-secret" scheme so the reference build still runs; the asymmetric
path is the production default. See `_backend()`.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from core import config, vault


class RefillError(Exception):
    """Raised when a token is malformed, forged, expired, or already used."""


@dataclass(frozen=True)
class RefillResult:
    credits_added: int
    new_balance: int
    package: str
    nonce: str


# ---------------------------------------------------------------------------
# base64url helpers (padding-safe)
# ---------------------------------------------------------------------------
def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------------------
# Crypto backend selection
# ---------------------------------------------------------------------------
def _has_cryptography() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ed25519  # noqa: F401

        return True
    except Exception:
        return False


def _backend() -> str:
    return "ed25519" if _has_cryptography() else "hmac-fallback"


def _verify_signature(payload_bytes: bytes, signature: bytes) -> bool:
    """Verify a token signature with whichever backend is active."""
    if _backend() == "ed25519":
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        try:
            pub = load_pem_public_key(config.VENDOR_PUBLIC_KEY_FILE.read_bytes())
            pub.verify(signature, payload_bytes)  # raises on mismatch
            return True
        except (InvalidSignature, FileNotFoundError, ValueError):
            return False

    # HMAC fallback: signature = HMAC(SECRET_SALT, payload). Not truly
    # asymmetric, but keeps the demo functional without external deps.
    import hashlib
    import hmac

    expected = hmac.new(config.SECRET_SALT, payload_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Replay protection (used-token registry)
# ---------------------------------------------------------------------------
def _register_nonce(nonce: str) -> None:
    """Record a redeemed token nonce. Raises RefillError if already present."""
    conn = sqlite3.connect(config.VAULT_FILE)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS redeemed_tokens ("
            "nonce TEXT PRIMARY KEY, redeemed_at TEXT NOT NULL)"
        )
        try:
            conn.execute(
                "INSERT INTO redeemed_tokens (nonce, redeemed_at) VALUES (?, ?)",
                (nonce, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise RefillError("This credit token has already been redeemed.") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def redeem_token(token: str, hardware_id: str) -> RefillResult:
    """Validate and redeem a vendor-issued credit token, crediting the vault."""
    token = (token or "").strip()
    if token.count(".") != 1:
        raise RefillError("Malformed token: expected '<payload>.<signature>'.")

    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload_bytes = _b64u_decode(payload_b64)
        signature = _b64u_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001 - any decode error is a bad token
        raise RefillError("Malformed token: base64 decode failed.") from exc

    # 1. Cryptographic authenticity.
    if not _verify_signature(payload_bytes, signature):
        raise RefillError("Invalid token signature -- not issued by the vendor.")

    # 2. Parse payload.
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
        credits = int(payload["credits"])
        nonce = str(payload["nonce"])
        package = str(payload.get("package", "custom"))
        expires = str(payload["expires"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise RefillError("Token payload is malformed.") from exc

    if credits <= 0:
        raise RefillError("Token grants no credits.")

    # 3. Expiry.
    try:
        expires_dt = datetime.fromisoformat(expires)
    except ValueError as exc:
        raise RefillError("Token has an invalid expiry date.") from exc
    if datetime.now(timezone.utc) > expires_dt:
        raise RefillError("This credit token has expired.")

    # 4. Replay protection and credit vault (combined inside a single transaction).
    try:
        state = vault.credit(
            hardware_id, credits, detail=f"refill:{package}:{nonce[:8]}", nonce_to_register=nonce
        )
    except vault.VaultError as exc:
        if "already been redeemed" in str(exc):
            raise RefillError("This credit token has already been redeemed.") from exc
        raise RefillError(str(exc)) from exc

    return RefillResult(
        credits_added=credits,
        new_balance=state.balance,
        package=package,
        nonce=nonce,
    )
