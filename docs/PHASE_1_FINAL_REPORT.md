# InvoiceOps Phase 1 final verification report

Date: 2026-11-11 (Asia/Kolkata)

## Environment restored

- Interpreter: CPython 3.13.13, provisioned under `uv`'s managed Python directory.
- Fresh virtual environment: `.venv-phase1` (the broken checked-in `.venv` was not used).
- Installed `requirements-dev.txt` through `uv`; `uv pip check` reports all 63
  installed packages compatible. `google-genai 2.34.0`, `pytest 8.4.2`, SQLAlchemy,
  Streamlit, and PyMuPDF import successfully.
- Tesseract is installed and callable: 5.5.3.20260724. The scanned replay fixture
  ran successfully in the manifest.
- `.gitignore` now ignores `.venv*/`, `.uv-cache/`, and `.temp-work/`.

The machine Python launchers initially referenced missing interpreters. The
managed Python executable was usable. A first `venv` bootstrap failed because
`ensurepip` could not complete in the sandbox; `uv venv` succeeded after its
temporary directory and cache were directed into writable workspace directories.
Dependency installation initially failed sandbox DNS access; it completed after
the approved network retry to PyPI.

## Test collection and execution

Latest collection/execution: **246 tests**. The post-correction offline run was
**239 passed, 7 skipped, 0 failed, 0 collection errors** (`58.18s`).
`GEMINI_API_KEY` and `PG_TEST_URI` were explicitly empty, confirming this run did
not need Gemini credentials and did not connect to PostgreSQL. No live Gemini
requests were made.

Exact final test command and environment:

```powershell
$env:DATABASE_URL = ''
$env:PG_TEST_URI = ''
$env:PYTEST_ADDOPTS = '--basetemp=.temp-work/pytest'
$env:GEMINI_API_KEY = ''
$env:GEMINI_MODEL = 'gemini-3.5-flash-lite'
.\.venv-phase1\Scripts\python.exe -m pytest -q
```

The `--basetemp` override is needed in this managed sandbox: pytest's default
temporary location resolves to a restricted AppData path. An initial run without
the override collected and executed tests but showed **158 passed, 6 skipped,
71 setup errors** because `tmp_path` could not create that external directory.
After directing temporary files into the workspace, the suite passed. The earlier
Phase 1 counts predate the PostgreSQL reviewer-concurrency and dashboard/configuration
regressions; the latest totals above supersede them.

Skip reasons from the final run:

- 1 OCR quality/tolerance test is intentionally evaluated separately.
- 6 PostgreSQL tests require `PG_TEST_URI` (five existing parity/concurrency
  tests plus the new concurrent reviewer-action test).

The execution summary reported **246 test items**. The seven skips are one
OCR-quality case and six PostgreSQL cases (the PostgreSQL run is recorded below).

## Replay manifest

Command executed with a fresh temporary SQLite database, committed fixture model,
and no Gemini API key:

```powershell
$env:DATABASE_URL = 'sqlite:///C:/Users/aashn/OneDrive/Desktop/Placement/zamp/main/.temp-work/phase1_manifest_offline.db'
$env:GEMINI_API_KEY = ''
$env:GEMINI_MODEL = 'gemini-3.5-flash-lite'
.\.venv-phase1\Scripts\python.exe -m scripts.run_manifest --provider replay --reset
```

Result: **11/11 PASS**, with **11/11 replay cache hits**. The manifest includes
the corrected-submission and adversarial impersonation, inflated-line-total, and
currency-mismatch fixtures. Existing replay fixtures were not modified.

## Phase 1 changes verified

- Reviewer override persistence: the reviewer regression verifies the overridden
  check names and original FAILED results survive a database reload while the
  original run validation rows remain FAILED.
- Reviewer identity, timestamp, comment, request-information explanation, and
  corrected-invoice relationship are covered by the passing reviewer tests.
