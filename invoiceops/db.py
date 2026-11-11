"""Database engine and session helpers."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import PROJECT_ROOT, get_settings, normalize_database_url
from .models import Base


def get_database_url(database_url: str | None = None) -> str:
    """Explicit URL wins; otherwise DATABASE_URL from env/.env; else local SQLite.

    Provider-style ``postgres://`` URLs are rewritten for the psycopg v3 driver,
    and the SQLite default is an absolute path (independent of the cwd).
    """
    if database_url:
        return normalize_database_url(database_url)
    return get_settings().database_url


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    url = get_database_url(database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(
        url,
        echo=echo,
        pool_pre_ping=not url.startswith("sqlite"),
        connect_args=connect_args,
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create tables and apply small additive migrations needed by app versions."""
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    migrations = {
        "invoice_line_items": {
            "matched_po_line_id": (
                "INTEGER REFERENCES po_line_items(id) ON DELETE SET NULL"
            )
        },
        "review_actions": {
            "generated_email": "TEXT",
            "overridden_checks": "JSON",
        },
    }
    with engine.begin() as connection:
        for table_name, columns in migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {name} {declaration}")
                    )
