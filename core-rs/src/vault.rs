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
use std::sync::{Mutex, OnceLock};
use std::collections::HashMap;

static MOCK_KEYCHAIN: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();

fn get_mock_keychain() -> &'static Mutex<HashMap<String, String>> {
    MOCK_KEYCHAIN.get_or_init(|| Mutex::new(HashMap::new()))
}

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

fn sync_keychain_balance(hardware_id: &str, balance: i64) {
    if std::env::var("KHALEEJNODE_TESTING_MOCK_KEYCHAIN").is_ok() {
        let mut mock = get_mock_keychain().lock().unwrap();
        mock.insert(hardware_id.to_string(), balance.to_string());
        return;
    }
    if std::env::var("KHALEEJNODE_TESTING").is_ok() {
        return;
    }
    if let Ok(entry) = keyring::Entry::new("com.khaleejnode.app", &format!("vault_balance_{}", hardware_id)) {
        let _ = entry.set_password(&balance.to_string());
    }
}

fn get_keychain_balance(hardware_id: &str) -> Option<i64> {
    if std::env::var("KHALEEJNODE_TESTING_MOCK_KEYCHAIN").is_ok() {
        let mock = get_mock_keychain().lock().unwrap();
        return mock.get(hardware_id).and_then(|v| v.parse::<i64>().ok());
    }
    if std::env::var("KHALEEJNODE_TESTING").is_ok() {
        return None;
    }
    if let Ok(entry) = keyring::Entry::new("com.khaleejnode.app", &format!("vault_balance_{}", hardware_id)) {
        if let Ok(password) = entry.get_password() {
            if let Ok(bal) = password.parse::<i64>() {
                return Some(bal);
            }
        }
    }
    None
}

fn compute_tokens_signature(conn: &Connection, salt: &[u8]) -> Result<String, VaultError> {
    let mut stmt = conn.prepare("SELECT nonce FROM redeemed_tokens ORDER BY nonce ASC")?;
    let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
    let mut nonces = Vec::new();
    for r in rows {
        nonces.push(r?);
    }
    let message = nonces.join("||");
    let mut mac = HmacSha256::new_from_slice(salt).expect("hmac key");
    mac.update(message.as_bytes());
    Ok(hex::encode(mac.finalize().into_bytes()))
}

