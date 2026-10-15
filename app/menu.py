"""Normalize a query-meals `data` payload into the join-page menu."""
from decimal import InvalidOperation

from app.money import yuan_to_fen


def _price_fen(v):
    """Integer fen for a currentPrice (number or numeric string), or None if it is unusable:
    missing, bool, non-numeric, NaN/Infinity, out of range (decimal.InvalidOperation), negative."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        fen = yuan_to_fen(v)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return fen if fen >= 0 else None


def normalize_menu(data: dict) -> dict:
    meals = data.get("meals") or {}
    usable = {}
    for code, m in meals.items():
        if not isinstance(m, dict) or m.get("withOrder"):
            continue
        fen = _price_fen(m.get("currentPrice"))
        if fen is None:  # M4: one broken meal is skipped, it never fails session creation
            continue
        usable[code] = (m, fen)
    categories = []
    used: set[str] = set()
    for cat in data.get("categories") or []:
        name = (cat.get("name") or "").replace("\n", "")
        if "麦金卡" in name:
            continue
        codes = []
        for ref in cat.get("meals") or []:
            code = ref.get("code")
            if code in usable and code not in codes:
                codes.append(code)
        if codes:
            categories.append({"name": name, "codes": codes})
            used.update(codes)
    items = {
        code: {
            "name": usable[code][0].get("name", ""),
            "image": usable[code][0].get("image", ""),
            "price_fen": usable[code][1],
        }
        for code in used
    }
    return {"categories": categories, "items": items}
