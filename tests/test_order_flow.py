"""Task 6: quote / order / refresh / uncertain / cancel / pay QR (the money path)."""
import copy
import dataclasses
import inspect
import json
import re
import threading
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.mcp_client import McdClient, McdError, McdTimeout, MockMcdClient, _check_tool
from app.money import fen_to_yuan, yuan_to_fen
from app.security import hash_secret

PRIVATE = ["pay_url", "payH5Url", "payId", "address_id", "addressId", "phone", "mobilePhone",
           "deliveryInfo", "ADDR-SECRET", "example.invalid/pay", "138****0000"]


class FakeClient(MockMcdClient):
    """MockMcdClient whose per-tool result can be preset: a dict (returned), an exception
    (raised) or a callable(args) (sync or async). Every call is recorded in .calls."""

    def __init__(self):
        super().__init__()
        self.preset: dict = {}

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    def args(self, tool) -> list[dict]:
        return [a for t, a in self.calls if t == tool]

    async def call(self, tool, args):
        p = self.preset.get(tool)
        if p is None:
            return await super().call(tool, args)
        _check_tool(tool)
        self.calls.append((tool, copy.deepcopy(args)))
        if isinstance(p, BaseException):
            raise p
        if callable(p):
            r = p(args)
            return await r if inspect.isawaitable(r) else r
        return copy.deepcopy(p)


@pytest.fixture
def mock_client():  # overrides conftest: app/client fixtures pick this up
    return FakeClient()


@pytest.fixture
def fake(mock_client):
    return mock_client


def codes(menu, n=2):
    return sorted(menu["items"], key=lambda c: (menu["items"][c]["price_fen"], c))[:n]


@pytest.fixture
def two_entries(menu):
    c1, c2 = codes(menu)
    return [("小王", [{"code": c1, "qty": 2}, {"code": c2, "qty": 1}]),
            ("小李", [{"code": c1, "qty": 1}])]


@pytest.fixture
def locked(app, make_session, two_entries):
    """A LOCKED session with two entries (c1 x3 total, c2 x1)."""
    def make(**kw):
        kw.setdefault("state", "locked")
        sid = make_session(**kw)
        for name, items in two_entries:
            app.state.store.add_entry(sid, name, items, "", hash_secret("s-" + name))
        return sid
    return make


def state(app, sid):
    return app.state.store.get_session(sid)["state"]


def p(sid, what):
    return f"/api/host/sessions/{sid}/{what}"


def quote_fen(client, sid):
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 200, r.text
    return r.json()["quote_fen"]


def place(client, sid, **kw):
    q = quote_fen(client, sid)
    body = {"quote_fen": q}
    body.update(kw)
    return client.post(p(sid, "order"), json=body), q


# ---------------- quote ----------------

def test_quote_only_when_locked(client, fake, make_session, app, locked):
    sid = make_session()  # OPEN, cutoff in the future
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 409 and r.json() == {"error": "bad_state"}
    for st in ("ordering", "uncertain", "ordered", "cancelled"):
        sid = locked(state=st)
        assert client.post(p(sid, "quote"), json={}).status_code == 409, st
    assert fake.calls == []
    assert client.post(p("nope", "quote"), json={}).status_code == 404


def test_quote_materializes_cutoff_lock(client, fake, locked, clock, app):
    sid = locked(state="open")
    assert client.post(p(sid, "quote"), json={}).status_code == 409  # still before cutoff
    assert state(app, sid) == "open"
    clock.advance(minutes=31)
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 200
    assert state(app, sid) == "locked"  # effective LOCKED was persisted
    assert fake.tools() == ["query-store-coupons", "calculate-price"]


def test_quote_aggregates_and_returns_fen(client, fake, locked, menu, app):
    sid = locked()
    c1, c2 = codes(menu)
    fake.preset["calculate-price"] = {"success": True, "data": {
        "productOriginalPrice": 5000, "productPrice": 4001, "deliveryOriginalPrice": 900,
        "deliveryPrice": 602, "packingOriginalPrice": 400, "packingPrice": 303,
        "originalPrice": 6300, "discount": 44, "price": 4862,
        "productList": [{"productCode": c1, "productName": "甲", "quantity": 3,
                         "originalSubtotal": 3100, "subtotal": 3000},
                        {"productCode": "920174", "productName": "打包服务费", "quantity": 4,
                         "originalSubtotal": 40, "subtotal": 0}]}}
    r = client.post(p(sid, "quote"), json={"need_tableware": False})
    assert r.status_code == 200
    items = [{"productCode": c, "quantity": q} for c, q in sorted([(c1, 3), (c2, 1)])]
    assert [c for c in fake.calls if c[0] != "query-store-coupons"] == [("calculate-price", {
        "storeCode": "S1", "beCode": "BE1", "beType": 2, "orderType": 2,
        "items": items, "needTableware": False})]
    j = r.json()
    assert j["quote_fen"] == 4862 and type(j["quote_fen"]) is int
    assert j["breakdown"] == {"product_fen": 4001, "delivery_fen": 602, "packing_fen": 303,
                              "discount_fen": 44, "original_fen": 6300}
    assert j["lines"][0] == {"code": c1, "name": "甲", "qty": 3, "subtotal_fen": 3000,
                             "original_fen": 3100}
    assert len(j["lines"]) == 2
    assert json.loads(app.state.store.get_session(sid)["quote_json"])["quote_fen"] == 4862
    assert client.get(f"/api/host/sessions/{sid}").json()["quote"]["quote_fen"] == 4862
    # default need_tableware is true, and an empty body is accepted
    fake.calls.clear()
    assert client.post(p(sid, "quote")).status_code == 200
    assert fake.args("calculate-price")[0]["needTableware"] is True


def test_quote_with_mock_matches_menu_prices(client, fake, locked, menu):
    sid = locked()
    c1, c2 = codes(menu)
    product = 3 * menu["items"][c1]["price_fen"] + menu["items"][c2]["price_fen"]
    j = client.post(p(sid, "quote"), json={}).json()
    assert j["breakdown"]["product_fen"] == product
    assert j["quote_fen"] == product + 600 + min(4 * 110, 900)


def test_quote_and_order_empty_422_without_mcp(client, fake, make_session, app):
    sid = make_session(state="locked")
    assert client.post(p(sid, "quote"), json={}).json() == {"error": "empty"}
    r = client.post(p(sid, "order"), json={"quote_fen": 0})
    assert r.status_code == 422 and r.json() == {"error": "empty"}
    assert fake.calls == [] and state(app, sid) == "locked"


@pytest.mark.parametrize("bad", [
    McdError("calculate-price", "x"), McdTimeout("calculate-price", "slow"),
    {"success": True, "data": {"price": "48.6"}}, {"success": True, "data": {}},
    {"success": True, "data": {"price": True}}])
