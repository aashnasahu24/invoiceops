# InvoiceOps — invoice validation and review workflow (Stage 4)

AI-assisted invoice processing and PO reconciliation for the Zamp AI Solutions
Analyst case study (PS-1). Principle: **AI understands documents; deterministic
Python enforces policy.**

Stage 1 delivered schemas, database, reference data, labelled fixtures, PDF/OCR
extraction, and a verified extraction boundary. Stage 2 adds Gemini extraction,
retry/replay support, and fixture evaluation. Stage 3 adds persisted workflow stages
and a Streamlit process/dashboard slice. Stage 4 adds deterministic PO-line and
split-invoice checks, reviewer actions, corrected resubmissions, stable replay, and
an ordered manifest runner.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
cp .env.example .env                                  # required for Gemini/OCR settings
python -m scripts.seed_data                           # create tables + seed demo vendors/POs
pytest -q
```

Run the app locally after seeding the database:

```bash
streamlit run app.py
```

OCR needs the **Tesseract binary** (`sudo apt-get install tesseract-ocr`, or the
Windows installer). If it is not on PATH set `TESSERACT_CMD` in `.env`.
Without it, scanned PDFs raise a clear `OCRUnavailableError`; they are never
silently skipped.

## Layout

| Path | Purpose |
|---|---|
| `invoiceops/config.py` | Env settings + versioned business-policy constants (`PolicyConfig`) |
| `invoiceops/schemas.py` | Domain schemas, enums, and the strict LLM-facing output schema |
| `invoiceops/models.py` | SQLAlchemy models (9 tables incl. `run_stages`) |
| `invoiceops/db.py`, `seed.py` | Engine/session helpers; idempotent seed; `reset_demo_state` |
| `invoiceops/repository.py` | Run, stage, extraction artifact, invoice, review, and PO-balance persistence |
| `invoiceops/pdf_processor.py` | PyMuPDF text (row-preserving) + Tesseract OCR fallback with confidence |
| `invoiceops/grounding.py` | Checks extracted values/evidence against the real source text |
| `invoiceops/extraction_service.py` | Provider-neutral LLM boundary: validate → convert → verify → attach metadata |
| `invoiceops/providers/` | Gemini adapter, bounded retries, and recording/replay providers |
| `invoiceops/normalization.py` | Invoice-number / tax-ID / company-name normalisation |
| `invoiceops/matching.py`, `rules.py` | Pure exact/fuzzy line matching and deterministic validation/decision logic |
| `invoiceops/orchestrator.py` | Persisted generator pipeline from PDF bytes to decision |
| `invoiceops/reviewer.py` | Reviewer queue data and deterministic approve/reject/request-information actions |
| `app.py`, `invoiceops/ui_components.py` | Thin Streamlit process, dashboard, review queue, and manifest pages |
| `data/` | Vendors, POs, fixture PDFs + hashes, `test_manifest.json`, extraction labels, replay cache |
| `assets/fonts/` | Licensed DejaVu Sans fonts used to generate the scanned test fixture |
| `scripts/` | Evaluation, Gemini schema smoke, seed/reset, fixture generation, deploy smoke test |

## Key design decisions

- **LLM output is untrusted.** The model returns a strict, string-valued schema
  (`InvoiceExtractionLLM`) with evidence snippets. Code converts it, then checks every
  value and snippet against the document text. Values not found land in
  `extraction.ungrounded_fields` (a review signal). LLM self-reported confidence is
  not used.
- **The LLM only extracts.** Gemini receives the document as input data and the
  extraction instructions as the system instruction. Grounding and validation stay
  deterministic; Gemini never matches vendors/POs or makes invoice decisions.
- **Attempts vs identity.** `workflow_runs` = every attempt; `invoices` = identity.
  `(vendor_id, invoice_number_normalized)` is unique **among active invoices only**
  (partial index), so a corrected resubmission of a returned invoice is allowed while
  a true duplicate is still blocked. Identical file bytes (`file_sha256`) are always blocked.
- **Only approved invoices consume PO balance;** `invoices.net_amount` (subtotal +
  shipping − discount) is stored for that. `invoices.current_decision` is the effective
  decision (automated or reviewer override); the automated decision stays on the run.
- **Live run view is backed by `run_stages`** (one row per stage per run), plus the
  extracted text and raw LLM response on the run for audit.
- **Policy lives in `PolicyConfig`** (2% price tolerance, INR 1 rounding, 75% OCR
  confidence) and is versioned (`v1`). Prototype demo policy, not accounting advice.

## Running tests

```bash
pytest -q                                   # unit tests (SQLite); OCR tests skip without Tesseract
```

SQLite ignores row locks and stores NUMERIC as float, so DB-sensitive behaviour is
also checked against real PostgreSQL:

```bash
export PG_TEST_URI=postgresql://user:pass@localhost:5432/invoiceops_test   # a scratch DB!
pytest -q tests/test_pg_parity.py           # drops & recreates all tables in that DB
```

(`pip install pgserver` gives a throw-away embedded Postgres for this if you have none.)

## Stage 4 rules and demo workflow

The policy defaults remain explicit in `PolicyConfig`: 2% unit-price variance,
INR 1 arithmetic tolerance, 75% minimum OCR confidence, 85 RapidFuzz description
match, and a 7-day soft-duplicate window. Matched PO-line quantities and approved
net amounts are checked again under a PostgreSQL `SELECT FOR UPDATE` lock before
an invoice is saved. Low OCR confidence always sends a scan to review.

The Review queue supports approve (with a comment for check overrides), reject,
and request information. Returned invoices become inactive, and a corrected
resubmission links to the returned invoice. Dashboard outcome counts use effective
invoice decisions; run details retain the original automated decision and show the
review outcome and return status separately. Recent activity is limited to 15 runs.
CSV export, charts, reviewer assignment, and multi-user permissions are not
implemented. See [`docs/stage4_notes.md`](docs/stage4_notes.md) for policy limits.

Run the ordered manifest offline using the committed replay cache:

```bash
python -m scripts.run_manifest --provider replay --reset
```

`--reset` clears prior invoice history before the run; use it only with disposable
or local demo data. Production disables reset unless `ENABLE_DEMO_RESET=true`.
`--provider gemini` runs live and records responses.
The manifest performs the scanned-invoice REQUEST_INFORMATION action before the
corrected resubmission fixture. Do not regenerate the existing PDFs: the test
`tests/test_fixture_hashes.py` verifies their committed SHA-256 values. To generate
only the new resubmission PDF when intentionally updating fixtures, use
`python -m scripts.generate_sample_invoices --fixture resubmitted_clean.pdf`.

The replay cache identity is now `SHA256(file_sha256 + schema_hash + model)`;
the extracted-text hash is retained only for debugging. After changing the model
or extraction schema, bypass cached hits to record fresh Gemini responses:

```bash
python -m scripts.eval_extraction --provider gemini --cache-dir data/llm_cache_demo --refresh-cache
python -m scripts.eval_extraction --provider replay --cache-dir data/llm_cache_demo
```

`--refresh-cache` is also available on `scripts.run_manifest` when using Gemini.

## Demo data

Fixtures must run in order because results depend on persisted state:
`happy_path` → `duplicate`; `split_invoice_1` → `split_invoice_2`.
Between rehearsal and the real run: `python -m scripts.seed_data --reset`.
This reset deletes invoice/workflow history; production blocks it unless
`ENABLE_DEMO_RESET=true` is explicitly configured.

Regenerate all fixtures only when intentionally changing the fixture set:
`python -m scripts.generate_sample_invoices`.
All names, tax IDs, invoices and POs are fictional.

## LLM provider (Gemini)

The runtime SDK is Google's official [`google-genai`](https://github.com/googleapis/python-genai)
package. The adapter uses the SDK's `client.models.generate_content(...)` API with
JSON MIME output, the `InvoiceExtractionLLM` schema, temperature 0, and a 30-second
request timeout. The model name is supplied only through configuration; it is never
hardcoded in the application. See Google's [structured-output documentation](https://ai.google.dev/gemini-api/docs/structured-output).

1. Copy `.env.example` to `.env` and set `GEMINI_API_KEY` and `GEMINI_MODEL` to a
   Gemini Developer API key and a model available to your account with structured
   output support. Keep the key private; `.env` is ignored by git.
2. Install runtime dependencies with `python -m pip install -r requirements-dev.txt`.
3. Make one schema-acceptance call against a digital fixture:

   ```bash
   python -m scripts.smoke_gemini_schema
   ```

   This call verifies that Gemini accepts the current adapted Pydantic schema. If
   Gemini rejects schema constructs, the script reports the API error; see
   [`docs/stage2_notes.md`](docs/stage2_notes.md) for the current smoke status and
   schema adaptation record.
4. Evaluate all twelve labelled PDFs (eleven digital, one scanned), writing raw
   responses to the demo cache:

   ```bash
   python -m scripts.eval_extraction --provider gemini --cache-dir data/llm_cache_demo
   ```

   The command prints per-field correctness, line-item accuracy, grounding signals,
   OCR confidence, request latency, cache status, and aggregate accuracy. Any wrong
   `invoice_number`, `vendor_name`, or `grand_total` on a digital PDF exits with code
   1. Comparison is strict by default; `--lenient-names` opts into company-name
   normalization.

   Google currently lists `gemini-3.8-flash` as stable, structured-output capable,
   and available on the free tier. A temporary `503 UNAVAILABLE`/high-demand or
   `504 DEADLINE_EXCEEDED` response is a capacity issue, not an invalid model name;
   the evaluator retries these transient errors. Avoid `gemini-2.5-flash`, which is
   now deprecated. `gemini-3.5-flash-lite` is a stable alternative optimized for
   document parsing if you choose to switch `GEMINI_MODEL`, then rerun the labelled
   evaluation.

   Useful options:

   ```bash
   python -m scripts.eval_extraction --provider gemini --fixture happy_path_invoice --json-out eval.json
   python -m scripts.eval_extraction --provider replay --cache-dir data/llm_cache_demo
   ```

   The offline manifest tests use the committed fixture model
   `REPLAY_FIXTURE_MODEL` (`gemini-3.5-flash-lite`) and do not require `.env`, an
   API key, or network access. The replay CLI still requires its configured model
   to match the recording model. Local
   `data/llm_cache_*/` directories are ignored except `data/llm_cache_demo/`, which
   is intentionally eligible for committing. The replay provider never makes a
   network call and errors explicitly on a cache miss. Free-tier throttling is
   handled with at most three provider attempts and exponential backoff with jitter.

The scanned fixture should display an explicit low-OCR-confidence warning when the
processor reports confidence below the existing 75% threshold. Do not weaken the
strict field comparison to make fixture scores pass.

## Stage 3 workflow

The Process invoice page consumes the persisted generator and shows the seven stages
as they run. The decision engine uses only deterministic checks; low OCR confidence,
ungrounded values, missing identifiers, unmatched vendors/POs, and arithmetic or
balance failures route to review. A blocked vendor and duplicates are rejected.
Gemini is used only through the provider-neutral `StructuredOutputProvider` boundary.

The Dashboard shows aggregate effective invoice outcomes and the latest 15 workflow
runs. Select a row to inspect automated and effective decisions, reviewer action and
invoice lifecycle, extracted values, evidence-verification markers, and checks. The
Review queue supports approve, reject, and request-information actions. Local
development enables demo reset and reviewer actions by default; production defaults
to read-only reviewer actions and disables reset.

```bash
streamlit run app.py
```

Implemented Stage 4 rules include PO-line matching and quantity/price checks,
soft-duplicate signals, serialized reviewer actions, returned-invoice resubmission,
and cumulative split-invoice PO balance handling. See
[`docs/stage4_notes.md`](docs/stage4_notes.md) for known policy limitations.

## Deploy

### Application and test databases

The application reads `DATABASE_URL` and initializes the schema on startup. In
local development, reference-data seeding is enabled by default. In production,
`AUTO_SEED_REFERENCE_DATA` defaults to `false`; seed intentionally with
`python -m scripts.seed_data` after reviewing the target database. Seeding upserts
the bundled vendors and POs, updates matching reference fields and PO lines,
removes seeded PO lines no longer present in the fixture data, and recalculates PO
statuses. It does not clear invoice/workflow history. Never use `--reset` on
retained data.

The app uses `DATABASE_URL`. PostgreSQL tests use only `PG_TEST_URI` and their fixture
drops and recreates application tables in that database. Configure `PG_TEST_URI` to
a separate disposable database (for example `invoiceops_test`) and never point it
at `DATABASE_URL`.

For production, set `APP_ENV=production`. Demo reset and reviewer actions then
default off. To enable a protected reviewer session, set
`ENABLE_REVIEWER_ACTIONS=true` and provide `REVIEWER_ACCESS_KEY`; the Review queue
prompts for that key. To enable destructive demo reset, explicitly set
`ENABLE_DEMO_RESET=true`. Keep these values and credentials in deployment secrets.
Local development defaults to automatic demo seeding, reviewer actions, and reset.

The app reads `DATABASE_URL`, `GEMINI_API_KEY`, and `GEMINI_MODEL` from the process
environment (or `.env` locally). For Neon, create a database and paste its
SSL-enabled connection URL into `DATABASE_URL`; PostgreSQL URLs are normalized to
the installed psycopg v3 driver. Keep secrets in the hosting provider's secret/env
settings, never in the image or repository. Tesseract is installed in the Docker
image; set `TESSERACT_CMD` only when using a nonstandard executable path.

Build and run the same image locally:

```bash
docker build -t invoiceops .
docker run --rm --env-file .env -e PORT=8501 -p 8501:8501 invoiceops
```

The container uses a non-root account and binds Streamlit to `0.0.0.0:$PORT`.
Configure the deployment platform to pass its assigned `PORT` and the Neon/Gemini
environment variables. Run the preflight before deploying:

```bash
python -m scripts.smoke_test
```

The smoke check verifies database connectivity and tables, Tesseract, the Gemini key,
and readable replay-cache entries; it does not make a Gemini request. Free hosting
may suspend an idle service. Before a demo, open the deployed URL, wait for the app
to finish waking and display the Dashboard, then process a replay-cache fixture
before choosing Gemini for a live call.

## Schema changes

`create_all()` only creates missing tables. After deploying, schema changes need
Alembic migrations (or drop/recreate for this throw-away demo DB).
