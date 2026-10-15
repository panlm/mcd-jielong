import json

import httpx
import pytest

from app.mcp_client import ALLOWED_TOOLS, McdClient, McdError, McdTimeout, MockMcdClient

TOKEN = "not-a-real-token"


def make_client(handler):
    return McdClient(TOKEN, transport=httpx.MockTransport(handler))


def rpc(structured=None, is_error=False):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": "x"}], "isError": is_error, "structuredContent": structured}})


async def test_call_rejects_tool_not_in_whitelist():
    sent = []

    def handler(req):
        sent.append(req)
        return rpc({"success": True})

    with pytest.raises(McdError):
        await make_client(handler).call("auto-bind-coupons", {})
    assert sent == []


def test_allowed_tools_is_the_nine():
    assert ALLOWED_TOOLS == frozenset({
        "delivery-query-addresses", "delivery-query-stores", "query-meals", "calculate-price",
        "query-store-coupons", "create-order", "query-order", "order-list", "cancel-order"})


async def test_call_sends_jsonrpc_and_headers():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["headers"] = req.headers
        return rpc({"success": True, "data": {}})

    await make_client(handler).call("query-meals", {"storeCode": "1"})
    assert seen["body"]["jsonrpc"] == "2.0"
    assert seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"] == {"name": "query-meals", "arguments": {"storeCode": "1"}}
    assert seen["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert "text/event-stream" in seen["headers"]["accept"]
    assert seen["headers"]["content-type"].startswith("application/json")


async def test_call_returns_structured_content():
    sc = {"success": True, "code": 200, "data": {"a": 1}}
    assert await make_client(lambda r: rpc(sc)).call("order-list", {}) == sc


async def test_call_raises_on_iserror():
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc({"message": "boom"}, is_error=True)).call("order-list", {})
    assert ei.value.tool == "order-list"


async def test_call_raises_on_success_false():
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc({"success": False, "message": "库存不足"})).call("create-order", {})
    assert ei.value.tool == "create-order"
    assert "库存不足" in ei.value.message


async def test_timeout_raises_mcdtimeout():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(McdTimeout) as ei:
        await make_client(handler).call("create-order", {})
    assert isinstance(ei.value, McdError)


async def test_dropped_connection_after_send_is_mcdtimeout():
    def handler(req):
        raise httpx.RemoteProtocolError("dropped", request=req)

    with pytest.raises(McdTimeout):
        await make_client(handler).call("create-order", {})


async def test_connect_error_is_plain_mcderror_not_timeout():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(McdError) as ei:
        await make_client(handler).call("create-order", {})
    assert not isinstance(ei.value, McdTimeout)


async def test_http_error_status_raises_mcderror():
    with pytest.raises(McdError):
        await make_client(lambda r: httpx.Response(500, text="oops")).call("order-list", {})


async def test_sse_response_body_is_parsed():
    sc = {"success": True, "data": {"k": 1}}
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"isError": False, "structuredContent": sc}}
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    resp = httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    assert await make_client(lambda r: resp).call("order-list", {}) == sc


async def test_token_not_in_error_message():
    def handler(req):
        raise httpx.ConnectError(f"failed with {TOKEN}", request=req)

    with pytest.raises(McdError) as ei:
        await make_client(handler).call("order-list", {})
    assert TOKEN not in ei.value.message and TOKEN not in str(ei.value)

    with pytest.raises(McdError) as ei:
        await make_client(lambda r: httpx.Response(401, text=f"bad {TOKEN}")).call("order-list", {})
    assert TOKEN not in ei.value.message and TOKEN not in str(ei.value)

    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc({"success": False, "message": f"x {TOKEN}"})).call("order-list", {})
    assert TOKEN not in ei.value.message


async def test_mock_client_returns_fixture_and_records_calls():
    m = MockMcdClient()
    r = await m.call("delivery-query-addresses", {"a": 1})
    assert r["success"] is True
    assert r["data"]["addresses"][0]["addressId"] == "demo-addr-1"
    assert m.calls == [("delivery-query-addresses", {"a": 1})]


