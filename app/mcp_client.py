"""McDonald's China MCP client (JSON-RPC over HTTP) plus an offline mock."""
import copy
import json
import secrets
import re
from pathlib import Path

import httpx

from app.money import fen_to_yuan, yuan_to_fen

ALLOWED_TOOLS: frozenset[str] = frozenset({
    "delivery-query-addresses",
    "delivery-query-stores",
    "query-meals",
    "calculate-price",
    "query-store-coupons",
    "create-order",
    "query-order",
    "order-list",
    "cancel-order",
})

DEFAULT_MOCK_DIR = Path(__file__).resolve().parent / "mock_data"
_SSE_LINE = re.compile(r"\r\n|\r|\n")  # the only SSE line terminators (not U+2028 etc.)


class McdError(Exception):
    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class McdTimeout(McdError):
    """Timeout, or connection lost after the request was sent: outcome unknown."""


class McdUnknown(McdTimeout):
    """An answer arrived but does not prove the call failed (5xx, unreadable body, JSON-RPC
    error, isError without success=false, success=false that still carries an order id): the
    server may have executed it. Outcome unknown.

    Plain McdError is reserved for "definitely not executed": never sent (connect failure,
    tool not allowed), HTTP 4xx, or structuredContent.success == false with no order id."""


def _carries_order_id(sc: dict) -> bool:
    """True if a reply names an order where create-order would: orderId, data.orderId or
    data.orderDetail.orderId (non-empty str or int)."""
    data = sc.get("data")
    found = [sc.get("orderId")]
    if isinstance(data, dict):
        found.append(data.get("orderId"))
        od = data.get("orderDetail")
        if isinstance(od, dict):
            found.append(od.get("orderId"))
    return any(isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()
               for v in found)


def _check_tool(tool: str) -> None:
    if tool not in ALLOWED_TOOLS:
        raise McdError(tool, "tool not allowed")


