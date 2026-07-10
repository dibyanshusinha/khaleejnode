//! Tamper-evident prepaid-credit vault (Rust port of core/vault.py).
//!
//! Storage is SQLite via `rusqlite`. The value-bearing balance row is HMAC-SHA256
//! row-signed with `HMAC(salt, "<balance>||<hardware_id>||<nonce>")`. Every write
//! rotates the nonce and re-signs. Reads recompute and constant-time compare;
//! a mismatch (hand-edited balance) or a foreign `hardware_id` (copied db) is
//! rejected.
//!
//! Encryption-at-rest: this build uses `rusqlite` with the `bundled` SQLite. To
//! get SQLCipher-encrypted pages, switch the Cargo feature to
//! `bundled-sqlcipher-vendored-openssl` and issue `PRAGMA key` in `open_db()` —
//! the ledger logic is storage-agnostic, exactly like the Python `_connect()` seam.

use std::path::Path;

use hmac::{Hmac, Mac};
use rand::RngCore;
use rusqlite::Connection;
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, thiserror::Error)]
pub enum VaultError {
    #[error("Vault not initialized.")]
    NotInitialized,
    #[error("Vault integrity check FAILED — balance row was modified outside the core.")]
    Tampered,
    #[error("Vault belongs to a different machine — copied database rejected.")]
    ForeignMachine,
    #[error("Insufficient prepaid credits: have {have}, need {need}.")]
    Insufficient { have: i64, need: i64 },
    #[error("{0}")]
    Other(String),
}

impl From<rusqlite::Error> for VaultError {
    fn from(e: rusqlite::Error) -> Self {
        VaultError::Other(e.to_string())
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct VaultState {
    pub balance: i64,
    pub hardware_id: String,
    pub updated_at: String,
}

fn open_db(db_path: &Path) -> Result<Connection, VaultError> {
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| VaultError::Other(e.to_string()))?;
    }
    let conn = Connection::open(db_path)?;
    // SQLCipher swap-point: `conn.pragma_update(None, "key", "<passphrase>")?;`
    conn.pragma_update(None, "journal_mode", "WAL")?;
    Ok(conn)
}

fn sign_row(balance: i64, hardware_id: &str, nonce: &str, salt: &[u8]) -> String {
    let message = format!("{}||{}||{}", balance, hardware_id, nonce);
    let mut mac = HmacSha256::new_from_slice(salt).expect("hmac key");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn fresh_nonce() -> String {
    let mut buf = [0u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut buf);
    hex::encode(buf)
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339()
}

fn ensure_schema(conn: &Connection) -> Result<(), VaultError> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS ledger (
             id          INTEGER PRIMARY KEY CHECK (id = 1),
             balance     INTEGER NOT NULL,
             hardware_id TEXT    NOT NULL,
             nonce       TEXT    NOT NULL,
             updated_at  TEXT    NOT NULL,
             signature   TEXT    NOT NULL
         );
         CREATE TABLE IF NOT EXISTS audit_log (
             id      INTEGER PRIMARY KEY AUTOINCREMENT,
             event   TEXT NOT NULL,
             delta   INTEGER NOT NULL,
             balance INTEGER NOT NULL,
             detail  TEXT,
             at      TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS redeemed_tokens (
             nonce       TEXT PRIMARY KEY,
             redeemed_at TEXT NOT NULL
         );",
    )?;
    Ok(())
}

