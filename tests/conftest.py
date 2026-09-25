import os

import pytest


os.environ["DATABASE_URL"] = "sqlite:///./data/test_hoops_forward.db"


@pytest.fixture(autouse=True)
def reset_database():
    from core.database import SessionLocal, engine, seed_reference_data
    from domain.models import Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        seed_reference_data(session)
        session.commit()
    finally:
        session.close()
    yield
