---
name: airgapped_hardening
description: Transform standard software projects into secure, node-locked, pay-per-use, airgapped appliances.
---

# Secure Airgapped Software Hardening

This skill provides step-by-step instructions for hardening an application to run 100% offline (airgapped) under a pay-per-use or node-locked commercial model without requiring a network connection, ensuring it cannot be easily bypassed, cloned, or tampered with.

---

## 1. Architectural Decoupling (Presentation vs Core)

To secure the application, strictly decouple the Presentation (GUI) from the Secure Core (Shield):
1. **Dumb GUI**: The frontend (HTML/CSS/JS or desktop shell) holds no authority, no secrets, and no security logic. It only fetches state and posts user intents.
2. **Authoritative Core**: The backend (Secure Core) is written in a compiled language (e.g., Rust or compiled C/Python) and performs all checks (licensing, credit deduction, cryptographic validation).
3. **Out-of-Process Brain**: Heavy dependencies (ML models, document parsers) run as isolated out-of-process subprocesses or sidecars to keep the Core lightweight and compile-friendly.

---

## 2. Hardware Node-Locking (Lock 1)

Prevent copying the application instance to another physical machine:
1. **Gather Fingerprints**: Extract hardware characteristics of the host system:
   - **macOS**: `ioreg -rd1 -c IOPlatformExpertDevice` for the platform UUID.
   - **Windows**: `wmic csproduct get UUID` for the hardware UUID.
   - **Linux**: Read `/sys/class/dmi/id/product_uuid` or `/etc/machine-id`.
   - **Network**: Read the primary MAC address as a secondary fallback.
2. **Compute Node ID**: Concatenate parameters and compute a SHA-256 hash:
   $$\text{Hardware ID} = \text{SHA-256}(\text{"MB::" + motherboard\_uuid + "||MAC::" + primary\_mac})$$
3. **License Verification**: Generate a `license.key` containing this Node ID, issued metadata, and an HMAC-SHA256 signature over the payload, keyed by `SECRET_SALT`. On boot, compare the live hardware Node ID against the signed Node ID inside the license key.

---

## 3. Transaction-Safe, Tamper-Evident Ledger (Lock 2)

Maintain a local credit/transaction ledger (e.g., SQLite `vault.db`) that prevents manual edits:
1. **Ledger Row Signature**: Protect the ledger row (e.g., balance) by signing it at the application layer:
   $$\text{Signature} = \text{HMAC-SHA256}(\text{SECRET\_SALT}, \text{balance} \parallel \text{hardware\_id} \parallel \text{nonce} \parallel \text{tokens\_signature})$$
2. **Nonces and Upgrades**: On every deduction or credit, generate a fresh random nonce to prevent replay/record attacks, and re-sign the row.
3. **Spent Tokens Signature**: To protect the table tracking redeemed nonces (`redeemed_tokens`), compute an HMAC signature over all sorted nonces currently in the table, store it as `tokens_signature` inside the ledger, and include it in the main ledger row signature.
4. **Database Transactions**: Wrap all read-update operations (like checking balance, inserting a nonce, and writing the new balance) in a single database transaction (`BEGIN TRANSACTION` ... `COMMIT` / `ROLLBACK`) to guarantee atomicity.

---

## 4. Rollback & Replay Mitigation (Lock 3)

Prevent database state restoration (restoring a backup `vault.db` to reset spent credits):
1. **OS Keychain Synchronization**: Store the current balance inside the OS Keychain (using `keyring` service `"com.khaleejnode.app"` and user name `"vault_balance_" + hardware_id`).
2. **Verification Assertions**: During balance verification, read both the database balance and the OS Keychain balance:
   - If they do not match, raise a `VaultTamperError` and lock the application.
3. **Testing Bypasses**: Implement testing/CI bypass switches (like checking `KHALEEJNODE_TESTING` environment variable) and use mock in-memory keychains for unit test suites to prevent test runner collisions.

---

## 5. Offline Credit Refills (Asymmetric Cryptography)

Enable offline credit refills without server communication:
1. **Asymmetric Keypair**: Use asymmetric cryptography (Ed25519):
   - **Private Key**: Held only by the vendor server (never distributed).
   - **Public Key**: Compiled/embedded into the client application.
2. **Token Redemption**: When a user purchases credits, issue a cryptographically signed token string:
   $$\text{Token} = \text{Base64Url}(\text{payload\_json}) \parallel \text{"."} \parallel \text{Base64Url}(\text{signature})$$
3. **Replay Protection**: Extract the token's unique nonce, check if it exists in the `redeemed_tokens` table within the same transaction, verify the signature with the public key, and apply the credits.

---

## 6. Compilation & Hardening

1. **Obfuscate Secrets**: Embed the `SECRET_SALT` inside the binary image and obfuscate/split it at build-time.
2. **Native Compilation**: Compile the Secure Core to platform-native machine code (e.g. via Cargo for Rust, or Cython/Nuitka for Python core) to strip debugging symbols and bytecode, making disassembly extremely tedious.
3. **Data Protection**: Encrypt database pages at rest by integrating SQLCipher into the database connection factory.
