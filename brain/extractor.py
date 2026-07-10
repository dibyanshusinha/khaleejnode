"""
brain/extractor.py
==================
Multimodal extraction simulator (The Brain, input side).

In production this is where a vision-language model reads the shipping PDF and
emits a CustomsManifest. For an out-of-the-box, offline, deterministic build we
simulate that model: we hash the uploaded bytes and use the digest to seed a
pseudo-random-but-stable manifest. The same PDF always yields the same
extraction, and roughly 1 in 3 documents is seeded with a realistic *defect*
(vague description or weight mismatch) so the rules engine has something to
catch during the demo.

Swap `simulate_extraction` for a real model call and the rest of the pipeline
is unchanged -- it only depends on the CustomsManifest contract.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta

from brain.schema import CustomsManifest, LineItem, Party

_SHIPPERS = [
    ("Jebel Ali Trading FZE", "Plot 42, JAFZA, Dubai", "AE", "100234567800003"),
    ("Shenzhen Apex Electronics Co", "Bao'an District, Shenzhen", "CN", "91440300MA5"),
    ("Rotterdam Bulk Handlers BV", "Waalhaven 12, Rotterdam", "NL", "NL8123.45.678"),
    ("Mumbai Marine Exports Pvt", "Nhava Sheva, Navi Mumbai", "IN", "27AABCM1234K"),
]

_CONSIGNEES = [
    ("Emirates Advanced Retail LLC", "Sheikh Zayed Rd, Dubai", "AE", "100555666700001"),
    ("Abu Dhabi Industrial Supply", "Mussafah M-9, Abu Dhabi", "AE", "100777888900002"),
    ("Sharjah Auto Components LLC", "Industrial Area 6, Sharjah", "AE", "100111222300004"),
]

_PORTS_LOAD = ["Shanghai", "Rotterdam", "Nhava Sheva", "Singapore", "Busan"]
_PORTS_DISCHARGE = ["Jebel Ali", "Khalifa Port", "Port Rashid", "Hamriyah"]

# Clean, specific commodity descriptions.
_GOOD_ITEMS = [
    ("Lithium-ion power banks 10000mAh", "8507.60", 3.2, 85.0),
    ("Stainless steel M8 hex bolts", "7318.15", 0.02, 0.6),
    ("LED panel lights 40W 600x600", "9405.40", 2.1, 45.0),
    ("Automotive brake pads ceramic", "8708.30", 1.4, 30.0),
    ("Cotton knitted t-shirts size L", "6109.10", 0.18, 12.0),
]

# Deliberately vague descriptions the adversarial engine should flag.
_VAGUE_ITEMS = [
    ("cargo/parts", None, 1.0, 20.0),
    ("assorted goods", None, 0.5, 15.0),
    ("general merchandise", None, 2.0, 25.0),
    ("spare items", None, 0.7, 10.0),
]


def _seeded_rng(data: bytes) -> random.Random:
    digest = hashlib.sha256(data).hexdigest()
    return random.Random(int(digest[:16], 16))


def simulate_extraction(file_bytes: bytes, filename: str = "document.pdf") -> CustomsManifest:
    """Produce a deterministic mock CustomsManifest for the given upload."""
    rng = _seeded_rng(file_bytes or filename.encode())

    shipper = _build_party(rng.choice(_SHIPPERS))
    consignee = _build_party(rng.choice(_CONSIGNEES))

    # Roughly 1/3 of documents carry a defect for demo value.
    inject_defect = rng.random() < 0.34
    n_items = rng.randint(2, 4)

    items: list[LineItem] = []
    for i in range(n_items):
        if inject_defect and i == 0:
            desc, hs, uw, val = rng.choice(_VAGUE_ITEMS)
        else:
            desc, hs, uw, val = rng.choice(_GOOD_ITEMS)
        qty = rng.randint(10, 500)
        items.append(
            LineItem(
                description=desc,
                hs_code=hs,
                quantity=qty,
                unit="PCS",
                unit_weight_kg=uw,
                declared_value_aed=round(qty * val, 2),
            )
        )

    computed = round(sum(it.quantity * it.unit_weight_kg for it in items), 3)

    # If injecting a defect, also skew the declared gross weight so the
    # weight-discrepancy rule has something to catch (~18% off).
    if inject_defect:
        declared_gross = round(computed * rng.choice([0.80, 0.82, 1.19]), 3)
    else:
        # Realistic packaging overhead of a few percent.
        declared_gross = round(computed * rng.uniform(1.01, 1.04), 3)

    eta = date.today() + timedelta(days=rng.randint(3, 21))

    return CustomsManifest(
        bill_of_lading=f"MAEU{rng.randint(100000000, 999999999)}",
        vessel=rng.choice(["MSC Zoe", "Ever Ace", "CMA CGM Marco Polo", "COSCO Shipping"]),
        port_of_loading=rng.choice(_PORTS_LOAD),
        port_of_discharge=rng.choice(_PORTS_DISCHARGE),
        eta=eta,
        shipper=shipper,
        consignee=consignee,
        items=items,
        declared_gross_weight_kg=declared_gross,
        currency="AED",
    )


def _build_party(tup: tuple) -> Party:
    name, address, country, tax_id = tup
    return Party(name=name, address=address, country=country, tax_id=tax_id)
