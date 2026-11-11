# InvoiceOps v1 audit (Nov 10)

Scope: full read of `invoiceops/`, `app.py`, `scripts/`, tests, data, docs. Everything below was **run**, not assumed.
Environment: Python 3.12, Tesseract 5.3.4, SQLite + embedded PostgreSQL.

## 1. Verified state

| Check | Result |
|---|---|
| `pytest -q` (needs `GEMINI_MODEL=gemini-3.5-flash-lite`, see F-12) | **204 passed, 6 skipped** (matches your report) |
| `tests/test_pg_parity.py` on real PostgreSQL (incl. two-thread PO race) | **5 passed** (was never run before) |
| `run_manifest --provider replay` on SQLite | **8/8 PASS** |
| Same manifest on PostgreSQL | **8/8 PASS** |
| Replay cache | 12/12 fixtures hit; all match current prompt hash; `resubmitted_clean` is a real recording (earlier "circular score" suspicion **withdrawn**) |
| Scanned fixture OCR confidence | 44.8% (your machine) vs 42.5% (Tesseract 5.3.4 here). Decision is MANUAL_REVIEW on both, so the outcome is stable across OCR versions |
| Secrets in zip | none |

## 2. What exists (done)

Foundation (Stage 1): schemas, 9-table DB, partial-unique active business key, UTC datetimes, reset, seed, normalization, grounding.
Extraction (Stage 2): Gemini provider, strict LLM schema, retry/feedback wrapper, record/replay cache, eval harness with expected labels.
Pipeline (Stage 3): 7-stage generator orchestrator, persistence with stages/artifacts, Streamlit process + dashboard, Dockerfile, smoke test.
Decisioning (Stage 4): 15 deterministic checks, line matching, PO balance/quantity ledger, FOR UPDATE re-check, reviewer actions, resubmission supersede, manifest runner.

## 3. Workflow

```
PDF upload
 -> RECEIVED          sha256; exact-file duplicate => REJECTED (no further stages)
 -> TEXT_EXTRACTION   PyMuPDF (row-preserving) | Tesseract OCR with confidence
 -> AI_EXTRACTION     Gemini (or replay cache); retry on transient errors; 1 feedback retry on schema errors
 -> SCHEMA_VALIDATION placeholders -> null; grounding vs source text; evidence verified
 -> MATCHING          vendor: tax ID -> name/alias | PO: exact only | lines: SKU -> fuzzy | duplicate lookups
 -> VALIDATION        15 checks (PASSED / WARNING / FAILED, some flagged hard_reject / hard_block)
 -> DECISION          hard_reject => REJECTED | any FAILED or escalating WARNING => MANUAL_REVIEW | else APPROVED
                      re-check under PO row lock, persist invoice, update PO status
Review queue: APPROVE (re-checks; hard blocks immovable; override needs comment) | REJECT | REQUEST_INFORMATION (email; invoice inactive; corrected resubmission supersedes)
```

## 4. Findings (all reproduced)

`tests/test_audit_regressions.py` contains one `xfail(strict=True)` test per P0/P1 item below. Fix a bug -> its test XPASSes -> strict mode fails -> delete the marker.

### P0: wrong approvals or crashes

| ID | Finding | Evidence |
|---|---|---|
| F-1 | **Vendor identity is only half-checked.** Conflict is flagged only when tax ID and name resolve to *two different vendors*. A tax ID that matches nobody + a known name => accepted on name. A known tax ID + a totally unrelated name => accepted on tax ID. Both **auto-APPROVED**. | `29ZZZZZ9999Z9Z9` + "ABC Technologies Pvt Ltd" => APPROVED; V001 tax ID + "Totally Different Corp" => APPROVED |
| F-2 | **No `qty x unit_price == line_total` check.** Unit price matches the PO, line total is inflated 40,000 -> 45,000, subtotal/tax/total are made consistent => APPROVED and 45,000 consumed. | `matching.py`/`rules.py`: only line_sum vs subtotal is checked |
| F-3 | **Currency is never compared.** USD invoice against an INR PO => APPROVED. | no `currency` reference in `rules.py` |
| F-4 | **Unexpected exceptions crash the pipeline.** Only PDF + extraction errors are caught. Any other exception (SDK bug, rules bug, DB `IntegrityError` on a unique-index race) escapes the generator: run stays `PROCESSING` forever, a stage stays `RUNNING`, Streamlit shows a traceback. Gemini provider re-raises unmapped exceptions. | reproduced with `RuntimeError` in provider and `ZeroDivisionError` in rules |
| F-5 | **App restart reopens closed POs.** `get_resources()` calls `seed_reference_data()` on every start (free hosts sleep/restart constantly); seed overwrites `po.status`. PO-1003 CLOSED -> OPEN while its approved invoices remain. | reproduced |

