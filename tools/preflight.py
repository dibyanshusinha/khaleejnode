#!/usr/bin/env python3
"""
tools/preflight.py
==================
Production readiness check. Prints a GO / NO-GO verdict for shipping, flagging
anything still in a development/sample posture that would be unsafe to release.

Run:  python tools/preflight.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []  # (level, title, detail)


def check(level: str, title: str, detail: str) -> None:
    RESULTS.append((level, title, detail))


def run_checks() -> None:
    # 1. Secret salt must not be the compiled-in development default.
    dev_salt = b"KhaleejNode::v1::a7f3c1e9d2b48f60::do-not-share"
    if config.SECRET_SALT == dev_salt:
        check("FAIL", "Secret salt", "Using the DEV default salt. Set KHALEEJNODE_SALT / provision the OS keychain.")
    else:
        check("PASS", "Secret salt", "Non-default salt in use.")

    # 2. Vendor keys.
    if config.VENDOR_PUBLIC_KEY_FILE.exists():
        check("PASS", "Vendor public key", "Present (needed to verify credit tokens).")
    else:
        check("FAIL", "Vendor public key", "Missing — run tools/keygen.py.")
    if config.VENDOR_PRIVATE_KEY_FILE.exists():
        check("WARN", "Vendor private key", "Present in repo — MUST NOT be bundled into any client distribution.")

    # 3. Compliance reference data provenance.
    try:
        from brain.compliance import screening, tariff

        if tariff.is_sample_data():
            check("FAIL", "HS tariff data", "SAMPLE tariff loaded. Load the licensed GCC/UAE tariff before production.")
        else:
            check("PASS", "HS tariff data", "Official tariff loaded.")
        if screening.is_sample_data():
            check("FAIL", "Sanctions data", "SAMPLE denied-party list. Load official OFAC/UN/EU/UK/UAE lists.")
        else:
            check("PASS", "Sanctions data", "Official denied-party lists loaded.")
    except Exception as exc:  # noqa: BLE001
        check("FAIL", "Compliance data", f"Could not load: {exc}")

    # 4. Extraction engine.
    try:
        from brain import vision

        if vision.available():
            check("PASS", "Vision model", f"{vision.MODEL} reachable via Ollama.")
        else:
            check("WARN", "Vision model", "Ollama/model not reachable — extraction falls back to the mock simulator.")
    except Exception as exc:  # noqa: BLE001
        check("WARN", "Vision model", f"Probe failed: {exc}")

    # 5. Accuracy evidence.
    check("WARN", "Accuracy validation", "Run benchmark/run_benchmark.py at scale; heavy-degradation accuracy is currently 0%.")

    # 6. Customs submission integration.
    check("WARN", "Mirsal 2 submission", "Not integrated — output is an internal pre-check, not an official filing.")


def main() -> None:
    run_checks()
    icons = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
    print("\n=== KhaleejNode production preflight ===\n")
    for level, title, detail in RESULTS:
        print(f"  {icons[level]} {title:22} {detail}")

    fails = sum(1 for l, _, _ in RESULTS if l == "FAIL")
    warns = sum(1 for l, _, _ in RESULTS if l == "WARN")
    print("\n" + "-" * 60)
    if fails:
        print(f"  VERDICT: NO-GO for public release — {fails} blocker(s), {warns} warning(s).")
        print("  Safe to run as a SUPERVISED PILOT with the in-app disclaimers.")
    elif warns:
        print(f"  VERDICT: CONDITIONAL GO — 0 blockers, {warns} warning(s) to accept/mitigate.")
    else:
        print("  VERDICT: GO.")
    print("-" * 60 + "\n")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