- REJECT retains active business identity and the regression verifies a same-key
  submission is rejected as a duplicate. REQUEST_INFORMATION deactivates the
  original and permits a linked corrected submission.
- Dependent PO checks are skipped and carry `outcome: NOT_APPLICABLE`, rather than
  being reported as passed or as cascading warnings.
- Vendor email regression verifies vendor-actionable language and excludes
  internal cascade diagnostics.
- Replay rejects recorded responses when extraction instructions differ. The
  manifest confirms fixture-model alignment and cache hits.
- A PostgreSQL concurrency regression now covers two simultaneous terminal
  reviewer actions; it passed in the isolated PostgreSQL run below.

No additional business-logic defect was exposed by the passing SQLite/offline
suite. The initial 71 errors were environment path errors, not test assertion or
application failures.

## PostgreSQL verification

**PASS: 6 passed, 0 skipped, 0 failed** against the isolated Neon branch's
`invoiceops_test` database. The run completed in 58.00 seconds. This verifies the
PostgreSQL parity, PO approval locking, and reviewer action concurrency cases.
Fixture cleanup completed successfully.

### PostgreSQL harness audit

`tests/test_pg_parity.py` loads the local app settings (which reads `.env`) before
capturing `PG_TEST_URI`, then passes that URL explicitly to `create_db_engine`; it
never falls back to `DATABASE_URL`.
The engine normalizer rewrites Neon/PostgreSQL `postgresql://` or `postgres://`
URLs to SQLAlchemy's `postgresql+psycopg://` driver form. The declared dependency
is `psycopg[binary]` (psycopg v3).

The fixture runs `Base.metadata.drop_all(engine)` before initialization and again
in cleanup. These operations drop only tables known to this application's SQLAlchemy
metadata in the database named by `PG_TEST_URI`; they do not drop the database or
change `DATABASE_URL`. The first parity test also clears and reseeds test data.
Before these destructive operations, the fixture now refuses URLs that are not
PostgreSQL, do not name a database containing a standalone `test` token, or
resolve to the same host/database as the app's configured `DATABASE_URL` (including
Neon's direct/pooler hostname variant). It also disposes the engine in a `finally`
block.

Neon is compatible: the suite uses standard PostgreSQL DDL, JSON, partial unique
indexes, transactions, and `SELECT FOR UPDATE`. It requires no Neon-specific
dependency, extension, or special server setting. Use a **direct** Neon connection
for the threaded locking tests; SQLAlchemy will use psycopg v3 and TLS settings
from the copied URL.

The disposable branch and dedicated database were provisioned for this run. The
URI in `.env` now selects the direct endpoint and `invoiceops_test`; only
`PG_TEST_URI` was updated. `DATABASE_URL` was not changed. The initial pooled URI
selected `neondb`, so a one-off provisioner created `invoiceops_test` on the
separate Neon branch and then updated the test URI. The provisioner checked the
Neon host, verified it differed from the configured app endpoint, and checked
`CREATEDB` permission before creating the database.

```powershell
$env:PYTEST_ADDOPTS = '--tb=no'
.\.venv-phase1\Scripts\python.exe -m pytest -q tests/test_pg_parity.py
```

Result: **6 passed in 58.00s**. A first attempt without network escalation failed
DNS resolution before any database connection or destructive DDL; the approved
network-enabled rerun passed. `--tb=no` suppresses verbose database driver traces.

Credential hygiene: an earlier unsuppressed connection failure included
connection parameters in its traceback output. Rotate the Neon role password and
update only the local `PG_TEST_URI` entry in `.env`, then rerun the suite to
revalidate with the rotated credential. The password is not repeated here.

## Remaining failures and limitations

- No offline test failures remain in the final run.
- PostgreSQL parity and concurrency passed on Neon; rotate the Neon role password
  before relying on the current local URI because one initial verbose traceback
  exposed its connection parameters.
- No live Gemini provider/API integration was attempted. The provider module and
  test dependencies are installed, and deterministic provider tests are in the
  offline suite.