### P1: explanations and demo credibility

| ID | Finding |
|---|---|
| F-6 | Approved summary shows **this invoice's** share ("60,000 of 100,000 used (60%)") while the PO is CLOSED with 0 remaining. Should be cumulative. |
| F-7 | **Vendor email is unusable.** Lists every WARNING/FAILED message: "not grounded in the document: tax_total", "OCR confidence 42.46153846153846...", "possible OCR loss of decimal point" + 5 cascade bullets. Vendors should see only what *they* can fix. |
| F-8 | **Cascade noise.** One root cause (PO missing) produces 5 more escalating WARNINGs (po_status, line_on_po, unit_price, quantity, po_balance). Dependent checks should be `SKIPPED`/not-applicable, not warnings. |
| F-9 | Dashboard + decision card show `run.decision` (automated). After a reviewer approves/rejects, they still say "Sent to review". Needs an *effective* decision (`invoice.current_decision`) + "reviewed by". |
| F-10 | **Reviewer REJECT keeps the business key active**, so a corrected invoice with the same number is rejected as "duplicate of a rejected invoice". Contradicts `docs/03` and is inconsistent with auto-REJECTED invoices (which are not persisted at all). Policy decision needed. |
| F-11 | Message bugs: "factor of 100" hardcoded for factor 10; OCR confidence as raw float; sentences joined without punctuation ("...tolerance. off by..."); a blocked-vendor summary also lists an unrelated price variance (fixture PO-1005 has price 10,000 vs invoice 15,000, so the "clean except blocked" case isn't clean); `rs` accepted as an INR marker (substring of ordinary words), so INR is "grounded" by accident. |

### P2: robustness and hygiene

| ID | Finding |
|---|---|
| F-12 | **Cache/env coupling.** Cache was recorded with `gemini-3.5-flash-lite` but `.env.example`/README say `gemini-3.8-flash`. Copying `.env.example` => 100% replay miss (this is why 3 manifest tests failed for me). Cache records don't store the model. `test_manifest_e2e` reads `GEMINI_MODEL` from env so it fails on a clean checkout/CI. 12 of 24 cache files are orphans. `ReplayProvider` ignores the recorded prompt hash. `smoke_test` only checks JSON is readable, **not that fixtures hit**. |
| F-13 | Reviewer audit: the checks that were overridden are not stored; REQUEST_INFORMATION leaves `current_decision=MANUAL_REVIEW` (no explicit RETURNED state). |
| F-14 | Free-tier reality: quota/daily-limit exhaustion is retried 3x with backoff then fails the invoice; no automatic fallback to replay or banner. Live recorder writes into the committed demo cache dir (read-only container FS not tested; sandbox is root). |
| F-15 | UI: decision card is lost on any Streamlit rerun (not in `session_state`); Reset has no auth if deployed publicly; README header still says "Stage 3". |

### Known limits (not bugs; state them in the demo)
Tax-inclusive invoices route to review; price variance is symmetric (under-billing >2% also flagged); greedy one-to-one line matching; vendor matching is exact-normalized only (typos => review, deliberate); no date sanity checks (future-dated, due < invoice); no credit notes; INR only; single-tenant, synchronous.

## 5. Where we stand
Core pipeline, data model, all four required edge cases (duplicate, poor scan, PO mismatch, split), reviewer flow, replay-safe demo path: **working and verified on SQLite and PostgreSQL**. Remaining risk is not missing features but **5 ways to approve or crash wrongly (F-1..F-5)** and a demo that reads rough (F-6..F-11). Both are small, well-defined fixes with tests already written.

## 6. Plan
**5A Hardening (must, ~1 day):** F-1..F-5, F-11 numerics, then flip the xfail tests. Add 3 adversarial manifest fixtures (impersonation, inflated line, currency mismatch) + record them with Gemini.
**5B Explainability + UX (~1 day):** F-6..F-10, F-13, dependent-check N/A, vendor-facing email templates, effective decision on dashboard/card, KPI strip, PO ledger page, stage timeline polish, session_state results.
**5C Ship (~half day):** F-12, F-14, F-15; Docker build; Neon; smoke test that proves cache hits; wake procedure; 3 rehearsals with reset; 5-minute video script.

## 7. Decisions needed (my defaults in bold)
1. Reviewer-rejected invoice, same number resubmitted: **allow (set inactive)** / require new number.
2. Reviewer approval over PO balance: **allow with mandatory comment, recorded as "PO exception"** / hard block.
3. Tax-inclusive invoices: **review (current)** / derive net.
4. Price variance: **keep symmetric, label as "variance"** / one-sided.
