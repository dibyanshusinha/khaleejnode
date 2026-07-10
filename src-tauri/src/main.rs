// KhaleejNode — Tauri desktop backend.
//
// This replaces the Python `server/app.py` loopback bridge with native Tauri
// IPC. The UI calls these `#[command]`s via `invoke(...)`. All value-bearing
// logic is delegated to the compiled `khaleej_core` Rust Shield; the Python
// Brain is invoked out-of-process (a sidecar in production, `python3` in dev).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::Command;

use base64::Engine;
use khaleej_core::{config, hardware, license, refill, vault};
use tauri::Manager;

// The vendor's PUBLIC verification key is compiled into the binary. It is safe
// to embed — it can only verify tokens, never mint them.
const VENDOR_PUBLIC_KEY: &str = include_str!("../../core/keys/vendor_public.pem");

fn state_dir(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .app_data_dir()
        .unwrap_or_else(|_| PathBuf::from("state"))
}

fn vault_path(app: &tauri::AppHandle) -> PathBuf {
    state_dir(app).join("vault.db")
}

/// First-run bootstrap: node-lock a license and seed the vault so the app is
/// immediately usable (mirrors run.py). Idempotent.
fn bootstrap(app: &tauri::AppHandle) {
    let salt = config::secret_salt();
    let ident = hardware::resolve_identity();
    let sd = state_dir(app);
    let _ = std::fs::create_dir_all(&sd);

    if !license::verify(&sd, &salt, &ident.hardware_id).activated {
        let _ = license::generate(&sd, &salt, &ident.hardware_id);
    }
    if vault::read_state(&vault_path(app), &salt, &ident.hardware_id).is_err() {
        let _ = vault::initialize(
            &vault_path(app),
            &salt,
            &ident.hardware_id,
            config::FIRST_ACTIVATION_GRANT,
        );
    }
}

#[tauri::command]
fn status(app: tauri::AppHandle) -> serde_json::Value {
    let salt = config::secret_salt();
    let ident = hardware::resolve_identity();
    let lic = license::verify(&state_dir(&app), &salt, &ident.hardware_id);

    let (balance, vault_ok, vault_message) = if lic.activated {
        match vault::read_state(&vault_path(&app), &salt, &ident.hardware_id) {
            Ok(s) => (Some(s.balance), true, String::new()),
            Err(e) => (None, false, e.to_string()),
        }
    } else {
        (None, true, String::new())
    };

    serde_json::json!({
        "product": {
            "name": config::PRODUCT_NAME,
            "edition": config::PRODUCT_EDITION,
            "version": config::PRODUCT_VERSION,
            "region": config::PRODUCT_REGION,
        },
        "activated": lic.activated,
        "license_reason": lic.reason,
        "hardware": {
            "hardware_id": ident.hardware_id,
            "hardware_id_short": &ident.hardware_id[..16.min(ident.hardware_id.len())],
            "motherboard_uuid": ident.motherboard_uuid,
            "mac_address": ident.mac_address,
        },
        "balance": balance,
        "vault_ok": vault_ok,
        "vault_message": vault_message,
    })
}

#[tauri::command]
fn refill_token(app: tauri::AppHandle, token: String) -> Result<serde_json::Value, String> {
    let salt = config::secret_salt();
    let ident = hardware::resolve_identity();
    let lic = license::verify(&state_dir(&app), &salt, &ident.hardware_id);
    if !lic.activated {
        return Err(lic.reason);
    }

    let r = refill::redeem_token(
        &vault_path(&app),
        &salt,
        VENDOR_PUBLIC_KEY,
        &token,
        &ident.hardware_id,
    )
    .map_err(|e| e.to_string())?;

    Ok(serde_json::json!({
        "credits_added": r.credits_added,
        "new_balance": r.new_balance,
        "package": r.package,
    }))
}

#[tauri::command]
fn process(
    app: tauri::AppHandle,
    filename: String,
    content_base64: String,
) -> Result<serde_json::Value, String> {
    let salt = config::secret_salt();
    let ident = hardware::resolve_identity();
    let lic = license::verify(&state_dir(&app), &salt, &ident.hardware_id);
    if !lic.activated {
        return Err(lic.reason);
    }

    // Require a credit BEFORE doing the work.
    let state = vault::read_state(&vault_path(&app), &salt, &ident.hardware_id)
        .map_err(|e| e.to_string())?;
    if state.balance < 1 {
        return Err("No prepaid credits remaining. Add credits to run document checks.".into());
    }

    // Materialize the upload to a temp file for the Brain.
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(content_base64.as_bytes())
        .unwrap_or_else(|_| filename.as_bytes().to_vec());
    let safe_name = filename.replace(['/', '\\'], "_");
    let tmp = std::env::temp_dir().join(format!("kn-{}", safe_name));
    std::fs::write(&tmp, &bytes).map_err(|e| e.to_string())?;

    // The Brain (extraction + adversarial rules) runs out-of-process.
    let brain = run_brain(&tmp);
    let _ = std::fs::remove_file(&tmp);
    let brain = brain?;

    // Successful process -> deduct exactly one credit.
    let new_state = vault::deduct(
        &vault_path(&app),
        &salt,
        &ident.hardware_id,
        1,
        &format!("check:{}", filename),
    )
    .map_err(|e| e.to_string())?;

    Ok(serde_json::json!({
        "filename": filename,
        "new_balance": new_state.balance,
        "manifest": brain.get("manifest").cloned().unwrap_or(serde_json::Value::Null),
        "validation": brain.get("validation").cloned().unwrap_or(serde_json::Value::Null),
        "customs_submission": brain.get("customs_submission").cloned().unwrap_or(serde_json::Value::Null),
    }))
}

/// Locate the bundled Brain sidecar binary, if present.
///
/// Resolution order:
///   1. `KHALEEJNODE_BRAIN` env override (explicit path),
///   2. the sidecar Tauri bundles next to the app executable (production),
///   3. `None` -> caller falls back to `python3 brain/cli.py` (dev).
fn resolve_brain_binary() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("KHALEEJNODE_BRAIN") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    let name = if cfg!(windows) {
        "khaleej-brain.exe"
    } else {
        "khaleej-brain"
    };
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let candidate = dir.join(name);
            if candidate.exists() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Invoke the Python Brain out-of-process. Uses the bundled sidecar binary when
/// available (production), else `python3 brain/cli.py` (dev).
fn run_brain(file: &Path) -> Result<serde_json::Value, String> {
    let output = if let Some(sidecar) = resolve_brain_binary() {
        Command::new(sidecar).arg("--file").arg(file).output()
    } else {
        let python = std::env::var("KHALEEJNODE_PYTHON").unwrap_or_else(|_| "python3".into());
        let cli = std::env::var("KHALEEJNODE_BRAIN_CLI").unwrap_or_else(|_| "brain/cli.py".into());
        Command::new(python).arg(cli).arg("--file").arg(file).output()
    };

    let output = output.map_err(|e| format!("failed to launch Brain: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "Brain error: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("Brain returned invalid JSON: {e}"))
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            bootstrap(&app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![status, refill_token, process])
        .run(tauri::generate_context!())
        .expect("error while running KhaleejNode");
}
