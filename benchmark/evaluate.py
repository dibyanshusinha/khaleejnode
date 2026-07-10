"""
benchmark/evaluate.py
=====================
Field-level accuracy scoring: extracted manifest vs. ground truth.

Produces both an overall accuracy and a **critical-field** accuracy (HS code,
quantity, weight, value) — the fields whose misread causes a customs seizure —
because a 95% overall number can still hide a wrong duty-bearing value.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Fields whose accuracy matters most for customs liability.
CRITICAL_FIELDS = {"hs_code", "quantity", "unit_weight_kg", "declared_value_aed"}

_NUM_TOL = 0.01  # 1% tolerance for numeric equality
_TEXT_SIM = 0.90  # similarity threshold for text equality


def _norm_text(v) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower())


def _text_eq(a, b) -> bool:
    a, b = _norm_text(a), _norm_text(b)
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _TEXT_SIM


def _num_eq(a, b) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom <= _NUM_TOL


def _hs_eq(a, b) -> bool:
    da = re.sub(r"\D", "", str(a or ""))[:6]
    db = re.sub(r"\D", "", str(b or ""))[:6]
    return bool(da) and da == db


def _field_correct(field: str, truth, got) -> bool:
    if field == "hs_code":
        return _hs_eq(truth, got)
    if field in {"quantity", "unit_weight_kg", "declared_value_aed", "declared_gross_weight_kg"}:
        return _num_eq(truth, got)
    return _text_eq(truth, got)


def score(truth: dict, extracted: dict) -> dict:
    """Compare one extracted manifest against its ground truth."""
    checks: list[tuple[str, bool, bool]] = []  # (field, correct, is_critical)

    # Header fields.
    for f in ("bill_of_lading", "port_of_loading", "port_of_discharge", "declared_gross_weight_kg"):
        checks.append((f, _field_correct(f, truth.get(f), extracted.get(f)), False))
    for role in ("shipper", "consignee"):
        t = (truth.get(role) or {}).get("name")
        g = (extracted.get(role) or {}).get("name")
        checks.append((f"{role}.name", _text_eq(t, g), False))

    # Line items — matched by index (order preserved by the renderer).
    t_items = truth.get("items", [])
    g_items = extracted.get("items", [])
    line_count_ok = len(t_items) == len(g_items)
    for i, t_it in enumerate(t_items):
        g_it = g_items[i] if i < len(g_items) else {}
        for f in ("description", "hs_code", "quantity", "unit_weight_kg", "declared_value_aed"):
            checks.append(
                (f"items[{i}].{f}", _field_correct(f, t_it.get(f), g_it.get(f)), f in CRITICAL_FIELDS)
            )

    total = len(checks)
    correct = sum(1 for _, ok, _ in checks if ok)
    crit = [(ok) for _, ok, is_c in checks if is_c]
    crit_total = len(crit)
    crit_correct = sum(1 for ok in crit if ok)

    return {
        "overall_accuracy": round(correct / total, 4) if total else 0.0,
        "critical_accuracy": round(crit_correct / crit_total, 4) if crit_total else 0.0,
        "fields_total": total,
        "fields_correct": correct,
        "line_count_ok": line_count_ok,
        "mismatches": [f for f, ok, _ in checks if not ok],
    }