class McdClient:
    def __init__(self, token: str, url: str = "https://mcp.mcd.cn", timeout: float = 20.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._url = url
        self._timeout = timeout
        self._transport = transport
        self._next_id = 0

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def _err(self, tool: str, message: str, cls: type[McdError] = McdError) -> McdError:
        return cls(tool, self._scrub(message))

    @staticmethod
    def _sse_events(text: str) -> list[str]:
        """Data of each SSE event; multi-line `data:` fields are joined with LF (SSE spec)."""
        events, buf = [], []
        for line in _SSE_LINE.split(text):
            if line == "":
                if buf:
                    events.append("\n".join(buf))
                    buf = []
                continue
            if line.startswith(":"):  # comment / keepalive
                continue
            field, _, value = line.partition(":")
            if field == "data":
                buf.append(value[1:] if value.startswith(" ") else value)
        if buf:  # stream ended without the final blank line
            events.append("\n".join(buf))
        return events

    @classmethod
    def _parse_body(cls, resp: httpx.Response):
        text = resp.text
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for data in reversed(cls._sse_events(text)):  # the response is the last such event
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    return msg
            raise ValueError("no JSON-RPC response in event stream")
        return json.loads(text)

    async def call(self, tool: str, args: dict) -> dict:
        _check_tool(tool)
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                resp = await http.post(self._url, json=payload, headers=headers)
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as e:
            # request never reached the server: safe to treat as a plain failure
            raise self._err(tool, f"connect failed: {type(e).__name__}") from None
        except httpx.TimeoutException as e:
            raise self._err(tool, f"timeout: {type(e).__name__}", McdTimeout) from None
        except httpx.TransportError as e:
            raise self._err(tool, f"connection lost: {type(e).__name__}", McdTimeout) from None
        status = resp.status_code
        if 400 <= status < 500:  # refused: definitely not executed
            raise self._err(tool, f"http {status}")
        if not 200 <= status < 300:  # 5xx (incl. gateway timeouts), 3xx: may have executed
            raise self._err(tool, f"http {status}", McdUnknown)
        try:
            msg = self._parse_body(resp)
        except ValueError:  # bad JSON / no response event / undecodable text
            raise self._err(tool, "unreadable response", McdUnknown) from None
        result = msg.get("result") if isinstance(msg, dict) else None
        if not isinstance(result, dict):
            err = msg.get("error") if isinstance(msg, dict) else None
            detail = err.get("message") if isinstance(err, dict) else None
            raise self._err(tool, f"json-rpc error: {detail}" if detail else "no result", McdUnknown)
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and sc.get("success") is False:  # explicit McD business failure
            message = str(sc.get("message") or "tool reported failure")
            if _carries_order_id(sc):  # M5: "failed", yet an order id came back: may exist
                raise self._err(tool, f"failure reply with an order id: {message}", McdUnknown)
            raise self._err(tool, message)
        if not isinstance(sc, dict) or result.get("isError"):
            detail = sc.get("message") if isinstance(sc, dict) else None
            raise self._err(tool, str(detail or "no structured result"), McdUnknown)
        return sc


class MockMcdClient:
    """Offline stand-in that serves sanitized fixtures; records every call."""

    def __init__(self, data_dir: Path | None = None):
        self._dir = Path(data_dir) if data_dir else DEFAULT_MOCK_DIR
        self.calls: list[tuple[str, dict]] = []
        self._query_count: dict[str, int] = {}
        self._cancelled: set[str] = set()

    def _load(self, tool: str) -> dict:
        return json.loads((self._dir / f"{tool}.json").read_text(encoding="utf-8"))

    def _price(self, tool: str, args: dict) -> dict:
        meals = self._load("query-meals")["data"]["meals"]
        items = args.get("items") or []
        product = qty = 0
        lines = []
        for it in items:
            code = str(it.get("productCode"))
            meal = meals.get(code)
            if meal is None:
                raise McdError(tool, "unknown product")
            q = int(it.get("quantity", 1))
            sub = yuan_to_fen(meal["currentPrice"]) * q
            product += sub
            qty += q
            lines.append({"productCode": code, "productName": meal["name"], "quantity": q,
                          "originalSubtotal": sub, "subtotal": sub})
        packing = min(qty * 110, 900)
        return {"productPrice": product, "deliveryPrice": 600, "packingPrice": packing,
                "discount": 0, "price": product + 600 + packing, "productList": lines}

    async def call(self, tool: str, args: dict) -> dict:
        _check_tool(tool)
        self.calls.append((tool, copy.deepcopy(args)))
        out = self._load(tool)
        if tool == "calculate-price":
            out["data"].update(self._price(tool, args))
            out["data"]["productOriginalPrice"] = out["data"]["productPrice"]
            out["data"]["originalPrice"] = out["data"]["price"]
        elif tool == "create-order":
            price = self._price(tool, args)["price"]
            oid = "DEMO" + "".join(secrets.choice("0123456789") for _ in range(8))
            d = out["data"]
            d["orderId"] = d["orderDetail"]["orderId"] = oid
            d["payId"] = d["orderDetail"]["payId"] = "DEMOPAY" + oid[4:]
            d["orderDetail"]["realTotalAmount"] = fen_to_yuan(price)
        elif tool == "query-order":
            key = str(args.get("orderId"))
            n = self._query_count[key] = self._query_count.get(key, 0) + 1
            d = out["data"]
            if key in self._cancelled:
                d["status"], d["orderStatus"] = "60", "订单已取消"
            elif n == 1:
                d["status"], d["orderStatus"] = "10", "待支付"
            else:
                d["status"], d["orderStatus"] = "50", "已送达（演示）"
            if args.get("orderId"):
                d["orderId"] = args["orderId"]
        elif tool == "cancel-order":
            key = str(args.get("orderId"))
            self._cancelled.add(key)
            out["data"]["orderId"] = key
        return out
