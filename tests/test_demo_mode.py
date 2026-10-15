"""Task 8: demo mode wiring, demo limits, host-key throttle, log redaction."""
import logging
import sys
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.config import Settings
from app.main import create_app
from app.mcp_client import McdClient, MockMcdClient
from app.security import client_ip  # noqa: F401  (import check)


def _body(clock, **kw):
    b = dict(address_id="demo-addr-1", address_label="示例大厦21楼", store_code="1450498",
             be_code="145049802", store_name="麦当劳前滩", title="加班补给",
             cutoff_at=(clock() + timedelta(minutes=40)).isoformat(), cap_fen=4000)
    b.update(kw)
    return b


def _real_settings(tmp_path, key="KEY-1"):
    return Settings(mode="real", db_path=str(tmp_path / "r.db"),
                    public_base_url="https://mcd.example", host_key=key, retention_s=7 * 86400)


# ---------------- app_from_env / create_app wiring ----------------

def test_demo_app_from_env_never_loads_secrets(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("load_secrets must not be called in demo mode")

    monkeypatch.setattr(config, "load_secrets", boom)
    monkeypatch.setitem(sys.modules, "boto3", None)  # any import of boto3 would raise
    monkeypatch.setenv("MCD_MODE", "demo")
    monkeypatch.setenv("MCD_DB", str(tmp_path / "sub" / "d.db"))
    app = main.app_from_env()
    assert isinstance(app.state.client, MockMcdClient)
    assert app.state.settings.mode == "demo"


def test_real_app_from_env_uses_secrets_and_real_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_secrets", lambda s: ("TOK", "HK"))
    monkeypatch.setenv("MCD_MODE", "real")
    monkeypatch.setenv("MCD_DB", str(tmp_path / "r.db"))
    app = main.app_from_env()
    assert isinstance(app.state.client, McdClient)
    assert app.state.settings.host_key == "HK" and app.state.settings.mode == "real"
    assert app.state.settings.retention_s == 7 * 86400


def test_create_app_refuses_real_client_in_demo(settings):
    with pytest.raises(ValueError):
        create_app(settings, McdClient("tok"))


@pytest.mark.parametrize("key", [None, ""])
def test_create_app_refuses_real_mode_without_host_key(tmp_path, key):
    s = _real_settings(tmp_path, key)
    with pytest.raises(ValueError):
        create_app(s, MockMcdClient())


# ---------------- meta + demo host page ----------------

XRW = {"X-Requested-With": "mcd-jielong"}


def test_meta_reports_mode(client, tmp_path, clock):
    r = client.get("/api/meta")
    assert r.status_code == 200 and r.json() == {"mode": "demo"}
    real = TestClient(create_app(_real_settings(tmp_path), MockMcdClient(), now=clock))
    assert real.get("/api/meta").json() == {"mode": "real"}  # no auth needed


def test_demo_host_page_ignores_key(client):
    r = client.get("/host?k=anything", follow_redirects=False)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_frontend_shows_demo_banner_via_textcontent():
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "web"
    for name in ("host.js", "join.js"):
        js = (web / name).read_text(encoding="utf-8")
        assert "演示模式：数据是假的，不会真的下单" in js, name
        assert "api/meta" in js, name
        assert "innerHTML" not in js, name


# ---------------- demo limits ----------------

def test_demo_session_create_limits_429(client, clock):
    for i in range(5):
        r = client.post("/api/host/sessions", json=_body(clock))
        assert r.status_code == 201, (i, r.text)
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 429 and r.json() == {"error": "demo_limit"}
    clock.advance(minutes=61)
    assert client.post("/api/host/sessions", json=_body(clock, cutoff_at=(
        clock() + timedelta(minutes=40)).isoformat())).status_code == 201


def test_demo_global_active_session_cap_20(app, client, clock, make_session):
    for _ in range(20):
        make_session()
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 429 and r.json() == {"error": "demo_limit"}


def test_demo_cap_counts_only_active_states(app, client, clock, make_session):
    for _ in range(19):
        make_session()
    for _ in range(5):
        make_session(state="cancelled")
    assert client.post("/api/host/sessions", json=_body(clock)).status_code == 201  # 19 active -> 20th
    assert client.post("/api/host/sessions", json=_body(clock)).status_code == 429  # now 20 active


def test_demo_cap_ignores_purged_old_sessions(app, client, clock, make_session):
    for _ in range(20):
        make_session()
    clock.advance(seconds=7201)
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 201


def test_real_mode_has_no_create_limits(tmp_path, clock):
    c = TestClient(create_app(_real_settings(tmp_path), MockMcdClient(), now=clock), headers=XRW)
    c.cookies.set("hk", "KEY-1")
    for _ in range(7):
        assert c.post("/api/host/sessions", json=_body(clock)).status_code == 201


# ---------------- host-key brute-force throttle ----------------

def _real_client(tmp_path, clock):
    return TestClient(create_app(_real_settings(tmp_path), MockMcdClient(), now=clock), headers=XRW)


def _wrong(c, i=0):
    return c.get("/api/host/sessions/x", headers={"X-Host-Key": f"bad{i}"})


def test_host_auth_throttle_after_10_failures(tmp_path, clock):
    c = _real_client(tmp_path, clock)
    for i in range(10):
        assert _wrong(c, i).status_code == 401
    # throttled: wrong keys now get 429 ...
    r = _wrong(c, 99)
    assert r.status_code == 429 and r.json() == {"error": "rate_limited"}
    r = c.post("/api/host/login", json={"k": "wrong-again"})
    assert r.status_code == 429 and "set-cookie" not in r.headers
    clock.advance(seconds=300)
    for i in range(10):
        assert _wrong(c, i).status_code == 429
    clock.advance(seconds=301)  # the first 12 aged out; the 10 made while blocked still count
    assert _wrong(c).status_code == 429
    clock.advance(seconds=300)
    assert _wrong(c).status_code == 401


def test_host_auth_throttle_never_locks_out_the_right_key(tmp_path, clock):
    """M2: strangers sharing the IP (one office NAT) must not be able to lock the host out."""
    c = _real_client(tmp_path, clock)
    for i in range(25):
        _wrong(c, i)
    assert _wrong(c).status_code == 429
    assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "KEY-1"}).status_code == 404
    r = c.post("/api/host/login", json={"k": "KEY-1"})
    assert r.status_code == 200 and "hk=KEY-1" in r.headers["set-cookie"]
    c.cookies.set("hk", "KEY-1")
    assert c.get("/api/host/sessions/x").status_code == 404
    c.cookies.clear()
    assert _wrong(c).status_code == 429  # success does not reset the failure count


