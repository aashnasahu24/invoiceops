# Stage 3 notes — persisted vertical slice

## Implemented

- Repository functions create and complete workflow runs, upsert stage state, store
  extracted text/raw provider output, persist normalized invoices/checks, detect
  exact and active business-key duplicates, and derive PO balance using `Decimal`.
- Pure vendor/PO matchers prefer normalized tax ID, then normalized legal name or
  aliases. PO numbers require an exact normalized match; missing PO references return
  amount-ranked open candidates and never auto-match.
- Minimal deterministic checks cover required fields, grounding, OCR confidence,
  vendor status/identity conflict, PO presence, arithmetic, and PO balance. A blocked
  vendor and duplicates are hard rejects. Every warning or other failure routes to
  manual review; low OCR confidence below policy always does so.
- The orchestrator yields and persists the seven ordered stages, writes extraction
  artifacts, routes expected PDF/OCR/provider failures to a failed run and
  `MANUAL_REVIEW`, and persists invoices only for `APPROVED` or `MANUAL_REVIEW`.
- The Streamlit app has Process invoice and Dashboard pages, provider selection,
  live status updates, evidence markers, check results, recent run selection, and a
  confirmed reset action. Session objects are short-lived per operation.
- Deployment support includes a non-root Python/Tesseract Docker image,
  `.dockerignore`, and a smoke test for DB/schema, Tesseract, Gemini key, and replay
  cache readiness.

## Deliberately deferred to Stage 4

- Split-invoice PO balance lifecycle and cumulative balance edge cases beyond
  summing existing approved invoices.
- PO overrun price tolerance and price/quantity comparison against PO lines.
- Soft duplicates based on vendor, total, and date.
- Quantity billed versus quantity ordered and invoice lines that do not appear on a
  PO.
- Reviewer actions (approve, reject, request information, comments) and balance
  revalidation during review.
- Rich review queue, reporting/export, and broader dashboard analytics.

## Validation and deployment

Automated coverage is offline and uses SQLite plus fake/replay providers. PostgreSQL
parity remains an opt-in run using `PG_TEST_URI`. The deployment smoke test is
`python -m scripts.smoke_test`; it performs no LLM network request. Deploy with a
Neon `DATABASE_URL`, `GEMINI_API_KEY`, and configured `GEMINI_MODEL`, then verify
the deployed app by waking it and running a replay fixture before a live Gemini
request.
