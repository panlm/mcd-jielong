import json
from pathlib import Path

import pytest

from app.menu import normalize_menu

MOCK = Path(__file__).resolve().parent.parent / "app" / "mock_data" / "query-meals.json"


def _meal(price="10", **kw):
    return {"name": "x", "image": "http://i/x.png", "currentPrice": price, **kw}


def _data(categories, meals):
    return {"categories": categories, "meals": meals}


def test_category_names_strip_newlines():
    out = normalize_menu(_data([{"name": "鸡肉\n汉堡/卷", "meals": [{"code": "A"}]}], {"A": _meal()}))
    assert out["categories"][0]["name"] == "鸡肉汉堡/卷"


def test_drops_membership_card_category_and_withorder_items():
    data = _data(
        [
            {"name": "麦金卡专享", "meals": [{"code": "A"}]},
            {"name": "饮品", "meals": [{"code": "B"}, {"code": "W"}]},
            {"name": "炸鸡", "meals": [{"code": "W"}, {"code": "C"}]},
        ],
        {"A": _meal(), "B": _meal(), "C": _meal(), "W": _meal(withOrder=[{"code": "B"}])},
    )
    out = normalize_menu(data)
    assert [c["name"] for c in out["categories"]] == ["饮品", "炸鸡"]
    assert out["categories"][0]["codes"] == ["B"]
    assert out["categories"][1]["codes"] == ["C"]
    assert "W" not in out["items"] and "A" not in out["items"]


def test_price_string_and_number():
    out = normalize_menu(_data([{"name": "饮品", "meals": [{"code": "A"}, {"code": "B"}]}],
                               {"A": _meal("33.5"), "B": _meal(33.5)}))
    assert out["items"]["A"]["price_fen"] == 3350
    assert out["items"]["B"]["price_fen"] == 3350
    assert out["items"]["A"] == {"name": "x", "image": "http://i/x.png", "price_fen": 3350}


def test_drops_empty_categories_and_unknown_codes():
    data = _data(
        [
            {"name": "甲", "meals": [{"code": "A"}, {"code": "NOPE"}]},
            {"name": "乙", "meals": [{"code": "GONE"}]},
        ],
        {"A": _meal()},
    )
    out = normalize_menu(data)
    assert out["categories"] == [{"name": "甲", "codes": ["A"]}]
    assert list(out["items"]) == ["A"]


def test_mock_menu_nonempty():
    data = json.loads(MOCK.read_text(encoding="utf-8"))["data"]
    out = normalize_menu(data)
    assert len(out["categories"]) >= 5
    for c in out["categories"]:
        assert c["codes"]
        assert "\n" not in c["name"] and "麦金卡" not in c["name"]
        for code in c["codes"]:
            assert code in out["items"]


@pytest.mark.parametrize("bad", [None, "", "abc", "NaN", "Infinity", "-1", -3, True, {}, [], "1e999999"])
def test_meal_with_bad_price_is_skipped_not_fatal(bad):
    """M4: one broken meal (incl. decimal.InvalidOperation) must not break the whole menu."""
    meals = {"A": _meal("15.5"), "B": _meal(bad), "C": {"name": "无价"}}
    data = _data([{"name": "甲", "meals": [{"code": "A"}, {"code": "B"}, {"code": "C"}]},
                  {"name": "乙", "meals": [{"code": "B"}]}], meals)
    out = normalize_menu(data)
    assert out["categories"] == [{"name": "甲", "codes": ["A"]}]
    assert list(out["items"]) == ["A"] and out["items"]["A"]["price_fen"] == 1550


def test_zero_price_is_kept():
    out = normalize_menu(_data([{"name": "甲", "meals": [{"code": "A"}]}], {"A": _meal("0")}))
    assert out["items"]["A"]["price_fen"] == 0
