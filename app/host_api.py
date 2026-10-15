"""Host (organiser) endpoints. Auth via security.require_host; MCP only where noted.

Every MCP call here runs inside a host-triggered request: no background tasks, no retries.
"""
import io
import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app import group
from app.member_api import WEB_DIR, build_join_payload
from app.mcp_client import McdError, McdTimeout
from app.menu import normalize_menu
from app.money import yuan_to_fen
from app.store import StateConflict
from app.security import (MAX_HOST_KEY, HostAuthError, check_host_key, client_ip, csrf_ok,
                          new_sid, require_host)

log = logging.getLogger("app.order")

MAX_TITLE = 20
MAX_LABEL = 30
MAX_REMARK = 50
MAX_ORDER_ID = 64
RECENT_MAX = 5
DEMO_ACTIVE = frozenset({group.OPEN, group.LOCKED, group.ORDERING, group.UNCERTAIN, group.ORDERED})
DEMO_MAX_ACTIVE = 20
DEMO_CREATES_PER_IP_PER_HOUR = 5
CANCEL_REASONS = frozenset({"1", "2", "3", "4", "5", "-1"})
CANCELLED_STATUS = "60"
REOPEN_MIN = timedelta(minutes=10)
HOST_DELETABLE = frozenset({group.OPEN, group.LOCKED})  # by stored state (I5)
_PREFIX = re.compile(r"^(.+?省)?(.+?市)?(.+?[区县])?")


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _mcd_fail(tool: str, e: BaseException) -> JSONResponse:
    """502 for a failed MCP call. Logs only the type / scrubbed McdError text, never payloads."""
    if isinstance(e, McdError):
        log.warning("%s failed: %s", tool, e.message)
        return JSONResponse({"error": "mcd", "message": e.message}, status_code=502)
    log.warning("%s unusable response: %s", tool, type(e).__name__)
    return _err(502, "mcd")


def _fen(v) -> int:
    """Integer fen as returned by calculate-price; bool/float/str are rejected."""
    if type(v) is not int:
        raise ValueError("amount is not integer fen")
    return v


def _opt_fen(v):
    return v if type(v) is int else None


def _opt_str(v):
    return v if isinstance(v, str) and v else None


def _yuan_fen_or_none(v):
    try:
        return yuan_to_fen(v)
    except Exception:  # None / garbage / NaN from an upstream yuan string
        return None


def parse_quote(data: dict) -> dict:
    """calculate-price data -> {quote_fen, breakdown, lines}. Raises if `price` is not int fen."""
    lines = []
    for x in data.get("productList") or []:
        if isinstance(x, dict):
            lines.append({"code": str(x.get("productCode") or ""),
                          "name": str(x.get("productName") or ""),
                          "qty": x.get("quantity") if type(x.get("quantity")) is int else None,
                          "subtotal_fen": _opt_fen(x.get("subtotal")),
                          "original_fen": _opt_fen(x.get("originalSubtotal"))})
    return {
        "quote_fen": _fen(data["price"]),
        "breakdown": {"product_fen": _opt_fen(data.get("productPrice")),
                      "delivery_fen": _opt_fen(data.get("deliveryPrice")),
                      "packing_fen": _opt_fen(data.get("packingPrice")),
                      "discount_fen": _opt_fen(data.get("discount")),
                      "original_fen": _opt_fen(data.get("originalPrice"))},
        "lines": lines,
    }


MAX_COUPON = 64


