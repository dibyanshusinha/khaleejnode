"""
benchmark/degrade.py
====================
Programmatic document degradation to stress-test extraction on "messy" inputs.

Takes a clean rendered page image and applies realistic damage — skew, blur,
sensor noise, resolution loss, contrast loss, JPEG artifacts, and a sinusoidal
warp that mimics creases/crumples — at graded severity LEVELS. Feeding these
through the extractor produces an accuracy-vs-degradation curve, which is how you
turn "will it read messy papers?" into a measured number.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

# Each level is a bundle of transform parameters. `clean` is the untouched page.
LEVELS: dict[str, dict] = {
    "clean": {},
    "light": {"rotate": 2.0, "blur": 0.6, "noise": 5, "jpeg": 85},
    "medium": {"rotate": 5.0, "blur": 1.2, "noise": 12, "scale": 0.7, "contrast": 0.85, "jpeg": 60, "warp": 4},
    "heavy": {"rotate": 9.0, "blur": 2.0, "noise": 22, "scale": 0.5, "contrast": 0.70, "jpeg": 35, "warp": 8},
}
LEVEL_ORDER = ["clean", "light", "medium", "heavy"]


def _to_image(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _warp(img: Image.Image, amplitude: int, period: int = 90) -> Image.Image:
    """Sinusoidal pixel displacement — mimics paper creases/crumple."""
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    dx = (amplitude * np.sin(2 * np.pi * yy / period)).astype(int)
    dy = (amplitude * np.sin(2 * np.pi * xx / period)).astype(int)
    xs = np.clip(xx + dx, 0, w - 1)
    ys = np.clip(yy + dy, 0, h - 1)
    return Image.fromarray(arr[ys, xs])


def _add_noise(img: Image.Image, sigma: float) -> Image.Image:
    arr = np.asarray(img).astype(np.float32)
    noise = np.random.normal(0, sigma, arr.shape)
    return Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8))


def _jpeg(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def degrade(image_bytes: bytes, level: str, seed: int | None = None) -> bytes:
    """Return a PNG-encoded degraded version of the page at the given level."""
    if seed is not None:
        np.random.seed(seed)
    params = LEVELS.get(level, {})
    img = _to_image(image_bytes)

    if params.get("warp"):
        img = _warp(img, params["warp"])
    if params.get("rotate"):
        img = img.rotate(params["rotate"], expand=False, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    if params.get("scale"):
        w, h = img.size
        small = img.resize((max(1, int(w * params["scale"])), max(1, int(h * params["scale"]))), Image.BILINEAR)
        img = small.resize((w, h), Image.BILINEAR)
    if params.get("contrast"):
        img = ImageEnhance.Contrast(img).enhance(params["contrast"])
    if params.get("blur"):
        img = img.filter(ImageFilter.GaussianBlur(params["blur"]))
    if params.get("noise"):
        img = _add_noise(img, params["noise"])
    if params.get("jpeg"):
        img = _jpeg(img, params["jpeg"])

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
