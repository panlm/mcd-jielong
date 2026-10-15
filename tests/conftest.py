import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import TZ, Settings
from app.main import create_app
from app.mcp_client import MockMcdClient
from app.menu import normalize_menu

MOCK_DIR = Path(__file__).resolve().parent.parent / "app" / "mock_data"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def settings(tmp_path):
    return Settings(mode="demo", db_path=str(tmp_path / "t.db"),
                    public_base_url="http://testserver", retention_s=7200)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 14, 20, 0, tzinfo=TZ))


@pytest.fixture
def mock_client():
    return MockMcdClient()


@pytest.fixture
def app(settings, mock_client, clock):
    return create_app(settings, mock_client, now=clock)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def menu():
    data = json.loads((MOCK_DIR / "query-meals.json").read_text())["data"]
    return normalize_menu(data)


@pytest.fixture
def make_session(app, clock, menu):
    counter = iter(range(1, 1000))

    def make(**overrides) -> str:
        fields = dict(
            sid=f"sid{next(counter):09d}", title="加班补给", address_id="ADDR-SECRET",
            address_label="前滩中心21楼", store_code="S1", be_code="BE1",
            store_name="前滩店", cutoff_at=(clock() + timedelta(minutes=30)).isoformat(),
            cap_fen=4000, menu_json=json.dumps(menu, ensure_ascii=False), state="open",
            created_at=clock().isoformat(),
        )
        fields.update(overrides)
        return app.state.store.create_session(**fields)

    return make
