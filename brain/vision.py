"""
brain/vision.py
===============
Real multimodal extraction via a LOCAL Llama 3.2 Vision model served by Ollama.

This replaces the deterministic mock in `extractor.py`. It is still 100% offline:
Ollama runs on the client at `localhost:11434`, so there is no cloud API call and
no external network dependency — only a loopback request to a local model server.

Pipeline:
  1. Render the document to page image(s) with PyMuPDF (PDF, or common image
     formats).
  2. Send the image(s) + a strict extraction prompt to the local vision model.
  3. Parse the model's JSON and coerce it into the `CustomsManifest` contract,
     repairing missing/malformed fields defensively (the model can hallucinate
     shapes; the schema is the source of truth).

The rest of the pipeline (rules engine, vault, Rust Shield, UI) is unchanged —
this module only produces a `CustomsManifest`, exactly like the mock did.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import fitz  # PyMuPDF

from brain.schema import CustomsManifest, LineItem, Party

# Default vision model. Qwen2.5-VL is used because it runs on the local Ollama
# runner (unlike Llama 3.2 Vision's `mllama` arch, which this Ollama build cannot
# load) and is strong at document/OCR extraction. Override with the env var.
OLLAMA_URL = os.environ.get("KHALEEJNODE_OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("KHALEEJNODE_VISION_MODEL", "qwen2.5vl:7b")

MAX_PAGES = int(os.environ.get("KHALEEJNODE_MAX_PAGES", "3"))
RENDER_DPI = int(os.environ.get("KHALEEJNODE_RENDER_DPI", "150"))
REQUEST_TIMEOUT = int(os.environ.get("KHALEEJNODE_VISION_TIMEOUT", "300"))

_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "svg"}

EXTRACTION_PROMPT = """You are a UAE customs manifest extraction engine.
Read the attached shipping document image(s) and extract the data into STRICT JSON
with EXACTLY this structure and these keys:

{
  "bill_of_lading": "string",
  "vessel": "string or null",
  "port_of_loading": "string",
  "port_of_discharge": "string",
  "eta": "YYYY-MM-DD or null",
  "shipper":   {"name": "string", "address": "string", "country": "ISO-2 code", "tax_id": "string or null"},
  "consignee": {"name": "string", "address": "string", "country": "ISO-2 code", "tax_id": "string or null"},
  "items": [
    {"description": "string", "hs_code": "string or null", "quantity": 0,
     "unit": "string", "unit_weight_kg": 0.0, "declared_value_aed": 0.0,
     "country_of_origin": "ISO-2 country code or null"}
  ],
  "declared_gross_weight_kg": 0.0,
  "currency": "AED"
}

Rules:
- Respond with ONLY the JSON object. No prose, no markdown, no code fences.
- Transcribe TEXT values EXACTLY as they appear, preserving the original spelling —
  including any typos or misspellings. Do NOT auto-correct spelling or grammar
  (e.g. if the document says "irn nails", output "irn nails", not "iron nails").

NUMERIC FIELDS (quantity, unit_weight_kg, declared_value_aed, declared_gross_weight_kg):
- Output PLAIN numbers only. Strip currency symbols/codes, units, and thousands
  separators. Examples: "AED 24,000" -> 24000 ; "12 kg" -> 12 ; "1,250.5" -> 1250.5.
- Read EVERY line item's quantity, weight, and value. Each row in a line-items
  table almost always has all three — scan the row fully, including the rightmost
  columns where value/amount usually sits.
- "declared_value_aed" is the per-line declared / customs / invoice / amount value.
- "unit_weight_kg" is the weight of ONE unit. If only a line TOTAL weight is shown,
  divide it by the quantity.
- If a number is visibly present in the document, you MUST extract it — do NOT
  output 0 for a value that is actually shown.

- If a field is genuinely not present in the document, use null (or "" for required
  strings, 0 for required numbers). Do NOT invent data that is not visible.
"""

REPAIR_PROMPT_TEMPLATE = """You already read this shipping document, but some
NUMERIC values for the line items were missing. Look at the document image(s)
again and read ONLY the numbers for these line items.

Line items (index: description):
{lines}

Respond with STRICT JSON only, no prose or code fences:
{{
  "declared_gross_weight_kg": <number>,
  "items": [
    {{"index": <int>, "unit_weight_kg": <number>, "declared_value_aed": <number>}}
  ]
}}