/// Create schema and seed the signed balance row if absent. Idempotent.
pub fn initialize(
    db_path: &Path,
    salt: &[u8],
    hardware_id: &str,
    opening_balance: i64,
) -> Result<VaultState, VaultError> {
    let conn = open_db(db_path)?;
    ensure_schema(&conn)?;

    let exists: bool = conn
        .query_row("SELECT COUNT(*) FROM ledger WHERE id = 1", [], |r| {
            r.get::<_, i64>(0)
        })? > 0;

    if !exists {
        let nonce = fresh_nonce();
        let sig = sign_row(opening_balance, hardware_id, &nonce, salt);
        let now = now_iso();
        conn.execute(
            "INSERT INTO ledger (id, balance, hardware_id, nonce, updated_at, signature)
             VALUES (1, ?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![opening_balance, hardware_id, nonce, now, sig],
        )?;
        conn.execute(
            "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?1,?2,?3,?4,?5)",
            rusqlite::params!["INIT", opening_balance, opening_balance, "vault initialized", now],
        )?;
    }
    read_state(db_path, salt, hardware_id)
}

/// Read + verify the signed balance row.
pub fn read_state(db_path: &Path, salt: &[u8], hardware_id: &str) -> Result<VaultState, VaultError> {
    let conn = open_db(db_path)?;
    read_state_conn(&conn, salt, hardware_id)
}

fn read_state_conn(
    conn: &Connection,
    salt: &[u8],
    hardware_id: &str,
) -> Result<VaultState, VaultError> {
    let row = conn.query_row(
        "SELECT balance, hardware_id, nonce, updated_at, signature FROM ledger WHERE id = 1",
        [],
        |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
            ))
        },
    );

    let (balance, stored_hw, nonce, updated_at, stored_sig) = match row {
        Ok(v) => v,
        Err(rusqlite::Error::QueryReturnedNoRows) => return Err(VaultError::NotInitialized),
        Err(rusqlite::Error::SqliteFailure(_, _)) => return Err(VaultError::NotInitialized),
        Err(e) => return Err(VaultError::Other(e.to_string())),
    };

    let expected = sign_row(balance, &stored_hw, &nonce, salt);
    if !ct_eq(&expected, &stored_sig) {
        return Err(VaultError::Tampered);
    }
    if !ct_eq(&stored_hw, hardware_id) {
        return Err(VaultError::ForeignMachine);
    }

    Ok(VaultState {
        balance,
        hardware_id: stored_hw,
        updated_at,
    })
}

fn write_balance(
    conn: &Connection,
    new_balance: i64,
    hardware_id: &str,
    salt: &[u8],
    event: &str,
    delta: i64,
    detail: &str,
) -> Result<VaultState, VaultError> {
    let nonce = fresh_nonce();
    let sig = sign_row(new_balance, hardware_id, &nonce, salt);
    let now = now_iso();
    conn.execute(
        "UPDATE ledger SET balance = ?1, hardware_id = ?2, nonce = ?3, updated_at = ?4, signature = ?5 WHERE id = 1",
        rusqlite::params![new_balance, hardware_id, nonce, now, sig],
    )?;
    conn.execute(
        "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?1,?2,?3,?4,?5)",
        rusqlite::params![event, delta, new_balance, detail, now],
    )?;
    Ok(VaultState {
        balance: new_balance,
        hardware_id: hardware_id.to_string(),
        updated_at: now,
    })
}

/// Spend `amount` credits after verifying integrity. Refuses if insufficient.
pub fn deduct(
    db_path: &Path,
    salt: &[u8],
    hardware_id: &str,
    amount: i64,
    detail: &str,
) -> Result<VaultState, VaultError> {
    if amount <= 0 {
        return Err(VaultError::Other("Deduction amount must be positive.".into()));
    }
    let conn = open_db(db_path)?;
    let state = read_state_conn(&conn, salt, hardware_id)?;
    if state.balance < amount {
        return Err(VaultError::Insufficient {
            have: state.balance,
            need: amount,
        });
    }
    write_balance(&conn, state.balance - amount, hardware_id, salt, "DEDUCT", -amount, detail)
}

/// Add `amount` credits after verifying integrity.
pub fn credit(
    db_path: &Path,
    salt: &[u8],
    hardware_id: &str,
    amount: i64,
    detail: &str,
) -> Result<VaultState, VaultError> {
    if amount <= 0 {
        return Err(VaultError::Other("Credit amount must be positive.".into()));
    }
    let conn = open_db(db_path)?;
    let state = read_state_conn(&conn, salt, hardware_id)?;
    write_balance(&conn, state.balance + amount, hardware_id, salt, "CREDIT", amount, detail)
}

/// Record a redeemed token nonce (single-use). Returns Err if already present.
pub fn register_nonce(db_path: &Path, nonce: &str) -> Result<(), VaultError> {
    let conn = open_db(db_path)?;
    ensure_schema(&conn)?;
    let affected = conn.execute(
        "INSERT OR IGNORE INTO redeemed_tokens (nonce, redeemed_at) VALUES (?1, ?2)",
        rusqlite::params![nonce, now_iso()],
    )?;
    if affected == 0 {
        return Err(VaultError::Other(
            "This credit token has already been redeemed.".into(),
        ));
    }
    Ok(())
}