async def test_mock_client_rejects_unlisted_tool():
    with pytest.raises(McdError):
        await MockMcdClient().call("auto-bind-coupons", {})


async def _first_two_codes(m):
    meals = (await m.call("query-meals", {}))["data"]["meals"]
    return [c for c, v in meals.items() if "withOrder" not in v][:2], meals


async def test_mock_calculate_price_follows_items():
    m = MockMcdClient()
    (c1, c2), meals = await _first_two_codes(m)
    from app.money import yuan_to_fen
    p = lambda c: yuan_to_fen(meals[c]["currentPrice"])
    d = (await m.call("calculate-price", {"items": [
        {"productCode": c1, "quantity": 2}, {"productCode": c2, "quantity": 1}]}))["data"]
    assert d["productPrice"] == 2 * p(c1) + p(c2)
    assert d["deliveryPrice"] == 600
    assert d["packingPrice"] == min(3 * 110, 900)
    assert d["discount"] == 0
    assert d["price"] == d["productPrice"] + d["deliveryPrice"] + d["packingPrice"]
    big = (await m.call("calculate-price", {"items": [{"productCode": c1, "quantity": 20}]}))["data"]
    assert big["packingPrice"] == 900


async def test_mock_create_order_matches_price_and_query_order_progresses():
    from app.money import fen_to_yuan
    m = MockMcdClient()
    (c1, c2), _ = await _first_two_codes(m)
    items = [{"productCode": c1, "quantity": 3}, {"productCode": c2, "quantity": 1}]
    price = (await m.call("calculate-price", {"items": items}))["data"]["price"]
    o = (await m.call("create-order", {"items": items}))["data"]
    assert o["orderDetail"]["realTotalAmount"] == fen_to_yuan(price)
    assert o["orderId"].startswith("DEMO") and o["orderDetail"]["orderId"] == o["orderId"]
    q1 = (await m.call("query-order", {"orderId": o["orderId"]}))["data"]
    q2 = (await m.call("query-order", {"orderId": o["orderId"]}))["data"]
    assert (q1["status"], q1["orderStatus"]) == ("10", "待支付")
    assert q2["orderStatus"] == "已送达（演示）"


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("tool", ["create-order", "calculate-price", "order-list"])
async def test_gateway_5xx_is_mcdtimeout_for_all_tools(status, tool):
    # a gateway timeout may hide a request the upstream already executed: outcome unknown
    with pytest.raises(McdTimeout) as ei:
        await make_client(lambda r: httpx.Response(status, text="bad gateway")).call(tool, {})
    assert ei.value.tool == tool and str(status) in ei.value.message


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 499])
async def test_4xx_is_plain_mcderror(status):
    # the server refused the request: it definitely did not execute
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: httpx.Response(status, text="no")).call("create-order", {})
    assert not isinstance(ei.value, McdTimeout)


# ---- R11: anything that does not prove "not executed" is outcome-unknown (McdTimeout) ----

def _unknown(exc):
    return isinstance(exc, McdTimeout) and isinstance(exc, McdError)


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 599, 302, 304])
async def test_5xx_and_non_2xx_are_unknown(status):
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: httpx.Response(status, text="x")).call("create-order", {})
    assert _unknown(ei.value) and str(status) in ei.value.message


@pytest.mark.parametrize("resp", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, text=""),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={}),                                       # no result
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "x"}),  # result not object
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"isError": False}}),  # no SC
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                              "result": {"isError": False, "structuredContent": "x"}}),
    httpx.Response(200, text="event: message\n\n", headers={"content-type": "text/event-stream"}),
    httpx.Response(200, text="data: {\"jsonrpc\": \"2.0\", \"res\n\n",
                   headers={"content-type": "text/event-stream"}),  # truncated event
], ids=["text", "empty", "array", "no-result", "result-str", "no-sc", "sc-str", "sse-no-data",
        "sse-garbled"])
