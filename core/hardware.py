"""
core/hardware.py
================
Node-locking hardware fingerprint.

We read two host-specific "mock endpoints" that in a real enterprise deployment
map directly to physical identity:

  * Motherboard / platform UUID  -> unique per physical machine / VM.
  * Primary MAC address          -> unique per network interface.

Both are combined and hashed with SHA-256 to produce a stable HARDWARE ID.
If a client copies the installed application image to a *different* machine, the
recomputed HARDWARE ID will not match the one baked into `license.key`, and the
core refuses to run.

All reads are wrapped defensively: if the platform-specific command is missing
(locked-down container, exotic OS), we fall back to a deterministic mock derived
from `uuid.getnode()` so the application still boots. The fingerprint remains
stable across runs on the same host, which is all node-locking requires.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class HardwareIdentity:
    """A resolved, stable identity for the current physical host."""

    motherboard_uuid: str
    mac_address: str
    hardware_id: str  # SHA-256 hex of the combined endpoints


def _read_motherboard_uuid() -> str:
    """Best-effort read of the platform / motherboard UUID.

    Returns a lowercase string. Falls back to a deterministic mock derived from
    the node's MAC if the OS command is unavailable.
    """
    system = platform.system()

    try:
        if system == "Darwin":  # macOS
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode("utf-8", "ignore")
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    # line looks like:  "IOPlatformUUID" = "XXXX-...."
                    return line.split("=", 1)[1].strip().strip('"').lower()

        elif system == "Linux":
            # Requires root on some distros; guarded by try/except.
            for path in (
                "/sys/class/dmi/id/product_uuid",
                "/etc/machine-id",
            ):
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        value = fh.read().strip()
                        if value:
                            return value.lower()
                except OSError:
                    continue

        elif system == "Windows":
            out = subprocess.check_output(
                ["wmic", "csproduct", "get", "UUID"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode("utf-8", "ignore")
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if len(lines) >= 2:
                return lines[1].lower()

    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    # Deterministic fallback so the app never hard-crashes on identity read.
    node = uuid.getnode()
    return "mock-mb-" + hashlib.sha256(str(node).encode()).hexdigest()[:32]


def _read_primary_mac() -> str:
    """Return the primary MAC address as aa:bb:cc:dd:ee:ff.

    `uuid.getnode()` is stdlib and cross-platform; it returns the hardware MAC
    when discoverable and a stable random 48-bit value otherwise.
    """
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -1, -8))


def resolve_identity() -> HardwareIdentity:
    """Read both endpoints and derive the SHA-256 HARDWARE ID."""
    mb = _read_motherboard_uuid()
    mac = _read_primary_mac()

    # Order-stable, delimiter-guarded so the two fields can't collide/ambiguate.
    combined = f"MB::{mb}||MAC::{mac}".encode("utf-8")
    hardware_id = hashlib.sha256(combined).hexdigest()

    return HardwareIdentity(
        motherboard_uuid=mb,
        mac_address=mac,
        hardware_id=hardware_id,
    )


def get_hardware_id() -> str:
    """Convenience accessor returning only the SHA-256 hardware id string."""
    return resolve_identity().hardware_id


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    ident = resolve_identity()
    print("Motherboard UUID :", ident.motherboard_uuid)
    print("Primary MAC      :", ident.mac_address)
    print("HARDWARE ID      :", ident.hardware_id)
