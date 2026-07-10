"""
brain/compliance/tariff.py
===========================
HS-code (Harmonized System) tariff engine — offline.

Given a declared HS code + goods description, it:
  * normalizes and format-checks the code,
  * looks it up in the local tariff registry,
  * checks whether the declared description plausibly matches the code's official
    description (keyword overlap — catches gross misclassification),
  * computes the ad valorem duty for the line.

The registry (data/hs_tariff.json) is a REPRESENTATIVE starter dataset. Drop the
licensed GCC/UAE tariff in the same shape and every function below works
unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

_DATA = Path(__file__).resolve().parent / "data" / "hs_tariff.json"
_WORD_RE = re.compile(r"[A-Za-z]+")


@lru_cache(maxsize=1)
def _raw() -> dict:
    with open(_DATA, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _registry() -> dict:
    return _raw().get("codes", {})


def is_sample_data() -> bool:
    """True if the tariff is the bundled representative/sample set, not official."""
    source = str(_raw().get("_meta", {}).get("source", "")).lower()
    return any(w in source for w in ("representative", "sample", "not the", "not a"))


def normalize_code(code: Optional[str]) -> str:
    """Strip everything except digits, then re-insert the HS dot (NNNN.NN)."""
    if not code:
        return ""
    digits = re.sub(r"\D", "", str(code))
    if len(digits) >= 6:
        return f"{digits[:4]}.{digits[4:6]}"  # heading.subheading (6-digit)
    return digits


def is_valid_format(code: Optional[str]) -> bool:
    """True if the code has a plausible HS structure (>= 6 digits)."""
    return len(re.sub(r"\D", "", str(code or ""))) >= 6


@dataclass
class TariffResult:
    declared_code: str
    normalized_code: str
    known: bool
    official_description: str
    duty_rate: float
    duty_amount_aed: float
    description_matches: bool
    match_reason: str


def _description_matches(keywords: list, description: str) -> bool:
    """True if any registry keyword appears in the declared description."""
    if not keywords:
        return True
    desc_tokens = {t.lower() for t in _WORD_RE.findall(description)}
    desc_low = description.lower()
    for kw in keywords:
        kw = kw.lower()
        # phrase keyword (e.g. "power bank") -> substring; single word -> token
        if " " in kw or "-" in kw:
            if kw in desc_low:
                return True
        elif kw in desc_tokens:
            return True
    return False


def assess(code: Optional[str], description: str, declared_value_aed: float) -> TariffResult:
    """Full tariff assessment for one line item."""
    declared = str(code or "").strip()
    norm = normalize_code(code)
    reg = _registry()
    record = reg.get(norm)

    if record is None:
        return TariffResult(
            declared_code=declared,
            normalized_code=norm,
            known=False,
            official_description="",
            duty_rate=0.0,
            duty_amount_aed=0.0,
            description_matches=False,
            match_reason="HS code not found in tariff registry.",
        )

    matches = _description_matches(record.get("keywords", []), description)
    rate = float(record.get("duty_rate", 0.0))
    return TariffResult(
        declared_code=declared,
        normalized_code=norm,
        known=True,
        official_description=record.get("description", ""),
        duty_rate=rate,
        duty_amount_aed=round(max(0.0, declared_value_aed) * rate, 2),
        description_matches=matches,
        match_reason=(
            "Description aligns with the HS code."
            if matches
            else f"Declared goods do not match HS {norm} ('{record.get('description','')}')."
        ),
    )
