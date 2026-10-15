"""Group-member endpoints. Never touch MCP."""
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app import group
from app.security import RateLimiter, client_ip, hash_secret, new_secret
from app.store import StateConflict

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_ENTRIES = 50
MEMBER_WRITES_PER_MIN = 120  # per IP; a whole office usually shares one NAT IP (M3)
WRITABLE = frozenset({group.OPEN})  # re-checked by the store inside the write itself


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _enrich(entry: dict, menu: dict) -> dict:
    rows, subtotal = [], 0
    for r in entry["items"]:
        m = menu["items"].get(r["code"], {})
        price = m.get("price_fen", 0)
        rows.append({"code": r["code"], "name": m.get("name", r["code"]),
                     "qty": r["qty"], "price_fen": price})
        subtotal += price * r["qty"]
    return {"id": entry["id"], "seq": entry["seq"], "name": entry["name"],
            "items": rows, "note": entry["note"], "subtotal_fen": subtotal}


def build_join_payload(store, s: dict, now: datetime) -> dict:
    """Public (member-safe) view of a session row. Shared with the host API."""
    state = group.effective_state(s["state"], datetime.fromisoformat(s["cutoff_at"]), now)
    menu = json.loads(s["menu_json"])
    entries = [_enrich(e, menu) for e in store.list_entries(s["sid"])]
    return {
        "title": s["title"], "address_label": s["address_label"],
        "store_name": s["store_name"], "cutoff_at": s["cutoff_at"],
        "cap_fen": s["cap_fen"], "state": state,
        "order_status": s["order_status"],
        "expect_delivery_time": s["expect_delivery_time"],
        "now": now.isoformat(),
        "menu": menu, "entries": entries,
        "total_fen": sum(e["subtotal_fen"] for e in entries),
        "count": len(entries),
    }


def build_router(clock=None) -> APIRouter:
    router = APIRouter()
    limiter = RateLimiter(MEMBER_WRITES_PER_MIN, 60, clock or time.monotonic)

    def _load(request: Request, sid: str):
        s = request.app.state.store.get_session(sid)
        if s is None:
            return None, None, _err(404, "not_found")
        now = request.app.state.now()
        state = group.effective_state(s["state"], datetime.fromisoformat(s["cutoff_at"]), now)
        return s, state, None

    @router.get("/j/{sid}")
    def join_page(sid: str):
        return FileResponse(WEB_DIR / "join.html", media_type="text/html")

    @router.get("/api/j/{sid}")
    def join_payload(sid: str, request: Request):
        s, _state, err = _load(request, sid)
        if err:
            return err
        return build_join_payload(request.app.state.store, s, request.app.state.now())

    async def _body(request: Request):
        try:
            b = await request.json()
        except Exception:
            return None
        return b if isinstance(b, dict) else None

    def _validate(b, menu, cap_fen):
        return group.validate_entry(b.get("name"), b.get("items"), b.get("note", ""), menu, cap_fen)

    @router.post("/api/j/{sid}/entries")
    async def add_entry(sid: str, request: Request):
        if not limiter.check(client_ip(request)):
            return _err(429, "rate_limited")
        s, state, err = _load(request, sid)
        if err:
            return err
        if state != group.OPEN:
            return _err(409, "closed")
        store = request.app.state.store
        if store.count_entries(sid) >= MAX_ENTRIES:
            return _err(422, "full")
        b = await _body(request)
        if b is None:
            return _err(422, "bad_request")
        try:
            name, items, note, _ = _validate(b, json.loads(s["menu_json"]), s["cap_fen"])
        except group.EntryError as e:
            return _err(422, e.code)
        secret, h = new_secret()
        try:
            eid = store.add_entry(sid, name, items, note, h, only_if_state=WRITABLE)
        except StateConflict:  # host locked (or an order started) while we were parsing
            return _err(409, "closed")
        return JSONResponse({"id": eid, "secret": secret}, status_code=201)

    def _owned(request: Request, sid: str, eid: int, secret: str | None):
        """Return an error response, or None when eid belongs to sid (secret checked by store)."""
        if eid not in {e["id"] for e in request.app.state.store.list_entries(sid)}:
            return _err(404, "not_found")
        if not secret:
            return _err(403, "forbidden")
        return None

    @router.put("/api/j/{sid}/entries/{eid}")
    async def edit_entry(sid: str, eid: int, request: Request):
        if not limiter.check(client_ip(request)):
            return _err(429, "rate_limited")
        s, state, err = _load(request, sid)
        if err:
            return err
        if state != group.OPEN:
            return _err(409, "closed")
        secret = request.headers.get("x-entry-secret")
        err = _owned(request, sid, eid, secret)
        if err:
            return err
        b = await _body(request)
        if b is None:
            return _err(422, "bad_request")
        try:
            name, items, note, _ = _validate(b, json.loads(s["menu_json"]), s["cap_fen"])
        except group.EntryError as e:
            return _err(422, e.code)
        try:
            ok = request.app.state.store.update_entry(
                eid, hash_secret(secret), only_if_state=WRITABLE, name=name, items=items, note=note)
        except StateConflict:
            return _err(409, "closed")
        if not ok:
            return _err(403, "forbidden")
        return {"ok": True}

    @router.delete("/api/j/{sid}/entries/{eid}")
    def delete_entry(sid: str, eid: int, request: Request):
        if not limiter.check(client_ip(request)):
            return _err(429, "rate_limited")
        s, state, err = _load(request, sid)
        if err:
            return err
        if state != group.OPEN:
            return _err(409, "closed")
        secret = request.headers.get("x-entry-secret")
        err = _owned(request, sid, eid, secret)
        if err:
            return err
        try:
            ok = request.app.state.store.delete_entry(eid, hash_secret(secret), only_if_state=WRITABLE)
        except StateConflict:
            return _err(409, "closed")
        if not ok:
            return _err(403, "forbidden")
        return {"ok": True}

    return router
