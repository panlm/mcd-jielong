"""Group-order domain rules: states, entry validation, aggregation."""
from datetime import datetime

OPEN = "open"
LOCKED = "locked"
ORDERING = "ordering"
UNCERTAIN = "uncertain"
ORDERED = "ordered"
CANCELLED = "cancelled"

MAX_NAME = 20
MAX_NOTE = 40
MAX_ROWS = 20
MAX_QTY = 10


class EntryError(ValueError):
    """code: name|note|items|qty|unknown_code|cap|full"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def effective_state(state: str, cutoff_at: datetime, now: datetime) -> str:
    if state == OPEN and now >= cutoff_at:
        return LOCKED
    return state


def validate_entry(name, items, note, menu, cap_fen):
    name = (name or "").strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= MAX_NAME:
        raise EntryError("name")
    note = note.strip() if isinstance(note, str) else ""
    if len(note) > MAX_NOTE:
        raise EntryError("note")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ROWS:
        raise EntryError("items")
    merged: dict[str, int] = {}
    for row in items:
        if not isinstance(row, dict):
            raise EntryError("items")
        qty = row.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise EntryError("qty")
        code = row.get("code")
        if not isinstance(code, str) or code not in menu["items"]:
            raise EntryError("unknown_code")
        merged[code] = merged.get(code, 0) + qty
    cleaned = [{"code": c, "qty": q} for c, q in merged.items()]
    subtotal = sum(menu["items"][c]["price_fen"] * q for c, q in merged.items())
    if cap_fen and subtotal > cap_fen:
        raise EntryError("cap")
    return name, cleaned, note, subtotal


def aggregate_items(entries):
    totals: dict[str, int] = {}
    for e in entries:
        for row in e["items"]:
            totals[row["code"]] = totals.get(row["code"], 0) + row["qty"]
    return [{"productCode": c, "quantity": totals[c]} for c in sorted(totals)]