fn sign_row(balance: i64, hardware_id: &str, nonce: &str, tokens_signature: &str, salt: &[u8]) -> String {
    let message = format!("{}||{}||{}||{}", balance, hardware_id, nonce, tokens_signature);
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
             id               INTEGER PRIMARY KEY CHECK (id = 1),
             balance          INTEGER NOT NULL,
             hardware_id      TEXT    NOT NULL,
             nonce            TEXT    NOT NULL,
             updated_at       TEXT    NOT NULL,
             tokens_signature TEXT    NOT NULL DEFAULT '',
             signature        TEXT    NOT NULL
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

    // Check if tokens_signature column exists, if not, alter table
    let has_col: bool = {
        let mut stmt = conn.prepare("PRAGMA table_info(ledger)")?;
        let mut rows = stmt.query([])?;
        let mut found = false;
        while let Some(row) = rows.next()? {
            let col_name: String = row.get(1)?;
            if col_name == "tokens_signature" {
                found = true;
                break;
            }
        }
        found
    };
    if !has_col {
        conn.execute("ALTER TABLE ledger ADD COLUMN tokens_signature TEXT NOT NULL DEFAULT ''", [])?;
    }

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
        let tokens_sig = compute_tokens_signature(&conn, salt)?;
        let sig = sign_row(opening_balance, hardware_id, &nonce, &tokens_sig, salt);
        let now = now_iso();
        conn.execute(
            "INSERT INTO ledger (id, balance, hardware_id, nonce, updated_at, tokens_signature, signature)
             VALUES (1, ?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![opening_balance, hardware_id, nonce, now, tokens_sig, sig],
        )?;
        conn.execute(
            "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?1,?2,?3,?4,?5)",
            rusqlite::params!["INIT", opening_balance, opening_balance, "vault initialized", now],
        )?;
        sync_keychain_balance(hardware_id, opening_balance);
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
    ensure_schema(conn)?;

    let row = conn.query_row(
        "SELECT balance, hardware_id, nonce, updated_at, tokens_signature, signature FROM ledger WHERE id = 1",
        [],
        |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
            ))
        },
    );

    let (balance, stored_hw, nonce, updated_at, tokens_signature, stored_sig) = match row {
        Ok(v) => v,
        Err(rusqlite::Error::QueryReturnedNoRows) => return Err(VaultError::NotInitialized),
        Err(rusqlite::Error::SqliteFailure(_, _)) => return Err(VaultError::NotInitialized),
        Err(e) => return Err(VaultError::Other(e.to_string())),
    };

    // 1. Tokens integrity
    let expected_tokens_sig = compute_tokens_signature(conn, salt)?;
    if !ct_eq(&expected_tokens_sig, &tokens_signature) {
        return Err(VaultError::Tampered);
    }

    // 2. Integrity: does the stored signature match a fresh HMAC?
    let expected = sign_row(balance, &stored_hw, &nonce, &tokens_signature, salt);
    if !ct_eq(&expected, &stored_sig) {
        return Err(VaultError::Tampered);
    }
    if !ct_eq(&stored_hw, hardware_id) {
        return Err(VaultError::ForeignMachine);
    }

    // 3. Rollback protection
    if let Some(keychain_bal) = get_keychain_balance(hardware_id) {
        if keychain_bal != balance {
            return Err(VaultError::Tampered);
        }
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
    let tokens_sig = compute_tokens_signature(conn, salt)?;
    let sig = sign_row(new_balance, hardware_id, &nonce, &tokens_sig, salt);
    let now = now_iso();
    conn.execute(
        "UPDATE ledger SET balance = ?1, hardware_id = ?2, nonce = ?3, updated_at = ?4, tokens_signature = ?5, signature = ?6 WHERE id = 1",
        rusqlite::params![new_balance, hardware_id, nonce, now, tokens_sig, sig],
    )?;
    conn.execute(
        "INSERT INTO audit_log (event, delta, balance, detail, at) VALUES (?1,?2,?3,?4,?5)",
        rusqlite::params![event, delta, new_balance, detail, now],
    )?;
    sync_keychain_balance(hardware_id, new_balance);
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
    let mut conn = open_db(db_path)?;
    let tx = conn.transaction()?;

    let state = read_state_conn(&tx, salt, hardware_id)?;
    if state.balance < amount {
        return Err(VaultError::Insufficient {
            have: state.balance,
            need: amount,
        });
    }

    let new_state = write_balance(&tx, state.balance - amount, hardware_id, salt, "DEDUCT", -amount, detail)?;
    tx.commit()?;
    Ok(new_state)
}

/// Add `amount` credits after verifying integrity, optionally registering a refill token nonce in the same transaction.
pub fn credit(
    db_path: &Path,
    salt: &[u8],
    hardware_id: &str,
    amount: i64,
    detail: &str,
    nonce_to_register: Option<&str>,
) -> Result<VaultState, VaultError> {
    if amount <= 0 {
        return Err(VaultError::Other("Credit amount must be positive.".into()));
    }
    let mut conn = open_db(db_path)?;
    let tx = conn.transaction()?;

    let state = read_state_conn(&tx, salt, hardware_id)?;

    if let Some(nonce) = nonce_to_register {
        let affected = tx.execute(
            "INSERT OR IGNORE INTO redeemed_tokens (nonce, redeemed_at) VALUES (?1, ?2)",
            rusqlite::params![nonce, now_iso()],
        )?;
        if affected == 0 {
            return Err(VaultError::Other(
                "This credit token has already been redeemed.".into(),
            ));
        }
    }

    let new_state = write_balance(&tx, state.balance + amount, hardware_id, salt, "CREDIT", amount, detail)?;
    tx.commit()?;
    Ok(new_state)
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
