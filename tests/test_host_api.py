import re
import sys
import types
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.main import create_app
from app.mcp_client import McdError, MockMcdClient


def _body(clock, **kw):
    b = dict(address_id="demo-addr-1", address_label="示例大厦21楼", store_code="1450498",
             be_code="145049802", store_name="麦当劳前滩", title="加班补给",
             cutoff_at=(clock() + timedelta(minutes=40)).isoformat(), cap_fen=4000)
    b.update(kw)
    return b


XRW = {"X-Requested-With": "mcd-jielong"}  # what host.js sends on every host API call


def _real_app(tmp_path, clock, mc=None, base="https://mcd.example"):
    s = Settings(mode="real", db_path=str(tmp_path / "r.db"),
                 public_base_url=base, host_key="k", retention_s=7 * 86400)
    return create_app(s, mc or MockMcdClient(), now=clock)


@pytest.fixture
def real(tmp_path, clock):
    mc = MockMcdClient()
    return TestClient(_real_app(tmp_path, clock, mc), headers=XRW), mc


def test_real_mode_requires_host_key(real):
    c, _ = real
    r = c.get("/api/host/sessions/x")
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "bad"}).status_code == 401
    assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "k"}).status_code == 404
    c.cookies.set("hk", "k")
    assert c.get("/api/host/sessions/x").status_code == 404


