# KhaleejNode — Tauri Port (Rust Shield first)

This document covers the migration of KhaleejNode from the Python reference
build to a native **Tauri 2** desktop app. The security-critical core (the
Shield) is now **compiled Rust**; the multimodal Brain stays **Python**, invoked
out-of-process; the web UI is **unchanged** except for the transport.

## Why this shape

Tauri's backend is Rust and cannot run Python in-process, but the Brain's value
is its Python ML ecosystem. So KhaleejNode-on-Tauri is deliberately polyglot:

```
┌──────────────────────────┐   invoke()    ┌───────────────────────────────┐
│  WEB UI  (ui/, unchanged) │ ───────────▶  │  RUST SHIELD  (compiled)      │
│  window.__TAURI__.core    │               │  core-rs/  → src-tauri/       │
│  .invoke("status" | ...)  │ ◀───────────  │  node-lock · HMAC vault ·     │
└──────────────────────────┘   JSON         │  Ed25519 verify · salt→keyring│
                                            └───────────────┬───────────────┘
                                              spawns process │  --file <tmp>
                                            ┌───────────────▼───────────────┐
                                            │  PYTHON BRAIN  (sidecar)      │
                                            │  brain/cli.py → PyInstaller   │
                                            │  extraction + adversarial rules│
                                            └───────────────────────────────┘
```

The HTTP bridge (`server/app.py`) is **gone** — Tauri IPC replaces it.

## What maps where

| Python reference | Tauri target | Status |
|---|---|---|
| `core/config.py` (salt constant) | `core-rs/src/config.rs` — salt from env → **OS keychain** → dev fallback | ✅ ported |
| `core/hardware.py` | `core-rs/src/hardware.rs` (`machine-uid` + `mac_address`) | ✅ ported |
| `core/license.py` | `core-rs/src/license.rs` (HMAC-SHA256, constant-time) | ✅ ported |
| `core/vault.py` | `core-rs/src/vault.rs` (`rusqlite`, HMAC row-signing) | ✅ ported |
| `core/refill.py` | `core-rs/src/refill.rs` (`ed25519-dalek` verify) | ✅ ported |
| `server/app.py` | `src-tauri/src/main.rs` — `#[command]` handlers | ✅ ported |
| `brain/*.py` | packaged into the `khaleej-brain` PyInstaller sidecar | ✅ packaged |
| `ui/*` | unchanged; `fetch` → `invoke` (auto-detects Tauri) | ✅ wired |
| `tools/*` | unchanged — vendor-side, never ships | ✅ as-is |

**Cross-compatibility:** vendor tokens minted by the Python `tools/issue_token.py`
verify in Rust, because Ed25519 verification runs over the exact base64url-decoded
payload bytes the vendor signed — JSON formatting is irrelevant. This is covered
by the `refill_ed25519_roundtrip_replay_and_expiry` test.

## What is verified

- **Rust Shield unit tests — 6/6 pass** (`cd core-rs && cargo test`):
  hardware-id stability; license roundtrip + node-mismatch + forged-signature;
  vault deduct + hand-edit tamper detection; foreign-machine rejection;
  insufficient-balance; Ed25519 refill roundtrip + replay + forged-key + expiry.
- **Tauri backend type-checks** against Tauri 2.11 + the core crate
  (`cd src-tauri && cargo check`).
- **Brain CLI** emits the exact JSON contract the Rust `process` command consumes
  (`python brain/cli.py --file <path>`).
- **Packaged Brain sidecar** (`khaleej-brain`, ~7 MB) runs **standalone in a
  clean environment** — no venv, no Python on PATH, `pydantic`/`pydantic_core`
  frozen in — and returns the same JSON. This is the exact subprocess the Rust
  `process` command spawns.
- **`tauri info`** reports a green toolchain (rustc, cargo, node, Xcode CLT).

> The GUI window itself launches with `cargo tauri dev`, which needs a desktop
> session — run it on your Mac (below). Every layer it depends on is verified above.

## Security upgrades this port unlocks

- **Salt out of source and off disk:** `core-rs/src/config.rs` resolves the salt
  from the **OS keychain** (`keyring` crate) — provisioned once via
  `config::provision_salt()` — instead of a source constant.
- **Native compiled core:** the Shield is machine code, not `.py`/`.pyc` — no
  bytecode to decompile back to the salt or the signing logic.
- **SQLCipher-ready vault:** `vault.rs::open_db()` is the single swap-point.
  Change the `rusqlite` feature to `bundled-sqlcipher-vendored-openssl` and issue
  `PRAGMA key` there for encrypted pages — the ledger logic is untouched.

## Running it on your machine

```bash
# 1. one-time: Python venv with the Brain's deps
./setup.sh --no-launch          # creates .venv, installs pydantic/cryptography

# 2. one-time: Tauri CLI
npm install

# 3. launch the desktop app (Rust backend + venv Python Brain + ui/)
./run_tauri.sh                  # == npx tauri dev, with KHALEEJNODE_PYTHON preset
```

`run_tauri.sh` points the Rust backend's Brain bridge at `.venv/bin/python` so
`pydantic` resolves in dev. The window boots node-locked and seeded with 10
credits exactly like the Python build.

## Shipping (production)

1. **Package the Brain** as a sidecar binary (already wired):
   ```bash
   ./scripts/build_brain_sidecar.sh
   ```
   Produces `src-tauri/binaries/khaleej-brain-<target-triple>`.
   `tauri.conf.json` already declares `"externalBin": ["binaries/khaleej-brain"]`,
   so `tauri build` bundles it next to the app executable and the Rust backend
   resolves it automatically via `resolve_brain_binary()` — **no `python3`
   dependency on the client**. (Rebuild the sidecar on each target OS/arch you
   ship to; it is platform-specific and git-ignored.)
2. **Provision the salt** into the OS keychain on first run and rotate per build.
3. **Enable SQLCipher** at the `open_db()` seam.
4. `npm run build` (`tauri build`) to produce signed `.app` / `.dmg` /
   `.msi` / `.AppImage` installers. Add code-signing + notarization for
   distribution.

## Project layout (added by the port)

```
khaleejnode/
├── core-rs/                     ← the Rust Shield (library crate, unit-tested)
│   ├── Cargo.toml
│   └── src/{lib,config,hardware,license,vault,refill}.rs
├── src-tauri/                   ← the Tauri desktop app
│   ├── Cargo.toml  build.rs  tauri.conf.json
│   ├── capabilities/default.json
│   ├── icons/
│   └── src/main.rs              ← #[command] handlers (status/refill_token/process)
├── brain/cli.py                 ← Brain entrypoint (dev bridge + sidecar source)
├── scripts/build_brain_sidecar.sh
├── run_tauri.sh                 ← dev launcher
└── package.json                 ← Tauri CLI
```
