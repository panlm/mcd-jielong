"""Build sanitized mock fixtures from raw recorded MCP responses.

Usage: python3 -I scripts/make_mock_data.py /tmp/mcd-1024/raw app/mock_data
Raw files contain real personal data; only the sanitized output may enter the repo.
"""
import json
import sys
from pathlib import Path

PUBLIC_IMAGE_PREFIX = "https://menu-img.mcd.cn/"

MAPPING = {
    "addresses-after.json": "delivery-query-addresses.json",
    "stores-qiantan-bt2.json": "delivery-query-stores.json",
    "meals-1450326.json": "query-meals.json",
    "price-1450326.json": "calculate-price.json",
    "coupons-1450326.json": "query-store-coupons.json",
    "t1-create.json": "create-order.json",
    "t1-query-after-cancel.json": "query-order.json",
    "order-list.json": "order-list.json",
    "t1-cancel.json": "cancel-order.json",
}

FAKE_ADDRESSES = [
    {"addressId": "demo-addr-1", "contactName": "示例", "phone": "138****0000",
     "fullAddress": "上海市浦东新区示例路1号 示例大厦21楼"},
    {"addressId": "demo-addr-2", "contactName": "示例", "phone": "138****0000",
     "fullAddress": "上海市浦东新区示例路1号 示例大厦3楼"},
]
FAKE_DELIVERY_INFO = {
    "deliveryType": "麦乐送",
    "deliveryAddress": "上海市浦东新区示例路1号",
    "addressDetail": "示例大厦21楼",
    "customerNickname": "示例",
    "mobilePhone": "138****0000",
    "expectDeliveryTime": "立即送出",
}


def fake_id(real: str, prefix: str = "DEMO") -> str:
    h = 0
    for ch in real:
        h = (h * 131 + ord(ch)) % (10**8)
    return prefix + str(h).zfill(8)


def sanitize_order(o: dict) -> dict:
    """Rewrite one order-shaped dict in place (detail or list entry)."""
    if "orderId" in o:
        o["orderId"] = fake_id(o["orderId"])
    if "payId" in o:
        o["payId"] = fake_id(o["payId"], "DEMOPAY")
    if "deliveryInfo" in o:
        o["deliveryInfo"] = dict(FAKE_DELIVERY_INFO)
    if "remark" in o:
        o["remark"] = ""
    if "pickupCode" in o:
        o["pickupCode"] = ""
    return o


def sanitize(name: str, doc: dict) -> dict:
    doc = dict(doc)
    doc["traceId"] = "demo-trace"
    data = doc.get("data")
    if name == "delivery-query-addresses.json":
        data = {"addresses": [dict(a) for a in FAKE_ADDRESSES]}
    elif name == "create-order.json":
        detail = sanitize_order(data["orderDetail"])
        data["orderId"] = fake_id(data["orderId"])
        data["payId"] = fake_id(data["payId"], "DEMOPAY")
        data["payH5Url"] = "https://example.invalid/pay/demo"
        detail["orderId"] = data["orderId"]
        detail["payId"] = data["payId"]
    elif name in ("query-order.json", "cancel-order.json"):
        sanitize_order(data)
    elif name == "order-list.json":
        data["list"] = [sanitize_order(o) for o in data["list"][:2]]
    elif name == "query-meals.json":
        for i, meal in enumerate(data["meals"].values()):
            # public product photos are not personal data: keep them so the demo menu has pictures
            if not str(meal.get("image", "")).startswith(PUBLIC_IMAGE_PREFIX):
                meal["image"] = f"https://example.invalid/img/{i}.png"
    doc["data"] = data
    return doc


def main(src: Path, dst: Path) -> None:
    if not src.is_dir():
        sys.exit(f"raw dir not found: {src} (re-record raw MCP responses first)")
    dst.mkdir(parents=True, exist_ok=True)
    for raw_name, out_name in MAPPING.items():
        raw = json.loads((src / raw_name).read_text(encoding="utf-8"))
        doc = sanitize(out_name, raw["result"]["structuredContent"])
        (dst / out_name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("wrote", out_name)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
