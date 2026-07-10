"""
core/config.py
==============
Central, single-source-of-truth configuration for the KhaleejNode secure core.

In a shipped build these constants are compiled into a native binary (Cython /
Nuitka) so they never exist as readable text on the client machine. During
development they live here in plain Python for transparency and testing.

NOTHING in this file is a network endpoint. The entire product is designed to
run 100% offline.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# PROJECT_ROOT resolves to the directory that contains `core/`, `brain/`, etc.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Runtime state directory. Everything the app writes lives here so the source
# tree stays clean and the vault/license files are easy to locate and wipe.
STATE_DIR: Path = PROJECT_ROOT / "state"

# The node-lock license. Bound to *this* machine's hardware fingerprint.
LICENSE_FILE: Path = STATE_DIR / "license.key"

# The encrypted-at-rest token vault (SQLite database file).
VAULT_FILE: Path = STATE_DIR / "vault.db"

# Where the (public) vendor verification key lives on the client.
# The matching PRIVATE key never ships to the client -- only the vendor holds it.
KEYS_DIR: Path = PROJECT_ROOT / "core" / "keys"
VENDOR_PUBLIC_KEY_FILE: Path = KEYS_DIR / "vendor_public.pem"

# Vendor-side private key. Used ONLY by tools/issue_token.py to mint credit
# tokens. It is intentionally kept outside `core/` and must never be bundled
# into a client distribution.
VENDOR_PRIVATE_KEY_FILE: Path = PROJECT_ROOT / "tools" / "vendor_private.pem"

# The bundled web UI.
UI_DIR: Path = PROJECT_ROOT / "ui"

# ---------------------------------------------------------------------------
# Cryptographic material
# ---------------------------------------------------------------------------
# SECRET_SALT is the pepper mixed into every HMAC row-signature in the token
# vault. In a native build this is embedded in the compiled binary and, ideally,
# split / obfuscated. Overridable via env var so CI and staging can rotate it.
#
# NOTE: rotating this value invalidates every existing vault signature by
# design -- that is what makes copy-pasting a vault.db from another install fail.
SECRET_SALT: bytes = os.environ.get(
    "KHALEEJNODE_SALT",
    "KhaleejNode::v1::a7f3c1e9d2b48f60::do-not-share",
).encode("utf-8")

# ---------------------------------------------------------------------------
# Business defaults (safe, opinionated choices so the app is complete)
# ---------------------------------------------------------------------------
PRODUCT_NAME: str = "KhaleejNode"
PRODUCT_EDITION: str = "Enterprise TradeTech Node"
PRODUCT_VERSION: str = "1.0.0"
PRODUCT_REGION: str = "United Arab Emirates (GCC)"

# Free credits granted on first activation so a new install is immediately usable.
FIRST_ACTIVATION_GRANT: int = 10

# Retail credit packages the vendor sells (used by tools/issue_token.py).
# Amounts in AED are illustrative default price points.
CREDIT_PACKAGES: dict[str, dict] = {
    "starter": {"credits": 25, "price_aed": 249},
    "business": {"credits": 100, "price_aed": 899},
    "enterprise": {"credits": 500, "price_aed": 3999},
}

# How long a minted credit token is valid before it must be re-issued.
TOKEN_VALIDITY_DAYS: int = 90


def ensure_state_dir() -> None:
    """Create the runtime state directory if it does not yet exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