def test_host_auth_failures_via_login_count(tmp_path, clock):
    c = _real_client(tmp_path, clock)
    for i in range(10):
        r = c.post("/api/host/login", json={"k": f"wrong{i}"})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert c.post("/api/host/login", json={"k": "wrong"}).status_code == 429
    assert _wrong(c).status_code == 429  # same counter as the API guard


def test_requests_without_any_key_are_not_guesses(tmp_path, clock):
    """Opening the host page before logging in fires a few unauthenticated API calls; those
    carry no key at all and must not push the IP towards the throttle."""
    c = _real_client(tmp_path, clock)
    for _ in range(30):
        assert c.get("/api/host/sessions/x").status_code == 401
    assert _wrong(c).status_code == 401


def test_host_auth_success_does_not_count(tmp_path, clock):
    c = _real_client(tmp_path, clock)
    for _ in range(30):
        assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "KEY-1"}).status_code == 404
    for i in range(9):
        assert c.get("/api/host/sessions/x", headers={"X-Host-Key": f"bad{i}"}).status_code == 401
    assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "KEY-1"}).status_code == 404


def test_host_auth_failures_expire_individually(tmp_path, clock):
    c = _real_client(tmp_path, clock)
    for i in range(5):
        _wrong(c)
    clock.advance(seconds=400)
    for i in range(5):
        _wrong(c)
    assert _wrong(c).status_code == 429  # 10 in the window (this 11th is counted too)
    clock.advance(seconds=250)  # first five (t=0) expired; six (t=400) still count
    assert _wrong(c).status_code == 401


def test_demo_mode_has_no_host_throttle(client):
    for _ in range(15):
        assert client.get("/api/host/sessions/x").status_code == 404


# ---------------- log redaction ----------------

def _access_record(path):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("1.2.3.4:5", "GET", path, "1.1", 302), None)


def test_access_log_filter_redacts_host_key(settings):
    create_app(settings, MockMcdClient())
    flt = [f for f in logging.getLogger("uvicorn.access").filters
           if f.__class__.__name__ == "RedactHostKeyFilter"]
    assert len(flt) == 1
    create_app(settings, MockMcdClient())  # idempotent
    assert len([f for f in logging.getLogger("uvicorn.access").filters
                if f.__class__.__name__ == "RedactHostKeyFilter"]) == 1
    for path in ("/host?k=SECRET123", "/host?a=1&k=SECRET123&b=2", "/host?k=SECRET123&k=SECRET123"):
        rec = _access_record(path)
        assert flt[0].filter(rec) is True
        assert "SECRET123" not in rec.getMessage(), rec.getMessage()
        assert "k=***" in rec.getMessage()
    rec = _access_record("/api/j/abc?x=1")
    flt[0].filter(rec)
    assert "/api/j/abc?x=1" in rec.getMessage()


