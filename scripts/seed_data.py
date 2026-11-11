"""Create the database schema and seed synthetic vendor/PO reference data.

Usage:
    python -m scripts.seed_data            # create tables + upsert reference data
    python -m scripts.seed_data --reset    # ALSO wipe all runs/invoices (demo reset)
"""

import argparse
from pathlib import Path

from invoiceops.config import get_settings
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.seed import reset_demo_state, seed_reference_data

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete all processing history first")
    args = parser.parse_args()

    settings = get_settings()
    if args.reset and settings.is_production and not settings.enable_demo_reset:
        parser.error("--reset is disabled in production; set ENABLE_DEMO_RESET=true to allow it.")

    engine = create_db_engine(settings.database_url)
    try:
        init_db(engine)
        SessionLocal = create_session_factory(engine)
        with SessionLocal.begin() as session:
            if args.reset:
                counts = reset_demo_state(session, ROOT / "data")
                print(f"Reset demo state: {counts}")
            else:
                counts = seed_reference_data(session, ROOT / "data")
                print(
                    "Database ready. Seeded/updated "
                    f"{counts['vendors']} vendors and {counts['purchase_orders']} purchase orders."
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
