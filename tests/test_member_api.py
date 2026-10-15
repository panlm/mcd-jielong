import json
import re
from pathlib import Path

import pytest

from app.security import new_secret

PRIVATE = ["pay_url", "payH5Url", "address_id", "addressId", "phone", "deliveryInfo"]


def first_codes(menu, n=2):
    # menu["items"] order is not stable across runs; pick the cheapest, deterministically
    return sorted(menu["items"], key=lambda c: (menu["items"][c]["price_fen"], c))[:n]


def body(menu, name="小王", n=1, qty=1, note=""):
    return {"name": name, "items": [{"code": c, "qty": qty} for c in first_codes(menu, n)], "note": note}


def test_get_join_payload_shape(client, make_session):
    sid = make_session()
    r = client.get(f"/api/j/{sid}")
    assert r.status_code == 200
    d = r.json()
    for k in ["title", "address_label", "store_name", "cutoff_at", "cap_fen", "state",
              "order_status", "expect_delivery_time", "menu", "entries", "total_fen", "count"]:
        assert k in d
    assert d["state"] == "open" and d["count"] == 0 and d["entries"] == [] and d["total_fen"] == 0
    assert set(d["menu"]) == {"categories", "items"}
    assert client.get("/api/j/nope").status_code == 404
    page = client.get(f"/j/{sid}")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]


def test_join_payload_has_no_private_fields(client, make_session, app):
    sid = make_session(pay_url="https://pay.example/payH5Url?x=1", address_id="ADDR-SECRET")
    app.state.store.update_session(sid, order_status="制作中")
    text = json.dumps(client.get(f"/api/j/{sid}").json(), ensure_ascii=False)
    for p in PRIVATE:
        assert p not in text
    assert "pay.example" not in text and "ADDR-SECRET" not in text
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)


def test_post_entry_then_listed_with_subtotal(client, make_session, menu):
    sid = make_session(cap_fen=None)
    b = body(menu, n=2, qty=2, note="不要酱")
    r = client.post(f"/api/j/{sid}/entries", json=b)
    assert r.status_code == 201
    assert set(r.json()) == {"id", "secret"}
    d = client.get(f"/api/j/{sid}").json()
    e = d["entries"][0]
    c1, c2 = first_codes(menu)
    expect = 2 * (menu["items"][c1]["price_fen"] + menu["items"][c2]["price_fen"])
    assert e["subtotal_fen"] == expect == d["total_fen"] and d["count"] == 1
    assert e["name"] == "小王" and e["note"] == "不要酱" and e["seq"] == 1
    assert e["items"][0] == {"code": c1, "name": menu["items"][c1]["name"], "qty": 2,
                             "price_fen": menu["items"][c1]["price_fen"]}
    assert "secret" not in json.dumps(d)
    assert client.post(f"/api/j/{sid}/entries", json={"name": "", "items": [], "note": ""}).status_code == 422
    r = client.post(f"/api/j/{sid}/entries", json={"name": "a", "items": [{"code": "zzz", "qty": 1}]})
    assert r.status_code == 422 and r.json() == {"error": "unknown_code"}
    assert client.post("/api/j/nope/entries", json=b).status_code == 404


def test_post_entry_after_cutoff_409(client, make_session, menu, clock):
    sid = make_session()
    assert client.post(f"/api/j/{sid}/entries", json=body(menu)).status_code == 201
    clock.advance(minutes=31)
    r = client.post(f"/api/j/{sid}/entries", json=body(menu))
    assert r.status_code == 409 and r.json() == {"error": "closed"}
    assert client.get(f"/api/j/{sid}").json()["state"] == "locked"


