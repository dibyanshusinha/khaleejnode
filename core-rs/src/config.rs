//! Configuration + secret material for the Rust Shield.
//!
//! Unlike the Python reference build, the `SECRET_SALT` is NOT a source
//! constant here. It is resolved at runtime from, in priority order:
//!   1. the `KHALEEJNODE_SALT` environment variable (CI / staging override),
//!   2. the OS keychain (macOS Keychain / Windows Credential Manager / libsecret),
//!   3. a compiled-in development fallback.
//!
//! In a shipped Tauri build the salt is provisioned into the OS keychain on
//! first run, so it never lives on disk in plaintext and never ships inside the
//! binary image.

pub const PRODUCT_NAME: &str = "KhaleejNode";
pub const PRODUCT_EDITION: &str = "Enterprise TradeTech Node";
pub const PRODUCT_VERSION: &str = "1.0.0";
pub const PRODUCT_REGION: &str = "United Arab Emirates (GCC)";

pub const FIRST_ACTIVATION_GRANT: i64 = 10;
pub const TOKEN_VALIDITY_DAYS: i64 = 90;

const KEYCHAIN_SERVICE: &str = "com.khaleejnode.app";
const KEYCHAIN_USER: &str = "secret_salt";

/// Development fallback salt. Overridden in production by keychain provisioning.
const DEV_FALLBACK_SALT: &[u8] = b"KhaleejNode::v1::a7f3c1e9d2b48f60::do-not-share";

/// Resolve the active secret salt.
pub fn secret_salt() -> Vec<u8> {
    if let Ok(v) = std::env::var("KHALEEJNODE_SALT") {
        if !v.is_empty() {
            return v.into_bytes();
        }
    }
    if let Ok(entry) = keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_USER) {
        if let Ok(secret) = entry.get_password() {
            if !secret.is_empty() {
                return secret.into_bytes();
            }
        }
    }
    DEV_FALLBACK_SALT.to_vec()
}

/// Provision a salt into the OS keychain (called once at install time).
pub fn provision_salt(salt: &str) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        .map_err(|e| e.to_string())?;
    entry.set_password(salt).map_err(|e| e.to_string())
}
