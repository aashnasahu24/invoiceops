# InvoiceOps — Plan Revision 2

Supersedes the conflicting parts of docs 01 and 02. Everything marked **[done]** is implemented and tested in Stage 1.

## 1. Policy changes (v1.1)

| Topic | Old | New |
|---|---|---|
| Duplicate business key | Any prior invoice blocks | Only **active** invoices block. A returned/withdrawn invoice is set inactive, so a corrected resubmission with the same number is a new version (`supersedes_invoice_id`). Identical file bytes are always a hard duplicate. **[schema done]** |
| Approved balance | Implicit | `invoices.net_amount` stored; only `current_decision = APPROVED` consumes PO balance. Balance is **derived** (Python Decimal), never stored. PO status (OPEN / PARTIALLY_INVOICED / CLOSED) is derived from it. |
| Decision fields | One decision | `workflow_runs.decision` = automated decision (immutable). `invoices.current_decision` = effective decision after any reviewer action. |
| Reviewer approve | Unspecified | Re-runs balance and duplicate checks against current state before approving. |
| Ungrounded extraction | n/a | Any required field whose value is not found in the source text → MANUAL_REVIEW. **[signal done]** |
| OCR quality | Text length only | Mean OCR word confidence below `PolicyConfig.min_ocr_confidence` always forces MANUAL_REVIEW; grounding cannot detect OCR misreads. |
| PO line matching | No line-level match | Exact SKU first, then RapidFuzz `token_sort_ratio` at `PolicyConfig.line_match_threshold` (85). |
| Price/quantity | No comparison | 2% relative unit-price tolerance; billed quantity is compared with ordered quantity less earlier approved invoices. |
| Soft duplicate | None | Same vendor and net amount within 7 days, with a different invoice number, is a review signal. |

### Deterministic checks
1. Quantity billed vs quantity ordered.
2. Invoice line not on the PO at all → review (SKU first, then RapidFuzz on description).
3. Tax ID and name resolve to **different** vendors, or tax ID matches with a materially different name → review (fraud signal).
4. Soft duplicate: same vendor + same net amount + date within the configured days but different invoice number → review (catches OCR misreads).
5. Missing PO: list ranked **candidate POs** (vendor + amount/line fit). Never auto-match.

### Output additions
Every non-approved result carries a `recommended_action` ("Ask vendor for the PO number; likely PO-1003") and a plain-language summary **templated from the checks**, not LLM-decided.

## 2. Architecture changes

- **[done]** `config.py`: `Settings` (env) and `PolicyConfig` (versioned thresholds).
- **[done]** Two schemas on purpose: `InvoiceExtractionLLM` (strict, all-required, string values, evidence) goes to the model; `InvoiceExtraction` is the validated domain object. Formatting noise (`₹25,000.00`, `inr`) is cleaned by code, not rejected.
- **[done]** `grounding.py`: deterministic verification of values and evidence snippets. Replaces LLM confidence.
- **[done]** `run_stages` table + `StageEvent` schema; `workflow_runs.extracted_text` and `llm_response` for audit.
- **[done]** Timezone-safe `UTCDateTime`; partial unique index; `reset_demo_state`; DB URL normalisation.
- **Orchestrator = generator** yielding `StageEvent`s. Streamlit renders them live with `st.status` in the same script run and persists them for history. No polling.
- **DB testing:** SQLite for fast unit tests, real PostgreSQL for `tests/test_pg_parity.py` (run before every deploy). The approval transaction locks the PO with `SELECT … FOR UPDATE`, recomputes balance and duplicate state, and re-runs checks before persistence. PostgreSQL parity includes a two-thread approval race.
- **Failure handling:** LLM schema/validation errors → one retry with the error list fed back → then MANUAL_REVIEW with reason. A run never ends as a crash.

## 3. Scope cuts
Removed: ReportLab PDF reports (use CSV/JSON export), pandas, pdfplumber. Reviewer actions stay minimal: approve / reject / request info + comment.

## 4. Revised build order: vertical slice first
Nothing should be "done" until it runs end to end.

1. **Stage 2** Gemini adapter (timeout, bounded retries, error feedback) + `scripts/eval_extraction.py`: score labelled fixtures; extraction-only edge fixtures stay outside `test_manifest.json`.
2. **Stage 3** Persisted vertical slice: PDF upload → live stages → deterministic checks/decision → database → Streamlit process page and basic dashboard; prepare Docker/Neon deploy.
3. **Stage 4** Complete deterministic rules, review queue/actions, "Run all fixtures (actual vs expected)", and "Reset demo data".
4. **Stage 5** Rehearse, record video.

## 5. Demo plan
- `Reset demo data` button + `Run all fixtures in order` showing expected vs actual.
- Wake the hosted app before the demo (free tiers sleep).
- Interview talking points: LLM never decides; prompt-injection resistance; grounding; active-only duplicate identity; why Postgres parity tests exist.

## 6. Known limitations (state them, don't hide them)
INR only; no credit notes; tax rate not validated against jurisdiction; single-user, synchronous processing; Tesseract English only; fixtures are synthetic (two layouts + one degraded scan).