def test_quote_mcp_failure_502(client, fake, locked, app, bad):
    sid = locked()
    fake.preset["calculate-price"] = bad
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 502 and r.json()["error"] == "mcd"
    assert state(app, sid) == "locked"


def test_quote_bad_body_422(client, fake, locked):
    sid = locked()
    assert client.post(p(sid, "quote"), json={"need_tableware": "yes"}).status_code == 422
    assert client.post(p(sid, "quote"), json=[1]).status_code == 422
    assert fake.calls == []


# ---------------- order ----------------

def test_order_happy_path_calls_price_then_create(client, fake, locked, menu, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()
    long_remark = "无接触配送" * 12  # 60 chars
    r = client.post(p(sid, "order"), json={"quote_fen": q, "remark": long_remark})
    assert r.status_code == 200, r.text
    assert fake.tools() == ["calculate-price", "create-order"]
    c1, c2 = codes(menu)
    items = [{"productCode": c, "quantity": n} for c, n in sorted([(c1, 3), (c2, 1)])]
    assert fake.args("calculate-price")[0] == {
        "storeCode": "S1", "beCode": "BE1", "beType": 2, "orderType": 2,
        "items": items, "needTableware": True}
    assert fake.args("create-order")[0] == {
        "addressId": "ADDR-SECRET", "storeCode": "S1", "beCode": "BE1", "beType": 2,
        "orderType": 2, "items": items, "needTableware": True, "remark": long_remark[:50]}
    j = r.json()
    assert j["order_id"].startswith("DEMO") and j["pay_url"] == "https://example.invalid/pay/demo"
    assert j["real_total_fen"] == q and j["mismatch"] is False
    s = app.state.store.get_session(sid)
    assert s["state"] == "ordered" and s["order_id"] == j["order_id"]
    assert s["pay_url"] == j["pay_url"] and s["order_status"] == "待支付"
    assert s["expect_delivery_time"] == "立即送出"
    h = client.get(f"/api/host/sessions/{sid}").json()
    assert h["pay_url"] == j["pay_url"] and h["order_id"] == j["order_id"]
    pub = client.get(f"/api/j/{sid}")
    assert pub.json()["state"] == "ordered" and pub.json()["order_status"] == "待支付"
    assert pub.json()["expect_delivery_time"] == "立即送出"
    for bad in PRIVATE + [j["pay_url"]]:
        assert bad not in pub.text, bad
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", pub.text)


def test_order_need_tableware_false_and_remark_default(client, fake, locked):
    sid = locked()
    q = quote_fen(client, sid)
    r = client.post(p(sid, "order"), json={"quote_fen": q, "need_tableware": False})
    assert r.status_code == 200
    assert fake.args("calculate-price")[-1]["needTableware"] is False
    co = fake.args("create-order")[0]
    assert co["needTableware"] is False and co["remark"] == ""


def test_order_price_changed_aborts_without_create(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q - 1})
    assert r.status_code == 409
    j = r.json()
    assert j["error"] == "price_changed" and j["quote"]["quote_fen"] == q
    assert set(j["quote"]) >= {"quote_fen", "breakdown", "lines"}
    assert fake.tools() == ["calculate-price"]
    assert state(app, sid) == "locked"
    # re-confirming with the new price goes through
    r = client.post(p(sid, "order"), json={"quote_fen": j["quote"]["quote_fen"]})
    assert r.status_code == 200 and state(app, sid) == "ordered"
    assert fake.tools().count("create-order") == 1