- One OCR quality test remains intentionally skipped; scanned replay and Tesseract
  processing did run in the manifest.

## Integration-readiness corrections

After the Phase 1 baseline, the dashboard was changed to count persisted invoices
by `invoices.current_decision` and show only the latest 15 workflow runs. Invoice
details display both the original automated `workflow_runs.decision` and effective
invoice decision. Returned status is shown from the inactive invoice state together
with its persisted REQUEST_INFORMATION action; the automated decision is not
rewritten.

Application startup still initializes missing schema, but automatic reference-data
seeding now follows `AUTO_SEED_REFERENCE_DATA` (default on in development, off when
`APP_ENV=production`). Production also defaults demo reset and reviewer actions off.
Reviewer actions can be enabled with `ENABLE_REVIEWER_ACTIONS=true` and protected by
`REVIEWER_ACCESS_KEY`. Reset is enabled in production only when
`ENABLE_DEMO_RESET=true`. The explicit seed command remains available; it upserts
synthetic reference data and should be used only when that data is intended.

`scripts/check_db_connection.py` now uses the shared psycopg-aware engine and reports
connection failures by exception type without printing connection details. Its
SQLite path was exercised against an isolated temporary database and found all
nine tables.

Post-change verification:

- Targeted dashboard/config tests: **24 passed** in 11.26 seconds.
- Complete offline suite: **239 passed, 7 skipped, 0 failed** (246 collected,
  58.18 seconds). The six PostgreSQL cases and one OCR-quality case were skipped
  in this offline run.
- PostgreSQL parity/concurrency suite: **6 passed, 0 skipped, 0 failed** against the
  separately configured disposable Neon `invoiceops_test` target. A first sandboxed
  attempt could not reach the database; the network-enabled rerun completed in
  66.49 seconds. The application `DATABASE_URL` target was checked as distinct
  before running. No application database tables were targeted by the test run.

Exact offline command and environment:

```powershell
$env:DATABASE_URL = ''
$env:PG_TEST_URI = ''
$env:GEMINI_API_KEY = ''
$env:GEMINI_MODEL = 'gemini-3.5-flash-lite'
$env:PYTEST_ADDOPTS = '--basetemp=.temp-work/pytest'
.\.venv-phase1\Scripts\python.exe -m pytest -q
```

Exact PostgreSQL test command and environment:

```powershell
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
$env:GEMINI_API_KEY = ''
$env:GEMINI_MODEL = 'gemini-3.5-flash-lite'
$env:PYTEST_ADDOPTS = '--tb=no --basetemp=.temp-work/pytest-pg'
.\.venv-phase1\Scripts\python.exe -m pytest -q tests/test_pg_parity.py
```

The Neon app database and PostgreSQL test database remain separate: the app reads
`DATABASE_URL`; only the PostgreSQL parity fixture reads `PG_TEST_URI` and drops its
own test tables.

## Modified files summary

- Environment hygiene: `.gitignore`.
- Verification documentation: `docs/PHASE_1_BASELINE.md` and this report.
- Reviewer lifecycle and auditability: `invoiceops/reviewer.py`,
  `invoiceops/models.py`, `invoiceops/db.py`.
- Dependent checks and replay consistency: `invoiceops/rules.py`,
  `invoiceops/config.py`, `invoiceops/providers/replay.py`, `README.md`.
- Regression tests: `tests/test_audit_regressions.py`, `tests/test_reviewer.py`,
  `tests/test_matching_rules.py`, `tests/test_manifest_e2e.py`,
  `tests/test_replay_provider.py`, and `tests/test_pg_parity.py`.
- Integration readiness: dashboard outcome/activity and lifecycle rendering,
  environment-controlled startup seeding/reset/reviewer access, safe DB diagnostics,
  README configuration notes, and `tests/test_dashboard.py`.
- Local ignored configuration: `.env` `PG_TEST_URI` now points to
  `invoiceops_test`; `DATABASE_URL` was left unchanged.
