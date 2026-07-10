"""
core/license.py
===============
The node-lock: bind an installation to one physical machine.

A `license.key` file contains:
  * the HARDWARE ID this install is licensed to,
  * issue metadata,
  * an HMAC-SHA256 signature over the payload keyed by SECRET_SALT.

On every boot the core:
  1. recomputes the live HARDWARE ID from the host (core/hardware.py),
  2. loads license.key,
  3. verifies the signature (proves the file was produced by something holding
     SECRET_SALT -- i.e. the genuine vendor tooling / compiled core),
  4. verifies the licensed HARDWARE ID == the live HARDWARE ID.

Copy the whole install folder to a different laptop and step 4 fails: the
license is cryptographically welded to the original silicon.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core import config
from core.hardware import resolve_identity


class LicenseError(Exception):
    """Raised when the license is missing, malformed, forged, or node-mismatched."""


@dataclass(frozen=True)
class LicenseStatus:
    activated: bool
    hardware_id: str
    reason: str = ""


def _sign_payload(payload: dict) -> str:
    """Deterministic HMAC-SHA256 over the canonical JSON payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(config.SECRET_SALT, canonical, hashlib.sha256).hexdigest()


def generate_license(target_hardware_id: str | None = None) -> dict:
    """Create and persist a license.key bound to the given (or live) hardware id.

    Called by setup.sh / run.py so the first boot is already unlocked on the
    builder's own machine.
    """
    config.ensure_state_dir()

    hardware_id = target_hardware_id or resolve_identity().hardware_id

    payload = {
        "product": config.PRODUCT_NAME,
        "edition": config.PRODUCT_EDITION,
        "version": config.PRODUCT_VERSION,
        "hardware_id": hardware_id,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    document = {"payload": payload, "signature": _sign_payload(payload)}

    config.LICENSE_FILE.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return document


def _load_document(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LicenseError("No license.key present -- product is locked.") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise LicenseError("license.key is unreadable or corrupt.") from exc


def verify_license() -> LicenseStatus:
    """Verify the on-disk license against the live hardware. Never raises."""
    live_id = resolve_identity().hardware_id

    try:
        document = _load_document(config.LICENSE_FILE)
        payload = document["payload"]
        signature = document["signature"]
    except LicenseError as exc:
        return LicenseStatus(activated=False, hardware_id=live_id, reason=str(exc))
    except (KeyError, TypeError):
        return LicenseStatus(
            activated=False, hardware_id=live_id, reason="license.key is malformed."
        )

    # 1. Signature integrity -- constant-time compare defeats forgery attempts.
    expected = _sign_payload(payload)
    if not hmac.compare_digest(expected, str(signature)):
        return LicenseStatus(
            activated=False,
            hardware_id=live_id,
            reason="License signature invalid -- file was tampered with or forged.",
        )

    # 2. Node-lock: licensed hardware must equal live hardware.
    licensed_id = str(payload.get("hardware_id", ""))
    if not hmac.compare_digest(licensed_id, live_id):
        return LicenseStatus(
            activated=False,
            hardware_id=live_id,
            reason="Hardware mismatch -- this license is bound to a different machine.",
        )

    return LicenseStatus(activated=True, hardware_id=live_id, reason="OK")


def require_activation() -> str:
    """Return the live hardware id if activated, else raise LicenseError."""
    status = verify_license()
    if not status.activated:
        raise LicenseError(status.reason)
    return status.hardware_id


if __name__ == "__main__":  # pragma: no cover
    st = verify_license()
    print("Activated:", st.activated)
    print("Hardware :", st.hardware_id)
    print("Reason   :", st.reason)