def test_order_double_submit_calls_create_once(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()
    # 1) first request still in flight (state ORDERING): second POST is rejected, no MCP
    app.state.store.update_session(sid, state="ordering")
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 409 and r.json() == {"error": "busy"}
    assert fake.calls == []
    app.state.store.update_session(sid, state="locked")

    # 2) two tabs POST at the same moment: exactly one create-order
    real_create = fake._load("create-order")
    real_create["data"]["orderDetail"]["realTotalAmount"] = fen_to_yuan(q)

    async def slow_create(args):  # keep the first request inside ORDERING for a while
        import asyncio
        await asyncio.sleep(0.3)
        return copy.deepcopy(real_create)
    fake.preset["create-order"] = slow_create
    gate = threading.Barrier(2)
    codes_seen = []

    def go():
        gate.wait()
        codes_seen.append(client.post(p(sid, "order"), json={"quote_fen": q}).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert sorted(codes_seen) == [200, 409]
    assert fake.tools().count("create-order") == 1
    assert state(app, sid) == "ordered"
    # 3) and once ORDERED, a third click is also refused without MCP
    n = len(fake.calls)
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 409
    assert len(fake.calls) == n


def test_order_timeout_goes_uncertain_and_blocks_reorder(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.preset["create-order"] = McdTimeout("create-order", "timeout: ReadTimeout")
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 202 and r.json() == {"error": "uncertain"}
    assert state(app, sid) == "uncertain"
    assert client.get(f"/api/j/{sid}").json()["state"] == "uncertain"
    n = len(fake.calls)
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 409 and len(fake.calls) == n  # no 2nd create-order, no MCP at all
    assert r.json() == {"error": "bad_state"}
    assert client.post(p(sid, "quote"), json={}).status_code == 409
    assert client.post(p(sid, "reopen")).status_code == 409

    r = client.post(p(sid, "recent-orders"))
    assert r.status_code == 200
    assert fake.calls[-1] == ("order-list", {})
    rows = r.json()
    assert rows[0] == {"order_id": "DEMO46221886", "create_time": "2026-10-14 11:26:28",
                       "store_name": "麦当劳上海陆家嘴前滩中心餐厅", "status_text": "订单已取消",
                       "real_total_fen": 114250}
    assert rows[1]["order_id"] == "DEMO99221016" and rows[1]["real_total_fen"] == 2210
    for k in ("orderProductList", "beCode", "storeCode"):
        assert k not in r.text

    r = client.post(p(sid, "adopt"), json={"order_id": "DEMO46221886"})
    assert r.status_code == 200
    assert state(app, sid) == "ordered"
    assert fake.calls[-1] == ("query-order", {"orderId": "DEMO46221886"})
    s = app.state.store.get_session(sid)
    assert s["order_id"] == "DEMO46221886" and s["order_status"] == "待支付"
    assert s["expect_delivery_time"] == "立即送出"
    assert not s["pay_url"]  # query-order has no payH5Url: host is told to pay in the App
    assert r.json()["pay_url"] in (None, "") and r.json()["raw_state"] == "ordered"
    assert client.get(p(sid, "pay-qr.svg")).status_code == 404
    assert fake.tools().count("create-order") == 1


def test_recent_orders_only_when_uncertain_and_at_most_5(client, fake, locked, app):
    for st in ("locked", "ordered", "cancelled", "ordering"):
        sid = locked(state=st)
        assert client.post(p(sid, "recent-orders")).status_code == 409, st
    assert fake.calls == []
    sid = locked(state="uncertain")
    base = fake._load("order-list")["data"]["list"][0]
    many = []
    for i in range(7):
        o = dict(base, orderId=f"O{i}", realTotalAmount="15.5")
        many.append(o)
    many[6]["realTotalAmount"] = None  # unparsable amounts must not break the list
    many[1]["realTotalAmount"] = "garbage"
    fake.preset["order-list"] = {"success": True, "data": {"list": many}}
    rows = client.post(p(sid, "recent-orders")).json()
    assert [x["order_id"] for x in rows] == ["O0", "O1", "O2", "O3", "O4"]
    assert rows[0]["real_total_fen"] == 1550 and rows[1]["real_total_fen"] is None
    fake.preset["order-list"] = McdError("order-list", "x")
    assert client.post(p(sid, "recent-orders")).status_code == 502
    assert state(app, sid) == "uncertain"


def test_adopt_and_not_placed_rules(client, fake, locked, app):
    sid = locked(state="locked")
    assert client.post(p(sid, "adopt"), json={"order_id": "O1"}).status_code == 409
    assert client.post(p(sid, "not-placed")).status_code == 409
    assert state(app, sid) == "locked" and fake.calls == []
    sid = locked(state="uncertain")
    for bad in ({}, {"order_id": ""}, {"order_id": 5}, {"order_id": "x" * 65}):
        assert client.post(p(sid, "adopt"), json=bad).status_code == 422, bad
    assert state(app, sid) == "uncertain" and fake.calls == []
    # adopt still lands in ORDERED when the follow-up query-order fails
    fake.preset["query-order"] = McdError("query-order", "x")
    r = client.post(p(sid, "adopt"), json={"order_id": "O9"})
    assert r.status_code == 200 and state(app, sid) == "ordered"
    assert app.state.store.get_session(sid)["order_id"] == "O9"
    # not-placed: only after 查最近订单 (I2), then UNCERTAIN -> LOCKED and ordering works again
    del fake.preset["query-order"]
    sid = locked(state="uncertain")
    assert client.post(p(sid, "recent-orders")).status_code == 200
    r = client.post(p(sid, "not-placed"))
    assert r.status_code == 200 and state(app, sid) == "locked"
    r, _ = place(client, sid)
    assert r.status_code == 200 and state(app, sid) == "ordered"


# ---------------- I2: 「没下成」 only after 「查最近订单」 (spec 5.2) ----------------

def test_not_placed_refused_until_recent_orders_checked(client, fake, locked, app):
    sid = locked(state="uncertain")
    r = client.post(p(sid, "not-placed"))
    assert r.status_code == 409 and r.json() == {"error": "check_recent_first"}
    assert state(app, sid) == "uncertain" and fake.calls == []
    h = client.get(f"/api/host/sessions/{sid}").json()
    assert h["recent_checked"] is False
    assert "recent_checked" not in client.get(f"/api/j/{sid}").json()  # host-only field
    assert client.post(p(sid, "recent-orders")).status_code == 200
    assert client.get(f"/api/host/sessions/{sid}").json()["recent_checked"] is True
    assert client.post(p(sid, "not-placed")).status_code == 200 and state(app, sid) == "locked"


@pytest.mark.parametrize("bad", [McdError("order-list", "x"), McdTimeout("order-list", "t"),
                                 {"success": True, "data": {"list": "nope"}}, {"success": True}])
def test_failed_recent_orders_attempt_still_counts_as_checked(client, fake, locked, app, bad):
    """A failing order-list leaves the host with 「去麦当劳 App 我的订单 确认」; the attempt counts."""
    sid = locked(state="uncertain")
    fake.preset["order-list"] = bad
    assert client.post(p(sid, "recent-orders")).status_code == 502
    assert client.get(f"/api/host/sessions/{sid}").json()["recent_checked"] is True
    assert client.post(p(sid, "not-placed")).status_code == 200


def test_recent_checked_resets_on_every_entry_into_uncertain(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.preset["create-order"] = McdTimeout("create-order", "timeout")
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 202
    assert client.post(p(sid, "recent-orders")).status_code == 200
    assert client.post(p(sid, "not-placed")).status_code == 200 and state(app, sid) == "locked"
    q = quote_fen(client, sid)
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 202  # uncertain again
    assert client.get(f"/api/host/sessions/{sid}").json()["recent_checked"] is False
    assert client.post(p(sid, "not-placed")).json() == {"error": "check_recent_first"}
    assert fake.tools().count("create-order") == 2


def test_startup_sweep_into_uncertain_resets_recent_checked(settings, clock, make_session, app):
    sid = make_session(state="ordering")
    app.state.store.update_session(sid, recent_checked=1)
    create_app(settings, MockMcdClient(), now=clock)
    s = app.state.store.get_session(sid)
    assert s["state"] == "uncertain" and s["recent_checked"] == 0


def test_stale_recent_orders_does_not_mark_a_later_uncertain(client, fake, locked, app):
    """Two tabs: a slow 查最近订单 from an earlier UNCERTAIN must not unlock a newer one."""
    sid = locked(state="uncertain")
    store = app.state.store
    base = fake._load("order-list")

    def meanwhile(args):  # the other tab: not-placed, re-order, timeout -> UNCERTAIN again
        store.transition(sid, {"uncertain"}, "locked")
        store.transition(sid, {"locked"}, "ordering")
        store.transition(sid, {"ordering"}, "uncertain")
        return copy.deepcopy(base)
    fake.preset["order-list"] = meanwhile
    assert client.post(p(sid, "recent-orders")).status_code == 200
    assert store.get_session(sid)["recent_checked"] == 0
    assert client.post(p(sid, "not-placed")).json() == {"error": "check_recent_first"}


def test_order_mcd_error_back_to_locked_502(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.preset["create-order"] = McdError("create-order", "库存不足")
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 502 and r.json() == {"error": "mcd", "message": "库存不足"}
    assert state(app, sid) == "locked"
    assert app.state.store.get_session(sid)["order_id"] is None


@pytest.mark.parametrize("bad", [
    McdError("calculate-price", "x"), McdTimeout("calculate-price", "slow"),
    RuntimeError("bug in price"), {"success": True, "data": {"price": "48.6"}},
    {"success": True, "data": {}}])
def test_order_precheck_failure_back_to_locked_without_create(client, fake, locked, app, bad):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()
    fake.preset["calculate-price"] = bad
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 502 and r.json()["error"] == "mcd"
    assert fake.tools() == ["calculate-price"]
    assert state(app, sid) == "locked"


@pytest.mark.parametrize("bad", [
    {"success": True, "data": {}},                      # no orderId
    {"success": True},                                  # no data
    {"success": True, "data": {"orderId": ""}},
    {"success": True, "data": {"orderId": True}},
    {"success": True, "data": ["O1"]},
    RuntimeError("bug after send"), ValueError("weird")])
def test_order_unexpected_failure_after_create_is_uncertain(client, fake, locked, app, bad):
    sid = locked()
    q = quote_fen(client, sid)
    fake.preset["create-order"] = bad
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 202 and r.json() == {"error": "uncertain"}
    assert state(app, sid) == "uncertain"
    assert app.state.store.get_session(sid)["order_id"] is None


@pytest.mark.parametrize("detail", [
    {"realTotalAmount": None, "orderStatus": "待支付"},
    {"realTotalAmount": "garbage", "orderStatus": "待支付"},
    {"orderStatus": "待支付"},
    None,  # no orderDetail at all
])
def test_order_created_without_amount_is_ordered_with_warning(client, fake, locked, app, detail):
    sid = locked()
    q = quote_fen(client, sid)
    data = {"orderId": "O-NOAMT", "payH5Url": "https://pay.example.invalid/pay?x=1"}
    if detail is not None:
        data["orderDetail"] = detail
    fake.preset["create-order"] = {"success": True, "data": data}
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 200
    j = r.json()
    assert j["order_id"] == "O-NOAMT" and j["pay_url"] == "https://pay.example.invalid/pay?x=1"
    assert j["real_total_fen"] is None and j["mismatch"] is True and j["warning"] == "amount_unknown"
    s = app.state.store.get_session(sid)
    assert s["state"] == "ordered" and s["order_id"] == "O-NOAMT"
    assert s["pay_url"] == "https://pay.example.invalid/pay?x=1"
    assert s["order_status"] == ("待支付" if detail else None)
    qj = json.loads(s["quote_json"])
    assert qj["real_total_fen"] is None and qj["mismatch"] is True
    assert "cancel-order" not in fake.tools()


def _rpc_sc(body, sc):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                     "result": {"isError": False, "structuredContent": sc}})


def _sse_resp(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


@pytest.mark.parametrize("create,expect_state,expect_http", [
    (lambda b: httpx.Response(504, text="gateway timeout"), "uncertain", 202),
    (lambda b: httpx.Response(500, text="oops"), "uncertain", 202),
    (lambda b: httpx.Response(200, text="<html>proxy</html>"), "uncertain", 202),
    (lambda b: httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"],
                                         "error": {"code": -32603, "message": "x"}}), "uncertain", 202),
    (lambda b: httpx.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "result": {
        "isError": True, "content": [{"type": "text", "text": "boom"}]}}), "uncertain", 202),
    (lambda b: _sse_resp("event: message\n\n"), "uncertain", 202),
    (lambda b: httpx.Response(400, text="bad"), "locked", 502),
    (lambda b: _rpc_sc(b, {"success": False, "message": "门店已打烊"}), "locked", 502),
    (lambda b: _rpc_sc(b, {"success": False, "message": "x", "data": {"orderId": "O1"}}),
     "uncertain", 202),
    ("connect", "locked", 502),
], ids=["504", "500", "html-200", "jsonrpc-error", "iserror-no-sc", "sse-no-data", "400",
        "success-false", "success-false-with-orderId", "connect-error"])