Output PLAIN numbers only — strip currency symbols/codes, units and thousands
separators (e.g. "AED 24,000" -> 24000, "12 kg" -> 12). The value/amount is
usually in the rightmost column of each row. If a value truly is not shown in
the document, use 0.
"""


class VisionError(Exception):
    """Raised when local vision extraction cannot be completed."""


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------
def available() -> bool:
    """True if the Ollama server is reachable and the vision model is present."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            tags = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    names = {m.get("name", "") for m in tags.get("models", [])}
    # Match "llama3.2-vision" or "llama3.2-vision:latest" / ":11b".
    return any(n == MODEL or n.startswith(MODEL + ":") for n in names)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# Longest image edge sent to the model. Vision models downscale internally, and
# oversized pages waste tokens/time without helping OCR.
_MAX_EDGE = int(os.environ.get("KHALEEJNODE_MAX_EDGE", "1600"))
# Raster formats we hand to the model as-is (no re-render needed).
_RASTER_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}


def _render_page(page) -> bytes:
    """Rasterize a single page to PNG at RENDER_DPI, capped at _MAX_EDGE."""
    zoom = RENDER_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    longest = max(pix.width, pix.height)
    if longest > _MAX_EDGE:
        z = zoom * (_MAX_EDGE / longest)
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
    return pix.tobytes("png")


