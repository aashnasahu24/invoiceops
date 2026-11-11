"""Run the ordered invoice fixture manifest against Gemini or a replay cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from invoiceops.config import PROJECT_ROOT, get_settings, normalize_database_url
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.orchestrator import process_invoice
from invoiceops.providers.gemini_provider import GeminiProvider
from invoiceops.providers.replay import RecordingProvider, ReplayProvider
from invoiceops.repository import get_latest_run_by_hash
from invoiceops.reviewer import apply_review_action
from invoiceops.schemas import ReviewActionType
from invoiceops.seed import reset_demo_state, seed_reference_data

MANIFEST_PATH = PROJECT_ROOT / "data" / "test_manifest.json"
SAMPLES = PROJECT_ROOT / "data" / "sample_invoices"
CACHE_DIR = PROJECT_ROOT / "data" / "llm_cache_demo"
_TEST_DATABASE_NAME = re.compile(r"(?:^|[_-])test(?:$|[_-])", re.IGNORECASE)


def validate_manifest_database_url(database_url: str, app_database_url: str) -> str:
    """Allow manifests only on a clearly separate disposable test database."""
    try:
        target = make_url(normalize_database_url(database_url))
        app_target = make_url(normalize_database_url(app_database_url))
    except Exception as exc:
        raise ValueError("Manifest database URL is invalid.") from exc

    if _same_database_target(target, app_target):
        raise ValueError("Manifest database must be separate from DATABASE_URL.")

    if target.get_backend_name() == "postgresql":
        if not target.database or not _TEST_DATABASE_NAME.search(target.database):
            raise ValueError(
                "Manifest PostgreSQL database must have a test-named database, such as invoiceops_test."
            )
    elif target.get_backend_name() == "sqlite":
        path = target.database or ":memory:"
        if path != ":memory:":
            tokens = re.split(r"[\\/_.:-]+", path.lower())
            if not any(token in {"test", "tests", "tmp", "temp", "pytest"} for token in tokens):
                raise ValueError("Manifest SQLite database must be an in-memory or test/temp database.")
    else:
        raise ValueError("Manifest execution supports only a disposable PostgreSQL or SQLite test database.")

    return normalize_database_url(database_url)


def _same_database_target(target, app_target) -> bool:
    if target.get_backend_name() != app_target.get_backend_name():
        return False
    if target.get_backend_name() == "sqlite":
        target_path = target.database or ":memory:"
        app_path = app_target.database or ":memory:"
        if target_path == ":memory:" or app_path == ":memory:":
            return target_path == app_path
        return os.path.normcase(os.path.abspath(target_path)) == os.path.normcase(
            os.path.abspath(app_path)
        )
    target_host = (target.host or "").lower().replace("-pooler.", ".")
    app_host = (app_target.host or "").lower().replace("-pooler.", ".")
    return (
        target_host == app_host
        and (target.port or 5432) == (app_target.port or 5432)
        and target.database == app_target.database
    )


def _validate_manifest_session_factory(session_factory: sessionmaker[Session]) -> None:
    bind = session_factory.kw.get("bind")
    if not isinstance(bind, Engine):
        raise ValueError("Manifest execution requires a directly bound disposable test database.")
    validate_manifest_database_url(str(bind.url), get_settings().database_url)


def _apply_manifest_post_action(
    session_factory: sessionmaker[Session],
    digest: str,
    file_name: str,
    action_data: dict[str, Any],
) -> str | None:
    with session_factory() as session:
        run = get_latest_run_by_hash(session, digest)
        invoice_id = run.invoice_id if run else None
        if invoice_id is None:
            return f"No persisted invoice is available for {file_name} post-action."
        try:
            action = ReviewActionType(action_data["action"])
            apply_review_action(
                session,
                invoice_id,
                action,
                reviewer=action_data["reviewer"],
                comment=action_data.get("comment"),
            )
            session.commit()
        except (KeyError, TypeError, ValueError) as exc:
            session.rollback()
            return f"Reviewer post-action rejected for {file_name}: {exc}"
    return None


def run_manifest(
    provider: Any,
    session_factory: sessionmaker[Session],
    *,
    manifest_path: str | Path = MANIFEST_PATH,
    samples_dir: str | Path = SAMPLES,
    reset: bool = False,
) -> dict[str, Any]:
    _validate_manifest_session_factory(session_factory)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if reset:
        with session_factory() as session:
            reset_demo_state(session, PROJECT_ROOT / "data")
            session.commit()

    rows = []
    for case in manifest["cases"]:
        filename = case["file"]
        pdf_bytes = (Path(samples_dir) / filename).read_bytes()
        started = time.perf_counter()
        yielded = list(process_invoice(session_factory, provider, pdf_bytes, filename))
        elapsed = time.perf_counter() - started
        decision = yielded[-1]
        checks_present = {check.name for check in decision.checks}
        required_checks = set(case["must_include_checks"])
        missing_checks = sorted(required_checks - checks_present)

        post_action_error = None
        if case.get("post_action"):
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            post_action_error = _apply_manifest_post_action(
                session_factory,
                digest,
                filename,
                case["post_action"],
            )

        cache_hit = bool(getattr(provider, "last_call_was_cache_hit", False))
        passed = (
            decision.decision.value == case["expected_decision"]
            and not missing_checks
            and post_action_error is None
        )
        rows.append({
            "fixture": filename,
            "expected_decision": case["expected_decision"],
            "actual_decision": decision.decision.value,
            "expected_checks": sorted(required_checks),
            "present_checks": sorted(checks_present),
            "missing_checks": missing_checks,
            "post_action_error": post_action_error,
            "pass": passed,
            "duration_seconds": elapsed,
            "cache_hit": cache_hit,
            "summary": decision.summary,
        })
    return {
        "rows": rows,
        "passed": all(row["pass"] for row in rows),
        "total_seconds": sum(row["duration_seconds"] for row in rows),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"{'Fixture':30} {'Expected':16} {'Actual':16} {'Result':8} {'Time':9} {'Cache':6}")
    print("-" * 94)
    for row in report["rows"]:
        print(
            f"{row['fixture'][:30]:30} {row['expected_decision']:16} "
            f"{row['actual_decision']:16} {'PASS' if row['pass'] else 'FAIL':8} "
            f"{row['duration_seconds']:.2f}s{'':3} "
            f"{'hit' if row['cache_hit'] else 'miss':6}"
        )
        print("  Required checks: " + ", ".join(row["expected_checks"]))
        print("  Present checks:  " + ", ".join(row["present_checks"]))
        if row["missing_checks"]:
            print("  Missing checks: " + ", ".join(row["missing_checks"]))
        if row.get("post_action_error"):
            print(f"  Reviewer post-action error: {row['post_action_error']}")
        print(f"  Decision summary: {row['summary']}")
    print("-" * 94)
    print(
        f"{sum(row['pass'] for row in report['rows'])}/{len(report['rows'])} PASS | "
        f"total {report['total_seconds']:.2f}s"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gemini", "replay"), default="replay")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--database-url")
    parser.add_argument("--reset", action="store_true", help="Reset invoice history before the run.")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force Gemini requests and overwrite existing cache entries.",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.reset and settings.is_production and not settings.enable_demo_reset:
        parser.error("--reset is disabled in production; set ENABLE_DEMO_RESET=true to allow it.")
    database_url = args.database_url or os.getenv("PG_TEST_URI")
    if not database_url:
        print("Manifest execution requires PG_TEST_URI or an explicit --database-url for a disposable test database.", file=sys.stderr)
        return 2
    try:
        database_url = validate_manifest_database_url(database_url, settings.database_url)
    except ValueError as exc:
        print(f"Unsafe manifest database target: {exc}", file=sys.stderr)
        return 2
    if not settings.gemini_model:
        print("GEMINI_MODEL must be set in .env or the environment.", file=sys.stderr)
        return 2
    if args.provider == "replay":
        if args.refresh_cache:
            parser.error("--refresh-cache requires --provider gemini")
        provider = ReplayProvider(args.cache_dir, model=settings.gemini_model)
    else:
        if not settings.gemini_api_key:
            print("GEMINI_API_KEY must be set in .env or the environment.", file=sys.stderr)
            return 2
        provider = RecordingProvider(
            GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model),
            args.cache_dir,
            model=settings.gemini_model,
            force_refresh=args.refresh_cache,
        )

    engine = create_db_engine(database_url)
    try:
        init_db(engine)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            seed_reference_data(session, PROJECT_ROOT / "data")
        report = run_manifest(provider, factory, reset=args.reset)
        print_report(report)
        return 0 if report["passed"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
