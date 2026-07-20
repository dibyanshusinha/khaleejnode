"""
core/vault.py
=============
The Token Vault: a tamper-evident prepaid-credit ledger.

Storage is a local SQLite database (`state/vault.db`). Every row that carries
value is HMAC-SHA256 row-signed with:

        signature = HMAC( SECRET_SALT , balance || hardware_id || nonce )

Because the SECRET_SALT never leaves the compiled core, a user cannot open
vault.db in a SQLite editor, set `balance = 9999`, and produce a matching
signature. On the next read the recomputed HMAC won't match the stored one and
the vault refuses to serve a balance -- it reports tampering instead of quietly
trusting the edited number.

The vault is also welded to the machine: the signature includes the HARDWARE
ID, so a vault.db copied from another (legitimately high-balance) install is
rejected here.

NOTE ON "SQLCipher-style": for zero-dependency portability this reference build
uses stdlib sqlite3 with an application-layer HMAC integrity envelope. The code
is structured so the connection factory can be swapped for pysqlcipher3
(encrypted-at-rest pages) without touching the ledger logic -- see
`_connect()`.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from core import config


class VaultError(Exception):
    """Raised on tamper detection, insufficient balance, or storage faults."""


class VaultTamperError(VaultError):
    """Raised specifically when a row signature fails verification."""


@dataclass(frozen=True)
class VaultState:
    balance: int
    hardware_id: str
    updated_at: str


def _connect() -> sqlite3.Connection:
    """Open the vault database.

    Swap-point for SQLCipher: replace the sqlite3.connect call with
    pysqlcipher3 and issue `PRAGMA key = ...` here to get encrypted pages.
    The rest of the module is storage-agnostic.
    """
    config.ensure_state_dir()
    conn = sqlite3.connect(config.VAULT_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _compute_tokens_signature(conn: sqlite3.Connection) -> str:
    """Compute HMAC-SHA256 signature over all nonces in redeemed_tokens."""
    # Ensure redeemed_tokens table exists before querying.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS redeemed_tokens (
            nonce TEXT PRIMARY KEY, redeemed_at TEXT NOT NULL
        )
        """
    )
    rows = conn.execute("SELECT nonce FROM redeemed_tokens ORDER BY nonce ASC").fetchall()
    nonces = [row[0] for row in rows]
    message = "||".join(nonces).encode("utf-8")
    return hmac.new(config.SECRET_SALT, message, hashlib.sha256).hexdigest()


def _sync_keychain_balance(hardware_id: str, balance: int) -> None:
    """Store the balance in the OS keychain."""
    import os
    if os.environ.get("KHALEEJNODE_TESTING"):
        return
    try:
        import keyring
        service = "com.khaleejnode.app"
        username = f"vault_balance_{hardware_id}"
        keyring.set_password(service, username, str(balance))
    except Exception as exc:
        print(f"  [vault] Keychain sync warning: {exc}")


def _get_keychain_balance(hardware_id: str) -> int | None:
    """Fetch the balance from the OS keychain."""
    import os
    if os.environ.get("KHALEEJNODE_TESTING"):
        return None
    try:
        import keyring
        service = "com.khaleejnode.app"
        username = f"vault_balance_{hardware_id}"
        val = keyring.get_password(service, username)
        if val is not None:
            return int(val)
    except Exception:
        pass
    return None


