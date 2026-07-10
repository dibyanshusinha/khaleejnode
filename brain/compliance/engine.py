"""
brain/compliance/engine.py
==========================
Compliance orchestrator. Runs HS-tariff assessment, denied-party / dual-use /
country screening, and arithmetic cross-checks over a CustomsManifest, then
produces:

  * a list of finding dicts (code/severity/message/field) for the rules engine,
  * a structured compliance summary (duty roll-up, screening hits, and a
    mathematically scored risk value with band).

Kept independent of rules.py's Flag type to avoid a circular import.
"""

from __future__ import annotations

from typing import List, Tuple

from brain.compliance import crosschecks, screening, tariff
from brain.schema import CustomsManifest

# Risk contribution per finding severity.
_SEV_WEIGHT = {"BLOCK": 40, "WARN": 12, "INFO": 3}


def _finding(code: str, severity: str, message: str, field: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message, "field": field}


def assess(manifest: CustomsManifest) -> Tuple[List[dict], dict]:
    findings: List[dict] = []
    duty_lines: List[dict] = []
    total_duty = 0.0

    # --- Per-line: tariff + dual-use goods screening ---------------------
    for idx, item in enumerate(manifest.items):
        field = f"items[{idx}]"

        if not (item.hs_code or "").strip():
            findings.append(
                _finding("HS_MISSING", "WARN",
                         f"Line {idx + 1} ('{item.description}') has no HS code — customs "
                         f"cannot classify or rate it.", f"{field}.hs_code")
            )
        else:
            t = tariff.assess(item.hs_code, item.description, item.declared_value_aed)
            if not t.known:
                findings.append(
                    _finding("HS_UNKNOWN", "WARN",
                             f"Line {idx + 1}: HS code '{item.hs_code}' is not in the tariff "
                             f"registry — verify the classification.", f"{field}.hs_code")
                )
            else:
                total_duty += t.duty_amount_aed
                duty_lines.append({
                    "line": idx + 1,
                    "hs_code": t.normalized_code,
                    "official_description": t.official_description,
                    "duty_rate": t.duty_rate,
                    "duty_aed": t.duty_amount_aed,
                })
                if not t.description_matches:
                    findings.append(
                        _finding("HS_MISMATCH", "WARN",
                                 f"Line {idx + 1}: goods '{item.description}' may not match "
                                 f"HS {t.normalized_code} ('{t.official_description}'). "
                                 f"Possible misclassification.", f"{field}.hs_code")
                    )

        for ctrl in screening.screen_goods(item.hs_code, item.description):
            findings.append(
                _finding("DUAL_USE", ctrl.severity,
                         f"Line {idx + 1} [{ctrl.category}] {ctrl.control} (matched {ctrl.matched_on})",
                         f"{field}.description")
            )

        origin = getattr(item, "country_of_origin", None)
        _screen_country(findings, origin, f"Line {idx + 1} country of origin", f"{field}.country_of_origin")

    # --- Denied-party screening (shipper + consignee) --------------------
    screen_hits: List[dict] = []
    for role, party in (("shipper", manifest.shipper), ("consignee", manifest.consignee)):
        for m in screening.screen_party(party.name, party.country):
            screen_hits.append({"role": role, "name": party.name, "matched": m.list_name,
                                "score": m.score, "programs": m.programs})
            findings.append(
                _finding("SANCTIONS_HIT", "BLOCK",
                         f"{role.capitalize()} '{party.name}' matches denied party "
                         f"'{m.list_name}' (score {m.score}, programs {', '.join(m.programs) or 'n/a'}).",
                         f"{role}.name")
            )
        _screen_country(findings, party.country, f"{role.capitalize()} country", f"{role}.country")

    # --- Arithmetic cross-checks -----------------------------------------
    findings.extend(crosschecks.reconcile(manifest))

    # --- Data provenance: refuse to imply authority on sample data -------
    tariff_sample = tariff.is_sample_data()
    sanctions_sample = screening.is_sample_data()
    data_sources = {
        "tariff": "SAMPLE" if tariff_sample else "OFFICIAL",
        "sanctions": "SAMPLE" if sanctions_sample else "OFFICIAL",
    }
    data_mode = "OFFICIAL" if not (tariff_sample or sanctions_sample) else "SAMPLE"
    sample_parts = [k for k, v in data_sources.items() if v == "SAMPLE"]
    if sample_parts:
        findings.insert(0, _finding(
            "REFERENCE_DATA_SAMPLE", "WARN",
            f"Non-authoritative reference data in use: {', '.join(sample_parts)}. "
            f"Verify against official sources before acting.",
        ))

    # --- Score -----------------------------------------------------------
    risk = sum(_SEV_WEIGHT.get(f["severity"], 0) for f in findings)
    if any(f["code"] == "SANCTIONS_HIT" for f in findings):
        risk = max(risk, 85)
    risk = min(100, risk)
    band = ("critical" if risk >= 80 else "high" if risk >= 50
            else "elevated" if risk >= 20 else "low")

    tot = crosschecks.totals(manifest)
    summary = {
        "data_mode": data_mode,
        "data_sources": data_sources,
        "risk_score": risk,
        "risk_band": band,
        "duty": {
            "lines": duty_lines,
            "total_duty_aed": round(total_duty, 2),
            "total_declared_value_aed": tot["total_declared_value_aed"],
        },
        "screening": {
            "denied_party_hits": screen_hits,
            "checked_parties": 2,
        },
        "totals": tot,
    }
    return findings, summary


def _screen_country(findings: List[dict], code, label: str, field: str) -> None:
    risk = screening.screen_country(code)
    if not risk:
        return
    severity = {"embargo": "BLOCK", "high": "WARN", "elevated": "INFO"}.get(risk.level, "INFO")
    findings.append(
        _finding("COUNTRY_RISK", severity,
                 f"{label}: {risk.name} ({risk.code}) is on the {risk.level} list.", field)
    )
