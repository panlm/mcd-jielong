import json
from pathlib import Path

import leakscan
from app.mcp_client import ALLOWED_TOOLS

MOCK_DIR = Path(__file__).resolve().parent.parent / "app" / "mock_data"


def test_mock_data_has_all_whitelisted_tools():
    for tool in ALLOWED_TOOLS:
        data = json.loads((MOCK_DIR / f"{tool}.json").read_text(encoding="utf-8"))
        assert data["success"] is True and "data" in data


def test_mock_data_has_no_pii():
    """Generic shapes only (phone numbers, pay pages, account / instance ids); real names and
    addresses are checked through the optional, untracked tests/.pii-denylist.txt."""
    entries = leakscan.load_denylist() or []
    for p in sorted(MOCK_DIR.glob("*.json")):
        text = p.read_text(encoding="utf-8")
        assert not leakscan.generic_hits(text), (p.name, leakscan.generic_hits(text))
        assert not leakscan.denylist_hits(text, entries), p.name
    # the fake contacts the sanitizer writes are masked, never a dialable number
    addrs = json.loads((MOCK_DIR / "delivery-query-addresses.json").read_text(encoding="utf-8"))
    for a in addrs["data"]["addresses"]:
        assert "*" in a["phone"] and a["contactName"] == "示例"


def test_mock_menu_keeps_only_public_product_images():
    """Public product photos (menu-img.mcd.cn) stay so the demo menu has pictures; any other
    image host is replaced. Payment pages stay forbidden (see leakscan.PATTERNS)."""
    data = json.loads((MOCK_DIR / "query-meals.json").read_text(encoding="utf-8"))["data"]
    images = [m.get("image", "") for m in data["meals"].values()]
    assert any(i.startswith("https://menu-img.mcd.cn/") for i in images)
    for i in images:
        assert i.startswith(("https://menu-img.mcd.cn/", "https://example.invalid/")), i