def _coupon_str(v):
    """A coupon id/code from upstream: non-empty str, or a (non-bool) int rendered as str."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        v = str(v)
    return v.strip() if isinstance(v, str) and v.strip() else None


def parse_coupons(data) -> list:
    """query-store-coupons data -> [{id, code, name}]. Fully tolerant: the real shape is unknown
    (the host's store returned [] on 2026-10-14), so any surprise yields [] instead of an error."""
    try:
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        out = []
        for c in data if isinstance(data, list) else []:
            if not isinstance(c, dict):
                continue
            cid = next((x for x in (_coupon_str(c.get(k)) for k in ("couponId", "id")) if x), None)
            code = next((x for x in (_coupon_str(c.get(k)) for k in ("couponCode", "code")) if x), None)
            if cid is None and code is None:
                continue
            name = next((x for x in (_opt_str(c.get(k)) for k in
                                     ("couponName", "name", "title", "couponTitle")) if x), "优惠券")
            out.append({"id": cid, "code": code, "name": name})
        return out
    except Exception:
        return []


def with_coupon(items: list, coupon) -> list:
    """Copy of items with the coupon attached to the first line (items stay untouched)."""
    out = [dict(x) for x in items]
    if coupon and out:
        if coupon.get("id") is not None:
            out[0]["couponId"] = coupon["id"]
        if coupon.get("code") is not None:
            out[0]["couponCode"] = coupon["code"]
    return out


def _coupon_field(v):
    """None, or a str of at most MAX_COUPON chars; anything else -> False."""
    if v is None:
        return None
    return v if isinstance(v, str) and 0 < len(v) <= MAX_COUPON else False


def pick_coupon(raw, coupons: list):
    """Validate a client coupon against the latest list. Returns (coupon|None, ok)."""
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        return None, False
    cid, code = _coupon_field(raw.get("id")), _coupon_field(raw.get("code"))
    if cid is False or code is False or (cid is None and code is None):
        return None, False
    if any(c["id"] == cid and c["code"] == code for c in coupons):
        return {"id": cid, "code": code}, True
    return None, False


def _order_id(v):
    if isinstance(v, bool) or not isinstance(v, (str, int)) or not str(v).strip():
        return None
    return str(v).strip()


def parse_created(data) -> dict:
    """create-order data -> fields we keep.

    Raises only when no order id can be extracted (then the outcome is unknown). Everything
    else is best effort: a missing/garbled amount gives real_total_fen=None (warned, not hidden).
    """
    od = data.get("orderDetail") if isinstance(data, dict) else None
    od = od if isinstance(od, dict) else {}
    oid = _order_id(data.get("orderId")) if isinstance(data, dict) else None
    oid = oid or _order_id(od.get("orderId"))
    if oid is None:
        raise ValueError("no orderId in create-order reply")
    di = od.get("deliveryInfo")
    return {"order_id": oid, "pay_url": _opt_str(data.get("payH5Url")),
            "real_total_fen": _yuan_fen_or_none(od.get("realTotalAmount")),
            "order_status": _opt_str(od.get("orderStatus")),
            "expect_delivery_time": _opt_str(di.get("expectDeliveryTime")) if isinstance(di, dict) else None}


def parse_queried(data: dict) -> dict:
    """query-order data -> session fields to update (only those present)."""
    out = {}
    if _opt_str(data.get("orderStatus")):
        out["order_status"] = data["orderStatus"]
    di = data.get("deliveryInfo")
    if isinstance(di, dict) and _opt_str(di.get("expectDeliveryTime")):
        out["expect_delivery_time"] = di["expectDeliveryTime"]
    if _opt_str(data.get("payH5Url")):  # not returned as of 2026-10-14, kept if it ever is
        out["pay_url"] = data["payH5Url"]
    return out


def is_cancelled_status(data: dict) -> bool:
    """query-order status code "60" = 订单已取消."""
    return str(data.get("status")) == CANCELLED_STATUS


async def _json_body(request: Request):
    """{} for an empty body, the dict for a JSON object, None otherwise."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        b = json.loads(raw)
    except ValueError:
        return None
    return b if isinstance(b, dict) else None


def address_label(full: str) -> str:
    full = (full or "").strip()
    parts = full.split()
    if len(parts) > 1:
        return parts[-1]
    rest = full[_PREFIX.match(full).end():].strip()
    return rest or full


def _str(v, lo=1, hi=None):
    if not isinstance(v, str):
        return None
    v = v.strip()
    if len(v) < lo or (hi is not None and len(v) > hi):
        return None
    return v


def _validate_create(b: dict, now: datetime):
    """Return (fields, None) or (None, bad_field_name)."""
    out = {}
    for f in ("address_id", "store_code", "be_code", "store_name"):
        v = _str(b.get(f))
        if v is None:
            return None, f
        out[f] = v
    try:
        cutoff = datetime.fromisoformat(b.get("cutoff_at"))
    except (TypeError, ValueError):
        return None, "cutoff_at"
    if cutoff.tzinfo is None or cutoff <= now:
        return None, "cutoff_at"
    out["cutoff_at"] = cutoff.isoformat()
    title = b.get("title")
    title = "我请客" if title is None else _str(title, 1, MAX_TITLE)
    if title is None:
        return None, "title"
    out["title"] = title
    label = _str(b.get("address_label"), 1, MAX_LABEL)
    if label is None:
        return None, "address_label"
    out["address_label"] = label
    cap = b.get("cap_fen")
    if cap is None or (type(cap) is int and cap == 0):
        cap = None
    elif type(cap) is not int or cap < 0:
        return None, "cap_fen"
    out["cap_fen"] = cap
    return out, None


def build_router(settings) -> APIRouter:
    router = APIRouter()
    guard = [Depends(require_host(settings))]
    secure = settings.public_base_url.startswith("https://")

    # The cookie is only ever needed by /api/host/*. Scoping it there keeps it off every other
    # path, in particular the demo under /demo/ on the same origin (I3).
    cookie_path = urlsplit(settings.public_base_url).path.rstrip("/") + "/api/host"

    @router.get("/host")
    def host_page():
        """Static page, no data in it: no auth here (I3/M1). A `k` query param is ignored."""
        return FileResponse(WEB_DIR / "host.html", media_type="text/html")

    @router.post("/api/host/login")
    async def login(request: Request):
        """M1: host.js reads the key from the URL fragment (#k=, never sent to any server or
        log), POSTs it here, then strips the fragment. Same CSRF rule and throttle as the API."""
        if settings.mode == "demo":  # demo has no host key
            return {"ok": True}
        if not csrf_ok(request):
            return _err(403, "csrf")
        b = await _json_body(request)
        k = b.get("k") if b else None
        if not isinstance(k, str) or not 1 <= len(k) <= MAX_HOST_KEY:
            return _err(422, "bad_request")
        try:
            check_host_key(request, settings, (k,))
        except HostAuthError as e:
            return _err(e.status, e.code)
        resp = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        # expire the Path=/ cookie older builds set, so it stops reaching /demo/ too
        resp.delete_cookie("hk", path="/", httponly=True, samesite="strict", secure=secure)
        resp.set_cookie("hk", k, httponly=True, samesite="strict", path=cookie_path, secure=secure)
        return resp

    async def _mcp(request: Request, tool: str, args: dict):
        try:
            resp = await request.app.state.client.call(tool, args)
            return resp["data"], None
        except (McdError, KeyError, TypeError):
            return None, _err(502, "mcd")

    @router.get("/api/host/addresses", dependencies=guard)
    async def addresses(request: Request):
        data, err = await _mcp(request, "delivery-query-addresses", {})
        if err:
            return err
        try:
            return [{"address_id": a["addressId"], "label": address_label(a.get("fullAddress", ""))}
                    for a in data["addresses"]]
        except (KeyError, TypeError):
            return _err(502, "mcd")

    @router.get("/api/host/stores", dependencies=guard)
    async def stores(request: Request, address_id: str):
        data, err = await _mcp(request, "delivery-query-stores",
                               {"addressId": address_id, "beType": 2})
        if err:
            return err
        try:
            out = []
            for s in data:
                today = next((o.get("reservationOptionText", "")
                              for o in s.get("reservationTimeOptions") or [] if o.get("today") is True), "")
                out.append({"store_code": s["storeCode"], "be_code": s["beCode"],
                            "name": s["storeName"], "open": s.get("businessStartTime", ""),
                            "close": s.get("businessEndTime", ""), "today": today})
            return out
        except (KeyError, TypeError, AttributeError):
            return _err(502, "mcd")

    @router.post("/api/host/sessions", dependencies=guard)
    async def create_session(request: Request):
        try:
            b = await request.json()
        except Exception:
            b = None
        if not isinstance(b, dict):
            return _err(422, "bad_request")
        now = request.app.state.now()
        f, bad = _validate_create(b, now)
        if bad:
            return _err(422, bad)
        store = request.app.state.store
        store.purge_older_than(settings.retention_s, now)
        if settings.mode == "demo" and (
                store.count_sessions(DEMO_ACTIVE) >= DEMO_MAX_ACTIVE
                or not request.app.state.demo_create_limiter.check(client_ip(request))):
            return _err(429, "demo_limit")
        data, err = await _mcp(request, "query-meals", {
            "storeCode": f["store_code"], "beCode": f["be_code"], "beType": 2, "orderType": 2})
        if err:
            return err
        try:
            menu = normalize_menu(data)
        except (KeyError, TypeError, ValueError, AttributeError):
            return _err(502, "mcd")
        sid = new_sid()
        store.create_session(
            sid=sid, title=f["title"], address_id=f["address_id"], address_label=f["address_label"],
            store_code=f["store_code"], be_code=f["be_code"], store_name=f["store_name"],
            cutoff_at=f["cutoff_at"], cap_fen=f["cap_fen"],
            menu_json=json.dumps(menu, ensure_ascii=False), state=group.OPEN,
            created_at=now.isoformat())
        return JSONResponse({"sid": sid, "join_url": f"{settings.public_base_url}/j/{sid}"},
                            status_code=201)

    def _detail(request: Request, s: dict) -> dict:
        d = build_join_payload(request.app.state.store, s, request.app.state.now())
        d.update(quote=json.loads(s["quote_json"]) if s["quote_json"] else None,
                 pay_url=s["pay_url"], order_id=s["order_id"], raw_state=s["state"],
                 recent_checked=bool(s["recent_checked"]))
        return d

    @router.get("/api/host/sessions/{sid}", dependencies=guard)
    def detail(sid: str, request: Request):
        s = request.app.state.store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        return _detail(request, s)

    @router.post("/api/host/sessions/{sid}/lock", dependencies=guard)
    def lock(sid: str, request: Request):
        store = request.app.state.store
        if store.get_session(sid) is None:
            return _err(404, "not_found")
        if not store.transition(sid, [group.OPEN], group.LOCKED):
            return _err(409, "bad_state")
        return _detail(request, store.get_session(sid))

    @router.post("/api/host/sessions/{sid}/reopen", dependencies=guard)
    def reopen(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        now = request.app.state.now()
        cutoff = datetime.fromisoformat(s["cutoff_at"])
        if group.effective_state(s["state"], cutoff, now) != group.LOCKED:
            return _err(409, "bad_state")
        if not store.transition(sid, [group.OPEN, group.LOCKED], group.OPEN):
            return _err(409, "bad_state")
        store.update_session(sid, quote_json=None)  # entries may change: any quote is void
        floor = now + REOPEN_MIN
        if cutoff < floor:
            store.update_session(sid, cutoff_at=floor.isoformat())
        return _detail(request, store.get_session(sid))

    @router.delete("/api/host/sessions/{sid}/entries/{eid}", dependencies=guard)
    def host_delete_entry(sid: str, eid: int, request: Request):
        """I5: the host removes a member's entry (a duplicate, someone who left). Only while the
        stored state is OPEN or LOCKED, re-checked inside the write; the stored quote is voided
        in the same transaction, so /order needs a fresh 算价. Never calls MCP."""
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        if s["state"] not in HOST_DELETABLE:
            return _err(409, "bad_state")
        try:
            ok = store.host_delete_entry(sid, eid, only_if_state=HOST_DELETABLE)
        except StateConflict:  # an order started (or the state moved) since the check above
            return _err(409, "bad_state")
        if not ok:
            return _err(404, "not_found")
        return _detail(request, store.get_session(sid))

    # ---------------- quote / order (the money path) ----------------

    def _persist_cutoff_lock(store, s: dict, now: datetime):
        """Cutoff passed while the row still says OPEN: write LOCKED, then trust the row."""
        cutoff = datetime.fromisoformat(s["cutoff_at"])
        if s["state"] == group.OPEN and group.effective_state(s["state"], cutoff, now) == group.LOCKED:
            store.transition(s["sid"], {group.OPEN}, group.LOCKED)
            return store.get_session(s["sid"])
        return s

    def _price_args(s: dict, items: list, need_tableware: bool) -> dict:
        return {"storeCode": s["store_code"], "beCode": s["be_code"], "beType": 2,
                "orderType": 2, "items": items, "needTableware": need_tableware}

    async def _price(request: Request, s: dict, items: list, need_tableware: bool) -> dict:
        resp = await request.app.state.client.call(
            "calculate-price", _price_args(s, items, need_tableware))
        return parse_quote(resp["data"])

    async def _query(request: Request, order_id: str) -> dict:
        resp = await request.app.state.client.call("query-order", {"orderId": order_id})
        return resp["data"]

    def _quote_json(q: dict, items: list, coupons: list, coupon) -> str:
        """The quote plus the exact items it priced (R12: /order re-checks both; the selected
        coupon is already on items[0]) and the coupon list it was chosen from."""
        return json.dumps(dict(q, items=items, coupons=coupons, coupon=coupon), ensure_ascii=False)

    def _stored_quote(row) -> dict:
        try:
            d = json.loads(row["quote_json"]) if row and row["quote_json"] else {}
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    def _save_real_total(store, sid: str, real_total_fen):
        """Remember what was actually charged next to the quote (for the host view)."""
        s = store.get_session(sid)
        q = json.loads(s["quote_json"]) if s and s["quote_json"] else {}
        q["real_total_fen"] = real_total_fen
        q["mismatch"] = real_total_fen is None or real_total_fen != q.get("quote_fen")
        store.update_session(sid, quote_json=json.dumps(q, ensure_ascii=False))

    @router.post("/api/host/sessions/{sid}/quote", dependencies=guard)
    async def quote(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        b = await _json_body(request)
        if b is None:
            return _err(422, "bad_request")
        need = b.get("need_tableware", True)
        if type(need) is not bool:
            return _err(422, "need_tableware")
        s = _persist_cutoff_lock(store, s, request.app.state.now())
        if s is None:
            return _err(404, "not_found")
        if s["state"] != group.LOCKED:
            return _err(409, "bad_state")
        items = group.aggregate_items(store.list_entries(sid))
        if not items:
            return _err(422, "empty")
        # coupons are optional: a failing / odd query-store-coupons just means "none"
        try:
            resp = await request.app.state.client.call("query-store-coupons", {
                "storeCode": s["store_code"], "beCode": s["be_code"], "beType": 2, "orderType": 2})
            coupons = parse_coupons(resp.get("data"))
        except McdError as e:
            log.warning("query-store-coupons failed: %s", e.message)
            coupons = []
        except Exception as e:
            log.warning("query-store-coupons unusable response: %s", type(e).__name__)
            coupons = []
        coupon, ok = pick_coupon(b.get("coupon"), coupons)
        if not ok:
            return _err(422, "coupon")
        items = with_coupon(items, coupon)
        try:
            q = await _price(request, s, items, need)
        except Exception as e:
            return _mcd_fail("calculate-price", e)
        cur = store.get_session(sid)
        if cur and cur["state"] == group.LOCKED:  # do not clobber an order that started meanwhile
            store.update_session(sid, quote_json=_quote_json(q, items, coupons, coupon))
        return dict(q, coupons=coupons, coupon=coupon)

    @router.post("/api/host/sessions/{sid}/order", dependencies=guard)
    async def order(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        b = await _json_body(request)
        if b is None:
            return _err(422, "bad_request")
        quote_fen = b.get("quote_fen")
        if type(quote_fen) is not int:
            return _err(422, "quote_fen")
        remark = b.get("remark", "")
        if not isinstance(remark, str):
            return _err(422, "remark")
        remark = remark.strip()[:MAX_REMARK]
        need = b.get("need_tableware", True)
        if type(need) is not bool:
            return _err(422, "need_tableware")
        s = _persist_cutoff_lock(store, s, request.app.state.now())
        if s is None:
            return _err(404, "not_found")

        # The only gate against double submit: exactly one request can win LOCKED -> ORDERING.
        if not store.transition(sid, {group.LOCKED}, group.ORDERING):
            cur = store.get_session(sid)
            return _err(409, "busy" if cur and cur["state"] == group.ORDERING else "bad_state")
        # Where the session goes when this handler exits, however it exits (finally below).
        # LOCKED until create-order is invoked; UNCERTAIN from then on unless proven otherwise.
        exit_state = group.LOCKED
        try:
            # read entries + the stored quote only after winning ORDERING: both are frozen now
            items = group.aggregate_items(store.list_entries(sid))
            if not items:
                return _err(422, "empty")
            stored = _stored_quote(store.get_session(sid))
            quoted_items = stored.get("items")
            coupons = stored.get("coupons") if isinstance(stored.get("coupons"), list) else []
            coupon = stored.get("coupon") if isinstance(stored.get("coupon"), dict) else None
            # the coupon is whatever the confirmed quote carried, never what the client says now
            coupon_changed = "coupon" in b and b["coupon"] != (
                {"id": coupon["id"], "code": coupon["code"]} if coupon else None)
            items = with_coupon(items, coupon)
            try:
                q = await _price(request, s, items, need)
            except Exception as e:  # McdError, McdTimeout, malformed data: nothing was ordered
                return _mcd_fail("calculate-price", e)
            store.update_session(sid, quote_json=_quote_json(q, items, coupons, coupon))
            # what the host confirmed must be what we send: same amount AND same items (R12)
            if q["quote_fen"] != quote_fen or quoted_items != items or coupon_changed:
                return JSONResponse({"error": "price_changed", "quote": q}, status_code=409)

            exit_state = group.UNCERTAIN  # ---- point of no return ----
            try:
                resp = await request.app.state.client.call("create-order", {
                    "addressId": s["address_id"], **_price_args(s, items, need), "remark": remark})
                placed = parse_created(resp["data"])
            except McdTimeout as e:
                log.warning("create-order outcome unknown: %s", e.message)
                return _err(202, "uncertain")
            except McdError as e:  # McD answered with a failure: no order exists
                exit_state = group.LOCKED
                return _mcd_fail("create-order", e)
            except Exception as e:  # sent, but the reply is unusable: outcome unknown
                log.error("create-order reply unusable: %s", type(e).__name__)
                return _err(202, "uncertain")

            real = placed["real_total_fen"]
            mismatch = real is None or real != quote_fen  # flag only, never auto-cancel
            q.update(real_total_fen=real, mismatch=mismatch)
            store.update_session(
                sid, order_id=placed["order_id"], pay_url=placed["pay_url"],
                order_status=placed["order_status"],
                expect_delivery_time=placed["expect_delivery_time"],
                quote_json=_quote_json(q, items, coupons, coupon))
            exit_state = group.ORDERED
            out = {"order_id": placed["order_id"], "pay_url": placed["pay_url"],
                   "real_total_fen": real, "mismatch": mismatch}
            if real is None:
                log.warning("create-order reply has no usable realTotalAmount")
                out["warning"] = "amount_unknown"
            return out
        finally:
            store.transition(sid, {group.ORDERING}, exit_state)

    @router.post("/api/host/sessions/{sid}/refresh", dependencies=guard)
    async def refresh(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        if s["state"] not in (group.ORDERED, group.CANCELLED) or not s["order_id"]:
            return _err(409, "bad_state")
        try:
            data = await _query(request, s["order_id"])
            fields = parse_queried(data)
            cancelled = is_cancelled_status(data)
        except Exception as e:
            return _mcd_fail("query-order", e)
        fields.pop("pay_url", None)  # refresh only updates the status shown to the group
        if fields:
            store.update_session(sid, **fields)
        if cancelled:  # cancelled elsewhere (e.g. in the McD App): follow it (R13)
            store.transition(sid, {group.ORDERED}, group.CANCELLED)
        return _detail(request, store.get_session(sid))

    @router.post("/api/host/sessions/{sid}/recent-orders", dependencies=guard)
    async def recent_orders(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        if s["state"] != group.UNCERTAIN:
            return _err(409, "bad_state")
        # I2: an attempted order-list counts as "checked" whether it worked or not (if it
        # failed, the UI sends the host to the McD App's 我的订单). Marked only for the same
        # UNCERTAIN episode we read above, so a slow call can't unlock a later one.
        try:
            resp = await request.app.state.client.call("order-list", {})
            rows = resp["data"]["list"]
            if not isinstance(rows, list):
                raise TypeError("list")
        except Exception as e:
            store.mark_recent_checked(sid, s["updated_at"])
            return _mcd_fail("order-list", e)
        store.mark_recent_checked(sid, s["updated_at"])
        out = []
        for o in rows:  # newest first
            if not isinstance(o, dict) or not o.get("orderId"):
                continue
            out.append({"order_id": str(o["orderId"]), "create_time": _opt_str(o.get("createTime")),
                        "store_name": _opt_str(o.get("storeName")),
                        "status_text": _opt_str(o.get("orderStatus")),
                        "real_total_fen": _yuan_fen_or_none(o.get("realTotalAmount"))})
            if len(out) == RECENT_MAX:
                break
        return out

    @router.post("/api/host/sessions/{sid}/adopt", dependencies=guard)
    async def adopt(sid: str, request: Request):
        store = request.app.state.store
        if store.get_session(sid) is None:
            return _err(404, "not_found")
        b = await _json_body(request)
        oid = b.get("order_id") if b else None
        oid = oid.strip() if isinstance(oid, str) else ""
        if not 1 <= len(oid) <= MAX_ORDER_ID:
            return _err(422, "order_id")
        if not store.transition(sid, {group.UNCERTAIN}, group.ORDERED):
            return _err(409, "bad_state")
        store.update_session(sid, order_id=oid, pay_url=None)
        try:
            data = await _query(request, oid)
            store.update_session(sid, **parse_queried(data))
            _save_real_total(store, sid, _yuan_fen_or_none(data.get("realTotalAmount")))
        except Exception as e:  # adopted anyway; the host can press 刷新状态
            _mcd_fail("query-order", e)
        return _detail(request, store.get_session(sid))

    @router.post("/api/host/sessions/{sid}/not-placed", dependencies=guard)
    def not_placed(sid: str, request: Request):
        """Spec 5.2: leaving UNCERTAIN as "not placed" needs 查最近订单 first (recent_checked,
        checked in the same compare-and-set as the state change)."""
        store = request.app.state.store
        if store.get_session(sid) is None:
            return _err(404, "not_found")
        if not store.transition(sid, {group.UNCERTAIN}, group.LOCKED, only_if={"recent_checked": 1}):
            cur = store.get_session(sid)
            if cur and cur["state"] == group.UNCERTAIN:
                return _err(409, "check_recent_first")
            return _err(409, "bad_state")
        return _detail(request, store.get_session(sid))

    @router.post("/api/host/sessions/{sid}/cancel", dependencies=guard)
    async def cancel(sid: str, request: Request):
        store = request.app.state.store
        s = store.get_session(sid)
        if s is None:
            return _err(404, "not_found")
        b = await _json_body(request)
        code = b.get("reason_code") if b else None
        if not isinstance(code, str) or code not in CANCEL_REASONS:
            return _err(422, "reason_code")
        if s["state"] != group.ORDERED or not s["order_id"]:
            return _err(409, "bad_state")
        try:
            resp = await request.app.state.client.call(
                "cancel-order", {"orderId": s["order_id"], "cancelReasonCode": code})
            ok = resp["data"]["cancelResult"] is True
        except Exception as e:
            return _mcd_fail("cancel-order", e)
        if not ok:
            return _err(502, "cancel_failed")
        store.transition(sid, {group.ORDERED}, group.CANCELLED)
        try:
            fields = parse_queried(await _query(request, s["order_id"]))
            fields.pop("pay_url", None)
            if fields:
                store.update_session(sid, **fields)
        except Exception as e:  # cancelled anyway; status text catches up on 刷新状态
            _mcd_fail("query-order", e)
        return _detail(request, store.get_session(sid))

    @router.get("/api/host/sessions/{sid}/pay-qr.svg", dependencies=guard)
    def pay_qr(sid: str, request: Request):
        s = request.app.state.store.get_session(sid)
        url = s["pay_url"] if s and s["state"] == group.ORDERED else None  # nothing to pay otherwise
        if not isinstance(url, str) or not url.startswith("https://"):
            return _err(404, "not_found")
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=4, border=2)
        return Response(buf.getvalue(), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    return router
