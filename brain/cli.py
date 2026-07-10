#!/usr/bin/env python3
"""
brain/cli.py
============
Command-line entrypoint for the Brain, used as the Tauri backend's out-of-process
extraction engine. In development the Rust `process` command runs
`python3 brain/cli.py --file <path>`; in production this module is packaged with
PyInstaller into a `khaleej-brain` binary and shipped as a Tauri sidecar.

Reads a document, runs the multimodal extraction simulator + the adversarial
customs rules engine, and prints a single JSON object to stdout:

    {"manifest": {...}, "validation": {...}, "customs_submission": {...}}

It performs NO licensing and NO vault access — the Rust Shield owns all of that
and only calls this after a credit is confirmed available.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running directly as `python brain/cli.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import extractor, rules, vision
from brain.schema import manifest_to_dict


DISCLAIMER = (
    "Assistive pre-check only — NOT customs/legal advice and NOT an official "
    "submission. A qualified human must verify every result against official "
    "tariff and sanctions sources before acting."
)


def submission(report: "rules.ValidationReport", engine: str) -> dict:
    data_mode = (report.compliance or {}).get("data_mode", "SAMPLE")

    # Fail-safe: never auto-clear. A result is only "READY" when it passed AND
    # ran on OFFICIAL data. Otherwise a human must review it.
    if not report.passed:
        status, would_submit = "HELD", False
        note = "Blocking findings present. Withheld until resolved."
    elif data_mode != "OFFICIAL":
        status, would_submit = "REVIEW", False
        note = "No blocking findings, but reference data is non-authoritative — human review required."
    else:
        status, would_submit = "READY", True
        note = "All blocking checks passed against official reference data."

    human_review_required = status != "READY"
    return {
        "status": status,
        "would_submit": would_submit,
        "human_review_required": human_review_required,
        "data_mode": data_mode,
        "engine": engine,
        "note": f"{note} · engine: {engine}",
        "disclaimer": DISCLAIMER,
    }


def choose_and_extract(file_bytes: bytes, filename: str):
    """Select the extraction engine and run it. Returns (manifest, engine_name).

    KHALEEJNODE_EXTRACTOR:
        "vision" -> require the local vision model (error if unavailable),
        "mock"   -> always use the deterministic simulator,
        "auto"   -> use vision when available, else fall back to the simulator
                    (the default).
    """
    mode = os.environ.get("KHALEEJNODE_EXTRACTOR", "auto").lower()

    if mode in ("vision", "auto"):
        if vision.available():
            try:
                manifest = vision.extract_with_vision(file_bytes, filename)
                return manifest, f"llama-vision:{vision.MODEL}"
            except vision.VisionError as exc:
                if mode == "vision":
                    raise
                sys.stderr.write(f"[vision fallback -> mock] {exc}\n")
        elif mode == "vision":
            raise vision.VisionError(
                "Vision engine requested but Ollama/model is not available on localhost."
            )

    manifest = extractor.simulate_extraction(file_bytes, filename)
    return manifest, "mock-simulator"


def run(file_path: str) -> dict:
    path = Path(file_path)
    try:
        file_bytes = path.read_bytes()
    except OSError:
        file_bytes = file_path.encode()

    manifest, engine = choose_and_extract(file_bytes, path.name)
    report = rules.validate(manifest)

    return {
        "manifest": manifest_to_dict(manifest),
        "validation": report.to_dict(),
        "customs_submission": submission(report, engine),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="KhaleejNode Brain — extraction + rules.")
    ap.add_argument("--file", required=True, help="path to the document to process")
    args = ap.parse_args()

    try:
        result = run(args.file)
    except Exception as exc:  # noqa: BLE001 - surface as nonzero exit for the caller
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
