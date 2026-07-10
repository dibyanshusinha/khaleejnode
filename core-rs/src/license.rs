//! Node-locked license (Rust port of core/license.py).
//!
//! A `license.key` binds the install to one machine's HARDWARE ID and is signed
//! with HMAC-SHA256 keyed by the secret salt. On boot the Rust core recomputes
//! the live HARDWARE ID and verifies both the signature and the node match,
//! using a constant-time comparison.

use std::path::Path;

use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

use crate::config;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Serialize, Deserialize)]
struct LicensePayload {
    product: String,
    edition: String,
    version: String,
    hardware_id: String,
    issued_at: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct LicenseDocument {
    payload: LicensePayload,
    signature: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct LicenseStatus {
    pub activated: bool,
    pub hardware_id: String,
    pub reason: String,
}

fn sign_payload(payload: &LicensePayload, salt: &[u8]) -> String {
    // serde_json serializes struct fields in declaration order -> deterministic,
    // so re-serializing a parsed payload yields identical bytes for verification.
    let canonical = serde_json::to_vec(payload).expect("serialize license payload");
    let mut mac = HmacSha256::new_from_slice(salt).expect("hmac key");
    mac.update(&canonical);
    hex::encode(mac.finalize().into_bytes())
}

/// Constant-time string comparison.
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

fn license_path(state_dir: &Path) -> std::path::PathBuf {
    state_dir.join("license.key")
}

/// Generate and persist a license bound to `hardware_id`.
pub fn generate(state_dir: &Path, salt: &[u8], hardware_id: &str) -> std::io::Result<()> {
    std::fs::create_dir_all(state_dir)?;
    let payload = LicensePayload {
        product: config::PRODUCT_NAME.to_string(),
        edition: config::PRODUCT_EDITION.to_string(),
        version: config::PRODUCT_VERSION.to_string(),
        hardware_id: hardware_id.to_string(),
        issued_at: chrono::Utc::now().to_rfc3339(),
    };
    let signature = sign_payload(&payload, salt);
    let document = LicenseDocument { payload, signature };
    let json = serde_json::to_string_pretty(&document).expect("serialize license");
    std::fs::write(license_path(state_dir), json)
}

/// Verify the on-disk license against `live_hardware_id`. Never panics.
pub fn verify(state_dir: &Path, salt: &[u8], live_hardware_id: &str) -> LicenseStatus {
    let locked = |reason: &str| LicenseStatus {
        activated: false,
        hardware_id: live_hardware_id.to_string(),
        reason: reason.to_string(),
    };

    let raw = match std::fs::read_to_string(license_path(state_dir)) {
        Ok(r) => r,
        Err(_) => return locked("No license.key present — product is locked."),
    };
    let document: LicenseDocument = match serde_json::from_str(&raw) {
        Ok(d) => d,
        Err(_) => return locked("license.key is unreadable or corrupt."),
    };

    let expected = sign_payload(&document.payload, salt);
    if !ct_eq(&expected, &document.signature) {
        return locked("License signature invalid — file was tampered with or forged.");
    }
    if !ct_eq(&document.payload.hardware_id, live_hardware_id) {
        return locked("Hardware mismatch — this license is bound to a different machine.");
    }

    LicenseStatus {
        activated: true,
        hardware_id: live_hardware_id.to_string(),
        reason: "OK".to_string(),
    }
}
