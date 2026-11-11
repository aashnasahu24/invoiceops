"""Central configuration: environment settings and v1 business-policy constants.

Two separate concerns live here on purpose:
- ``Settings``: deployment/environment values (DB URL, API keys, binaries).
- ``PolicyConfig``: business-rule thresholds. These are prototype demo policy,
  versioned so every stored decision can say which rules produced it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_url: str
    gemini_api_key: str | None
    gemini_model: str | None
    tesseract_cmd: str | None
    app_env: str = "development"
    auto_seed_reference_data: bool = True
    enable_demo_reset: bool = True
    enable_reviewer_actions: bool = True
    reviewer_access_key: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env in {"prod", "production"}


@dataclass(frozen=True)
class PolicyConfig:
    """Prototype business policy (v1). Not universal accounting policy."""

    version: str = "v1"
    # Max relative difference between invoice and PO unit price (2%).
    price_tolerance_pct: Decimal = Decimal("0.02")
    # Max absolute rounding difference for totals/lines (INR 1 in the fixtures).
    rounding_tolerance: Decimal = Decimal("1.00")
    # OCR pages below this mean word confidence (0-100) become a review signal.
    min_ocr_confidence: float = 75.0
    line_match_threshold: int = 85
    vendor_name_threshold: int = 70
    soft_duplicate_days: int = 7


def default_sqlite_url() -> str:
    """Absolute path so the DB location never depends on the working directory."""
    return f"sqlite:///{(PROJECT_ROOT / 'invoiceops.db').as_posix()}"


def normalize_database_url(url: str) -> str:
    """Make provider-style URLs (Neon, Render, Heroku) use the psycopg v3 driver.

    ``postgres://`` and ``postgresql://`` would otherwise make SQLAlchemy look
    for psycopg2, which is not installed.
    """
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be set to true or false")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    # `or` (not a getenv default) so an empty `DATABASE_URL=` also falls back.
    database_url = os.getenv("DATABASE_URL") or default_sqlite_url()
    app_env = (os.getenv("APP_ENV") or "development").strip().lower()
    production = app_env in {"prod", "production"}
    return Settings(
        database_url=normalize_database_url(database_url),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL") or None,
        tesseract_cmd=os.getenv("TESSERACT_CMD") or None,
        app_env=app_env,
        auto_seed_reference_data=_env_bool("AUTO_SEED_REFERENCE_DATA", not production),
        enable_demo_reset=_env_bool("ENABLE_DEMO_RESET", not production),
        enable_reviewer_actions=_env_bool("ENABLE_REVIEWER_ACTIONS", not production),
        reviewer_access_key=os.getenv("REVIEWER_ACCESS_KEY") or None,
    )


DEFAULT_POLICY = PolicyConfig()

# Model identifier attached to the committed offline replay fixtures. This is
# intentionally separate from GEMINI_MODEL, which controls live extraction.
REPLAY_FIXTURE_MODEL = "gemini-3.5-flash-lite"