def test_access_log_filter_handles_preformatted_message(settings):
    create_app(settings, MockMcdClient())
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            'GET /host?k=SECRET123 HTTP/1.1', None, None)
    logging.getLogger("uvicorn.access").filters[-1].filter(rec)
    assert "SECRET123" not in rec.getMessage()


def test_access_log_filter_end_to_end_through_handler(settings):
    import io
    create_app(settings, MockMcdClient())
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("uvicorn.access")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/host?k=SECRET123", "1.1", 302)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    assert "SECRET123" not in buf.getvalue() and "k=***" in buf.getvalue()


async def test_mcd_client_never_logs_token_or_headers(caplog):
    token = "not-a-real-token"

    def handler(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [], "isError": False, "structuredContent": {"success": True, "data": {}}}})

    c = McdClient(token, transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG):
        await c.call("order-list", {})
        def bad(req):
            return httpx.Response(500, text="boom")
        with pytest.raises(Exception):
            await McdClient(token, transport=httpx.MockTransport(bad)).call("order-list", {})
    text = "\n".join(r.getMessage() + str(r.args) for r in caplog.records)
    assert token not in text and "Bearer" not in text and "Authorization" not in text.title()


def test_host_key_not_in_app_logs_on_login(tmp_path, clock, caplog):
    c = _real_client(tmp_path, clock)
    with caplog.at_level(logging.DEBUG):
        c.post("/api/host/login", json={"k": "KEY-1"})
        c.post("/api/host/login", json={"k": "WRONG-KEY"})
        c.get("/host?k=KEY-1", follow_redirects=False)  # legacy link: ignored, still redacted
    # httpx (the test client itself) logs its own request URLs; only the app's loggers matter
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith(("app", "uvicorn")))
    assert "KEY-1" not in text and "WRONG-KEY" not in text


# ---------------- full mock flow ----------------

def test_full_mock_flow_end_to_end(client, clock, menu):
    codes = sorted(menu["items"], key=lambda c: (menu["items"][c]["price_fen"], c))[:2]
    r = client.post("/api/host/sessions", json=_body(clock, cap_fen=None))
    assert r.status_code == 201
    sid = r.json()["sid"]
    assert r.json()["join_url"] == f"http://testserver/j/{sid}"
    assert client.get(f"/api/j/{sid}").json()["state"] == "open"

    for name, items in [("小王", [{"code": codes[0], "qty": 2}]),
                        ("小李", [{"code": codes[1], "qty": 1}]),
                        ("小张", [{"code": codes[0], "qty": 1}, {"code": codes[1], "qty": 1}])]:
        r = client.post(f"/api/j/{sid}/entries", json={"name": name, "items": items, "note": ""})
        assert r.status_code == 201, r.text
    j = client.get(f"/api/j/{sid}").json()
    assert j["count"] == 3 and j["state"] == "open"

    r = client.post(f"/api/host/sessions/{sid}/lock")
    assert r.status_code == 200 and r.json()["state"] == "locked"
    assert client.post(f"/api/j/{sid}/entries", json={
        "name": "迟到", "items": [{"code": codes[0], "qty": 1}], "note": ""}).status_code == 409

    r = client.post(f"/api/host/sessions/{sid}/quote", json={})
    assert r.status_code == 200
    q = r.json()["quote_fen"]
    assert isinstance(q, int) and q > 0

    r = client.post(f"/api/host/sessions/{sid}/order", json={"quote_fen": q})
    assert r.status_code == 200, r.text
    assert r.json()["order_id"].startswith("DEMO")
    assert client.get(f"/api/host/sessions/{sid}").json()["raw_state"] == "ordered"

    r = client.post(f"/api/host/sessions/{sid}/refresh")
    assert r.status_code == 200 and r.json()["raw_state"] == "ordered"

    r = client.post(f"/api/host/sessions/{sid}/cancel", json={"reason_code": "1"})
    assert r.status_code == 200 and r.json()["raw_state"] == "cancelled"
    pub = client.get(f"/api/j/{sid}")
    assert pub.json()["state"] == "cancelled"
    for bad in ("payH5Url", "ADDR", "demo-addr-1", "138****0000", "deliveryInfo"):
        assert bad not in pub.text
