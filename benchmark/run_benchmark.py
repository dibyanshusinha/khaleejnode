#!/usr/bin/env python3
"""
benchmark/run_benchmark.py
=========================
Accuracy & degradation benchmark harness.

For each ground-truth fixture, renders a clean page, degrades it across severity
LEVELS, runs the extractor, and scores the result against the known answer key.
Outputs an accuracy-vs-degradation curve — the methodology behind the
"N documents at X% accuracy" traction metric.

Usage:
    python benchmark/run_benchmark.py                       # vision, all levels
    python benchmark/run_benchmark.py --engine mock         # fast pipeline check
    python benchmark/run_benchmark.py --levels clean,heavy  # subset
    python benchmark/run_benchmark.py --limit 1 --out report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark import degrade, evaluate, fixtures
from brain.schema import manifest_to_dict


def _extract(engine: str, image_bytes: bytes, name: str) -> dict:
    if engine == "vision":
        from brain import vision

        return manifest_to_dict(vision.extract_with_vision(image_bytes, name))
    from brain import extractor

    return manifest_to_dict(extractor.simulate_extraction(image_bytes, name))


def run(engine: str, levels: list[str], limit: int, seed: int) -> dict:
    fx_list = fixtures.FIXTURES[:limit] if limit else fixtures.FIXTURES
    per_level: dict[str, list[dict]] = {lv: [] for lv in levels}
    rows = []

    for fx in fx_list:
        clean = fixtures.render_clean_png(fx)
        for lv in levels:
            img = degrade.degrade(clean, lv, seed=seed)
            t0 = time.time()
            try:
                extracted = _extract(engine, img, f"{fx['name']}_{lv}.png")
                sc = evaluate.score(fx, extracted)
                sc["error"] = None
            except Exception as exc:  # noqa: BLE001
                sc = {"overall_accuracy": 0.0, "critical_accuracy": 0.0, "error": str(exc), "mismatches": []}
            sc["seconds"] = round(time.time() - t0, 1)
            per_level[lv].append(sc)
            rows.append({"fixture": fx["name"], "level": lv, **sc})
            print(f"  {fx['name']:24} {lv:7} overall={sc['overall_accuracy']:.0%} "
                  f"critical={sc['critical_accuracy']:.0%} ({sc['seconds']}s)")

    curve = {}
    for lv in levels:
        scores = per_level[lv]
        curve[lv] = {
            "overall_accuracy": round(statistics.mean(s["overall_accuracy"] for s in scores), 4),
            "critical_accuracy": round(statistics.mean(s["critical_accuracy"] for s in scores), 4),
            "samples": len(scores),
        }
    return {"engine": engine, "seed": seed, "levels": levels, "curve": curve, "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser(description="KhaleejNode extraction accuracy benchmark.")
    ap.add_argument("--engine", choices=["vision", "mock"], default="vision")
    ap.add_argument("--levels", default="all", help="comma list or 'all'")
    ap.add_argument("--limit", type=int, default=0, help="max fixtures (0 = all)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    levels = degrade.LEVEL_ORDER if args.levels == "all" else [l.strip() for l in args.levels.split(",")]

    print(f"\n=== KhaleejNode benchmark — engine={args.engine}, levels={levels} ===")
    report = run(args.engine, levels, args.limit, args.seed)

    print("\n--- Accuracy vs. degradation ---")
    print(f"  {'level':8} {'overall':>9} {'critical':>9} {'n':>4}")
    for lv in levels:
        c = report["curve"][lv]
        print(f"  {lv:8} {c['overall_accuracy']:>8.0%} {c['critical_accuracy']:>8.0%} {c['samples']:>4}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n  Wrote {args.out}")


if __name__ == "__main__":
    main()