def test_order_through_real_client_classifies_outcome(settings, clock, menu, two_entries,
                                                     create, expect_state, expect_http):
    """R4/R11 end to end: only a provable non-execution of create-order goes back to LOCKED."""
    seen = []
    fixture = json.loads((Path(__file__).resolve().parent.parent / "app" / "mock_data" /
                          "calculate-price.json").read_text())

    def handler(req):
        body = json.loads(req.content)
        tool = body["params"]["name"]
        seen.append(tool)
        if tool == "create-order":
            if create == "connect":
                raise httpx.ConnectError("refused", request=req)
            return create(body)
        return _rpc_sc(body, fixture)
    # the real McdClient is only allowed in real mode (Task 8), which needs the host key
    real = dataclasses.replace(settings, mode="real", host_key="hk-test")
    app = create_app(real, McdClient("t", transport=httpx.MockTransport(handler)), now=clock)
    c = TestClient(app, headers={"X-Requested-With": "mcd-jielong"})
    c.cookies.set("hk", "hk-test")
    sid = app.state.store.create_session(
        sid="sid504", title="t", address_id="A", address_label="L", store_code="S1",
        be_code="BE1", store_name="店", cutoff_at=(clock() + timedelta(minutes=30)).isoformat(),
        cap_fen=None, menu_json=json.dumps(menu, ensure_ascii=False), state="locked",
        created_at=clock().isoformat())
    for name, items in two_entries:
        app.state.store.add_entry(sid, name, items, "", hash_secret(name))
    q = c.post(p(sid, "quote"), json={}).json()["quote_fen"]
    assert q == 4140
    r = c.post(p(sid, "order"), json={"quote_fen": q})
    assert seen == ["query-store-coupons", "calculate-price", "calculate-price", "create-order"]
    assert r.status_code == expect_http, r.text
    assert app.state.store.get_session(sid)["state"] == expect_state


@pytest.mark.parametrize("body,field", [
    ({}, "quote_fen"), ({"quote_fen": "4140"}, "quote_fen"), ({"quote_fen": 41.4}, "quote_fen"),
    ({"quote_fen": True}, "quote_fen"), ({"quote_fen": 1, "remark": 5}, "remark"),
    ({"quote_fen": 1, "need_tableware": "no"}, "need_tableware")])
def test_order_bad_body_422_no_state_change(client, fake, locked, app, body, field):
    sid = locked()
    r = client.post(p(sid, "order"), json=body)
    assert r.status_code == 422 and r.json() == {"error": field}
    assert fake.calls == [] and state(app, sid) == "locked"


