# KhaleejNode — Real Extraction Engine (local vision model, offline)

This replaces the deterministic **mock** in `brain/extractor.py` with a real
multimodal model that actually *reads* the shipping document. It stays **100%
offline**: the model runs locally via [Ollama](https://ollama.com) on
`localhost:11434` — a loopback call, not a cloud API.

> **Model note:** the default engine is **Qwen2.5-VL 7B** (`qwen2.5vl:7b`).
> Llama 3.2 Vision was the original target, but its `mllama` architecture does
> not load on the current Ollama runner (`unknown model architecture: 'mllama'`),
> so we use Qwen2.5-VL — a strong document/OCR vision model that runs on the
> standard Ollama runner. The engine is model-agnostic: set
> `KHALEEJNODE_VISION_MODEL` to swap in any Ollama vision model.

## How it works

`brain/vision.py`:

1. **Render** — PyMuPDF rasterizes the document (PDF or image) to page PNG(s).
2. **Infer** — the page image(s) + a strict extraction prompt go to the local
   `llama3.2-vision` model via Ollama's `/api/chat` (with `format: "json"`,
   `temperature: 0`).
3. **Coerce** — the model's JSON is validated and repaired into the
   `CustomsManifest` Pydantic contract (the schema is the source of truth; the
   model can't break the downstream pipeline).

Everything after extraction — the adversarial rules engine, the vault, the Rust
Shield, the UI — is **unchanged**, because this module produces the same
`CustomsManifest` the mock did.

## Setup

```bash
# 1. Ollama (macOS)
brew install ollama
brew services start ollama          # background server on :11434

# 2. Pull the model (~6 GB)
ollama pull qwen2.5vl:7b

# 3. PDF rendering dep (in the project venv)
source .venv/bin/activate && pip install pymupdf
```

> **Hardware:** the 11B vision model needs ~8 GB free RAM to run. On a 16 GB
> machine it works but is memory-tight; expect tens of seconds per page.
> For lighter clients, set `KHALEEJNODE_VISION_MODEL` to a smaller vision model.

## Engine selection

The Brain picks its engine from `KHALEEJNODE_EXTRACTOR`:

| Value | Behavior |
|---|---|
| `auto` (default) | Use the vision model if Ollama + the model are reachable, else fall back to the mock simulator. |
| `vision` | Require the vision model; error if unavailable. |
| `mock` | Always use the deterministic simulator. |

Other overrides: `KHALEEJNODE_VISION_MODEL` (default `qwen2.5vl:7b`),
`KHALEEJNODE_OLLAMA_URL` (default `http://localhost:11434`),
`KHALEEJNODE_MAX_PAGES` (default `3`), `KHALEEJNODE_RENDER_DPI` (default `150`),
`KHALEEJNODE_MAX_EDGE` (default `1600`, longest image edge sent to the model),
`KHALEEJNODE_REPAIR_PASSES` (default `1`, numeric-recovery follow-up passes).

## Supported inputs

All inputs are rendered to page images before the model reads them, so the
model does visual OCR regardless of source:

- **Born-digital PDFs** (with a text layer) ✅
- **Scanned / image-only PDFs** (no text layer) ✅ — rendered and read visually
- **Plain images** — PNG, JPG, WEBP, GIF, BMP, TIFF ✅ (passed straight through)
- **SVG** ✅ (rendered via PyMuPDF)

## Numeric fidelity

Values (quantity, weights, declared value, gross weight) are the fields a vision
model most often drops. Two mechanisms improve fidelity:

1. **Tightened prompt** — explicit rules to strip currency/units/separators
   ("AED 24,000" → 24000), scan the rightmost table columns, and never emit 0
   for a value that is actually shown.
2. **Light repair pass** — after the first extraction, if any per-line weight/
   value or the gross weight is still missing, a focused follow-up asks the model
   only for those numbers and merges them in (never overwriting captured values).
   Controlled by `KHALEEJNODE_REPAIR_PASSES`; it only fires when gaps remain, so
   clean documents pay no extra inference.

> Tip: if values still read 0, first check the source document actually shows
> them within the page bounds — text that runs off the page edge won't render
> into the image the model sees.

The active engine is reported back to the UI in each result's
`customs_submission.engine` field (e.g. `llama-vision:llama3.2-vision` vs
`mock-simulator`), so you can always see which path produced the data.

## Running it

- **Dev (via venv Python):** `./run_tauri.sh` already points the Rust backend at
  `.venv/bin/python`, which has `brain/vision.py` + PyMuPDF. With Ollama running
  and the model pulled, uploads are extracted for real.
- **CLI check:** `python brain/cli.py --file /path/to/manifest.pdf` prints the
  full JSON, including which engine ran.

## Shipping (sidecar rebuild required)

The packaged Brain sidecar must be **rebuilt** to include PyMuPDF:

```bash
./scripts/build_brain_sidecar.sh      # now bundles pymupdf/fitz
```

Two deployment notes for a real product:

- The frozen sidecar still calls `localhost:11434`, so **Ollama + the model must
  be present on the client**. Options: bundle/instruct an Ollama install, or
  embed a smaller model runtime directly in the sidecar.
- The model weights (~8 GB) are **not** shipped inside the app bundle; they are
  provisioned to the client's Ollama once. Plan your installer accordingly.
