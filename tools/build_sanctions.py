#!/usr/bin/env python3
"""
tools/build_sanctions.py
========================
VENDOR tooling. Convert the official public sanctions lists into KhaleejNode's
denied-party schema, producing `brain/compliance/data/denied_parties.json` with
an OFFICIAL provenance stamp (so `screening.is_sample_data()` returns False and
compliance `data_mode` can report OFFICIAL for screening).

Sources (public, free):
  * OFAC SDN  — legacy CSVs: sdn.csv, alt.csv, add.csv
                https://www.treasury.gov/ofac/downloads/{sdn,alt,add}.csv
  * UN        — Consolidated List XML
                https://scsanctions.un.org/resources/xml/en/consolidated.xml

The client stays fully offline — this runs vendor-side to prepare the data file
that ships inside the app. Re-run periodically to refresh the lists.

Usage:
    python tools/build_sanctions.py --ofac-dir /tmp --un /tmp/un_consolidated.xml
    python tools/build_sanctions.py --ofac-dir /tmp   # OFAC only
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import config  # noqa: E402

OUT_DEFAULT = config.PROJECT_ROOT / "brain" / "compliance" / "data" / "denied_parties.json"


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v in ("-0-", "") else v


def _norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


# ---------------------------------------------------------------------------
# OFAC SDN (legacy CSV trio)
# ---------------------------------------------------------------------------
def parse_ofac(sdn: Path, alt: Path, add: Path) -> list[dict]:
    records: dict[str, dict] = {}
    with open(sdn, encoding="latin-1", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 4:
                continue
            ent = row[0].strip()
            name = _clean(row[1])
            if not name:
                continue
            sdn_type = (_clean(row[2]) or "entity").lower()
            programs = [p.strip() for p in re.split(r"[;,]", _clean(row[3])) if p.strip()]
            records[ent] = {
                "name": name,
                "type": sdn_type if sdn_type in ("individual", "vessel", "aircraft") else "entity",
                "programs": set(programs) or {"OFAC-SDN"},
                "aliases": set(),
                "countries": set(),
            }
    if alt.exists():
        with open(alt, encoding="latin-1", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 4:
                    continue
                ent, alt_name = row[0].strip(), _clean(row[3])
                if ent in records and alt_name:
                    records[ent]["aliases"].add(alt_name)
    if add.exists():
        with open(add, encoding="latin-1", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 5:
                    continue
                ent, country = row[0].strip(), _clean(row[4])
                if ent in records and country:
                    records[ent]["countries"].add(country)

    out = []
    for rec in records.values():
        out.append({
            "name": rec["name"],
            "aliases": sorted(rec["aliases"])[:12],
            "type": rec["type"],
            "program": sorted(rec["programs"]),
            "country": sorted(rec["countries"])[0] if rec["countries"] else "",
            "source": "OFAC-SDN",
        })
    return out


# ---------------------------------------------------------------------------
# UN Consolidated List (XML)
# ---------------------------------------------------------------------------
def _un_text(node, tag: str) -> str:
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_un(path: Path) -> list[dict]:
    if not path or not path.exists():
        return []
    root = ET.parse(path).getroot()
    out: list[dict] = []

    for ind in root.findall(".//INDIVIDUALS/INDIVIDUAL"):
        parts = [_un_text(ind, t) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        name = " ".join(p for p in parts if p)
        if not name:
            continue
        aliases = {_un_text(a, "ALIAS_NAME") for a in ind.findall(".//INDIVIDUAL_ALIAS")}
        aliases.discard("")
        country = _un_text(ind.find(".//NATIONALITY") or ET.Element("x"), "VALUE")
        prog = _un_text(ind, "UN_LIST_TYPE") or "UN"
        out.append({"name": name, "aliases": sorted(aliases)[:12], "type": "individual",
                    "program": [prog], "country": country, "source": "UN"})

    for ent in root.findall(".//ENTITIES/ENTITY"):
        name = _un_text(ent, "FIRST_NAME")
        if not name:
            continue
        aliases = {_un_text(a, "ALIAS_NAME") for a in ent.findall(".//ENTITY_ALIAS")}
        aliases.discard("")
        country = _un_text(ent.find(".//ENTITY_ADDRESS") or ET.Element("x"), "COUNTRY")
        prog = _un_text(ent, "UN_LIST_TYPE") or "UN"
        out.append({"name": name, "aliases": sorted(aliases)[:12], "type": "entity",
                    "program": [prog], "country": country, "source": "UN"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build official denied-party list.")
    ap.add_argument("--ofac-dir", help="dir containing sdn.csv, alt.csv, add.csv")
    ap.add_argument("--sdn"); ap.add_argument("--alt"); ap.add_argument("--add")
    ap.add_argument("--un", help="UN consolidated XML")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    entries: list[dict] = []
    if args.ofac_dir or args.sdn:
        base = Path(args.ofac_dir) if args.ofac_dir else None
        sdn = Path(args.sdn) if args.sdn else base / "ofac_sdn.csv"
        alt = Path(args.alt) if args.alt else base / "ofac_alt.csv"
        add = Path(args.add) if args.add else base / "ofac_add.csv"
        if sdn.exists():
            ofac = parse_ofac(sdn, alt, add)
            print(f"  OFAC SDN: {len(ofac)} records")
            entries += ofac
        else:
            print(f"  OFAC sdn file not found: {sdn}", file=sys.stderr)

    if args.un:
        un = parse_un(Path(args.un))
        print(f"  UN Consolidated: {len(un)} records")
        entries += un

    if not entries:
        print("  No records parsed — nothing written.", file=sys.stderr)
        sys.exit(1)

    # De-duplicate by normalized name, merging aliases/programs.
    merged: dict[str, dict] = {}
    for e in entries:
        k = _norm_key(e["name"])
        if k in merged:
            m = merged[k]
            m["aliases"] = sorted(set(m["aliases"]) | set(e["aliases"]))[:12]
            m["program"] = sorted(set(m["program"]) | set(e["program"]))
        else:
            merged[k] = e
    final = sorted(merged.values(), key=lambda x: x["name"])

    doc = {
        "_meta": {
            "source": "Official consolidated sanctions data (OFAC SDN + UN Consolidated List).",
            "generated": date.today().isoformat(),
            "provenance": {
                "ofac_sdn": "https://www.treasury.gov/ofac/downloads/sdn.csv",
                "un_consolidated": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
            },
            "entry_count": len(final),
            "refresh": "Re-run tools/build_sanctions.py to update; lists change frequently.",
        },
        "entries": final,
    }
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  Wrote {len(final)} denied parties -> {args.out}")


if __name__ == "__main__":
    main()
