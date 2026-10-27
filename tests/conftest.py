import os

os.environ["DATABASE_URL"] = "sqlite://"

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.connectors import build_connector  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app, get_connector_builder  # noqa: E402
from app.models import Connection  # noqa: E402
from mock_tracker.main import create_app as create_tracker  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def make_tracker(**kwargs) -> TestClient:
    kwargs.setdefault("failure_rate", 0.0)
    kwargs.setdefault("rate_limit_per_sec", 10_000)
    kwargs.setdefault("seed", 7)
    return TestClient(create_tracker(**kwargs))


@pytest.fixture
def tracker() -> TestClient:
    return make_tracker()


def add_connection(db, tenant="acme", provider="tracker", config=None, secret="s3cret") -> Connection:
    conn = Connection(tenant=tenant, provider=provider, name="{} {}".format(tenant, provider),
                      config_json=json.dumps(config or {}), webhook_secret=secret)
    db.add(conn)
    db.commit()
    return conn


@pytest.fixture
def api(tracker):
    app.dependency_overrides[get_connector_builder] = lambda: (lambda conn: build_connector(conn, http=tracker))
    with TestClient(app) as client:
        yield client