@pytest.mark.parametrize("raw", [b"[1]", b'"x"', b"5", b"null", b"nope", b"{"])
def test_order_non_object_body_422(client, fake, locked, app, raw):
    sid = locked()
    r = client.post(p(sid, "order"), content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json() == {"error": "bad_request"}
    assert fake.calls == [] and state(app, sid) == "locked"


@pytest.mark.parametrize("st,code", [("open", "bad_state"), ("ordering", "busy"),
                                     ("uncertain", "bad_state"), ("ordered", "bad_state"),
                                     ("cancelled", "bad_state")])
def test_order_wrong_state_codes(client, fake, locked, app, st, code):
    sid = locked(state=st)
    r = client.post(p(sid, "order"), json={"quote_fen": 1})
    assert r.status_code == 409 and r.json() == {"error": code}
    assert fake.calls == [] and state(app, sid) == st


def test_order_requires_locked(client, fake, locked, app, clock):
    sid = locked(state="open")
    assert client.post(p(sid, "order"), json={"quote_fen": 1}).status_code == 409
    assert fake.calls == [] and state(app, sid) == "open"
    clock.advance(minutes=31)  # cutoff passed: effective LOCKED gets persisted, order allowed
    q = client.post(p(sid, "quote"), json={}).json()["quote_fen"]
    clock.advance(minutes=1)
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 200
    assert client.post(p("nope", "order"), json={"quote_fen": 1}).status_code == 404


def test_order_direct_after_cutoff_materializes(client, fake, locked, app, clock):
    sid = locked(state="open")
    clock.advance(minutes=31)
    r = client.post(p(sid, "order"), json={"quote_fen": 1})
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert state(app, sid) == "locked"


def test_order_amount_mismatch_flags_only(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    co = fake._load("create-order")
    co["data"]["orderDetail"]["realTotalAmount"] = fen_to_yuan(q + 150)
    fake.preset["create-order"] = co
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 200
    j = r.json()
    assert j["mismatch"] is True and j["real_total_fen"] == q + 150
    assert "cancel-order" not in fake.tools()
    assert state(app, sid) == "ordered"
    h = client.get(f"/api/host/sessions/{sid}").json()
    assert h["quote"]["real_total_fen"] == q + 150 and h["quote"]["mismatch"] is True


class Boom(BaseException):
    """Not an Exception: stands in for task cancellation / interpreter shutdown."""


def test_order_base_exception_after_create_still_uncertain(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.preset["create-order"] = Boom()
    with pytest.raises(BaseException):
        client.post(p(sid, "order"), json={"quote_fen": q})
    assert state(app, sid) == "uncertain"


def test_order_unexpected_failure_before_create_back_to_locked(client, fake, locked, app,
                                                               monkeypatch):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()

    def broken(_sid):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(app.state.store, "list_entries", broken)
    with pytest.raises(RuntimeError):
        client.post(p(sid, "order"), json={"quote_fen": q})
    assert state(app, sid) == "locked" and fake.calls == []


def test_quote_stores_quoted_items(client, fake, locked, app, menu):
    sid = locked()
    quote_fen(client, sid)
    c1, c2 = codes(menu)
    qj = json.loads(app.state.store.get_session(sid)["quote_json"])
    assert qj["items"] == [{"productCode": c, "quantity": n} for c, n in sorted([(c1, 3), (c2, 1)])]


def _same_price_pair(menu):
    by_price = {}
    for c in sorted(menu["items"]):
        by_price.setdefault(menu["items"][c]["price_fen"], []).append(c)
    return next(v[:2] for _, v in sorted(by_price.items()) if len(v) >= 2)


def test_order_items_changed_since_quote_aborts(client, fake, make_session, app, menu):
    """R12: same total, different items -> no create-order, fresh quote returned."""
    a, b = _same_price_pair(menu)
    sid = make_session(state="locked")
    store = app.state.store
    store.add_entry(sid, "小王", [{"code": a, "qty": 1}], "", hash_secret("w"))
    q = quote_fen(client, sid)
    # entries change behind the quote (e.g. reopen, edit, lock again) with an identical total
    store.transition(sid, {"locked"}, "open")
    assert store.update_entry(1, hash_secret("w"), items=[{"code": b, "qty": 1}])
    store.transition(sid, {"open"}, "locked")
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert r.json()["quote"]["quote_fen"] == q  # the amount did not change, the items did
    assert fake.tools() == ["calculate-price"] and state(app, sid) == "locked"
    assert json.loads(store.get_session(sid)["quote_json"])["items"] == [
        {"productCode": b, "quantity": 1}]
    r = client.post(p(sid, "order"), json={"quote_fen": q})  # re-confirmed against the new items
    assert r.status_code == 200 and fake.args("create-order")[0]["items"] == [
        {"productCode": b, "quantity": 1}]


def test_order_without_stored_quote_aborts(client, fake, locked, app, menu):
    sid = locked()
    c1, c2 = codes(menu)
    exact = 3 * menu["items"][c1]["price_fen"] + menu["items"][c2]["price_fen"] + 600 + 440
    r = client.post(p(sid, "order"), json={"quote_fen": exact})  # right amount, never quoted
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert r.json()["quote"]["quote_fen"] == exact
    assert fake.tools() == ["calculate-price"] and state(app, sid) == "locked"
    assert client.post(p(sid, "order"), json={"quote_fen": exact}).status_code == 200


def test_reopen_clears_quote(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    assert app.state.store.get_session(sid)["quote_json"]
    assert client.post(p(sid, "reopen")).status_code == 200
    assert app.state.store.get_session(sid)["quote_json"] is None
    assert client.get(f"/api/host/sessions/{sid}").json()["quote"] is None
    assert client.post(p(sid, "lock")).status_code == 200
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert "create-order" not in fake.tools()


@pytest.mark.parametrize("winner", ["ordering", "ordered"])
def test_late_quote_does_not_touch_quote_json(client, fake, locked, app, winner):
    sid = locked()
    store = app.state.store
    sentinel = json.dumps({"quote_fen": 1, "items": [], "real_total_fen": 1})
    real = fake._load("calculate-price")

    def meanwhile(args):  # /order wins while this /quote waits for calculate-price
        store.update_session(sid, state=winner, quote_json=sentinel)
        return copy.deepcopy(real)
    fake.preset["calculate-price"] = meanwhile
    assert client.post(p(sid, "quote"), json={}).status_code == 200
    s = store.get_session(sid)
    assert s["quote_json"] == sentinel and s["state"] == winner


def test_startup_sweeps_ordering_to_uncertain(settings, clock, make_session, app):
    """A crash during create-order must not leave a session in ORDERING forever."""
    stuck = make_session(state="ordering")
    others = {st: make_session(state=st) for st in ("open", "locked", "uncertain", "ordered")}
    create_app(settings, MockMcdClient(), now=clock)  # restart on the same database
    store = app.state.store
    assert store.get_session(stuck)["state"] == "uncertain"
    for st, sid in others.items():
        assert store.get_session(sid)["state"] == st


# ---------------- refresh ----------------

def _order(client, sid):
    r, _ = place(client, sid)
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def test_refresh_updates_status_shown_to_group(client, fake, locked, app):
    sid = locked()
    assert client.post(p(sid, "refresh")).status_code == 409  # LOCKED: not allowed
    assert fake.calls == []
    oid = _order(client, sid)
    qo = fake._load("query-order")
    qo["data"].update(orderStatus="制作中", status="30")
    qo["data"]["deliveryInfo"]["expectDeliveryTime"] = "20:45"
    fake.preset["query-order"] = qo
    r = client.post(p(sid, "refresh"))
    assert r.status_code == 200
    assert fake.calls[-1] == ("query-order", {"orderId": oid})
    assert r.json()["order_status"] == "制作中"
    pub = client.get(f"/api/j/{sid}")
    assert pub.json()["order_status"] == "制作中" and pub.json()["expect_delivery_time"] == "20:45"
    assert "mobilePhone" not in pub.text and "deliveryInfo" not in pub.text
    fake.preset["query-order"] = McdError("query-order", "x")
    assert client.post(p(sid, "refresh")).status_code == 502
    assert client.get(f"/api/j/{sid}").json()["order_status"] == "制作中"
    assert state(app, sid) == "ordered"


def test_refresh_status_60_marks_cancelled(client, fake, locked, app):
    """R13: cancelled elsewhere (e.g. in the McD App) -> CANCELLED here too."""
    sid = locked()
    _order(client, sid)
    qo = fake._load("query-order")
    qo["data"].update(status="60", orderStatus="订单已取消")
    fake.preset["query-order"] = qo
    r = client.post(p(sid, "refresh"))
    assert r.status_code == 200 and r.json()["raw_state"] == "cancelled"
    assert state(app, sid) == "cancelled"
    assert client.get(f"/api/j/{sid}").json()["state"] == "cancelled"
    assert client.get(p(sid, "pay-qr.svg")).status_code == 404
    assert client.post(p(sid, "cancel"), json={"reason_code": "1"}).status_code == 409
    assert client.post(p(sid, "refresh")).status_code == 200  # still allowed in CANCELLED
    assert state(app, sid) == "cancelled"


@pytest.mark.parametrize("st", ["ordered", "cancelled"])
def test_refresh_never_writes_pay_url(client, fake, locked, app, st):
    sid = locked(state=st, order_id="O1", pay_url="https://pay.example.invalid/pay/real")
    qo = fake._load("query-order")
    qo["data"]["payH5Url"] = "https://evil.example/pay"
    qo["data"]["status"] = "10"
    fake.preset["query-order"] = qo
    assert client.post(p(sid, "refresh")).status_code == 200
    assert app.state.store.get_session(sid)["pay_url"] == "https://pay.example.invalid/pay/real"


def test_refresh_states(client, fake, locked):
    for st in ("open", "locked", "ordering", "uncertain"):
        sid = locked(state=st)
        assert client.post(p(sid, "refresh")).status_code == 409, st
    assert fake.calls == []
    sid = locked(state="cancelled", order_id="O1")
    assert client.post(p(sid, "refresh")).status_code == 200
    assert fake.calls == [("query-order", {"orderId": "O1"})]


# ---------------- cancel ----------------

def test_cancel_requires_valid_reason_and_ordered_state(client, fake, locked, app):
    sid = locked()
    assert client.post(p(sid, "cancel"), json={"reason_code": "3"}).status_code == 409  # LOCKED
    oid = _order(client, sid)
    n = len(fake.calls)
    for bad in ({}, {"reason_code": 3}, {"reason_code": "6"}, {"reason_code": ""},
                {"reason_code": "-2"}, {"reason_code": None}):
        r = client.post(p(sid, "cancel"), json=bad)
        assert r.status_code == 422 and r.json() == {"error": "reason_code"}, bad
    assert len(fake.calls) == n and state(app, sid) == "ordered"

    fake.preset["cancel-order"] = {"success": True, "data": {"cancelResult": False, "orderId": oid}}
    r = client.post(p(sid, "cancel"), json={"reason_code": "3"})
    assert r.status_code == 502 and state(app, sid) == "ordered"
    fake.preset["cancel-order"] = McdError("cancel-order", "x")
    assert client.post(p(sid, "cancel"), json={"reason_code": "3"}).status_code == 502
    assert state(app, sid) == "ordered"
    del fake.preset["cancel-order"]

    fake.calls.clear()
    r = client.post(p(sid, "cancel"), json={"reason_code": "-1"})
    assert r.status_code == 200
    assert fake.calls == [("cancel-order", {"orderId": oid, "cancelReasonCode": "-1"}),
                          ("query-order", {"orderId": oid})]
    assert state(app, sid) == "cancelled"
    assert app.state.store.get_session(sid)["order_status"] == "订单已取消"
    assert client.get(f"/api/j/{sid}").json()["order_status"] == "订单已取消"
    assert client.post(p(sid, "cancel"), json={"reason_code": "3"}).status_code == 409
    assert client.post(p(sid, "order"), json={"quote_fen": 1}).status_code == 409


@pytest.mark.parametrize("code", ["1", "2", "3", "4", "5", "-1"])
def test_cancel_accepts_each_reason(client, fake, locked, code):
    sid = locked(state="ordered", order_id="O7")
    assert client.post(p(sid, "cancel"), json={"reason_code": code}).status_code == 200
    assert fake.args("cancel-order") == [{"orderId": "O7", "cancelReasonCode": code}]


# ---------------- pay QR ----------------

def test_pay_qr_svg(client, fake, locked, app):
    sid = locked()
    assert client.get(p(sid, "pay-qr.svg")).status_code == 404  # no pay_url yet
    _order(client, sid)
    r = client.get(p(sid, "pay-qr.svg"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text
    import segno
    assert r.text == _svg(segno, "https://example.invalid/pay/demo")
    sid2 = locked(state="ordered", pay_url="https://pay.example.invalid/pay?id=2")
    assert client.get(p(sid2, "pay-qr.svg")).text != r.text
    sid3 = locked(state="ordered", pay_url="javascript:alert(1)")
    assert client.get(p(sid3, "pay-qr.svg")).status_code == 404  # only https links get a QR
    assert client.post(p(sid, "cancel"), json={"reason_code": "3"}).status_code == 200
    assert app.state.store.get_session(sid)["pay_url"]  # still stored, but nothing to pay any more
    assert client.get(p(sid, "pay-qr.svg")).status_code == 404
    assert client.get(p("nope", "pay-qr.svg")).status_code == 404


def _svg(segno, url):
    import io
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=2)
    return buf.getvalue().decode()


# ---------------- guard / frontend ----------------

def test_order_routes_guarded_in_real_mode(tmp_path, clock):
    fc = FakeClient()
    s = Settings(mode="real", db_path=str(tmp_path / "r.db"),
                 public_base_url="https://mcd.example", host_key="k")
    c = TestClient(create_app(s, fc, now=clock), headers={"X-Requested-With": "mcd-jielong"})
    for m, path in [("post", "quote"), ("post", "order"), ("post", "refresh"),
                    ("post", "recent-orders"), ("post", "adopt"), ("post", "not-placed"),
                    ("post", "cancel"), ("get", "pay-qr.svg")]:
        assert getattr(c, m)(p("x", path)).status_code == 401, path
        assert getattr(c, m)(p("x", path), headers={"X-Host-Key": "k"}).status_code in (404, 422)
    assert fc.calls == []


def test_host_js_order_ui_rules():
    js = (Path(__file__).resolve().parent.parent / "web" / "host.js").read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert bad not in js, bad
    assert "pay-qr.svg" in js and "confirm(" in js
    assert js.count("payLink.href") == 1
    i = js.index("payLink.href")
    g = js.rfind("indexOf('https://') === 0", 0, i)
    assert g != -1 and i - g < 120  # href is set only behind the https check
    assert js.count("setInterval(") == 1 and "setInterval(refresh, 5000)" in js
    # the MCP-backed /refresh is only fired from the 刷新状态 button, never from the poll
    assert js.count("'refresh'") == 1
    i = js.index("'refresh'")
    assert 0 < i - js.rfind("function doRefreshStatus", 0, i) < 80
    assert js.count("doRefreshStatus") == 2 and "'刷新状态', doRefreshStatus)" in js
    assert "去麦当劳 App 支付" in js
    # 「没下成，回到截单」 is shown in UNCERTAIN before (not only after) the list is loaded, but
    # stays disabled with the App hint until the server says 查最近订单 was attempted (I2)
    u = js[js.index("function uncertainView"):js.index("function orderedView")]
    assert u.index("'没下成，回到截单'") < u.index("if (ord.recent)")
    assert "return;" not in u.split("'没下成，回到截单'")[0].split("'查最近订单'")[1]
    assert "!data.recent_checked" in u and "np.disabled = true" in u
    assert "先点「查最近订单」；查不到也请去麦当劳 App「我的订单」确认" in u
    assert "data.recent_checked" in js[js.index("function renderOrder"):]
    assert "check_recent_first" in js
    # a used confirmation is dropped on every /order outcome, and on network failure
    o = js[js.index("function doOrder"):js.index("function doRecent")]
    cb = o[o.index("function (res) {"):]
    assert cb.index("ord.quote = null") < cb.index("if (res.status === 200")
    assert "if (which === 'order') ord.quote = null;" in js
    assert "http://" not in js and "//cdn" not in js


# ---------------- coupons (best effort, Task 7) ----------------

COUPON = {"couponId": "C1", "couponCode": "CODE1", "couponName": "满30减5"}


def _coupons(fake, data):
    fake.preset["query-store-coupons"] = {"success": True, "code": 200, "data": data}


def test_quote_lists_coupons_empty_ok(client, fake, locked):
    sid = locked()
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 200
    assert r.json()["coupons"] == [] and r.json()["coupon"] is None
    assert fake.tools() == ["query-store-coupons", "calculate-price"]
    assert fake.args("query-store-coupons")[0]["storeCode"] == "S1"


@pytest.mark.parametrize("data", [
    None, "x", 5, [None, 3, "a", {}, {"name": "无码"}], {"a": 1}, {"a": "s"}, [[{"id": "x"}]]])
def test_quote_coupons_garbage_gives_empty(client, fake, locked, data):
    sid = locked()
    _coupons(fake, data)
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 200 and r.json()["coupons"] == []


@pytest.mark.parametrize("exc", [McdError("query-store-coupons", "boom"), McdTimeout("query-store-coupons", "t")])
def test_quote_coupon_query_failure_still_quotes(client, fake, locked, exc):
    sid = locked()
    fake.preset["query-store-coupons"] = exc
    r = client.post(p(sid, "quote"), json={})
    assert r.status_code == 200 and r.json()["coupons"] == []
    assert "calculate-price" in fake.tools()


def test_quote_calculate_price_error_still_502_with_coupons(client, fake, locked):
    sid = locked()
    _coupons(fake, [COUPON])
    fake.preset["calculate-price"] = McdError("calculate-price", "boom")
    assert client.post(p(sid, "quote"), json={}).status_code == 502


def test_parse_coupons_shapes():
    from app.host_api import parse_coupons
    alt = {"id": 7, "code": "K", "title": "T"}
    assert parse_coupons([COUPON, alt, {"couponCode": "only"}, {"couponId": "i", "name": "N"}]) == [
        {"id": "C1", "code": "CODE1", "name": "满30减5"},
        {"id": "7", "code": "K", "name": "T"},
        {"id": None, "code": "only", "name": "优惠券"},
        {"id": "i", "code": None, "name": "N"}]
    assert parse_coupons({"coupons": [COUPON]}) == [{"id": "C1", "code": "CODE1", "name": "满30减5"}]


def test_coupon_attached_to_first_line(client, fake, locked, app, menu):
    sid = locked()
    _coupons(fake, [COUPON])
    r = client.post(p(sid, "quote"), json={"coupon": {"id": "C1", "code": "CODE1"}})
    assert r.status_code == 200
    assert r.json()["coupons"] == [{"id": "C1", "code": "CODE1", "name": "满30减5"}]
    assert r.json()["coupon"] == {"id": "C1", "code": "CODE1"}
    items = fake.args("calculate-price")[0]["items"]
    assert items[0]["couponId"] == "C1" and items[0]["couponCode"] == "CODE1"
    assert all("couponId" not in x and "couponCode" not in x for x in items[1:]) and len(items) == 2
    qj = json.loads(app.state.store.get_session(sid)["quote_json"])
    assert qj["items"] == items and qj["coupon"] == {"id": "C1", "code": "CODE1"}


@pytest.mark.parametrize("bad", [
    {"id": "NOPE", "code": "CODE1"}, {"id": "C1", "code": "WRONG"}, {"id": "C1"}, {},
    {"id": "C" * 65, "code": "CODE1"}, {"id": 1, "code": "CODE1"}, "C1", ["C1"], 5,
    {"id": None, "code": None}])
def test_quote_bad_coupon_422_without_price(client, fake, locked, bad):
    sid = locked()
    _coupons(fake, [COUPON])
    r = client.post(p(sid, "quote"), json={"coupon": bad})
    assert r.status_code == 422 and r.json() == {"error": "coupon"}
    assert "calculate-price" not in fake.tools()


def test_quote_coupon_when_list_empty_422(client, fake, locked):
    sid = locked()
    r = client.post(p(sid, "quote"), json={"coupon": {"id": "C1", "code": "CODE1"}})
    assert r.status_code == 422 and "calculate-price" not in fake.tools()


def _quoted_with_coupon(client, fake, sid):
    _coupons(fake, [COUPON])
    return client.post(p(sid, "quote"), json={"coupon": {"id": "C1", "code": "CODE1"}}).json()["quote_fen"]


def test_order_uses_stored_coupon(client, fake, locked, app):
    sid = locked()
    q = _quoted_with_coupon(client, fake, sid)
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q})  # no coupon in body: stored one rules
    assert r.status_code == 200, r.text
    for tool in ("calculate-price", "create-order"):
        it = fake.args(tool)[0]["items"]
        assert it[0]["couponId"] == "C1" and it[0]["couponCode"] == "CODE1"
    qj = json.loads(app.state.store.get_session(sid)["quote_json"])
    assert qj["coupon"] == {"id": "C1", "code": "CODE1"} and qj["coupons"][0]["id"] == "C1"
    assert state(app, sid) == "ordered"


def test_order_same_coupon_in_body_ok(client, fake, locked):
    sid = locked()
    q = _quoted_with_coupon(client, fake, sid)
    r = client.post(p(sid, "order"), json={"quote_fen": q, "coupon": {"id": "C1", "code": "CODE1"}})
    assert r.status_code == 200


@pytest.mark.parametrize("body_coupon", [None, {"id": "C2", "code": "X"}, "C1", {"id": "C1"}])
def test_order_coupon_differs_from_stored_409(client, fake, locked, app, body_coupon):
    sid = locked()
    q = _quoted_with_coupon(client, fake, sid)
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q, "coupon": body_coupon})
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert "create-order" not in fake.tools() and state(app, sid) == "locked"


def test_order_coupon_in_body_but_quote_had_none_409(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q, "coupon": {"id": "C1", "code": "CODE1"}})
    assert r.status_code == 409 and "create-order" not in fake.tools()


