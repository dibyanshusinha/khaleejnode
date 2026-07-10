//! Node-lock hardware fingerprint (Rust port of core/hardware.py).
//!
//! Combines the platform/motherboard UUID with the primary MAC address and
//! hashes them with SHA-256 to derive a stable HARDWARE ID. The scheme mirrors
//! the Python core so the concept is identical, though the concrete id value is
//! Rust-native (the Rust core mints and verifies its own license).

use sha2::{Digest, Sha256};

#[derive(Debug, Clone, serde::Serialize)]
pub struct HardwareIdentity {
    pub motherboard_uuid: String,
    pub mac_address: String,
    pub hardware_id: String,
}

fn read_motherboard_uuid() -> String {
    match machine_uid::get() {
        Ok(id) if !id.is_empty() => id.to_lowercase(),
        _ => {
            // Deterministic fallback derived from the MAC so we never panic.
            let mac = read_primary_mac();
            let digest = hex::encode(Sha256::digest(mac.as_bytes()));
            format!("mock-mb-{}", &digest[..32])
        }
    }
}

fn read_primary_mac() -> String {
    match mac_address::get_mac_address() {
        Ok(Some(addr)) => addr.to_string().to_lowercase(),
        _ => "00:00:00:00:00:00".to_string(),
    }
}

pub fn resolve_identity() -> HardwareIdentity {
    let mb = read_motherboard_uuid();
    let mac = read_primary_mac();
    let combined = format!("MB::{}||MAC::{}", mb, mac);
    let hardware_id = hex::encode(Sha256::digest(combined.as_bytes()));
    HardwareIdentity {
        motherboard_uuid: mb,
        mac_address: mac,
        hardware_id,
    }
}

pub fn get_hardware_id() -> String {
    resolve_identity().hardware_id
}
