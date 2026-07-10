# KhaleejNode — Go-Live Playbook

Read this before shipping. Run `python tools/preflight.py` for a live GO/NO-GO.

## The honest verdict

**Do NOT launch this "to the world" today as a customs *compliance* product.**
With the bundled sample tariff/sanctions data, a "clean" verdict is not
authoritative — and a compliance tool that gives false assurance is a liability,
not a feature. Preflight returns **NO-GO for public release**.

**You CAN launch today** as a **supervised, on-prem pilot** with 2–3 design
partners, positioned honestly as an *AI-assisted document pre-check that requires
human review* — which is exactly the blueprint's Week 7–8 pilot step. The app now
enforces that positioning in code.

## What was hardened for launch (done + verified)

| Guardrail | Effect |
|---|---|
| **Data-provenance detection** | Engine detects sample vs official reference data (`data_mode`). |
| **Loud sample warning** | Every result on sample data emits a `REFERENCE_DATA_SAMPLE` WARN. |
| **Fail-safe gating** | Nothing is ever "READY" on sample data — it becomes **REVIEW**; blockers become **HELD**. |
| **`human_review_required`** | Set on every non-authoritative result; surfaced in the UI. |
| **Disclaimers** | Persistent top banner + per-result disclaimer: "not customs/legal advice; verify against official sources." |
| **Preflight check** | `tools/preflight.py` prints a GO/NO-GO with blockers. |

These make the current build **safe to run in front of a human operator**.

## Blockers before a public / authoritative launch (cannot be faked in code)

1. **License real data.**
   - ✅ **Sanctions — DONE.** Real OFAC SDN + UN Consolidated lists are wired
     (~20k entries) via `tools/build_sanctions.py`; screening reports
     `sanctions: OFFICIAL`. Re-run the tool on a schedule to refresh.
   - ⬜ **Tariff — still needed.** Official GCC/UAE tariff (HS 2022). Drop it into
     `brain/compliance/data/hs_tariff.json` and `data_mode` flips fully to
     `OFFICIAL`. Add EU/UK/UAE sanctions lists to the builder too if in scope.
2. **Legal + liability.** Terms of service, disclaimers reviewed by counsel,
   and a clear "assistive, not authoritative" contract — you are in regulated
   compliance territory.
3. **Mirsal 2 integration.** Real API access/credentials/certification to make
   the output an actual filing rather than an internal pre-check.
4. **Accuracy at scale.** Run `benchmark/` on a large, real messy-document set;
   publish an accuracy number. Add input preprocessing (deskew/denoise) and a
   human-in-the-loop review queue for low-confidence fields (heavy-degradation
   accuracy is currently 0%).
5. **Production security.** Provision a real `SECRET_SALT` to the OS keychain
   (not the dev default), keep `tools/vendor_private.pem` off every client build,
   code-sign + notarize the installer, and add an update channel.
6. **Data protection.** Even on-prem you handle client manifests — document
   retention, access control, and DPA posture.

## Recommended launch path (this week)

1. `python tools/preflight.py` — expect NO-GO for public, **OK for supervised pilot**.
2. Provision a real salt + sign the build (removes 1 security blocker).
3. Ship the on-prem app to 2–3 friendly forwarders as a **pre-check assistant**.
   Keep the disclaimers on. Have operators treat every output as a draft.
4. Collect real documents + corrections → feed `benchmark/` → get your true
   accuracy number. This is the VC traction metric.
5. In parallel, start the **data-licensing** and **Mirsal 2 access** processes —
   they are the long-lead items and the real moat-completers.

## One-line summary

The infrastructure and the compliance *engine* are launch-grade; the **data,
integrations, and legal wrapper** are not. Ship a supervised pilot today, sell
the roadmap, and flip to authoritative once real data + Mirsal 2 land.