def test_order_quoted_without_coupon_sends_none(client, fake, locked):
    sid = locked()
    q = quote_fen(client, sid)
    assert client.post(p(sid, "order"), json={"quote_fen": q, "coupon": None}).status_code == 200
    assert all("couponId" not in x for x in fake.args("create-order")[0]["items"])


def test_order_double_submit_with_coupon_calls_create_once(client, fake, locked):
    sid = locked()
    q = _quoted_with_coupon(client, fake, sid)
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 200
    assert client.post(p(sid, "order"), json={"quote_fen": q}).status_code == 409
    assert fake.tools().count("create-order") == 1


# ---------------- I5: the host can delete a member entry ----------------

def _eids(app, sid):
    return [e["id"] for e in app.state.store.list_entries(sid)]


def de(sid, eid):
    return f"/api/host/sessions/{sid}/entries/{eid}"


@pytest.mark.parametrize("st", ["open", "locked"])
def test_host_delete_entry_in_open_or_locked(client, fake, locked, app, st):
    sid = locked(state=st)
    app.state.store.update_session(sid, quote_json=json.dumps({"quote_fen": 1, "items": []}))
    first, second = _eids(app, sid)
    r = client.delete(de(sid, first))
    assert r.status_code == 200 and r.json()["count"] == 1 and r.json()["quote"] is None
    assert _eids(app, sid) == [second]
    row = app.state.store._db.execute("SELECT deleted FROM entries WHERE id=?", (first,)).fetchone()
    assert row[0] == 1  # soft delete
    assert app.state.store.get_session(sid)["quote_json"] is None
    assert client.get(f"/api/j/{sid}").json()["count"] == 1
    assert fake.calls == [] and state(app, sid) == st


