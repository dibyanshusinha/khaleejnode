"""
benchmark/fixtures.py
=====================
Ground-truth manifests + a clean renderer.

Each fixture is a known-correct manifest (the ground truth) plus a function that
renders it to a clean page image. The benchmark degrades that image and checks
how much of the ground truth survives extraction — so accuracy is measured
against data we know exactly, not guessed.
"""

from __future__ import annotations

import fitz  # PyMuPDF

# Ground-truth manifests. Values here are the "answer key" the scorer compares to.
FIXTURES: list[dict] = [
    {
        "name": "steel_hamburg",
        "bill_of_lading": "IRNX-5566-102",
        "vessel": "MSC ANKARA",
        "port_of_loading": "Jebel Ali",
        "port_of_discharge": "Hamburg",
        "shipper": {"name": "Emirates Steel Trading LLC", "country": "AE"},
        "consignee": {"name": "Hamburg Metal Imports GmbH", "country": "DE"},
        "items": [
            {"description": "iron nails 50mm", "hs_code": "7317.00", "quantity": 800,
             "unit_weight_kg": 12, "declared_value_aed": 24000, "country_of_origin": "AE"},
            {"description": "stainless steel bolts", "hs_code": "7318.15", "quantity": 500,
             "unit_weight_kg": 10, "declared_value_aed": 18000, "country_of_origin": "AE"},
        ],
        "declared_gross_weight_kg": 14600,
    },
    {
        "name": "electronics_rotterdam",
        "bill_of_lading": "MAEU-7781-330",
        "vessel": "EVER ACE",
        "port_of_loading": "Shanghai",
        "port_of_discharge": "Jebel Ali",
        "shipper": {"name": "Shenzhen Apex Electronics Co", "country": "CN"},
        "consignee": {"name": "Emirates Advanced Retail LLC", "country": "AE"},
        "items": [
            {"description": "Lithium-ion power banks 10000mAh", "hs_code": "8507.60", "quantity": 300,
             "unit_weight_kg": 0.3, "declared_value_aed": 25500, "country_of_origin": "CN"},
            {"description": "LED panel lights 40W", "hs_code": "9405.40", "quantity": 120,
             "unit_weight_kg": 2.1, "declared_value_aed": 5400, "country_of_origin": "CN"},
        ],
        "declared_gross_weight_kg": 342,
    },
]


def render_clean_png(fx: dict, zoom: float = 2.0) -> bytes:
    """Render a fixture manifest to a clean page image (PNG bytes)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 55), "UAE CUSTOMS SHIPPING MANIFEST", fontsize=14, fontname="helvetica-bold")

    lines = [
        f"Bill of Lading:  {fx['bill_of_lading']}",
        f"Vessel:  {fx['vessel']}",
        f"Port of Loading:  {fx['port_of_loading']}",
        f"Port of Discharge:  {fx['port_of_discharge']}",
        "",
        f"SHIPPER:  {fx['shipper']['name']}   Country: {fx['shipper']['country']}",
        f"CONSIGNEE:  {fx['consignee']['name']}   Country: {fx['consignee']['country']}",
        "",
    ]
    for i, it in enumerate(fx["items"], 1):
        lines += [
            f"LINE ITEM {i}",
            f"  Description:      {it['description']}",
            f"  HS Code:         {it['hs_code']}",
            f"  Quantity:        {it['quantity']} CTN",
            f"  Unit Weight:     {it['unit_weight_kg']} kg",
            f"  Declared Value:  AED {it['declared_value_aed']}",
            f"  Country of Origin:  {it['country_of_origin']}",
            "",
        ]
    lines += [f"Declared Gross Weight:  {fx['declared_gross_weight_kg']} kg", "Currency:  AED"]

    page.insert_text((50, 85), "\n".join(lines), fontsize=11, fontname="courier")
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    data = pix.tobytes("png")
    doc.close()
    return data
