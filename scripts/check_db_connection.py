"""Check configured database connectivity without exposing connection secrets."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from invoiceops.config import get_settings
from invoiceops.db import create_db_engine
from invoiceops.models import Base


def main() -> int:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                database, schema, version = connection.execute(
                    text("SELECT current_database(), current_schema(), version()")
                ).one()
            elif engine.dialect.name == "sqlite":
                database, schema, version = (
                    "SQLite",
                    "main",
                    connection.execute(text("SELECT sqlite_version()")).scalar_one(),
                )
            else:
                database, schema, version = engine.dialect.name, "default", "unknown"
            available = set(inspect(connection).get_table_names())
        missing = sorted(set(Base.metadata.tables) - available)
        print(f"Connected to database: {database}")
        print(f"Driver: {engine.dialect.name}/{engine.dialect.driver}; schema: {schema}")
        print(f"Server: {version}")
        if missing:
            print("Missing application tables: " + ", ".join(missing))
            return 1
        print(f"All {len(Base.metadata.tables)} application tables are available.")
        return 0
    except SQLAlchemyError as exc:
        print(f"Database check failed ({type(exc).__name__}); connection details suppressed.")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
