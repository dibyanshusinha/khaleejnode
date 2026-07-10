"""
server/app.py
=============
Local API bridge between the presentation layer (ui/) and the secure core.

Design intent: the GUI is *dumb*. It never sees SECRET_SALT, never touches the
vault, and never verifies a token. It only renders state and posts intents to
this loopback-only server, which delegates every security-sensitive decision to
the compiled core. That is the "decoupled presentation" property described in
the README: swap this HTTP bridge for a Tauri IPC command and the UI is
unchanged.

Runs on 127.0.0.1 only (never binds a public interface). Zero external network
calls -- 100% offline.

Endpoints:
    GET  /                      -> ui/index.html
    GET  /<static>             -> ui asset (css/js)
    GET  /api/status           -> license + balance + hardware summary
    POST /api/refill           -> {token} redeem credit token
    POST /api/process          -> {filename, content_base64} run a doc check
    GET  /api/audit            -> recent ledger events
"""

from __future__ import annotations

import base64
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from brain import extractor, rules
from brain.schema import manifest_to_dict
from core import config, license as licensing, refill, vault
from core.hardware import resolve_identity

HOST = "127.0.0.1"
DEFAULT_PORT = 8787


# ---------------------------------------------------------------------------
# Service layer (pure-ish; no HTTP objects leak in here)
# ---------------------------------------------------------------------------
def service_status() -> dict:
    ident = resolve_identity()
    lic = licensing.verify_license()

    balance = None
    vault_ok = True
    vault_message = ""
    if lic.activated:
        try:
            state = vault.read_state(ident.hardware_id)
            balance = state.balance
        except vault.VaultError as exc:
            vault_ok = False
            vault_message = str(exc)

    return {
        "product": {
            "name": config.PRODUCT_NAME,
            "edition": config.PRODUCT_EDITION,
            "version": config.PRODUCT_VERSION,
            "region": config.PRODUCT_REGION,
        },
        "activated": lic.activated,
        "license_reason": lic.reason,
        "hardware": {
            "hardware_id": ident.hardware_id,
            "hardware_id_short": ident.hardware_id[:16],
            "motherboard_uuid": ident.motherboard_uuid,
            "mac_address": ident.mac_address,
        },
        "balance": balance,
        "vault_ok": vault_ok,
        "vault_message": vault_message,
    }


def service_refill(token: str) -> dict:
    ident = licensing.require_activation()  # raises LicenseError if locked
    result = refill.redeem_token(token, ident)
    return {
        "credits_added": result.credits_added,
        "new_balance": result.new_balance,
        "package": result.package,
    }


def service_process(filename: str, file_bytes: bytes) -> dict:
    hardware_id = licensing.require_activation()

    # Ensure there is at least one credit BEFORE doing the work.
    state = vault.read_state(hardware_id)
    if state.balance < 1:
        raise vault.VaultError(
            "No prepaid credits remaining. Add credits to run document checks."
        )

    # The Brain: extract, then adversarially validate.
    manifest = extractor.simulate_extraction(file_bytes, filename)
    report = rules.validate(manifest)

    # Successful process -> deduct exactly one credit (tamper-evident write).
    new_state = vault.deduct(hardware_id, 1, detail=f"check:{filename}")

    return {
        "filename": filename,
        "new_balance": new_state.balance,
        "manifest": manifest_to_dict(manifest),
        "validation": report.to_dict(),
        "customs_submission": _mock_submission(report),
    }


def _mock_submission(report: rules.ValidationReport) -> dict:
    """Pretend to push to UAE Customs -- but only if it would pass."""
    if report.passed:
        return {
            "would_submit": True,
            "status": "READY",
            "note": "All BLOCK-level checks passed. (Offline demo: not actually transmitted.)",
        }
    return {
        "would_submit": False,
        "status": "HELD",
        "note": "Blocking flags present. Submission withheld until resolved.",
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "KhaleejNode/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.UI_DIR / rel).resolve()
        # Path-traversal guard: keep everything inside ui/.
        if config.UI_DIR.resolve() not in target.parents and target != config.UI_DIR.resolve():
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        route = urlparse(self.path).path
        try:
            if route == "/api/status":
                self._send_json(service_status())
            elif route == "/api/audit":
                self._send_json({"events": vault.audit_trail()})
            else:
                self._serve_static(route)
        except Exception as exc:  # noqa: BLE001 - never leak a stack to the client
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        payload = self._read_json()
        try:
            if route == "/api/refill":
                token = str(payload.get("token", ""))
                self._send_json(service_refill(token))
            elif route == "/api/process":
                filename = str(payload.get("filename", "document.pdf"))
                content_b64 = str(payload.get("content_base64", ""))
                try:
                    file_bytes = base64.b64decode(content_b64) if content_b64 else b""
                except Exception:  # noqa: BLE001
                    file_bytes = filename.encode()
                self._send_json(service_process(filename, file_bytes))
            else:
                self.send_error(404, "Not found")
        except licensing.LicenseError as exc:
            self._send_json({"error": str(exc), "locked": True}, status=403)
        except (vault.VaultError, refill.RefillError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=500)

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        return


def serve(port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    print(f"  KhaleejNode running at  http://{HOST}:{port}")
    print("  (loopback only, 100% offline). Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down KhaleejNode.")
        httpd.shutdown()


if __name__ == "__main__":
    import sys

    p = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    serve(p)