def test_put_delete_require_secret(client, make_session, menu, clock):
    sid = make_session()
    other = make_session()
    r = client.post(f"/api/j/{sid}/entries", json=body(menu)).json()
    eid, sec = r["id"], r["secret"]
    url = f"/api/j/{sid}/entries/{eid}"
    new = body(menu, name="小李", n=2)
    assert client.put(url, json=new).status_code == 403
    assert client.put(url, json=new, headers={"X-Entry-Secret": new_secret()[0]}).status_code == 403
    assert client.delete(url, headers={"X-Entry-Secret": "bad"}).status_code == 403
    # right secret but wrong session -> 404
    assert client.put(f"/api/j/{other}/entries/{eid}", json=new, headers={"X-Entry-Secret": sec}).status_code == 404
    assert client.delete(f"/api/j/{other}/entries/{eid}", headers={"X-Entry-Secret": sec}).status_code == 404
    assert client.put(url, json=new, headers={"X-Entry-Secret": sec}).status_code == 200
    assert client.get(f"/api/j/{sid}").json()["entries"][0]["name"] == "小李"
    assert client.put(url, json={"name": "", "items": []}, headers={"X-Entry-Secret": sec}).status_code == 422
    # closed -> 409 even with the right secret
    clock.advance(minutes=31)
    assert client.delete(url, headers={"X-Entry-Secret": sec}).status_code == 409
    clock.advance(minutes=-31)
    assert client.delete(url, headers={"X-Entry-Secret": sec}).status_code == 200
    assert client.get(f"/api/j/{sid}").json()["entries"] == []
    assert client.delete(url, headers={"X-Entry-Secret": sec}).status_code == 404


def test_per_person_cap_422(client, make_session, menu):
    sid = make_session(cap_fen=1000)
    r = client.post(f"/api/j/{sid}/entries", json=body(menu, qty=10))
    assert r.status_code == 422 and r.json() == {"error": "cap"}


def test_entry_cap_422(client, make_session, menu, app):
    sid = make_session()
    code = first_codes(menu, 1)
    for i in range(50):
        app.state.store.add_entry(sid, f"u{i}", [{"code": code[0], "qty": 1}], "", "h")
    r = client.post(f"/api/j/{sid}/entries", json=body(menu))
    assert r.status_code == 422 and r.json() == {"error": "full"}


def test_member_requests_never_call_mcp(client, make_session, menu, mock_client):
    sid = make_session()
    r = client.post(f"/api/j/{sid}/entries", json=body(menu)).json()
    h = {"X-Entry-Secret": r["secret"]}
    client.get(f"/j/{sid}")
    client.get(f"/api/j/{sid}")
    client.put(f"/api/j/{sid}/entries/{r['id']}", json=body(menu, n=2), headers=h)
    client.delete(f"/api/j/{sid}/entries/{r['id']}", headers=h)
    assert mock_client.calls == []


def test_write_rate_limit_429(client, make_session, menu, clock):
    """M3: 120 writes/min per IP (a whole office shares one NAT IP)."""
    from app.member_api import MEMBER_WRITES_PER_MIN
    assert MEMBER_WRITES_PER_MIN == 120
    sid = make_session(cap_fen=None)
    codes = [client.post(f"/api/j/{sid}/entries", json=body(menu)).status_code for _ in range(121)]
    assert codes[:50] == [201] * 50  # then the 50-entry cap answers 422 until the limiter kicks in
    assert set(codes[50:120]) == {422}
    assert codes[120] == 429
    assert client.get(f"/api/j/{sid}").status_code == 200  # reads are not limited
    clock.advance(seconds=61)  # the window follows the app clock
    assert client.post(f"/api/j/{sid}/entries", json=body(menu)).status_code == 422


def test_forwarded_for_only_trusted_from_loopback():
    from app.security import RateLimiter, client_ip

    class Req:
        def __init__(self, host, xff):
            self.client = type("C", (), {"host": host})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    assert client_ip(Req("127.0.0.1", "9.9.9.9")) == "9.9.9.9"
    assert client_ip(Req("127.0.0.1", "6.6.6.6, 9.9.9.9")) == "9.9.9.9"
    assert client_ip(Req("5.5.5.5", "9.9.9.9")) == "5.5.5.5"
    t = [0.0]
    rl = RateLimiter(2, 60, clock=lambda: t[0])
    assert rl.check("a") and rl.check("a") and not rl.check("a") and rl.check("b")
    t[0] = 61
    assert rl.check("a")


