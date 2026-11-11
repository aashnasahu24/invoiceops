"""Check deployment prerequisites without calling Gemini or another network API."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from invoiceops.config import PROJECT_ROOT, Settings, get_settings
from invoiceops.db import create_db_engine
from invoiceops.models import Base


def _check_database(engine: Engine) -> tuple[bool, str]:
    try:
        with engine.connect():
            available = set(inspect(engine).get_table_names())
        missing = sorted(set(Base.metadata.tables) - available)
    except SQLAlchemyError as exc:
        return False, f"database connection failed ({type(exc).__name__})"
    if missing:
        return False, f"database is missing tables: {', '.join(missing)}"
    return True, "database connected and all tables exist"


def _check_tesseract(command: str | None) -> tuple[bool, str]:
    resolved = command or "tesseract"
    if (Path(resolved).is_file() and os.access(resolved, os.X_OK)) or shutil.which(resolved):
        return True, f"Tesseract found ({resolved})"
    return False, f"Tesseract executable not found ({resolved}); install tesseract-ocr or set TESSERACT_CMD"


def _check_cache(cache_dir: Path) -> tuple[bool, str]:
    if not cache_dir.is_dir():
        return False, f"replay cache directory does not exist: {cache_dir}"
    files = sorted(cache_dir.glob("*.json"))
    if not files:
        return False, f"replay cache contains no JSON entries: {cache_dir}"
    for path in files:
        try:
            content: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"replay cache entry is unreadable ({path.name}): {exc}"
        if not isinstance(content, dict):
            return False, f"replay cache entry is not a JSON object: {path.name}"
    return True, f"replay cache readable ({len(files)} JSON entrie(s))"


def check_prerequisites(settings: Settings, engine: Engine, cache_dir: Path) -> list[tuple[str, bool, str]]:
    db_ok, db_message = _check_database(engine)
    tesseract_ok, tesseract_message = _check_tesseract(settings.tesseract_cmd)
    key_ok = bool(settings.gemini_api_key)
    key_message = "GEMINI_API_KEY is set" if key_ok else "GEMINI_API_KEY is missing"
    cache_ok, cache_message = _check_cache(cache_dir)
    return [
        ("Database", db_ok, db_message),
        ("Tesseract", tesseract_ok, tesseract_message),
        ("Gemini key", key_ok, key_message),
        ("Replay cache", cache_ok, cache_message),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "llm_cache_demo",
        help="Path to the replay JSON cache",
    )
    args = parser.parse_args()
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        checks = check_prerequisites(settings, engine, args.cache_dir)
    finally:
        engine.dispose()

    failed = False
    for name, ok, message in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {message}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
