import json

from invoiceops.config import Settings
from invoiceops.db import create_db_engine, init_db
from scripts.smoke_test import check_prerequisites


def test_smoke_checks_report_ready_local_setup(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'smoke.db'}")
    init_db(engine)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "entry.json").write_text(json.dumps({"response": {}}), encoding="utf-8")
    monkeypatch.setattr("scripts.smoke_test.shutil.which", lambda command: "tesseract.exe")
    settings = Settings(
        database_url="sqlite://",
        gemini_api_key="test-key",
        gemini_model="test-model",
        tesseract_cmd=None,
    )
    try:
        results = check_prerequisites(settings, engine, cache_dir)
    finally:
        engine.dispose()

    assert all(ok for _, ok, _ in results)


def test_smoke_checks_report_missing_key_and_cache(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'smoke.db'}")
    init_db(engine)
    monkeypatch.setattr("scripts.smoke_test.shutil.which", lambda command: "tesseract.exe")
    settings = Settings(
        database_url="sqlite://",
        gemini_api_key=None,
        gemini_model=None,
        tesseract_cmd=None,
    )
    try:
        results = check_prerequisites(settings, engine, tmp_path / "missing-cache")
    finally:
        engine.dispose()

    failures = {name: message for name, ok, message in results if not ok}
    assert "Gemini key" in failures
    assert "Replay cache" in failures