def _render_to_png(file_bytes: bytes, filename: str) -> list[bytes]:
    """Turn any supported input into a list of page images for the model.

    * Real PDFs (text-layer OR scanned/image-only) -> rendered page images.
    * Plain raster images (PNG/JPG/…) -> passed straight through.
    * SVG / unknown -> rendered via PyMuPDF.

    Because the model always receives pixels, scanned/image-based PDFs work
    exactly like born-digital ones — the model reads them visually.
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    is_pdf = ext == "pdf" or file_bytes[:5] == b"%PDF-"

    # Raster image input: hand it to the model directly (it resizes internally).
    if not is_pdf and ext in _RASTER_EXTS:
        return [file_bytes]

    try:
        if is_pdf:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        elif ext in _IMAGE_EXTS:  # e.g. svg
            doc = fitz.open(stream=file_bytes, filetype=ext)
        else:
            doc = fitz.open(stream=file_bytes)  # let PyMuPDF sniff
    except Exception as exc:  # noqa: BLE001
        raise VisionError(f"could not open document for rendering: {exc}") from exc

    images: list[bytes] = []
    try:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES:
                break
            images.append(_render_page(page))
    finally:
        doc.close()

    if not images:
        raise VisionError("document produced no renderable pages")
    return images


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------
def _call_model(images_png: list[bytes], prompt: str = EXTRACTION_PROMPT) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(im).decode("ascii") for im in images_png],
            }
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        raise VisionError(f"local vision model request failed: {exc}") from exc
    except ValueError as exc:
        raise VisionError(f"local vision model returned non-JSON envelope: {exc}") from exc

    content = body.get("message", {}).get("content", "")
    if not content:
        raise VisionError("local vision model returned an empty response")
    return content


# ---------------------------------------------------------------------------
# Coercion into the schema
# ---------------------------------------------------------------------------
def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_float(value, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def _opt(value):
    s = _as_str(value)
    return s or None


def _party(raw, role: str) -> Party:
    raw = raw if isinstance(raw, dict) else {}
    return Party(
        name=_as_str(raw.get("name"), f"UNKNOWN {role}"),
        address=_as_str(raw.get("address")),
        country=_as_str(raw.get("country"), "XX")[:2].upper() or "XX",
        tax_id=_opt(raw.get("tax_id")),
    )


def _coerce_manifest(data: dict) -> CustomsManifest:
    if not isinstance(data, dict):
        raise VisionError("model output was not a JSON object")

    items: list[LineItem] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        items.append(
            LineItem(
                description=_as_str(raw.get("description"), "UNSPECIFIED"),
                hs_code=_opt(raw.get("hs_code")),
                quantity=_as_int(raw.get("quantity")),
                unit=_as_str(raw.get("unit"), "PCS") or "PCS",
                unit_weight_kg=_as_float(raw.get("unit_weight_kg")),
                declared_value_aed=_as_float(raw.get("declared_value_aed")),
                country_of_origin=_opt(raw.get("country_of_origin")),
            )
        )

    eta = _as_str(data.get("eta"))
    # Let Pydantic validate the date; drop it if malformed.
    eta_value = eta if len(eta) == 10 and eta[4] == "-" and eta[7] == "-" else None

    return CustomsManifest(
        bill_of_lading=_as_str(data.get("bill_of_lading"), "UNKNOWN"),
        vessel=_opt(data.get("vessel")),
        port_of_loading=_as_str(data.get("port_of_loading"), "UNKNOWN"),
        port_of_discharge=_as_str(data.get("port_of_discharge"), "UNKNOWN"),
        eta=eta_value,
        shipper=_party(data.get("shipper"), "shipper"),
        consignee=_party(data.get("consignee"), "consignee"),
        items=items,
        declared_gross_weight_kg=_as_float(data.get("declared_gross_weight_kg")),
        currency=_as_str(data.get("currency"), "AED") or "AED",
    )


def _parse_json(content: str) -> dict:
    content = content.strip()
    # Strip accidental code fences if the model added them despite instructions.
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Salvage the outermost {...} block.
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise VisionError("could not parse JSON from model output")


# ---------------------------------------------------------------------------
# Numeric repair pass
# ---------------------------------------------------------------------------
REPAIR_PASSES = int(os.environ.get("KHALEEJNODE_REPAIR_PASSES", "1"))


def _has_numeric_gaps(manifest: CustomsManifest) -> bool:
    """True if any per-line weight/value or the gross weight is missing (<= 0)."""
    if manifest.declared_gross_weight_kg <= 0:
        return True
    for item in manifest.items:
        if item.unit_weight_kg <= 0 or item.declared_value_aed <= 0:
            return True
    return False


def _repair_numeric(images: list[bytes], manifest: CustomsManifest) -> CustomsManifest:
    """Ask the model a focused follow-up for the missing numbers and merge them.

    Only fills fields that are currently <= 0; never overwrites a value the first
    pass already captured. Returns the original manifest unchanged on any error.
    """
    lines = "\n".join(f'{i}: "{it.description}"' for i, it in enumerate(manifest.items))
    prompt = REPAIR_PROMPT_TEMPLATE.format(lines=lines)

    try:
        data = _parse_json(_call_model(images, prompt))
    except VisionError:
        return manifest

    by_index: dict[int, dict] = {}
    for row in data.get("items") or []:
        if isinstance(row, dict) and "index" in row:
            try:
                by_index[int(row["index"])] = row
            except (TypeError, ValueError):
                continue

    new_items: list[LineItem] = []
    for i, item in enumerate(manifest.items):
        row = by_index.get(i, {})
        uw = item.unit_weight_kg
        if uw <= 0:
            uw = _as_float(row.get("unit_weight_kg"), 0.0)
        val = item.declared_value_aed
        if val <= 0:
            val = _as_float(row.get("declared_value_aed"), 0.0)
        new_items.append(
            LineItem(
                description=item.description,
                hs_code=item.hs_code,
                quantity=item.quantity,
                unit=item.unit,
                unit_weight_kg=uw,
                declared_value_aed=val,
                country_of_origin=item.country_of_origin,
            )
        )

    gross = manifest.declared_gross_weight_kg
    if gross <= 0:
        gross = _as_float(data.get("declared_gross_weight_kg"), 0.0)

    return CustomsManifest(
        bill_of_lading=manifest.bill_of_lading,
        vessel=manifest.vessel,
        port_of_loading=manifest.port_of_loading,
        port_of_discharge=manifest.port_of_discharge,
        eta=manifest.eta,
        shipper=manifest.shipper,
        consignee=manifest.consignee,
        items=new_items,
        declared_gross_weight_kg=gross,
        currency=manifest.currency,
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def extract_with_vision(file_bytes: bytes, filename: str = "document.pdf") -> CustomsManifest:
    """Extract a CustomsManifest from a document using the local vision model.

    Runs one extraction pass, then — only if numeric fields look missing — up to
    KHALEEJNODE_REPAIR_PASSES focused follow-up passes to recover them.
    """
    images = _render_to_png(file_bytes, filename)
    manifest = _coerce_manifest(_parse_json(_call_model(images)))

    passes = REPAIR_PASSES
    while passes > 0 and _has_numeric_gaps(manifest):
        repaired = _repair_numeric(images, manifest)
        # Stop early if a pass made no further progress (avoid wasted inference).
        if repaired == manifest:
            break
        manifest = repaired
        passes -= 1

    return manifest
