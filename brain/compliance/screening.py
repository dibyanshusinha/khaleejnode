"""
brain/compliance/screening.py
==============================
Offline compliance screening:

  * Denied-party screening — fuzzy-matches shipper/consignee names against a
    local denied-party list (robust to legal suffixes, punctuation, spelling
    variants, and aliases). This is the hard, valuable part; the matching logic
    is production-grade even though the bundled list is a sample.
  * Dual-use / controlled-goods screening — by HS-code prefix and by keyword.
  * Country-risk / embargo screening.

All datasets under data/ are REPRESENTATIVE. Replace them with the official
consolidated lists (OFAC/UN/EU/UK/UAE) in the same schema to go production.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

_DATA_DIR = Path(__file__).resolve().parent / "data"

# Legal-entity suffixes stripped before matching so "Northwind Metals Co" and
# "North Wind Metals LLC" compare on their distinctive tokens.
_SUFFIXES = {
    "llc", "fze", "fzco", "fzc", "dmcc", "ltd", "limited", "inc", "incorporated",
    "corp", "corporation", "co", "company", "gmbh", "bv", "nv", "sa", "ag", "srl",
    "plc", "pvt", "private", "llp", "lp", "sarl", "spa", "pte", "sdn", "bhd",
    "trading", "general", "group", "holding", "holdings", "intl", "international",
}
_MATCH_THRESHOLD = 0.86


def _load(name: str) -> dict:
    with open(_DATA_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _denied_raw() -> dict:
    return _load("denied_parties.json")


def _denied() -> list:
    return _denied_raw().get("entries", [])


def is_sample_data() -> bool:
    """True if the denied-party list is the bundled sample set, not official."""
    source = str(_denied_raw().get("_meta", {}).get("source", "")).lower()
    return any(w in source for w in ("representative", "sample", "not a", "not the", "fictional"))


@lru_cache(maxsize=1)
def _dual_use() -> dict:
    return _load("dual_use.json")


@lru_cache(maxsize=1)
def _country_risk() -> dict:
    return _load("country_risk.json").get("countries", {})


def _normalize(name: str) -> str:
    name = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    tokens = [t for t in name.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Token containment boost: all tokens of the shorter name present in the other.
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if shorter and shorter.issubset(longer):
            ratio = max(ratio, 0.9)
    return ratio


@dataclass
class ScreenMatch:
    list_name: str
    matched_on: str
    score: float
    programs: list
    country: str


# Token buckets larger than this are treated as too common to be discriminating
# (e.g. "mohammed", "trading") and skipped when gathering candidates — this is
# the "blocking" step that keeps screening fast against 20k+ real entries.
_COMMON_TOKEN_CUTOFF = 2500


@lru_cache(maxsize=1)
def _index():
    """Build an inverted token index over the denied-party list (cached).

    Returns (entries, per_entry_candidates, inverted) where per_entry_candidates
    is a list of [(original, normalized), ...] and inverted maps token -> set of
    entry indices.
    """
    entries = _denied()
    per_entry: list[list[tuple]] = []
    inverted: dict[str, set] = {}
    for i, e in enumerate(entries):
        cands = []
        tokens: set[str] = set()
        for raw in [e.get("name", "")] + list(e.get("aliases", [])):
            norm = _normalize(raw)
            if norm:
                cands.append((raw, norm))
                tokens |= set(norm.split())
        per_entry.append(cands)
        for t in tokens:
            if len(t) >= 3:
                inverted.setdefault(t, set()).add(i)
    return entries, per_entry, inverted


def screen_party(name: str, country: Optional[str] = None) -> List[ScreenMatch]:
    """Fuzzy-screen a party name against the denied-party list (indexed)."""
    q = _normalize(name)
    if not q or len(q) < 3:
        return []
    q_tokens = [t for t in set(q.split()) if len(t) >= 3]
    if not q_tokens:
        return []

    entries, per_entry, inverted = _index()

    # Gather candidate entries that share a discriminating token with the query.
    candidate_ids: set = set()
    for t in q_tokens:
        bucket = inverted.get(t)
        if bucket and len(bucket) <= _COMMON_TOKEN_CUTOFF:
            candidate_ids |= bucket

    matches: List[ScreenMatch] = []
    for i in candidate_ids:
        best_score, best_on = 0.0, ""
        for original, norm in per_entry[i]:
            score = _similarity(q, norm)
            if score > best_score:
                best_score, best_on = score, original
        if best_score >= _MATCH_THRESHOLD:
            e = entries[i]
            matches.append(
                ScreenMatch(
                    list_name=e.get("name", ""),
                    matched_on=best_on,
                    score=round(best_score, 3),
                    programs=list(e.get("program", [])),
                    country=e.get("country", ""),
                )
            )
    return sorted(matches, key=lambda m: m.score, reverse=True)


@dataclass
class GoodsControl:
    category: str
    control: str
    severity: str
    matched_on: str


def screen_goods(hs_code: Optional[str], description: str) -> List[GoodsControl]:
    """Screen a line item against dual-use / controlled-goods rules."""
    du = _dual_use()
    hits: List[GoodsControl] = []
    digits = re.sub(r"\D", "", str(hs_code or ""))
    norm6 = f"{digits[:4]}.{digits[4:6]}" if len(digits) >= 6 else ""

    for prefix, rule in du.get("by_hs_prefix", {}).items():
        pdigits = re.sub(r"\D", "", prefix)
        if digits and digits.startswith(pdigits):
            hits.append(GoodsControl(rule["category"], rule["control"], rule["severity"], f"HS {prefix}"))

    low = (description or "").lower()
    for kw, rule in du.get("by_keyword", {}).items():
        if kw in low:
            hits.append(GoodsControl(rule["category"], rule["control"], rule["severity"], f"'{kw}'"))

    # De-duplicate by (category, matched_on).
    seen, unique = set(), []
    for h in hits:
        key = (h.category, h.matched_on)
        if key not in seen:
            seen.add(key)
            unique.append(h)
    return unique


@dataclass
class CountryRisk:
    code: str
    name: str
    level: str


def screen_country(code: Optional[str]) -> Optional[CountryRisk]:
    """Return a risk record if the country is on the risk list, else None."""
    if not code:
        return None
    rec = _country_risk().get(str(code).upper())
    if not rec:
        return None
    return CountryRisk(code=str(code).upper(), name=rec.get("name", ""), level=rec.get("level", ""))
