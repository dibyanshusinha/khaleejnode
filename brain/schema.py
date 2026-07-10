"""
brain/schema.py
===============
Typed data contract for a UAE Customs Manifest.

We use Pydantic so the extraction layer produces validated, structured data
rather than loose dicts. This is the schema the "multimodal extraction
simulator" fills and the "adversarial rules engine" audits.

Pydantic v2 is the target; a thin shim keeps it importable under v1 too.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional

try:  # Pydantic v2
    from pydantic import BaseModel, Field, field_validator

    _PYDANTIC_V2 = True
except Exception:  # pragma: no cover - v1 fallback
    from pydantic import BaseModel, Field  # type: ignore
    from pydantic import validator as field_validator  # type: ignore

    _PYDANTIC_V2 = False


class Party(BaseModel):
    """A shipper or consignee entity on the manifest."""

    name: str
    address: str
    country: str
    tax_id: Optional[str] = Field(default=None, description="TRN / VAT / EORI")


class LineItem(BaseModel):
    """A single declared commodity line."""

    description: str
    hs_code: Optional[str] = Field(default=None, description="Harmonized System code")
    quantity: int = Field(ge=0)
    unit: str = "PCS"
    unit_weight_kg: float = Field(ge=0)
    declared_value_aed: float = Field(ge=0)
    country_of_origin: Optional[str] = Field(
        default=None, description="ISO-2 country of origin for this commodity line"
    )

    @property
    def line_weight_kg(self) -> float:
        return round(self.quantity * self.unit_weight_kg, 3)


class CustomsManifest(BaseModel):
    """A complete UAE customs manifest as extracted from shipping documents."""

    bill_of_lading: str
    vessel: Optional[str] = None
    port_of_loading: str
    port_of_discharge: str
    eta: Optional[date] = None

    shipper: Party
    consignee: Party

    items: List[LineItem]
    declared_gross_weight_kg: float = Field(ge=0)
    currency: str = "AED"

    # --- derived helpers -------------------------------------------------
    @property
    def computed_weight_kg(self) -> float:
        return round(sum(item.line_weight_kg for item in self.items), 3)

    @property
    def total_declared_value(self) -> float:
        return round(sum(item.declared_value_aed for item in self.items), 2)

    def summary(self) -> dict:
        return {
            "bill_of_lading": self.bill_of_lading,
            "route": f"{self.port_of_loading} -> {self.port_of_discharge}",
            "line_count": len(self.items),
            "declared_gross_weight_kg": self.declared_gross_weight_kg,
            "computed_weight_kg": self.computed_weight_kg,
            "total_declared_value_aed": self.total_declared_value,
        }


def manifest_to_dict(manifest: CustomsManifest) -> dict:
    """Serialize across Pydantic v1/v2 uniformly."""
    if _PYDANTIC_V2:
        return manifest.model_dump(mode="json")
    return manifest.dict()  # type: ignore[attr-defined]