def test_host_delete_uses_stored_state_even_after_cutoff(client, fake, locked, app, clock):
    sid = locked(state="open")
    clock.advance(minutes=31)  # effectively LOCKED, stored OPEN: still deletable
    assert client.delete(de(sid, _eids(app, sid)[0])).status_code == 200
    assert fake.calls == []


@pytest.mark.parametrize("st", ["ordering", "uncertain", "ordered", "cancelled"])
def test_host_delete_refused_once_ordering_started(client, fake, locked, app, st):
    sid = locked(state=st)
    before = _eids(app, sid)
    r = client.delete(de(sid, before[0]))
    assert r.status_code == 409 and r.json() == {"error": "bad_state"}
    assert _eids(app, sid) == before and fake.calls == []


def test_host_delete_entry_must_belong_to_session(client, fake, locked, app):
    sid, other = locked(), locked()
    theirs = _eids(app, other)[0]
    assert client.delete(de(sid, theirs)).json() == {"error": "not_found"}
    assert client.delete(de(sid, 99999)).status_code == 404
    assert client.delete(de("nope", theirs)).status_code == 404
    assert theirs in _eids(app, other)
    mine = _eids(app, sid)[0]
    assert client.delete(de(sid, mine)).status_code == 200
    assert client.delete(de(sid, mine)).status_code == 404  # already gone
    assert fake.calls == []


