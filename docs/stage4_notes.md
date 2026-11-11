# Stage 4 notes

## Implemented

- Stable replay keys use `SHA256(file_sha256 + schema_hash + model)`. The extracted
  text hash remains in each cache record for diagnostics. Existing fixture responses
  were re-keyed without changing their raw responses. The new
  `resubmitted_clean.pdf` replay response is seeded from its labelled fixture
  extraction so the offline manifest is runnable; record it from Gemini before
  treating that entry as a live model result.
- DejaVu Sans regular/bold fonts and their license are bundled in
  `assets/fonts/`. Only `resubmitted_clean.pdf` was generated in this stage;
  `data/fixtures.sha256` and `tests/test_fixture_hashes.py` protect every fixture
  PDF against accidental regeneration.
- PO line matching prefers exact SKU and otherwise uses RapidFuzz token-sort
  similarity with `PolicyConfig.line_match_threshold = 85`. Price tolerance is
  2%, arithmetic tolerance is INR 1, minimum OCR mean confidence is 75%, and the
  soft-duplicate window is 7 days.
- Deterministic checks cover PO status/balance, matched line and unit price,
  cumulative approved quantity, arithmetic, active business duplicates, soft
  duplicates, missing invoice numbers, vendor identity conflicts, and OCR
  confidence. Decision summaries are templated and include PO impact.
- Approved net amounts and matched PO-line quantities are rechecked under a
  `SELECT FOR UPDATE` PO lock before persistence. PO status is updated from
  approved balance. Reviewer approvals repeat the checks; hard blocks cannot be
  overridden, and other failed/escalating checks require a comment. Requesting
  information stores a generated email and deactivates the returned invoice so a
  corrected version can supersede it.
- The manifest now includes required check names, the scanned-invoice return
  action, and the clean resubmission. Both CLI and Streamlit can run it.

The additive database migration adds `invoice_line_items.matched_po_line_id` and
`review_actions.generated_email`. Reference-data seeding now updates PO lines by
line number, preserving line IDs already referenced by approved invoices.

## Offline validation

`python -m scripts.eval_extraction --provider replay --cache-dir data/llm_cache_demo`
reported **142/144 fields correct (98.61%), 11/12 fixtures fully correct**. The
only incorrect fixture is `scanned_missing_po.pdf`: OCR confidence was 44.8%;
`tax_total` replayed as `180000` rather than `1800.00`, and the line total replayed
as `1000000` rather than `10000.00`. Both remain visible mismatches; no comparison
was loosened. The arithmetic check reports the decimal-point-loss hint and the
OCR confidence check forces review. Re-record the scan from Gemini if refreshing
the cache; do not edit the expected labels to hide this OCR failure. Use
`python -m scripts.eval_extraction --provider gemini --cache-dir data/llm_cache_demo --refresh-cache`
to force fresh requests rather than reusing the migrated cache, then run the
same command with `--provider replay` to verify the recorded responses.

The ordered replay manifest completed **8/8 PASS** in the clean temporary SQLite
database used for validation:

| Fixture | Decision | Summary |
|---|---|---|
| `happy_path_invoice.pdf` | APPROVED | Approved. 25,000 of 50,000 on PO-1001 used (50%). |
| `duplicate_invoice.pdf` | REJECTED | Rejected as an active business-key duplicate; original invoice/run are named in the decision. |
| `scanned_missing_po.pdf` | MANUAL_REVIEW | Arithmetic factor-of-100 hint, low OCR confidence (44.8%), and missing PO reference. |
| `resubmitted_clean.pdf` | APPROVED | Approved. 10,000 of 75,000 on PO-1002 used (13%). |
| `po_overrun_invoice.pdf` | MANUAL_REVIEW | 80,000 exceeds the remaining 65,000; unit price is 6.7% above PO price; billed quantity exceeds remaining ordered quantity. |
| `split_invoice_1.pdf` | APPROVED | Approved. 40,000 of 100,000 on PO-1003 used (40%). |
| `split_invoice_2.pdf` | APPROVED | Approved. 60,000 of 100,000 on PO-1003 used (60%); PO becomes CLOSED after both approvals. |
| `blocked_vendor_invoice.pdf` | REJECTED | Rejected because the vendor is blocked. |

Offline tests cover cache hit/miss and key changes, all decision branches, reviewer
actions, the scanned OCR factor hint, resubmission, split invoices in either order,
a third invoice against a closed PO, and the manifest. PostgreSQL-specific tests
include a two-thread approval race. `PG_TEST_URI` was not configured in this
environment, so that live PostgreSQL test was not run here. Final local validation
was **204 passed, 6 skipped**; the separate PostgreSQL parity command reported
**5 skipped** because `PG_TEST_URI` was unset.

## Deliberate limits

- Split invoices consume aggregate PO net balance and matched-line quantities,
  but there is no allocation of fractional quantities or tax-inclusive PO logic.
- Price variance is a fixed policy percentage; there is no PO-specific price
  tolerance or negotiated exception workflow.
- Reviewer actions are limited to approve/reject/request-information; there is no
  editing UI, authentication, multi-reviewer assignment, or audit export.
- Soft duplicates are advisory signals and do not use image similarity.
- No CSV export, charts, or broader reporting are included.
