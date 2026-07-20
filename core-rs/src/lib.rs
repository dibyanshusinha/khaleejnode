//! KhaleejNode secure core — native Rust port of the Python Shield (`core/`).
//!
//! This crate is deliberately independent of Tauri so it can be unit-tested in
//! isolation and reused from any host (the Tauri backend in `src-tauri/`, a CLI,
//! or FFI). Every value-bearing operation — node-lock, HMAC vault, Ed25519
//! refill — lives here in compiled Rust, which is what makes the shipped binary
//! hard to reverse and keeps the salt out of readable source.

pub mod config;
pub mod hardware;
pub mod license;
pub mod refill;
pub mod vault;

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use ed25519_dalek::pkcs8::EncodePublicKey;
    use ed25519_dalek::{Signer, SigningKey};
    use rand::rngs::OsRng;
    use rand::RngCore;

    const SALT: &[u8] = b"unit-test-salt-fixed";

    fn b64u(raw: &[u8]) -> String {
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw)
    }

    fn new_signing() -> SigningKey {
        let mut secret = [0u8; 32];
        OsRng.fill_bytes(&mut secret);
        SigningKey::from_bytes(&secret)
    }

    #[test]
    fn hardware_id_is_stable() {
        let a = hardware::resolve_identity();
        let b = hardware::resolve_identity();
        assert_eq!(a.hardware_id, b.hardware_id);
        assert_eq!(a.hardware_id.len(), 64); // SHA-256 hex
    }

    #[test]
    fn license_roundtrip_and_tamper() {
        let dir = tempfile::tempdir().unwrap();
        let hw = "hw-machine-alpha";

        license::generate(dir.path(), SALT, hw).unwrap();

        // Correct machine -> activated.
        let ok = license::verify(dir.path(), SALT, hw);
        assert!(ok.activated, "reason: {}", ok.reason);

        // Different machine -> locked (node mismatch).
        let other = license::verify(dir.path(), SALT, "hw-machine-beta");
        assert!(!other.activated);
        assert!(other.reason.contains("Hardware mismatch"));

        // Wrong salt -> locked (forged/tampered signature).
        let forged = license::verify(dir.path(), b"wrong-salt", hw);
        assert!(!forged.activated);
        assert!(forged.reason.contains("signature invalid"));
    }

    #[test]
    fn vault_deduct_and_tamper_detection() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        let hw = "hw-machine-alpha";

        let s = vault::initialize(&db, SALT, hw, 10).unwrap();
        assert_eq!(s.balance, 10);

        let s = vault::deduct(&db, SALT, hw, 1, "check").unwrap();
        assert_eq!(s.balance, 9);

        // Hand-edit the balance directly in SQLite -> integrity fails.
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            conn.execute("UPDATE ledger SET balance = 999999 WHERE id = 1", [])
                .unwrap();
        }
        match vault::read_state(&db, SALT, hw) {
            Err(vault::VaultError::Tampered) => {}
            other => panic!("expected Tampered, got {other:?}"),
        }
    }

    #[test]
    fn vault_foreign_machine_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        vault::initialize(&db, SALT, "machine-A", 5).unwrap();
        // Same db, different machine asking -> rejected.
        match vault::read_state(&db, SALT, "machine-B") {
            Err(vault::VaultError::Tampered) | Err(vault::VaultError::ForeignMachine) => {}
            other => panic!("expected rejection, got {other:?}"),
        }
    }

    #[test]
    fn vault_insufficient_balance() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        let hw = "hw-insufficient";
        vault::initialize(&db, SALT, hw, 1).unwrap();
        vault::deduct(&db, SALT, hw, 1, "check").unwrap();
        match vault::deduct(&db, SALT, hw, 1, "check") {
            Err(vault::VaultError::Insufficient { have: 0, need: 1 }) => {}
            other => panic!("expected Insufficient, got {other:?}"),
        }
    }

    #[test]
    fn vault_tokens_tamper_detection() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        let hw = "hw-tokens-tamper";
        vault::initialize(&db, SALT, hw, 10).unwrap();

        // Redeem a token to populate redeemed_tokens
        let signing = new_signing();
        let pub_pem = signing
            .verifying_key()
            .to_public_key_pem(ed25519_dalek::pkcs8::spki::der::pem::LineEnding::LF)
            .unwrap();
        let future = "2999-01-01T00:00:00+00:00";
        let token = mint_token(&signing, 25, "nonce-to-delete", future);
        refill::redeem_token(&db, SALT, &pub_pem, &token, hw).unwrap();

        // Check state read works fine
        let state = vault::read_state(&db, SALT, hw).unwrap();
        assert_eq!(state.balance, 35);

        // Delete the nonce from redeemed_tokens directly in SQLite -> tamper detected!
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            conn.execute("DELETE FROM redeemed_tokens WHERE nonce = 'nonce-to-delete'", [])
                .unwrap();
        }
        match vault::read_state(&db, SALT, hw) {
            Err(vault::VaultError::Tampered) => {}
            other => panic!("expected Tampered due to tokens table edit, got {other:?}"),
        }
    }

    #[test]
    fn vault_rollback_detection() {
        std::env::set_var("KHALEEJNODE_TESTING_MOCK_KEYCHAIN", "1");
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        let hw = "hw-rollback-test";

        // Initialize -> writes 10 to DB and to mock keychain
        vault::initialize(&db, SALT, hw, 10).unwrap();

        // Backup db file
        let backup = dir.path().join("vault.db.bak");
        std::fs::copy(&db, &backup).unwrap();

        // Deduct 2 credits -> DB balance becomes 8, mock keychain balance becomes 8
        vault::deduct(&db, SALT, hw, 2, "check").unwrap();
        assert_eq!(vault::read_state(&db, SALT, hw).unwrap().balance, 8);

        // Restore backup db -> DB has 10 credits, but mock keychain has 8
        std::fs::copy(&backup, &db).unwrap();

        // Read state should detect rollback!
        match vault::read_state(&db, SALT, hw) {
            Err(vault::VaultError::Tampered) => {}
            other => panic!("expected Tampered due to rollback, got {other:?}"),
        }
        std::env::remove_var("KHALEEJNODE_TESTING_MOCK_KEYCHAIN");
    }

    fn mint_token(signing: &SigningKey, credits: i64, nonce: &str, expires: &str) -> String {
        // Byte-compatible with tools/issue_token.py: sign the raw payload bytes.
        let payload = serde_json::json!({
            "credits": credits,
            "nonce": nonce,
            "package": "starter",
            "issued": "2026-01-01T00:00:00+00:00",
            "expires": expires,
        });
        let payload_bytes = serde_json::to_vec(&payload).unwrap();
        let sig = signing.sign(&payload_bytes);
        format!("{}.{}", b64u(&payload_bytes), b64u(&sig.to_bytes()))
    }

    #[test]
    fn refill_ed25519_roundtrip_replay_and_expiry() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("vault.db");
        let hw = "hw-refill";
        vault::initialize(&db, SALT, hw, 10).unwrap();

        let signing = new_signing();
        let pub_pem = signing
            .verifying_key()
            .to_public_key_pem(ed25519_dalek::pkcs8::spki::der::pem::LineEnding::LF)
            .unwrap();

        let future = "2999-01-01T00:00:00+00:00";
        let token = mint_token(&signing, 25, "nonce-abc-123456", future);

        // Valid redemption.
        let r = refill::redeem_token(&db, SALT, &pub_pem, &token, hw).unwrap();
        assert_eq!(r.credits_added, 25);
        assert_eq!(r.new_balance, 35);

        // Replay -> rejected.
        match refill::redeem_token(&db, SALT, &pub_pem, &token, hw) {
            Err(refill::RefillError::AlreadyRedeemed) => {}
            other => panic!("expected AlreadyRedeemed, got {other:?}"),
        }

        // Forged signature (wrong key) -> rejected.
        let attacker = new_signing();
        let bad = mint_token(&attacker, 9999, "nonce-evil-000000", future);
        match refill::redeem_token(&db, SALT, &pub_pem, &bad, hw) {
            Err(refill::RefillError::BadSignature) => {}
            other => panic!("expected BadSignature, got {other:?}"),
        }

        // Expired -> rejected.
        let expired = mint_token(&signing, 5, "nonce-old-7777777", "2000-01-01T00:00:00+00:00");
        match refill::redeem_token(&db, SALT, &pub_pem, &expired, hw) {
            Err(refill::RefillError::Expired) => {}
            other => panic!("expected Expired, got {other:?}"),
        }
    }
}
