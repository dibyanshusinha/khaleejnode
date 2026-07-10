"""
brain/compliance/crosschecks.py
===============================
Arithmetic reconciliation checks that catch SILENT extraction misreads.

A vision model can confidently output a wrong digit (e.g. a weight or value).
These deterministic cross-checks catch such errors because a single wrong number
breaks an internal sum or a plausibility ratio — the model can't fake internal
consistency it never computed.

Returns plain dicts so this module stays independent of the rules engine's
Flag type (rules.py wraps them).
"""

from __future__ import annotations

from typing import List

from brain.schema import CustomsManifest

# Reconciliation tolerance between summed line weights and the declared gross.
_WEIGHT_TOL_PCT = 5.0
# Plausible band for declared value per kg (AED). Outside this, a digit is
# probably misread. Deliberately wide so only gross errors trip it.
_VPK_FLOOR = 0.10
_VPK_CEIL = 2_000_000.0


def reconcile(manifest: CustomsManifest) -> List[dict]:
    flags: List[dict] = []

    computed = round(sum(it.quantity * it.unit_weight_kg for it in manifest.items), 3)
    declared_gross = manifest.declared_gross_weight_kg

    # 1. Gross-weight reconciliation: sum of line weights vs declared gross.
    if computed > 0 and declared_gross > 0:
        diff_pct = abs(declared_gross - computed) / computed * 100.0
        if diff_pct > _WEIGHT_TOL_PCT:
            flags.append(
                {
                    "code": "WEIGHT_RECONCILE",
                    "severity": "BLOCK" if diff_pct > 10 else "WARN",
                    "message": (
                        f"Weights do not reconcile: line items sum to {computed} kg but "
                        f"declared gross is {declared_gross} kg ({diff_pct:.1f}% off). "
                        f"Likely a misread quantity or weight."
                    ),
                    "field": "declared_gross_weight_kg",
                }
            )
    elif computed > 0 and declared_gross <= 0:
        flags.append(
            {
                "code": "GROSS_WEIGHT_MISSING",
                "severity": "WARN",
                "message": f"No declared gross weight, but line items sum to {computed} kg.",
                "field": "declared_gross_weight_kg",
            }
        )

    # 2. Per-line value-per-kg plausibility (catches a misread value or weight).
    for idx, it in enumerate(manifest.items):
        line_weight = it.quantity * it.unit_weight_kg
        if line_weight > 0 and it.declared_value_aed > 0:
            vpk = it.declared_value_aed / line_weight
            if vpk < _VPK_FLOOR or vpk > _VPK_CEIL:
                flags.append(
                    {
                        "code": "VALUE_WEIGHT_IMPLAUSIBLE",
                        "severity": "WARN",
                        "message": (
                            f"Line {idx + 1}: implausible value/weight ratio "
                            f"({vpk:,.2f} AED/kg) for '{it.description}'. "
                            f"Check the declared value or weight for a misread digit."
                        ),
                        "field": f"items[{idx}].declared_value_aed",
                    }
                )
    return flags


def totals(manifest: CustomsManifest) -> dict:
    """Roll-up totals used in the compliance summary."""
    return {
        "line_count": len(manifest.items),
        "computed_weight_kg": round(
            sum(it.quantity * it.unit_weight_kg for it in manifest.items), 3
        ),
        "declared_gross_weight_kg": manifest.declared_gross_weight_kg,
        "total_declared_value_aed": round(
            sum(it.declared_value_aed for it in manifest.items), 2
        ),
    }
