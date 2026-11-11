# InvoiceOps — Final Deployment Readiness Report

## Changes made

- Manifest execution is unavailable from the Streamlit UI in production. In development, it requires `PG_TEST_URI` and validates that it is a separate test-named database rather than the configured application database.
- The manifest CLI requires `PG_TEST_URI` or an explicit `--database-url`; it never falls back to `DATABASE_URL`. Invalid reviewer post-actions are reported against their fixture and fail that manifest case without bypassing reviewer validation.
- Added `scripts/verify_docker.ps1`. It builds the existing Docker image, starts a uniquely named temporary container on a dynamically assigned localhost port, checks Streamlit health/root, PostgreSQL/psycopg and all nine tables, production safety flags, running state, and startup logs. It removes only the temporary container it created. It does not seed or run the manifest. The built verification image is retained.
- Updated README deployment and manifest instructions, including Render's supplied `PORT`, production environment settings, and the separate test database rule.
- Added `.env.docker` and `/final.zip` to `.gitignore`, and restored `.env.docker` exclusion from `.dockerignore`.

## Verification results

Commands below were run from the repository root. `.env.docker` values and database credentials were not printed.

| Check | Command | Result |
|---|---|---|
| New manifest safety and reviewer regressions | `$env:DATABASE_URL=''; $env:PG_TEST_URI=''; $env:APP_ENV='development'; $env:GEMINI_API_KEY=''; $env:GEMINI_MODEL='gemini-3.5-flash-lite'; $env:PYTEST_ADDOPTS='--basetemp=.temp-work/pytest-manifest'; .\.venv-phase1\Scripts\python.exe -m pytest -q tests/test_manifest_safety.py tests/test_reviewer.py` | **10 passed** |
| Dockerized Streamlit verification | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify_docker.ps1 -EnvironmentFile .env.docker` | **PASS**: build; health and root HTTP 200; PostgreSQL/psycopg; all nine tables; production safety settings; container running; startup logs clean. Temporary container removed. |
| Complete offline test suite | `$env:PG_TEST_URI=''; $env:PYTEST_ADDOPTS='--basetemp=.temp-work/pytest-final'; .\.venv-phase1\Scripts\python.exe -m pytest -q` | **267 collected: 260 passed, 7 skipped**, 0 failures, in 108.91 seconds. Six PostgreSQL tests skipped because `PG_TEST_URI` was unset; one OCR/fixture-dependent test was skipped. |
| Existing application container | `docker ps --filter name=^invoiceops$ --format "{{.Names}} {{.Status}}"` | `invoiceops` remained running; the verification script did not restart or remove it. |

PostgreSQL parity tests were not rerun. The earlier verified 6/6 result against isolated Neon `invoiceops_test` remains the recorded baseline; the Docker verification queried the configured application database read-only to check connectivity and table availability.

## Remaining blockers and limitations

- No code/test blocker found in the requested checks. The seven skips are documented above; PostgreSQL parity was intentionally not repeated, and host-side OCR-dependent coverage remains conditional on the host OCR setup.
- Render deployment itself has not been performed. The image build was verified; deployment requires the Render service and environment values below.
- `scripts/verify_docker.ps1` retains its uniquely tagged verification image. Its temporary container is removed. It intentionally does not inspect, stop, or delete other containers.

## Render environment

Configure these values in the Render service's environment settings; do not commit secrets:

```text
APP_ENV=production
DATABASE_URL=<Neon application database URL>
GEMINI_API_KEY=<Gemini API key>
GEMINI_MODEL=<supported Gemini model>
AUTO_SEED_REFERENCE_DATA=false
ENABLE_DEMO_RESET=false
ENABLE_REVIEWER_ACTIONS=false
```

Render supplies `PORT`; leave it to the platform. Do not set `PG_TEST_URI` on the application service. If reviewer actions are intentionally enabled, set `ENABLE_REVIEWER_ACTIONS=true` and configure a private `REVIEWER_ACCESS_KEY`. Keep demo reset disabled for retained application data.

## Deployment procedure (not executed)

1. In Render, create a Docker web service for this repository and use the checked-in `Dockerfile`.
2. Add the environment variables above. Use the Neon application database URL as `DATABASE_URL`; never use `PG_TEST_URI` there.
3. Leave Render's `PORT` setting to Render. The Docker command binds Streamlit to `0.0.0.0:$PORT`.
4. Deploy only after approval. Then open the Render URL and verify the Streamlit health route and application pages. Do not run the manifest, reset, seed, or PostgreSQL destructive tests against the retained Neon application database.