def _sign_row(balance: int, hardware_id: str, nonce: str, tokens_signature: str) -> str:
    """HMAC-SHA256 row signature over balance + hardware id + nonce + tokens_signature."""
    message = f"{balance}||{hardware_id}||{nonce}||{tokens_signature}".encode("utf-8")
    return hmac.new(config.SECRET_SALT, message, hashlib.sha256).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(hardware_id: str, opening_balance: int) -> VaultState:
    """Create the vault schema and seed the single signed balance row.

    Idempotent: if a valid balance row already exists it is returned untouched.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                balance          INTEGER NOT NULL,
                hardware_id      TEXT    NOT NULL,
                nonce            TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                tokens_signature TEXT    NOT NULL DEFAULT '',
                signature        TEXT    NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event        TEXT NOT NULL,
                delta        INTEGER NOT NULL,
                balance      INTEGER NOT NULL,
                detail       TEXT,
                at           TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeemed_tokens (
                nonce       TEXT PRIMARY KEY,
                redeemed_at TEXT NOT NULL
            );
            """
        )

        # Schema migration: alter ledger table if tokens_signature is missing
        info = conn.execute("PRAGMA table_info(ledger)").fetchall()
        columns = [col[1] for col in info]
        if columns and "tokens_signature" not in columns:
            conn.execute("ALTER TABLE ledger ADD COLUMN tokens_signature TEXT NOT NULL DEFAULT ''")

        row = conn.execute("SELECT balance FROM ledger WHERE id = 1").fetchone()
        if row is None:
            nonce = _fresh_nonce()
            tokens_sig = _compute_tokens_signature(conn)
            sig = _sign_row(opening_balance, hardware_id, nonce, tokens_sig)
            now = _now()
            conn.execute(
                "INSERT INTO ledger (id, balance, hardware_id, nonce, updated_at, tokens_signature, signature) "
                "VALUES (1, ?, ?, ?, ?, ?, ?)",
                (opening_balance, hardware_id, nonce, now, tokens_sig, sig),
            )
            conn.execute(
                "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?,?,?,?,?)",
                ("INIT", opening_balance, opening_balance, "vault initialized", now),
            )
            conn.commit()
            _sync_keychain_balance(hardware_id, opening_balance)
        conn.commit()
        return read_state(hardware_id, _conn=conn)
    finally:
        conn.close()


def _fresh_nonce() -> str:
    import secrets

    return secrets.token_hex(16)


