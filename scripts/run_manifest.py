"""Run the ordered invoice fixture manifest against Gemini or a replay cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from invoiceops.config import PROJECT_ROOT, get_settings
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


def run_manifest(
    provider: Any,
    session_factory: sessionmaker[Session],
    *,
    manifest_path: str | Path = MANIFEST_PATH,
    samples_dir: str | Path = SAMPLES,
    reset: bool = False,
) -> dict[str, Any]:
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

        if case.get("post_action"):
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            with session_factory() as session:
                run = get_latest_run_by_hash(session, digest)
                invoice_id = run.invoice_id if run else None
                if invoice_id is None:
                    raise RuntimeError(f"No persisted invoice is available for {case['file']} post-action.")
                action = case["post_action"]
                apply_review_action(
                    session,
                    invoice_id,
                    ReviewActionType(action["action"]),
                    reviewer=action["reviewer"],
                    comment=action.get("comment"),
                )
                session.commit()

        cache_hit = bool(getattr(provider, "last_call_was_cache_hit", False))
        passed = (
            decision.decision.value == case["expected_decision"]
            and not missing_checks
        )
        rows.append({
            "fixture": filename,
            "expected_decision": case["expected_decision"],
            "actual_decision": decision.decision.value,
            "expected_checks": sorted(required_checks),
            "present_checks": sorted(checks_present),
            "missing_checks": missing_checks,
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

    engine = create_db_engine(args.database_url or settings.database_url)
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed_reference_data(session, PROJECT_ROOT / "data")
        session.commit()
    try:
        report = run_manifest(provider, factory, reset=args.reset)
        print_report(report)
        return 0 if report["passed"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
