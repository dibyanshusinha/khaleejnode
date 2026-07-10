"""
brain/rules.py
==============
Adversarial Rules Engine (The Brain, audit side).

Given an extracted CustomsManifest, this engine hunts for the defects that get
shipments held, fined, or rejected at UAE customs -- BEFORE anything is
"submitted". It thinks like an adversarial customs officer, not a friendly
form-filler.

Each rule returns zero or more Flags. A Flag has a severity:
    BLOCK  -- would be rejected outright; do not submit.
    WARN   -- likely to trigger inspection / query.
    INFO   -- advisory, non-blocking.

The engine is pure and offline: same manifest in, same flags out.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List

from brain.compliance import engine as compliance_engine
from brain.schema import CustomsManifest

# Descriptions that are too vague to clear customs.
_VAGUE_PATTERNS = [
    r"\bcargo\b",
    r"\bparts?\b",
    r"\bassorted\b",
    r"\bgeneral\b",
    r"\bmerchandise\b",
    r"\bmisc(ellaneous)?\b",
    r"\bgoods\b",
    r"\bitems?\b",
    r"\bstuff\b",
    r"\bother\b",
]
_VAGUE_RE = re.compile("|".join(_VAGUE_PATTERNS), re.IGNORECASE)

# ---------------------------------------------------------------------------
# Offline spelling check for commodity descriptions
# ---------------------------------------------------------------------------
# A misspelled commodity ("irn nails" -> "iron nails") gets misclassified at
# customs. We use a local, offline spell checker (pyspellchecker ships its own
# frequency dictionary) and BIAS suggestions toward a trade/commodity lexicon so
# "irn" resolves to "iron" rather than the generic top pick "in".

# Common trade / customs vocabulary. Used both to suppress false positives on
# domain jargon and to prefer domain-correct suggestions.
_COMMODITY_TERMS = {
    "iron", "steel", "stainless", "galvanized", "aluminium", "aluminum", "copper",
    "brass", "bronze", "zinc", "nickel", "titanium", "alloy", "metal", "nails",
    "screws", "bolts", "nuts", "washers", "rivets", "coils", "coil", "sheets",
    "sheet", "plates", "plate", "pipes", "pipe", "tubes", "tube", "wire", "rods",
    "bars", "beams", "cotton", "polyester", "nylon", "fabric", "textile", "textiles",
    "garments", "apparel", "yarn", "ceramic", "porcelain", "tiles", "cement",
    "concrete", "plywood", "timber", "lumber", "furniture", "machinery", "pump",
    "pumps", "valve", "valves", "bearing", "bearings", "gasket", "compressor",
    "generator", "engine", "engines", "motor", "motors", "vehicle", "vehicles",
    "tyres", "tires", "brake", "brakes", "chassis", "battery", "batteries",
    "lithium", "electronics", "cable", "cables", "resistor", "capacitor",
    "transformer", "solar", "panel", "panels", "module", "modules", "fertilizer",
    "chemical", "chemicals", "resin", "polymer", "plastic", "plastics", "granules",
    "pellets", "rice", "wheat", "sugar", "flour", "spices", "dates", "seafood",
    "frozen", "chilled", "pharmaceutical", "cosmetics", "appliances", "refrigerator",
    "hot", "cold", "rolled", "sanitary", "ware", "hardware", "fittings", "flanges",
}

_SPELL_SKIP = {
    "pcs", "mt", "kg", "kgs", "hs", "trn", "vat", "eori", "fze", "llc", "bv",
    "ltd", "uae", "aed", "usd", "eur", "cbm", "ctn", "ctns", "qty", "pkg", "pkgs",
    "mm", "cm", "gsm", "dia", "led", "pvc", "hdpe", "ldpe", "abs", "oem",
    # units / electronics measures that are not real dictionary words
    "mah", "wh", "ah", "kwh", "ghz", "mhz", "khz", "rpm", "psi", "ml", "ltr",
    "hp", "kw", "kva", "rgb", "usb", "hdmi", "dc", "ac", "nm", "watt", "volt",
}

try:
    from spellchecker import SpellChecker as _SpellChecker

    _SPELL = _SpellChecker(distance=2)
    # Make trade terms first-class dictionary words so they are never flagged.
    _SPELL.word_frequency.load_words(_COMMODITY_TERMS)
    _SPELL_OK = True
except Exception:  # noqa: BLE001 - degrade gracefully if the package is missing
    _SPELL = None
    _SPELL_OK = False

_WORD_RE = re.compile(r"[A-Za-z]+")


def _spelling_suggestion(token: str):
    """Return a likely correction for `token`, preferring commodity terms.

    Returns None if the token looks fine or no confident suggestion exists.
    """
    if not _SPELL_OK:
        return None
    low = token.lower()
    if len(low) < 3 or low in _SPELL_SKIP or low in _COMMODITY_TERMS:
        return None
    if _SPELL.known([low]):  # a real, known word -> not a typo
        return None
    candidates = _SPELL.candidates(low) or set()
    if not candidates:
        return None
    # Prefer a candidate that is a known commodity term.
    domain_hits = [c for c in candidates if c in _COMMODITY_TERMS]
    if domain_hits:
        return sorted(domain_hits, key=len)[0]
    correction = _SPELL.correction(low)
    if correction and correction != low:
        return correction
    return None


@dataclass
class Flag:
    code: str
    severity: str  # BLOCK | WARN | INFO
    message: str
    field: str = ""


@dataclass
class ValidationReport:
    passed: bool
    flags: List[Flag] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    compliance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "flags": [asdict(f) for f in self.flags],
            "summary": self.summary,
            "compliance": self.compliance,
            "counts": self.counts(),
        }

    def counts(self) -> dict:
        c = {"BLOCK": 0, "WARN": 0, "INFO": 0}
        for f in self.flags:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------
def _rule_vague_descriptions(m: CustomsManifest) -> List[Flag]:
    flags: List[Flag] = []
    for idx, item in enumerate(m.items):
        if _VAGUE_RE.search(item.description):
            flags.append(
                Flag(
                    code="VAGUE_DESCRIPTION",
                    severity="BLOCK",
                    message=(
                        f"Line {idx + 1} description '{item.description}' is too vague "
                        f"for customs classification. Provide a specific commodity."
                    ),
                    field=f"items[{idx}].description",
                )
            )
    return flags


def _rule_zero_value(m: CustomsManifest) -> List[Flag]:
    flags: List[Flag] = []
    for idx, item in enumerate(m.items):
        if item.declared_value_aed <= 0:
            flags.append(
                Flag(
                    code="ZERO_DECLARED_VALUE",
                    severity="WARN",
                    message=f"Line {idx + 1} has a zero declared value.",
                    field=f"items[{idx}].declared_value_aed",
                )
            )
    return flags


def _rule_party_completeness(m: CustomsManifest) -> List[Flag]:
    flags: List[Flag] = []
    for role, party in (("shipper", m.shipper), ("consignee", m.consignee)):
        if not party.tax_id:
            flags.append(
                Flag(
                    code="MISSING_TAX_ID",
                    severity="WARN",
                    message=f"{role.capitalize()} '{party.name}' is missing a tax/TRN id.",
                    field=f"{role}.tax_id",
                )
            )
    return flags


def _rule_spelling(m: CustomsManifest) -> List[Flag]:
    """Flag likely misspelled words in commodity descriptions."""
    flags: List[Flag] = []
    for idx, item in enumerate(m.items):
        seen: set[str] = set()
        for token in _WORD_RE.findall(item.description):
            low = token.lower()
            if low in seen:
                continue
            suggestion = _spelling_suggestion(token)
            if suggestion:
                seen.add(low)
                flags.append(
                    Flag(
                        code="SPELLING",
                        severity="WARN",
                        message=(
                            f"Line {idx + 1}: '{token}' looks misspelled — did you "
                            f"mean '{suggestion}'? (in '{item.description}')"
                        ),
                        field=f"items[{idx}].description",
                    )
                )
    return flags


# Basic document-quality rules. HS validation, restricted/dual-use goods, and
# weight reconciliation are now handled by the compliance engine (see below),
# which supersedes the old shallow keyword/HS/weight rules.
_RULES = [
    _rule_vague_descriptions,
    _rule_spelling,
    _rule_zero_value,
    _rule_party_completeness,
]


def validate(manifest: CustomsManifest) -> ValidationReport:
    """Run document-quality rules + the compliance engine; assemble a report.

    `passed` is False if any BLOCK-level finding is present.
    """
    flags: List[Flag] = []
    for rule in _RULES:
        flags.extend(rule(manifest))

    # Compliance engine: HS tariff, sanctions/dual-use/country screening,
    # arithmetic cross-checks, and a scored compliance summary.
    comp_findings, comp_summary = compliance_engine.assess(manifest)
    for f in comp_findings:
        flags.append(
            Flag(code=f["code"], severity=f["severity"], message=f["message"], field=f.get("field", ""))
        )

    passed = not any(f.severity == "BLOCK" for f in flags)
    return ValidationReport(
        passed=passed, flags=flags, summary=manifest.summary(), compliance=comp_summary
    )