def read_state(hardware_id: str, _conn: sqlite3.Connection | None = None) -> VaultState:
    """Read and verify the signed balance row. Raises on tamper/mismatch."""
    owns_conn = _conn is None
    conn = _conn or _connect()
    try:
        try:
            # Schema migration: alter ledger table if tokens_signature is missing
            info = conn.execute("PRAGMA table_info(ledger)").fetchall()
            columns = [col[1] for col in info]
            if columns and "tokens_signature" not in columns:
                conn.execute("ALTER TABLE ledger ADD COLUMN tokens_signature TEXT NOT NULL DEFAULT ''")

            row = conn.execute(
                "SELECT balance, hardware_id, nonce, updated_at, tokens_signature, signature FROM ledger WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # Table absent -> vault has never been initialized on this machine.
            raise VaultError("Vault not initialized.") from exc
        if row is None:
            raise VaultError("Vault not initialized.")

        balance, stored_hw, nonce, updated_at, tokens_signature, stored_sig = row

        # 1. Integrity: does the stored tokens signature match the actual nonces?
        expected_tokens_sig = _compute_tokens_signature(conn)
        if not hmac.compare_digest(expected_tokens_sig, str(tokens_signature)):
            raise VaultTamperError(
                "Vault integrity check FAILED -- spent tokens table was modified outside the core."
            )

        # 2. Integrity: does the stored signature match a fresh HMAC?
        expected = _sign_row(int(balance), str(stored_hw), str(nonce), str(tokens_signature))
        if not hmac.compare_digest(expected, str(stored_sig)):
            raise VaultTamperError(
                "Vault integrity check FAILED -- balance row was modified outside the core."
            )

        # 3. Node-lock: is this vault bound to the machine asking?
        if not hmac.compare_digest(str(stored_hw), hardware_id):
            raise VaultTamperError(
                "Vault belongs to a different machine -- copied vault.db rejected."
            )

        # 4. Rollback protection: does the db balance match the keychain balance?
        keychain_bal = _get_keychain_balance(hardware_id)
        if keychain_bal is not None and keychain_bal != int(balance):
            raise VaultTamperError(
                f"Vault rollback detected -- database balance ({balance}) does not match keychain ({keychain_bal})."
            )

        return VaultState(balance=int(balance), hardware_id=str(stored_hw), updated_at=str(updated_at))
    finally:
        if owns_conn:
            conn.close()


def _write_balance(conn: sqlite3.Connection, new_balance: int, hardware_id: str,
                   event: str, delta: int, detail: str) -> VaultState:
    """Re-sign and persist a new balance, appending an audit entry."""
    nonce = _fresh_nonce()  # fresh nonce each write -> signatures aren't reusable
    tokens_sig = _compute_tokens_signature(conn)
    sig = _sign_row(new_balance, hardware_id, nonce, tokens_sig)
    now = _now()
    conn.execute(
        "UPDATE ledger SET balance = ?, hardware_id = ?, nonce = ?, updated_at = ?, tokens_signature = ?, signature = ? WHERE id = 1",
        (new_balance, hardware_id, nonce, now, tokens_sig, sig),
    )
    conn.execute(
        "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?,?,?,?,?)",
        (event, delta, new_balance, detail, now),
    )
    _sync_keychain_balance(hardware_id, new_balance)
    return VaultState(balance=new_balance, hardware_id=hardware_id, updated_at=now)



def deduct(hardware_id: str, amount: int = 1, detail: str = "document check") -> VaultState:
    """Spend `amount` credits. Verifies integrity first; refuses if insufficient."""
    if amount <= 0:
        raise VaultError("Deduction amount must be positive.")
    conn = _connect()
    try:
        conn.execute("BEGIN TRANSACTION;")
        try:
            state = read_state(hardware_id, _conn=conn)  # tamper/node check up front
        except Exception as exc:
            conn.execute("ROLLBACK;")
            raise exc

        if state.balance < amount:
            conn.execute("ROLLBACK;")
            raise VaultError(
                f"Insufficient prepaid credits: have {state.balance}, need {amount}."
            )

        try:
            new_state = _write_balance(
                conn, state.balance - amount, hardware_id, "DEDUCT", -amount, detail
            )
            conn.execute("COMMIT;")
            return new_state
        except Exception as exc:
            conn.execute("ROLLBACK;")
            raise exc
    finally:
        conn.close()


def credit(hardware_id: str, amount: int, detail: str = "manual credit", nonce_to_register: str | None = None) -> VaultState:
    """Add `amount` credits (used by the offline refill path after token verify)."""
    if amount <= 0:
        raise VaultError("Credit amount must be positive.")
    conn = _connect()
    try:
        conn.execute("BEGIN TRANSACTION;")
        
        try:
            state = read_state(hardware_id, _conn=conn)
        except Exception as exc:
            conn.execute("ROLLBACK;")
            raise exc

        if nonce_to_register:
            try:
                # Check if already present
                row = conn.execute("SELECT 1 FROM redeemed_tokens WHERE nonce = ?", (nonce_to_register,)).fetchone()
                if row:
                    raise VaultError("This credit token has already been redeemed.")
                conn.execute(
                    "INSERT INTO redeemed_tokens (nonce, redeemed_at) VALUES (?, ?)",
                    (nonce_to_register, datetime.now(timezone.utc).isoformat()),
                )
            except Exception as exc:
                conn.execute("ROLLBACK;")
                if "already been redeemed" in str(exc):
                    raise exc
                raise VaultError(f"Database error during nonce registration: {exc}") from exc

        try:
            new_state = _write_balance(
                conn, state.balance + amount, hardware_id, "CREDIT", amount, detail
            )
            conn.execute("COMMIT;")
            return new_state
        except Exception as exc:
            conn.execute("ROLLBACK;")
            raise exc
    finally:
        conn.close()


def audit_trail(limit: int = 25) -> list[dict]:
    """Return the most recent ledger events for the UI / support tooling."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT event, delta, balance, detail, at FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"event": e, "delta": d, "balance": b, "detail": det, "at": at}
            for (e, d, b, det, at) in rows
        ]
    finally:
        conn.close()