async def test_unreadable_2xx_is_unknown(resp):
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: resp).call("create-order", {})
    assert _unknown(ei.value)


async def test_jsonrpc_error_envelope_is_unknown_and_keeps_message():
    body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "internal oops"}}
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: httpx.Response(200, json=body)).call("create-order", {})
    assert _unknown(ei.value) and "internal oops" in ei.value.message


async def test_iserror_without_success_false_is_unknown():
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc({"message": "boom"}, is_error=True)).call("create-order", {})
    assert _unknown(ei.value)


@pytest.mark.parametrize("is_error", [False, True])
async def test_success_false_is_plain_business_failure(is_error):
    sc = {"success": False, "code": 500, "message": "门店已打烊"}
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc(sc, is_error=is_error)).call("create-order", {})
    assert not isinstance(ei.value, McdTimeout) and "门店已打烊" in ei.value.message


@pytest.mark.parametrize("sc", [
    {"success": False, "message": "超时", "data": {"orderId": "O123"}},
    {"success": False, "message": "超时", "orderId": "O123"},
    {"success": False, "data": {"orderId": 1142}},
    {"success": False, "data": {"orderDetail": {"orderId": "O123"}}},
], ids=["data.orderId", "orderId", "int-orderId", "orderDetail.orderId"])
async def test_success_false_but_order_id_present_is_unknown(sc):
    """M5: "failed" yet carrying an order id may mean the order exists: outcome unknown."""
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc(sc)).call("create-order", {})
    assert _unknown(ei.value)


@pytest.mark.parametrize("data", [None, {}, {"orderId": ""}, {"orderId": None}, {"orderId": False},
                                  {"orderId": "  "}, "O123", ["O123"]])
async def test_success_false_without_usable_order_id_stays_definite(data):
    sc = {"success": False, "message": "门店已打烊", "data": data}
    with pytest.raises(McdError) as ei:
        await make_client(lambda r: rpc(sc)).call("create-order", {})
    assert not isinstance(ei.value, McdTimeout)


def _sse(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


async def test_sse_multiline_data_is_joined():
    sc = {"success": True, "data": {"k": "v"}}
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "result": {"isError": False, "structuredContent": sc}}, indent=1)
    body = "event: message\n" + "".join(f"data: {ln}\n" for ln in payload.split("\n")) + "\n"
    assert payload.count("\n") > 3
    assert await make_client(lambda r: _sse(body)).call("order-list", {}) == sc


async def test_sse_picks_the_response_event_crlf_and_u2028():
    sc = {"success": True, "data": {"name": "a\u2028b"}}
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
    resp = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "result": {"isError": False, "structuredContent": sc}}, ensure_ascii=False)
    body = f": keepalive\r\ndata: {note}\r\n\r\nevent: message\r\ndata:{resp}\r\n\r\n"
    assert await make_client(lambda r: _sse(body)).call("order-list", {}) == sc


async def test_sse_without_final_blank_line_still_parsed():
    sc = {"success": True}
    resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": sc}})
    assert await make_client(lambda r: _sse(f"data: {resp}")).call("order-list", {}) == sc


async def test_mock_lines_follow_items_and_cancel_shows_in_query_order():
    m = MockMcdClient()
    (c1, c2), meals = await _first_two_codes(m)
    d = (await m.call("calculate-price", {"items": [{"productCode": c1, "quantity": 2}]}))["data"]
    assert [(x["productCode"], x["productName"], x["quantity"]) for x in d["productList"]] == [
        (c1, meals[c1]["name"], 2)]
    c = (await m.call("cancel-order", {"orderId": "DEMO1", "cancelReasonCode": "3"}))["data"]
    assert c == {"cancelResult": True, "orderId": "DEMO1"}
    q = (await m.call("query-order", {"orderId": "DEMO1"}))["data"]
    assert (q["status"], q["orderStatus"]) == ("60", "订单已取消")
    q = (await m.call("query-order", {"orderId": "DEMO2"}))["data"]
    assert q["status"] == "10"
