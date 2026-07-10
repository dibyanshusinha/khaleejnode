//! Offline credit refill via Ed25519 (Rust port of core/refill.py).
//!
//! Tokens are minted by the VENDOR's Python tooling (`tools/issue_token.py`) and
//! verified here with the bundled Ed25519 PUBLIC key. Because verification runs
//! over the exact base64url-decoded payload bytes — the same bytes the vendor
//! signed — the Rust verifier is byte-compatible with Python-minted tokens
//! regardless of JSON formatting. Tokens are single-use (nonce recorded) and
//! time-boxed (expiry checked).

use std::path::Path;

use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use ed25519_dalek::pkcs8::DecodePublicKey;
use serde::Deserialize;

use crate::vault;

#[derive(Debug, thiserror::Error)]
pub enum RefillError {
    #[error("Malformed token: expected '<payload>.<signature>'.")]
    Malformed,
    #[error("Malformed token: base64 decode failed.")]
    BadBase64,
    #[error("Invalid token signature — not issued by the vendor.")]
    BadSignature,
    #[error("Token payload is malformed.")]
    BadPayload,
    #[error("Token grants no credits.")]
    NoCredits,
    #[error("This credit token has expired.")]
    Expired,
    #[error("This credit token has already been redeemed.")]
    AlreadyRedeemed,
    #[error("{0}")]
    Other(String),
}

#[derive(Debug, Deserialize)]
struct TokenPayload {
    credits: i64,
    nonce: String,
    #[serde(default = "default_package")]
    package: String,
    expires: String,
}

fn default_package() -> String {
    "custom".to_string()
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct RefillResult {
    pub credits_added: i64,
    pub new_balance: i64,
    pub package: String,
    pub nonce: String,
}

fn b64u_decode(text: &str) -> Result<Vec<u8>, RefillError> {
    // Python emits URL-safe base64 with '=' padding stripped.
    let cleaned: String = text.trim_end_matches('=').to_string();
    base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(cleaned.as_bytes())
        .map_err(|_| RefillError::BadBase64)
}

/// Validate and redeem a token, crediting the vault. `public_key_pem` is the
/// bundled vendor verification key.
pub fn redeem_token(
    db_path: &Path,
    salt: &[u8],
    public_key_pem: &str,
    token: &str,
    hardware_id: &str,
) -> Result<RefillResult, RefillError> {
    let token = token.trim();
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 2 {
        return Err(RefillError::Malformed);
    }

    let payload_bytes = b64u_decode(parts[0])?;
    let sig_bytes = b64u_decode(parts[1])?;

    // 1. Signature authenticity.
    let verifying_key = VerifyingKey::from_public_key_pem(public_key_pem)
        .map_err(|e| RefillError::Other(format!("bad public key: {e}")))?;
    let signature = Signature::from_slice(&sig_bytes).map_err(|_| RefillError::BadSignature)?;
    verifying_key
        .verify(&payload_bytes, &signature)
        .map_err(|_| RefillError::BadSignature)?;

    // 2. Parse payload.
    let payload: TokenPayload =
        serde_json::from_slice(&payload_bytes).map_err(|_| RefillError::BadPayload)?;
    if payload.credits <= 0 {
        return Err(RefillError::NoCredits);
    }

    // 3. Expiry.
    let expires = chrono::DateTime::parse_from_rfc3339(&payload.expires)
        .map_err(|_| RefillError::BadPayload)?
        .with_timezone(&chrono::Utc);
    if chrono::Utc::now() > expires {
        return Err(RefillError::Expired);
    }

    // 4. Single-use replay protection.
    vault::register_nonce(db_path, &payload.nonce).map_err(|_| RefillError::AlreadyRedeemed)?;

    // 5. Credit the tamper-evident vault.
    let short = &payload.nonce[..payload.nonce.len().min(8)];
    let detail = format!("refill:{}:{}", payload.package, short);
    let state = vault::credit(db_path, salt, hardware_id, payload.credits, &detail)
        .map_err(|e| RefillError::Other(e.to_string()))?;

    Ok(RefillResult {
        credits_added: payload.credits,
        new_balance: state.balance,
        package: payload.package,
        nonce: payload.nonce,
    })
}