def test_host_delete_then_order_without_requote_is_price_changed(client, fake, locked, app):
    sid = locked()
    q = quote_fen(client, sid)
    assert client.delete(de(sid, _eids(app, sid)[0])).status_code == 200
    fake.calls.clear()
    r = client.post(p(sid, "order"), json={"quote_fen": q})
    assert r.status_code == 409 and r.json()["error"] == "price_changed"
    assert "create-order" not in fake.tools() and state(app, sid) == "locked"
    q2 = quote_fen(client, sid)  # a fresh 算价 for the remaining entry, then it goes through
    assert q2 < q
    assert client.post(p(sid, "order"), json={"quote_fen": q2}).status_code == 200
    assert fake.tools().count("create-order") == 1


def test_host_delete_conditional_on_state_at_write_time(client, fake, locked, app, monkeypatch):
    """An /order that wins ORDERING between the handler's check and the write blocks the delete."""
    sid = locked()
    store = app.state.store
    orig = store.host_delete_entry

    def late(*a, **kw):
        store.transition(sid, {"locked"}, "ordering")
        return orig(*a, **kw)
    monkeypatch.setattr(store, "host_delete_entry", late)
    before = _eids(app, sid)
    assert client.delete(de(sid, before[0])).json() == {"error": "bad_state"}
    assert _eids(app, sid) == before


def test_host_js_has_entry_delete_with_confirm():
    js = (Path(__file__).resolve().parent.parent / "web" / "host.js").read_text()
    lst = js[js.index("function renderList"):js.index("// ---------------- order section")]
    assert "'删除'" in lst and "confirm(" in lst and "'DELETE'" in lst
    assert "raw_state === 'open' || data.raw_state === 'locked'" in lst
