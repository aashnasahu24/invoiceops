# Phase 1 baseline and repository audit

Audit date: 2026-11-11 (Asia/Kolkata)

## Architecture and module responsibilities

InvoiceOps is a synchronous Streamlit application. `app.py` composes the UI,
settings, SQLAlchemy engine, and workflow/reviewer services. `invoiceops/pdf_processor.py`
extracts PDF text and uses Tesseract for scanned pages. `invoiceops/providers/`
contains the Gemini adapter, retry wrapper, recording provider, and strict cache
replay provider. `invoiceops/extraction_service.py` validates the provider's
structured response and `invoiceops/grounding.py` checks evidence against source
text. `invoiceops/matching.py` matches vendor, PO, and invoice lines;
`invoiceops/rules.py` makes deterministic decisions. `invoiceops/orchestrator.py`
coordinates persisted workflow stages and applies the PO lock before saving.
`invoiceops/reviewer.py` re-runs checks and persists reviewer actions.
`invoiceops/models.py`, `db.py`, and `repository.py` define persistence,
initialization, and business-key/PO utilization queries. `invoiceops/ui_components.py`
contains shared Streamlit UI components. `scripts/` provides seed, manifest,
evaluation, and smoke-test entry points. SQLite is the local/unit-test backend;
PostgreSQL-specific tests exercise numeric, index, and row-lock behavior.

## Configuration and execution

Documented setup is `python -m venv .venv`, install `requirements-dev.txt`, copy
`.env.example` to `.env`, then run `pytest -q`. The offline manifest command is
`python -m scripts.run_manifest --provider replay --reset`. PostgreSQL verification
uses `PG_TEST_URI=postgresql://... pytest -q tests/test_pg_parity.py`; this suite
drops and recreates tables in the configured test database, so it must only point
to a disposable test database. Live Gemini evaluation uses
`python -m scripts.eval_extraction --provider gemini` and requires API credentials
and network access. Tesseract is an external binary needed for scanned-PDF tests.

Settings are read from `.env`/environment in `invoiceops/config.py`: `DATABASE_URL`,
`GEMINI_API_KEY`, `GEMINI_MODEL`, and optional `TESSERACT_CMD`. Policy thresholds
are in `PolicyConfig`: symmetric 2% unit-price tolerance, INR 1 arithmetic
tolerance, 75% OCR confidence, 85 description-match threshold, and seven-day
soft-duplicate window. The README and `.env.example` currently name
`gemini-3.8-flash`; the previous audit and the replay manifest tests identify
`gemini-3.5-flash-lite` as the fixture model. Replay identity includes the model,
but its cache records do not independently state the recording model. Therefore
the manifest test currently requires the environment to supply the matching model.

## Current baseline run

The documented `pytest -q` command could not start in this workspace, so test
collection and pass/fail totals are **unavailable**, not zero. The checked-in
`.venv\Scripts\python.exe` is a 274 KB launcher that reports it cannot start its
configured `C:\Users\aashn\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\python.exe`.
The `py` launcher points to `C:\Python313\python.exe`, which is absent, and the
PowerShell `python` command resolves to an inaccessible WindowsApps alias. Thus
pytest could not collect tests. The exact attempts were:

```powershell
$env:GEMINI_MODEL='gemini-3.5-flash-lite'; .\.venv\Scripts\python.exe -m pytest -q
$env:GEMINI_MODEL='gemini-3.5-flash-lite'; py -m pytest -q
```

Both failed before pytest startup. The first failed to create the configured
process; the second reported its Python executable missing. No actual collected,
passed, failed, skipped, or collection-error counts can be claimed from this
environment. The historical `docs/05_Audit_v1.md` records 204 passed/6 skipped,
five PostgreSQL parity tests, and 8/8 manifest cases on SQLite and PostgreSQL in
a different Python 3.12/Tesseract/PostgreSQL environment; these are historical
results, not this run's baseline. PostgreSQL is BLOCKED here because no runnable
test interpreter or configured disposable PostgreSQL test execution was available.

## Test environment prerequisites

- A working Python interpreter with dependencies from `requirements-dev.txt`.
- `GEMINI_MODEL=gemini-3.5-flash-lite` for current committed replay fixtures and
  `tests/test_manifest_e2e.py` (no API key/network needed for replay).
- `google-genai` (declared in `requirements.txt`) to import and collect the Gemini
  provider test in a complete environment; live calls additionally need
  `GEMINI_API_KEY` and network access.
- Tesseract on `PATH` or `TESSERACT_CMD` for OCR-dependent tests; those tests may
  skip when unavailable.
- `PG_TEST_URI` pointing only to a disposable PostgreSQL test database for
  `tests/test_pg_parity.py`; SQLite is not a substitute for its lock tests.

## Verified current state and earlier fixes

The current tree includes fixes described as outstanding by the older audit:

- Vendor identity conflict logic checks unknown tax IDs against known names and
  compares a tax-ID match with the extracted name (`matching.py`); regression
  tests cover both and split-vendor identities.
- `line_arithmetic` verifies quantity × unit price against each line total, and
  `currency_match` compares invoice and PO currencies (`rules.py`).
- The orchestrator catches unexpected errors, persists a failed run/stage, and
  recovers an active-business-key race (`orchestrator.py`).
- The PO lock/recheck path and the PostgreSQL concurrency tests exist.
- PO utilization summaries use cumulative totals; arithmetic/OCR messages are
  formatted; INR grounding uses currency markers; startup seeding preserves PO
  status (`seed.py`, `rules.py`, `grounding.py`, and regression tests).
- The checked-in audit's old test count and findings describe a prior snapshot;
  they must not be read as current test evidence.

## Remaining issues found by source and test inspection (before hardening)

1. `CheckStatus` has only PASSED/WARNING/FAILED/SKIPPED; dependent checks still
   report escalating warnings when PO matching or required data is unavailable.
   There is no explicit NOT_APPLICABLE outcome, so missing prerequisites create
   cascaded review noise.
2. Reviewer approval records a comment/action but not which failed checks were
   overridden or each check's original result. Historical `validation_checks`
   are stored per run, while review-time checks are not persisted separately.
   Overrides therefore are not fully auditable or distinguishable from a general
   approval comment.
3. REQUEST_INFORMATION marks the invoice inactive and persists an action/email,
   and corrected intake links via `supersedes_invoice_id`; however the effective
   decision remains MANUAL_REVIEW because the current `Decision` enum has no
   returned state. The active flag and action currently represent returned status.
4. Vendor email generation includes every WARNING/FAILED internal check message,
   including cascading diagnostic text (`reviewer.py::_vendor_email`). The strict
   regression for vendor-actionable content remains marked `xfail(strict=True)`.
5. PostgreSQL approval locks and rechecks the PO, but reviewer approval also needs
   verification for transaction/concurrency and consistent check/action persistence.
6. `tests/test_audit_regressions.py` retains a strict xfail expecting same-number
   corrected submission after reviewer REJECT. That expectation conflicts with
   this task's explicit policy: final REJECT retains active business identity;
   only REQUEST_INFORMATION deactivates and permits linked correction. The test
   should be corrected to assert duplicate rejection, not implemented as bypass.
7. `tests/test_manifest_e2e.py` requires `GEMINI_MODEL` from the environment even
   for offline replay. The cache key requires the historical recording model;
   `.env.example`'s 3.8 value does not hit the existing 3.5-lite fixtures.

## Prioritized Phase 1 work

1. Make reviewer overrides explicit and persist check-level audit data without
   overwriting original validation results; verify reject/request-information
   lifecycle and database reload behavior.
2. Add explicit non-executed/dependent-check semantics using the current result
   model where feasible, and remove misleading cascaded diagnostics.
3. Restrict generated vendor messages to clear, actionable vendor requests.
4. Make deterministic replay tests select the model associated with committed
   fixtures without depending on developer `.env`; clearly distinguish optional
   Gemini SDK/provider tests from live integration calls.
5. Add the requested regression coverage, run the complete suite, and run
   PostgreSQL parity against a disposable database when the runtime is available.

The baseline was captured before implementation changes. The test suite could
not be collected because the configured Python interpreter is unavailable.

## Changes made after the baseline capture

- Added an additive `review_actions.overridden_checks` JSON field and migration.
  Reviewer approval now snapshots the exact overridden checks and their original
  statuses/messages/details while leaving original run validation rows intact.
- Serialized reviewer actions with a PostgreSQL invoice-row lock. Rejection keeps
  an invoice active; request-information requires an explanation and deactivates
  it, preserving the existing corrected-submission link behavior.
- Dependent PO checks now use the existing SKIPPED result status and an explicit
  `details.outcome = NOT_APPLICABLE`; this avoids changing the persisted enum
  constraint while distinguishing a prerequisite skip from a pass or failure.
- Vendor emails now map selected root checks to plain-language vendor requests
  and exclude internal cascade messages and diagnostic values.
- Offline manifest tests use `REPLAY_FIXTURE_MODEL`; replay rejects a cache entry
  when its recorded extraction instructions do not match the request.
- Updated regression coverage for request explanations, persisted check-level
  overrides, vendor email boundaries, dependent checks, replay instruction
  mismatch, and duplicate prevention after final reviewer rejection.

At the time this baseline and implementation note were first written, the suite
could not run because the configured Python interpreter was unavailable. The
environment was subsequently restored and the complete offline verification was
run; see [`PHASE_1_FINAL_REPORT.md`](PHASE_1_FINAL_REPORT.md) for current counts
and PostgreSQL status.
