# KhaleejNode — Compliance Engine & Accuracy Benchmark

This is the intelligence layer that turns extracted manifests into an auditable,
scored customs-compliance result — plus the harness that measures extraction
accuracy under document degradation. All offline.

> **Data disclaimer:** the bundled tariff and sanctions/dual-use/country datasets
> under `brain/compliance/data/` are **REPRESENTATIVE starter sets**, not the
> licensed GCC Common Tariff or the official OFAC/UN/EU/UK/UAE lists. The *engine
> logic is production-shaped*; drop the real lists in the same JSON schema and
> everything works unchanged.

## 1. Compliance engine (`brain/compliance/`)

Runs on every extracted `CustomsManifest` (via `brain/rules.py`) and emits
findings + a scored summary.

| Module | Does |
|---|---|
| `tariff.py` | Normalizes/validates HS codes, looks them up, checks the declared description plausibly matches the code, computes ad-valorem **duty**. |
| `screening.py` | **Denied-party** screening (fuzzy name match, robust to legal suffixes/aliases/spelling), **dual-use/controlled-goods** (by HS prefix + keyword), **country-risk/embargo**. |
| `crosschecks.py` | Arithmetic reconciliation — sum of line weights vs declared gross, value-per-kg plausibility. Catches **silent misreads** a confident model won't self-report. |
| `engine.py` | Orchestrates the above into findings + a `compliance` summary with a **mathematically scored risk value** (0–100) and band (low/elevated/high/critical). |

**Finding codes:** `HS_MISSING`, `HS_UNKNOWN`, `HS_MISMATCH`, `DUAL_USE`,
`SANCTIONS_HIT` (BLOCK), `COUNTRY_RISK`, `WEIGHT_RECONCILE`,
`VALUE_WEIGHT_IMPLAUSIBLE`, `GROSS_WEIGHT_MISSING`.

The result rides in `validation.compliance`:
```json
{
  "risk_score": 100, "risk_band": "critical",
  "duty": {"lines": [...], "total_duty_aed": 1100.0, "total_declared_value_aed": 22000.0},
  "screening": {"denied_party_hits": [{"role":"shipper","matched":"...","score":1.0}]},
  "totals": {"line_count": 2, "computed_weight_kg": 560.0, ...}
}
```
The UI renders this as a risk chip + duty/value + sanctions badge on each entry.

### Data provenance & going production
The engine reports `data_sources` per list (`OFFICIAL` vs `SAMPLE`) and only sets
`data_mode: OFFICIAL` when **all** are official. A list is "official" when its
`_meta.source` doesn't contain sample/representative markers.

- **Sanctions — DONE (official).** `data/denied_parties.json` is built from the
  real public **OFAC SDN + UN Consolidated** lists via:
  ```bash
  # vendor-side; downloads change frequently, so refresh on a schedule
  curl -sSL -o /tmp/ofac_sdn.csv https://www.treasury.gov/ofac/downloads/sdn.csv
  curl -sSL -o /tmp/ofac_alt.csv https://www.treasury.gov/ofac/downloads/alt.csv
  curl -sSL -o /tmp/ofac_add.csv https://www.treasury.gov/ofac/downloads/add.csv
  curl -sSL -o /tmp/un.xml https://scsanctions.un.org/resources/xml/en/consolidated.xml
  python tools/build_sanctions.py --ofac-dir /tmp --un /tmp/un.xml
  ```
  ~20k entries; screening is indexed (1–4 ms/query). Add EU OFSI / UK / UAE lists
  to `tools/build_sanctions.py` the same way.
- **Tariff — still sample.** Replace `data/hs_tariff.json` with the licensed
  GCC/UAE tariff (HS 2022) to flip `data_mode` fully to OFFICIAL.
- Replace `data/dual_use.json` / `data/country_risk.json` with your control lists.
- Consider embeddings for HS↔description matching (current check is keyword-based).

## 2. Accuracy & degradation benchmark (`benchmark/`)

Measures extraction accuracy against **known ground truth**, across graded
document damage — the methodology behind a "N documents at X% accuracy" metric.

```bash
python benchmark/run_benchmark.py --engine vision            # full curve
python benchmark/run_benchmark.py --engine vision --limit 1  # one fixture (faster)
python benchmark/run_benchmark.py --engine mock              # pipeline check (fast)
python benchmark/run_benchmark.py --levels clean,heavy --out report.json
```

- `fixtures.py` — ground-truth manifests + a clean renderer.
- `degrade.py` — `clean → light → medium → heavy` damage (skew, blur, noise,
  resolution loss, contrast, JPEG artifacts, crease-like warp).
- `evaluate.py` — field-level scoring, reporting **overall** and **critical-field**
  accuracy (HS code, quantity, weight, value — the fields that cause seizures).

**Sample run (Qwen2.5-VL 7B, 1 fixture):**

| Level | Overall | Critical |
|---|---|---|
| clean | 100% | 100% |
| light | 100% | 100% |
| medium | 94% | 88% |
| heavy | 0% | 0% |

Reads through moderate mess; falls off a cliff on heavy crumpling/blur. Scale up
`fixtures.py` for a statistically meaningful number.