def test_host_page_is_static_and_unauthenticated(real):
    """I3/M1: the page has no data in it; auth lives on /api/host/* only. A `k` query param
    is ignored: no cookie, no redirect, nothing counted."""
    c, mc = real
    for path in ("/host", "/host?k=k", "/host?k=wrong", "/host?sid=abc"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path
        assert "host.js" in r.text and "set-cookie" not in r.headers, path
    for i in range(12):
        c.get(f"/host?k=wrong{i}")
    assert c.get("/api/host/sessions/x", headers={"X-Host-Key": "bad"}).status_code == 401  # not 429
    assert mc.calls == []


def test_host_login_sets_cookie_scoped_to_host_api(tmp_path, clock):
    c = TestClient(_real_app(tmp_path, clock), base_url="https://testserver", headers=XRW)
    c.cookies.set("hk", "k", domain="testserver.local", path="/")  # set by the previous build
    r = c.post("/api/host/login", json={"k": "k"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookies = [x.lower() for x in r.headers.get_list("set-cookie")]
    live = [x for x in cookies if x.startswith("hk=k;")]
    assert len(live) == 1
    low = live[0]
    assert "httponly" in low and "samesite=strict" in low and "secure" in low
    assert "path=/api/host" in low and not re.search(r"path=/(;|$)", low)
    # the legacy Path=/ cookie is expired, so it stops travelling to /demo/ as well
    legacy = [x for x in cookies if x not in live]
    assert len(legacy) == 1 and re.search(r"path=/(;|$)", legacy[0]) and "max-age=0" in legacy[0]
    assert c.get("/api/host/sessions/x").status_code == 404  # the cookie works for the API
    # ... and is never attached to the demo prefix (Caddy serves the demo under /demo/)
    assert "hk=" in (c.build_request("GET", "/api/host/sessions/x").headers.get("cookie") or "")
    for path in ("/demo/api/host/sessions/x", "/demo/host", "/j/abc", "/host", "/"):
        assert "hk=" not in (c.build_request("GET", path).headers.get("cookie") or ""), path


@pytest.mark.parametrize("body,status,code", [
    ({"k": "wrong"}, 401, "unauthorized"), ({}, 422, "bad_request"), ({"k": 5}, 422, "bad_request"),
    ({"k": ""}, 422, "bad_request"), ({"k": "x" * 257}, 422, "bad_request"), ([1], 422, "bad_request")])
def test_host_login_rejects(real, body, status, code):
    c, _ = real
    r = c.post("/api/host/login", json=body)
    assert r.status_code == status and r.json() == {"error": code}
    assert "set-cookie" not in r.headers


def test_host_login_requires_csrf_header(real):
    c, _ = real
    c.headers.pop("X-Requested-With")
    r = c.post("/api/host/login", json={"k": "k"})
    assert r.status_code == 403 and r.json() == {"error": "csrf"} and "set-cookie" not in r.headers
    r = c.post("/api/host/login", json={"k": "k"},
               headers={**XRW, "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403


# ---------------- I4: CSRF guard on every /api/host/* route (real mode) ----------------

GUARDED = [("get", "/api/host/addresses"), ("get", "/api/host/stores?address_id=demo-addr-1"),
           ("post", "/api/host/sessions"), ("get", "/api/host/sessions/x"),
           ("post", "/api/host/sessions/x/lock"), ("post", "/api/host/sessions/x/reopen"),
           ("post", "/api/host/sessions/x/quote"), ("post", "/api/host/sessions/x/order"),
           ("post", "/api/host/sessions/x/refresh"), ("post", "/api/host/sessions/x/recent-orders"),
           ("post", "/api/host/sessions/x/adopt"), ("post", "/api/host/sessions/x/not-placed"),
           ("post", "/api/host/sessions/x/cancel"), ("get", "/api/host/sessions/x/pay-qr.svg"),
           ("delete", "/api/host/sessions/x/entries/1")]


@pytest.mark.parametrize("m,path", GUARDED)
def test_csrf_header_required_even_with_valid_key(real, m, path):
    c, mc = real
    c.headers.pop("X-Requested-With")
    c.cookies.set("hk", "k")
    for extra in ({}, {"X-Requested-With": "XMLHttpRequest"}, {"X-Requested-With": ""}):
        r = getattr(c, m)(path, headers={"X-Host-Key": "k", **extra})
        assert r.status_code == 403 and r.json() == {"error": "csrf"}, (path, extra)
    assert mc.calls == []  # in particular GET addresses/stores never reach MCP


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", "bogus"])
@pytest.mark.parametrize("m,path", [("get", "/api/host/addresses"),
                                    ("get", "/api/host/stores?address_id=demo-addr-1"),
                                    ("post", "/api/host/sessions/x/order")])
def test_csrf_sec_fetch_site_must_be_same_origin(real, m, path, site):
    c, mc = real
    c.cookies.set("hk", "k")
    r = getattr(c, m)(path, headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403 and r.json() == {"error": "csrf"}
    assert mc.calls == []


def test_csrf_same_origin_with_header_passes(real):
    c, mc = real
    c.cookies.set("hk", "k")
    r = c.get("/api/host/addresses", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and mc.calls[-1][0] == "delivery-query-addresses"
    assert c.get("/api/host/sessions/x").status_code == 404  # no Sec-Fetch-Site (old browser): ok


def test_csrf_rejections_do_not_count_toward_throttle(real):
    c, _ = real
    c.headers.pop("X-Requested-With")
    for i in range(15):
        assert c.get("/api/host/sessions/x", headers={"X-Host-Key": f"bad{i}"}).status_code == 403
    assert c.get("/api/host/sessions/x", headers={**XRW, "X-Host-Key": "bad"}).status_code == 401


def test_demo_mode_needs_no_csrf_header(client):
    """Demo is unauthenticated mock data; the header is only enforced in real mode."""
    assert client.get("/api/host/addresses").status_code == 200
    assert client.post("/api/host/login", json={"k": "anything"}).json() == {"ok": True}


def test_real_mode_empty_host_key_refused_at_startup(tmp_path, clock):
    # Task 8: an empty key no longer yields an app that "denies all"; it refuses to start
    s = Settings(mode="real", db_path=str(tmp_path / "e.db"), public_base_url="https://x", host_key="")
    with pytest.raises(ValueError):
        create_app(s, MockMcdClient(), now=clock)


def test_cookie_not_secure_on_http(tmp_path, clock):
    c = TestClient(_real_app(tmp_path, clock, base="http://127.0.0.1:8001"), headers=XRW)
    sc = c.post("/api/host/login", json={"k": "k"}).headers["set-cookie"].lower()
    assert "hk=k" in sc and "secure" not in sc and "path=/api/host" in sc


def test_cookie_path_follows_public_base_path(tmp_path, clock):
    c = TestClient(_real_app(tmp_path, clock, base="https://x.example/jl/"), headers=XRW)
    assert "path=/jl/api/host" in c.post("/api/host/login", json={"k": "k"}).headers["set-cookie"].lower()


def test_addresses_hide_phone_and_contact(client, mock_client):
    r = client.get("/api/host/addresses")
    assert r.status_code == 200
    assert r.json() == [{"address_id": "demo-addr-1", "label": "示例大厦21楼"},
                        {"address_id": "demo-addr-2", "label": "示例大厦3楼"}]
    t = r.text
    assert "138" not in t and "示例路" not in t and "contactName" not in t and "phone" not in t


def test_address_label_rules():
    from app.host_api import address_label
    assert address_label("上海市浦东新区示例路1号 示例大厦21楼") == "示例大厦21楼"
    assert address_label("上海市浦东新区前滩中心21楼") == "前滩中心21楼"
    assert address_label("广东省深圳市南山区科技园1号") == "科技园1号"
    assert address_label("前滩中心") == "前滩中心"


def test_stores_mapping(client, mock_client):
    r = client.get("/api/host/stores", params={"address_id": "demo-addr-1"})
    assert r.status_code == 200
    d = r.json()
    assert len(d) == 3
    assert set(d[0]) == {"store_code", "be_code", "name", "open", "close", "today"}
    assert d[0]["store_code"] == "1450498" and d[0]["be_code"] == "145049802"
    assert d[0]["open"] == "07:00" and d[0]["close"] == "22:00"
    assert d[0]["today"].startswith("午餐(11:00")
    assert mock_client.calls[-1] == ("delivery-query-stores",
                                     {"addressId": "demo-addr-1", "beType": 2})
    assert client.get("/api/host/stores").status_code == 422


def test_mcp_failure_is_502(client, mock_client):
    async def boom(tool, args):
        raise McdError(tool, "x")
    mock_client.call = boom
    assert client.get("/api/host/addresses").status_code == 502


def test_create_session_calls_query_meals_once_and_returns_join_url(client, mock_client, clock, app):
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 201
    j = r.json()
    assert len(j["sid"]) == 12 and j["join_url"] == "http://testserver/j/" + j["sid"]
    assert mock_client.calls == [("query-meals", {"storeCode": "1450498", "beCode": "145049802",
                                                  "beType": 2, "orderType": 2})]
    s = app.state.store.get_session(j["sid"])
    assert s["state"] == "open" and s["address_id"] == "demo-addr-1" and s["cap_fen"] == 4000
    pub = client.get("/api/j/" + j["sid"]).json()
    assert pub["title"] == "加班补给" and pub["state"] == "open" and pub["menu"]["items"]


def test_create_session_defaults_and_nocap(client, clock):
    b = _body(clock, cap_fen=0)
    del b["title"]
    j = client.post("/api/host/sessions", json=b).json()
    pub = client.get("/api/j/" + j["sid"]).json()
    assert pub["title"] == "我请客" and pub["cap_fen"] is None
    j = client.post("/api/host/sessions", json=_body(clock, cap_fen=None)).json()
    assert client.get("/api/j/" + j["sid"]).json()["cap_fen"] is None


@pytest.mark.parametrize("field,val", [
    ("address_id", ""), ("store_code", None), ("be_code", 5), ("store_name", ""),
    ("cutoff_at", "garbage"), ("cutoff_at", "2026-10-14T21:00:00"),  # no offset
    ("cutoff_at", "2026-10-14T19:00:00+08:00"),  # past
    ("title", "x" * 21), ("title", "  "), ("address_label", ""), ("address_label", "y" * 31),
    ("cap_fen", -5), ("cap_fen", "40"), ("cap_fen", 1.5), ("cap_fen", True),
])
def test_create_session_validation(client, mock_client, clock, field, val):
    r = client.post("/api/host/sessions", json=_body(clock, **{field: val}))
    assert r.status_code == 422 and r.json() == {"error": field}
    assert mock_client.calls == []


def test_create_session_missing_field_and_bad_json(client, clock):
    b = _body(clock)
    del b["be_code"]
    r = client.post("/api/host/sessions", json=b)
    assert r.status_code == 422 and r.json() == {"error": "be_code"}
    assert client.post("/api/host/sessions", content=b"nope").status_code == 422


def test_create_session_mcp_error_502_no_session(client, mock_client, clock, app):
    async def boom(tool, args):
        raise McdError(tool, "x")
    mock_client.call = boom
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 502 and r.json() == {"error": "mcd"}
    assert app.state.store._db.execute("select count(*) from sessions").fetchone()[0] == 0


def test_create_purges_old(client, clock, app, make_session):
    old = make_session(created_at=(clock() - timedelta(hours=3)).isoformat())
    client.post("/api/host/sessions", json=_body(clock))
    assert app.state.store.get_session(old) is None


def test_host_detail_superset_of_join_payload(client, make_session, app):
    sid = make_session()
    app.state.store.update_session(sid, order_id="O1", pay_url="https://pay/x",
                                   quote_json='{"price": 1}')
    pub = client.get(f"/api/j/{sid}").json()
    h = client.get(f"/api/host/sessions/{sid}").json()
    for k, v in pub.items():
        assert h[k] == v
    assert h["raw_state"] == "open" and h["order_id"] == "O1"
    assert h["pay_url"] == "https://pay/x" and h["quote"] == {"price": 1}
    assert "pay_url" not in pub and "order_id" not in pub
    assert client.get("/api/host/sessions/nope").status_code == 404


def test_lock_and_reopen_no_mcp(client, make_session, mock_client):
    sid = make_session()
    assert client.post(f"/api/host/sessions/{sid}/lock").json()["state"] == "locked"
    assert client.get(f"/api/j/{sid}").json()["state"] == "locked"
    assert client.post(f"/api/host/sessions/{sid}/lock").status_code == 409
    r = client.post(f"/api/host/sessions/{sid}/reopen")
    assert r.status_code == 200 and r.json()["state"] == "open"
    assert mock_client.calls == []
    assert client.get(f"/api/host/sessions/{sid}").json()["raw_state"] == "open"


def test_reopen_extends_cutoff_at_least_10_min(client, make_session, clock, app):
    sid = make_session(cutoff_at=(clock() + timedelta(minutes=2)).isoformat())
    client.post(f"/api/host/sessions/{sid}/lock")
    client.post(f"/api/host/sessions/{sid}/reopen")
    c = datetime.fromisoformat(app.state.store.get_session(sid)["cutoff_at"])
    assert c >= clock() + timedelta(minutes=10)
    far = (clock() + timedelta(hours=2)).isoformat()  # a later cutoff is never shortened
    sid2 = make_session(cutoff_at=far)
    client.post(f"/api/host/sessions/{sid2}/lock")
    client.post(f"/api/host/sessions/{sid2}/reopen")
    assert app.state.store.get_session(sid2)["cutoff_at"] == far


def test_reopen_after_cutoff_passed_while_raw_open(client, make_session, clock):
    sid = make_session()
    clock.advance(minutes=45)
    assert client.get(f"/api/j/{sid}").json()["state"] == "locked"
    assert client.post(f"/api/host/sessions/{sid}/reopen").status_code == 200
    assert client.get(f"/api/j/{sid}").json()["state"] == "open"


def test_reopen_conflicts(client, make_session):
    sid = make_session()
    assert client.post(f"/api/host/sessions/{sid}/reopen").status_code == 409  # already open
    sid2 = make_session(state="ordered")
    assert client.post(f"/api/host/sessions/{sid2}/reopen").status_code == 409
    assert client.post(f"/api/host/sessions/{sid2}/lock").status_code == 409
    assert client.post("/api/host/sessions/nope/lock").status_code == 404


def test_host_routes_all_guarded_in_real_mode(real):
    c, mc = real
    for m, p in GUARDED:
        assert getattr(c, m)(p).status_code == 401, p
    assert mc.calls == []


def test_load_settings_real_without_secrets_fails(monkeypatch, tmp_path):
    for k in ("MCD_TOKEN_FILE", "MCD_HOST_KEY"):
        monkeypatch.delenv(k, raising=False)

    class Boom:
        def client(self, *a, **k):
            raise RuntimeError("no aws in tests")
    monkeypatch.setitem(sys.modules, "boto3", Boom())
    monkeypatch.setenv("MCD_MODE", "real")
    monkeypatch.setenv("MCD_DB", str(tmp_path / "x.db"))
    s = config.load_settings()
    assert s.mode == "real" and s.retention_s == config.REAL_RETENTION_S
    with pytest.raises(Exception):
        config.load_secrets(s)


def test_load_secrets_from_env_and_empty_fails(monkeypatch, tmp_path):
    tf = tmp_path / "tok"
    tf.write_text("TOKEN\n")
    monkeypatch.setenv("MCD_TOKEN_FILE", str(tf))
    monkeypatch.setenv("MCD_HOST_KEY", "hk1")
    s = Settings(mode="real", db_path="x", public_base_url="https://x")
    assert config.load_secrets(s) == ("TOKEN", "hk1")
    tf.write_text("  \n")
    with pytest.raises(RuntimeError):
        config.load_secrets(s)


def test_load_secrets_ssm_fallback(monkeypatch):
    monkeypatch.delenv("MCD_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MCD_HOST_KEY", raising=False)
    calls = []

    class SSM:
        def get_parameter(self, Name, WithDecryption):
            calls.append((Name, WithDecryption))
            return {"Parameter": {"Value": "v-" + Name.rsplit("/", 1)[1]}}

    def fake_client(svc, region_name=None):
        calls.append((svc, region_name))
        return SSM()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=fake_client))
    s = Settings(mode="real", db_path="x", public_base_url="https://x")
    assert config.load_secrets(s) == ("v-mcd-mcp-token", "v-host-key")
    assert ("ssm", "ap-northeast-1") in calls
    assert ("/mcd-1024/mcd-mcp-token", True) in calls and ("/mcd-1024/host-key", True) in calls


def test_load_settings_demo_never_imports_boto3(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "boto3", None)  # any import would raise
    monkeypatch.setenv("MCD_MODE", "demo")
    monkeypatch.setenv("MCD_DB", str(tmp_path / "d.db"))
    s = config.load_settings()
    assert s.mode == "demo" and s.retention_s == config.DEMO_RETENTION_S
    monkeypatch.setenv("MCD_MODE", "bogus")
    with pytest.raises(RuntimeError):
        config.load_settings()


def test_host_page_served_and_js_rules(client):
    from pathlib import Path
    r = client.get("/host")
    assert r.status_code == 200 and "host.js" in r.text
    js = (Path(__file__).resolve().parent.parent / "web" / "host.js").read_text()
    assert "navigator.clipboard.writeText" in js and "api/host/" in js
    assert "http://" not in js and "//cdn" not in js


def test_host_js_sends_csrf_header_and_logs_in_via_fragment():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "web" / "host.js").read_text()
    assert "'X-Requested-With': 'mcd-jielong'" in js
    # every fetch() in host.js goes through api() / login / the QR loader, all with the header
    assert js.count("fetch(") == js.count("headers: hostHeaders(")
    assert "location.hash" in js and "history.replaceState" in js and "'api/host/login'" in js
    assert "?k=" not in js and "k=' +" not in js
    i = js.index("history.replaceState")
    assert i < js.index("'api/host/login'")  # the key leaves the address bar before the POST


def test_root_redirects_to_host_relative(client, real):
    for c in (client, real[0]):
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "host"


def test_create_session_survives_meals_with_bad_prices(client, mock_client, clock, app):
    """M4 end to end: a meal with an unusable currentPrice is dropped; the session is created."""
    import copy
    import json as _json
    from pathlib import Path
    data = _json.loads((Path(__file__).resolve().parent.parent / "app" / "mock_data" /
                        "query-meals.json").read_text(encoding="utf-8"))
    codes = list(data["data"]["meals"])
    data["data"]["meals"][codes[0]]["currentPrice"] = None
    data["data"]["meals"][codes[1]]["currentPrice"] = "abc"
    del data["data"]["meals"][codes[2]]["currentPrice"]

    async def fake(tool, args):
        return copy.deepcopy(data)
    mock_client.call = fake
    r = client.post("/api/host/sessions", json=_body(clock))
    assert r.status_code == 201, r.text
    items = client.get("/api/j/" + r.json()["sid"]).json()["menu"]["items"]
    assert items and not set(codes[:3]) & set(items)


def test_startup_purges_expired_sessions(settings, clock, make_session, app):
    """M7: retention is enforced at startup too, not only when a session is created."""
    old = make_session(created_at=(clock() - timedelta(seconds=settings.retention_s + 1)).isoformat())
    fresh = make_session()
    create_app(settings, MockMcdClient(), now=clock)  # restart on the same database
    assert app.state.store.get_session(old) is None
    assert app.state.store.get_session(fresh) is not None