def test_new_sid_base62():
    from app.security import new_sid
    s = new_sid()
    assert re.fullmatch(r"[0-9A-Za-z]{12}", s) and s != new_sid()


def test_frontend_never_uses_innerhtml():
    web = Path(__file__).resolve().parent.parent / "web"
    files = list(web.glob("*.js"))
    assert files
    for f in files:
        t = f.read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            assert bad not in t, f"{f.name} uses {bad}"


def test_frontend_img_src_only_after_https_check_and_copy():
    js = (Path(__file__).resolve().parent.parent / "web" / "join.js").read_text()
    i = js.index("img.src")
    guard = js.rfind("indexOf('https://') === 0", 0, i)
    assert guard != -1 and i - guard < 400  # src assignment sits inside the https guard
    assert js.count("img.src") == 1 and ".src =" not in js.replace("img.src =", "")
    assert "只给发起人看" not in js
    assert "大家都能看到，不会发给麦当劳" in js
    assert "已选：" in js and "writeVersion" in js


def _lock_just_before(monkeypatch, app, sid, method):
    """Host locks between the member handler's state check and its write."""
    store = app.state.store
    orig = getattr(store, method)

    def late(*a, **kw):
        store.transition(sid, {"open"}, "locked")
        return orig(*a, **kw)
    monkeypatch.setattr(store, method, late)


def test_add_entry_rejected_if_locked_before_write(client, make_session, menu, app, monkeypatch):
    sid = make_session(cap_fen=None)
    _lock_just_before(monkeypatch, app, sid, "add_entry")
    r = client.post(f"/api/j/{sid}/entries", json=body(menu))
    assert r.status_code == 409 and r.json() == {"error": "closed"}
    assert app.state.store.list_entries(sid) == []


def test_edit_delete_rejected_if_locked_before_write(client, make_session, menu, app, monkeypatch):
    sid = make_session(cap_fen=None)
    r = client.post(f"/api/j/{sid}/entries", json=body(menu)).json()
    url, h = f"/api/j/{sid}/entries/{r['id']}", {"X-Entry-Secret": r["secret"]}
    _lock_just_before(monkeypatch, app, sid, "update_entry")
    r2 = client.put(url, json=body(menu, name="小李", n=2), headers=h)
    assert r2.status_code == 409 and r2.json() == {"error": "closed"}
    assert app.state.store.list_entries(sid)[0]["name"] == "小王"
    app.state.store.transition(sid, {"locked"}, "open")
    _lock_just_before(monkeypatch, app, sid, "delete_entry")
    r3 = client.delete(url, headers=h)
    assert r3.status_code == 409 and r3.json() == {"error": "closed"}
    assert len(app.state.store.list_entries(sid)) == 1


def _node_eval(js_fn: str, calls: list[str]) -> list[str]:
    import json as _json
    import shutil
    import subprocess
    import pytest
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = js_fn + "\nconsole.log(JSON.stringify([" + ",".join(calls) + "]));"
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout)


def _fn(js: str, name: str) -> str:
    i = js.index(f"function {name}(")
    return js[i:js.index("\n  }\n", i) + 4]


@pytest.mark.parametrize("page", ["join.js", "host.js"])
def test_delivery_time_copy(page):
    """M10: 「预计 XX:XX 送达」 only for a clock time; text such as 立即送出 is 「配送：立即送出」."""
    js = (Path(__file__).resolve().parent.parent / "web" / page).read_text()
    args = ['"20:45"', '"2026-10-14 20:45:00"', '"2026-10-14T21:05"', '" 9:05 "', '"立即送出"',
            '"20:45-21:00"', '"明天"']
    out = _node_eval(_fn(js, "deliveryText"), [f"deliveryText({a})" for a in args])
    assert out == ["预计 20:45 送达", "预计 20:45 送达", "预计 21:05 送达", "预计 9:05 送达",
                   "配送：立即送出", "配送：20:45-21:00", "配送：明天"]
    assert "' 送达'" not in js.replace("'预计 ' + m[1] + ' 送达'", "")
    assert "预计送达：" not in js
