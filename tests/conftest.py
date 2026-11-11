import pytest

from invoiceops.db import create_db_engine, create_session_factory, init_db


@pytest.fixture
def db_session(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test_invoiceops.db'}")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()
