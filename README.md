# KhaleejNode — Enterprise TradeTech Node

**A 100% offline, node-locked, pay-per-check customs-document intelligence appliance for the UAE / GCC trade corridor.**

KhaleejNode ingests shipping PDFs, extracts a structured UAE Customs Manifest, runs an *adversarial* pre-clearance audit against it, and charges the operator exactly **one prepaid credit per successful document check** — all without ever touching a network. Credits are refilled offline by pasting a cryptographically signed vendor token. The whole product is welded to the physical machine it was licensed on, and its balance ledger cannot be edited by hand.

This repository is a **complete, runnable reference build**. The heavy vision-AI extraction is replaced by a deterministic local simulator so the entire pay-per-check mechanism runs out of the box with no external services.

---

## Table of contents

1. [What you get](#what-you-get)
2. [Architecture](#architecture)
3. [Defense specification](#defense-specification-why-this-is-hard-to-pirate)
4. [Directory layout](#directory-layout)
5. [Testing guide — run it right now](#testing-guide--run-it-right-now)
6. [The three attack drills](#the-three-attack-drills)
7. [Business defaults chosen for you](#business-defaults-chosen-for-you)
8. [Production hardening checklist](#production-hardening-checklist)

---

## What you get

| Layer | Module(s) | Responsibility |
|---|---|---|
| **Presentation (GUI)** | `ui/` | Native-feeling local dashboard: license/lock view, drag-and-drop PDF zone, live prepaid-credit counter, token redemption field, structured JSON + adversarial-flag output log. |
| **Secure Core (The Shield)** | `core/` | Node-locking, HMAC-signed token vault, offline public-key credit refill. Designed to be compiled to a native binary. |
| **Data Pipeline (The Brain)** | `brain/` | Pydantic UAE customs manifest schema, multimodal extraction simulator, adversarial customs rules engine. |
| **Bridge** | `server/` | Loopback-only HTTP bridge that keeps the GUI dumb and the Core authoritative. |
| **Vendor tooling** | `tools/` | Ed25519 keypair generation and offline credit-token minting. |
| **Init & launch** | `setup.sh`, `run.py` | One command: build env, node-lock a license to *this* machine, seed the vault, launch. |

---

## Architecture

### The decoupling principle: a dumb GUI in front of an authoritative core

The single most important architectural decision is that **the presentation layer holds no authority and no secrets.** The browser UI (`ui/app.js`) can only do two things:

1. **Render state** it fetched from `GET /api/status`.
2. **Post intents** — "redeem this token", "process this file" — to the core bridge.

It never computes a balance, never verifies a license, never sees the `SECRET_SALT`, and never touches `vault.db`. Every decision that has value attached to it is delegated across a process boundary to the **Secure Core**, which in production is a compiled native binary.

```
┌─────────────────────────────┐        loopback HTTP (127.0.0.1)        ┌──────────────────────────────┐
│      PRESENTATION LAYER      │  ───────────────────────────────────▶  │        SECURE CORE           │
│      ui/ (HTML/CSS/JS)       │      GET /api/status                    │        core/  (The Shield)   │
│                              │      POST /api/refill  {token}          │  • hardware fingerprint      │
│  • lock view                 │      POST /api/process {file}           │  • license verification      │
│  • drag & drop               │  ◀───────────────────────────────────  │  • HMAC-signed token vault   │
│  • credit counter            │      JSON state / results               │  • Ed25519 token redemption  │
│  • output log                │                                         │                              │
│    (NO secrets, NO authority)│                                         │  SECRET_SALT + private logic │
└─────────────────────────────┘                                         │  live ONLY here (compiled)   │
                                                                         └──────────────┬───────────────┘
                                                                                        │ calls
                                                                         ┌──────────────▼───────────────┐
                                                                         │        THE BRAIN  brain/     │
                                                                         │  Pydantic manifest schema    │
                                                                         │  extraction simulator        │
                                                                         │  adversarial rules engine    │
                                                                         └──────────────────────────────┘
```

Because the boundary is a **narrow message interface** (three endpoints), the transport is swappable. Today it is `http.server` on loopback. Tomorrow it is a Tauri `#[command]` or a Rust FFI call. **The UI does not change** when you swap it — that is the payoff of decoupling. An attacker who fully controls the browser layer gains nothing: they can forge a *request*, but they cannot forge the core's *answer*, because the answer depends on the salt and private key that only exist inside the compiled core.

### Request lifecycle of one paid document check

1. User drops `manifest.pdf` onto the dropzone. `app.js` base64-encodes it and `POST`s to `/api/process`.
2. `server/app.py` → `service_process()` calls **`core.license.require_activation()`**. If the machine's live hardware fingerprint doesn't match the signed `license.key`, the request is rejected `403` and nothing is charged.
3. The core reads the vault via **`core.vault.read_state()`**, which first **re-computes the HMAC row-signature** and refuses if it doesn't match the stored one (tamper) or if the vault is bound to a different machine (copied file).
4. If the balance is ≥ 1, **The Brain** runs: `brain.extractor.simulate_extraction()` produces a typed `CustomsManifest`, then `brain.rules.validate()` runs the adversarial audit.
5. Only on success does **`core.vault.deduct(1)`** write a *new* balance with a *fresh* nonce and a *fresh* HMAC signature, appending an audit-log row.
6. The structured manifest + validation flags + "would we submit to UAE Customs?" verdict are returned and rendered in the log.

---

## Defense specification (why this is hard to pirate)

There are three independent locks. Defeating one does not defeat the others.

### Lock 1 — Hardware binding: *you cannot copy the image to another machine*

`core/hardware.py` reads two host-specific endpoints:

- **Motherboard / platform UUID** (`ioreg` on macOS, `/sys/class/dmi/id/product_uuid` on Linux, `wmic csproduct` on Windows).
- **Primary MAC address** (`uuid.getnode()`).

They are concatenated with delimiters and hashed:

```
HARDWARE_ID = SHA-256( "MB::<motherboard_uuid>||MAC::<primary_mac>" )
```

At install time, `run.py` writes a `state/license.key` containing this `HARDWARE_ID` plus an **HMAC-SHA256 signature** over the payload, keyed by the secret salt. On **every boot**, `core/license.py`:

1. recomputes the live `HARDWARE_ID` from the current silicon,
2. verifies the license signature (proves it was minted by genuine vendor tooling holding the salt),
3. compares the licensed `HARDWARE_ID` against the live one with a **constant-time** compare.

> **Copy the entire installed folder — `license.key`, `vault.db`, binaries, everything — to a second laptop, and step 3 fails.** The second laptop has a different motherboard UUID and MAC, so its live `HARDWARE_ID` differs, and the license no longer matches. The app boots straight into the **locked view**. There is no "offline grace" bypass: no valid signature for the new hardware exists without the vendor's salt.

### Lock 2 — HMAC row-signing: *you cannot edit the balance in the database*

The vault (`core/vault.py`) is a local SQLite file. The single balance row is protected by an application-layer integrity envelope:

```
row_signature = HMAC-SHA256( SECRET_SALT , "<balance>||<hardware_id>||<nonce>" )
```

Every time the balance changes (a deduction or a refill), the core generates a **fresh random nonce** and re-signs the row. On every read, the core recomputes the HMAC and rejects the vault if it doesn't match.

> **Open `vault.db` in any SQLite editor and set `balance = 999999`.** On the next read, `core.vault.read_state()` recomputes the HMAC over your edited balance, gets a value that does *not* equal the stored signature (which was computed over the old balance), and raises **`VaultTamperError`**. The UI shows a vault-integrity error instead of your fake balance. To forge a matching signature you'd need `SECRET_SALT`, which lives only inside the compiled core — never in the database, never in the UI, never on disk in plaintext in a shipped build.
>
> Two further properties: the signature includes the **`hardware_id`**, so a high-balance `vault.db` lifted from a *legitimate* install is rejected on *your* machine (wrong hardware). And because each write rotates the **nonce**, you cannot "record" a valid signed high-balance row and replay it after spending down — the nonce won't line up.

### Lock 3 — Asymmetric refill tokens: *you cannot mint your own credits*

Refills are **offline** and use public-key cryptography (`core/refill.py`, `tools/issue_token.py`):

- The **vendor** holds an **Ed25519 private key** (`tools/vendor_private.pem`). It signs each credit token: `base64url(payload) + "." + base64url(signature)`, where `payload = {credits, nonce, package, issued, expires}`.
- The **client** ships only the **Ed25519 public key** (`core/keys/vendor_public.pem`). It can *verify* a signature but **cannot produce** one.

> A pirate can read the public key — that's fine, it's public. What they cannot do is **sign a new payload** granting themselves 10,000 credits, because that requires the private key the vendor never distributes. Tokens are additionally **single-use** (the nonce is recorded in `redeemed_tokens`; a second redemption of the same string is rejected) and **time-boxed** (a 90-day expiry). *(In the zero-dependency fallback where `cryptography` isn't installed, the signer degrades to an HMAC scheme so the demo still runs; the asymmetric path is the production default and the one you ship.)*

### Lock 4 (build-time) — *you cannot decompile it back to readable source*

Python `.py` is trivially readable, so the salt and licensing logic **must not ship as source**. The core is written as a self-contained package (`core/`) with no notebook-style globals precisely so it can be compiled ahead of distribution:

- **Cython / Nuitka** compiles `core/*.py` into platform-native machine code (`.so`/`.pyd`/a single executable). There is no `.py` and no `.pyc` bytecode to decompile — the salt and the HMAC/verify logic become opaque compiled instructions.
- The `SECRET_SALT` is read from a constant (overridable by env for rotation). In the compiled binary it is embedded in the binary image; harden further by splitting it, deriving it at runtime, or fetching it from an OS keystore.
- Combine with a bytecode-stripping build and, optionally, a commercial packer for anti-tamper / anti-debug.

The architecture is what makes this viable: because **all** value-bearing logic already lives behind the `core/` boundary and nothing secret leaks into `ui/` or `server/`, you compile exactly one directory and the attack surface for "read the secret from source" drops to zero.

---

## Directory layout

```
khaleejnode/
├── README.md                 ← you are here
├── setup.sh                  ← one-command init + launch (bash)
├── run.py                    ← bootstrapper + launcher (python)
├── requirements.txt
│
├── core/                     ← THE SHIELD  (compile this to native)
│   ├── __init__.py
│   ├── config.py             ← paths, salt, business defaults
│   ├── hardware.py           ← motherboard UUID + MAC → SHA-256 node id
│   ├── license.py            ← node-locked license.key gen + verify
│   ├── vault.py              ← HMAC row-signed prepaid credit ledger
│   ├── refill.py             ← Ed25519 offline credit-token redemption
│   └── keys/
│       └── vendor_public.pem ← (generated) public verify key — safe to ship
│
├── brain/                    ← THE BRAIN
│   ├── __init__.py
│   ├── schema.py             ← Pydantic UAE Customs Manifest contract
│   ├── extractor.py          ← deterministic multimodal extraction simulator
│   └── rules.py              ← adversarial customs validation engine
│
├── server/                   ← loopback bridge (keeps GUI dumb)
│   ├── __init__.py
│   └── app.py
│
├── ui/                       ← PRESENTATION LAYER
│   ├── index.html
│   ├── styles.css
│   └── app.js
│
├── tools/                    ← VENDOR-ONLY tooling (do not ship to clients)
│   ├── __init__.py
│   ├── keygen.py             ← generate Ed25519 keypair
│   ├── issue_token.py        ← mint signed offline credit tokens
│   └── vendor_private.pem    ← (generated) PRIVATE key — never distribute
│
└── state/                    ← (generated at runtime) license.key + vault.db
```

---

## Testing guide — run it right now

### Prerequisites
- Python **3.9+**
- macOS, Linux, or Windows (WSL). The commands below are macOS/Linux.

### Step 1 — Launch (one command)

```bash
cd khaleejnode
chmod +x setup.sh
./setup.sh
```

`setup.sh` creates a `.venv`, installs `pydantic` + `cryptography`, generates the vendor keypair, **mints a license node-locked to your machine**, seeds the vault with **10 free credits**, and opens the dashboard.

> Prefer no virtualenv? `pip install -r requirements.txt && python run.py` does the same bootstrap.

Your browser opens **http://127.0.0.1:8787** showing the dashboard with a credit counter reading **10**.

### Step 2 — Run a paid document check

1. Drag **any** PDF (or any file — the extractor is content-seeded) onto the dropzone, or click to browse.
2. Click **Run Customs Check**.
3. Watch the counter tick **10 → 9**. The output log shows:
   - the extracted **structured JSON** manifest (click *Toggle structured JSON extraction*),
   - **adversarial flags** (`BLOCK` / `WARN` / `INFO`),
   - a **READY** or **HELD** customs-submission verdict.

Roughly one in three documents is seeded with a real defect (a vague `"cargo/parts"` line or an >5% weight discrepancy) so you can see the rules engine raise a `BLOCK` and withhold submission. Drop several different files to see both outcomes.

### Step 3 — Run out of credits

Keep processing documents until the balance hits **0**. The next check is refused with *"No prepaid credits remaining"* and the counter turns to a warning — the pay-per-check gate works.

### Step 4 — Refill offline with a signed token

In a second terminal (activate the venv first: `source .venv/bin/activate`):

```bash
python tools/issue_token.py --package starter        # 25 credits
# or a custom amount:
python tools/issue_token.py --credits 5 --print-json
```

Copy the printed token string, paste it into the **Add Prepaid Credits** box in the UI, and click **Redeem Token**. The balance jumps up. Paste the **same** token again → it is rejected as **already redeemed** (single-use replay protection).

---

## The three attack drills

Prove the defenses to yourself. Stop the server first (`Ctrl+C`) where noted.

### Drill A — Tamper with the balance (defeated by HMAC row-signing)

```bash
sqlite3 state/vault.db "UPDATE ledger SET balance = 999999 WHERE id = 1;"
python run.py --no-browser
```

Open the dashboard: instead of 999,999 credits you get a **vault integrity error**. The forged balance has no matching HMAC signature. Restore with `python run.py --reset`.

### Drill B — Copy the vault from "another machine" (defeated by hardware binding)

Simulate a foreign vault by rewriting its hardware id, then boot:

```bash
sqlite3 state/vault.db "UPDATE ledger SET hardware_id = 'someone-elses-machine' WHERE id = 1;"
python run.py --no-browser
```

Rejected: the vault is bound to a different `hardware_id` than this machine's live fingerprint. (Even without editing, the stored signature won't match once the hardware id column diverges.)

### Drill C — Forge a credit token (defeated by asymmetric signing)

Try to hand-craft a token or reuse a spent one — verification fails because you don't hold the vendor's Ed25519 private key, and spent nonces are permanently recorded. Only `tools/issue_token.py` (which reads the private key) can mint acceptable tokens.

---

## Business defaults chosen for you

So the build is complete and non-blocking, these opinionated defaults are set in `core/config.py` — change any of them freely:

| Setting | Default | Where |
|---|---|---|
| Free credits on first activation | **10** | `FIRST_ACTIVATION_GRANT` |
| Credit packages | starter **25** (AED 249), business **100** (AED 899), enterprise **500** (AED 3999) | `CREDIT_PACKAGES` |
| Token validity | **90 days** | `TOKEN_VALIDITY_DAYS` |
| Weight-discrepancy tolerance | **5%** (>10% escalates to `BLOCK`) | `brain/rules.py` |
| Secret salt | dev default (env-overridable via `KHALEEJNODE_SALT`) | `SECRET_SALT` |
| Region | United Arab Emirates (GCC) | `PRODUCT_REGION` |
| Loopback port | **8787** | `server/app.py` |

---

## Production hardening checklist

- [ ] **Compile `core/`** with Cython or Nuitka; ship no `.py`/`.pyc` for the shield.
- [ ] **Rotate `SECRET_SALT`** to a per-build value; consider splitting/deriving it and storing in the OS keychain (Keychain / DPAPI / libsecret).
- [ ] **Protect the private key** (`tools/vendor_private.pem`) on a vendor-side HSM or offline signing box — it must never reach a client.
- [ ] **Bind more endpoints** into the hardware fingerprint (disk serial, TPM/Secure Enclave attestation) for stronger node-lock.
- [ ] **Encrypt the vault at rest** by swapping stdlib `sqlite3` for SQLCipher at the `_connect()` seam in `core/vault.py`.
- [ ] **Replace the extraction simulator** in `brain/extractor.py` with your real multimodal model — the rest of the pipeline is unchanged because it only depends on the `CustomsManifest` contract.
- [ ] **Wire real UAE Customs submission** where `_mock_submission()` lives in `server/app.py`.
- [ ] Add **anti-debug / anti-tamper** packing and code-signing to the distributed binary.

---

*KhaleejNode is a reference architecture. The cryptographic primitives are real and correctly used; the "customs network" and the vision model are deliberately mocked so the offline pay-per-check economics run end-to-end on your machine today.*
