from datetime import datetime, timedelta, timezone

import pytest

from app import group
from app.group import EntryError, validate_entry

MENU = {
    "categories": [{"name": "x", "codes": ["A", "B"]}],
    "items": {
        "A": {"name": "a", "image": "", "price_fen": 1550},
        "B": {"name": "b", "image": "", "price_fen": 3350},
    },
}
TZ = timezone(timedelta(hours=8))


def test_effective_state_locks_after_cutoff():
    cutoff = datetime(2026, 10, 14, 12, 0, tzinfo=TZ)
    before = cutoff - timedelta(seconds=1)
    assert group.effective_state(group.OPEN, cutoff, before) == group.OPEN
    assert group.effective_state(group.OPEN, cutoff, cutoff) == group.LOCKED
    assert group.effective_state(group.OPEN, cutoff, cutoff + timedelta(hours=1)) == group.LOCKED
    for st in (group.ORDERED, group.ORDERING, group.UNCERTAIN, group.CANCELLED, group.LOCKED):
        assert group.effective_state(st, cutoff, cutoff + timedelta(days=1)) == st


@pytest.mark.parametrize(
    "name,items,note,cap,code",
    [
        ("", [{"code": "A", "qty": 1}], "", None, "name"),
        ("   ", [{"code": "A", "qty": 1}], "", None, "name"),
        ("字" * 21, [{"code": "A", "qty": 1}], "", None, "name"),
        ("ok", [{"code": "A", "qty": 1}], "备" * 41, None, "note"),
        ("ok", [], "", None, "items"),
        ("ok", [{"code": "A", "qty": 1}] * 21, "", None, "items"),
        ("ok", [{"code": "A", "qty": 0}], "", None, "qty"),
        ("ok", [{"code": "A", "qty": 11}], "", None, "qty"),
        ("ok", [{"code": "ZZ", "qty": 1}], "", None, "unknown_code"),
        ("ok", [{"code": "B", "qty": 2}], "", 5000, "cap"),
    ],
)
def test_validate_entry_rules(name, items, note, cap, code):
    with pytest.raises(EntryError) as ei:
        validate_entry(name, items, note, MENU, cap)
    assert ei.value.code == code


def test_validate_entry_boundaries_ok():
    n, items, note, sub = validate_entry(
        " " + "字" * 20 + " ", [{"code": "A", "qty": 10}], "备" * 40, MENU, 15500
    )
    assert n == "字" * 20 and note == "备" * 40 and sub == 15500
    assert validate_entry("a", [{"code": "A", "qty": 1}], "", MENU, 0)[3] == 1550  # 0 = no cap


def test_validate_entry_merges_same_code_and_computes_subtotal():
    n, items, note, sub = validate_entry(
        " 小明 ", [{"code": "A", "qty": 1}, {"code": "B", "qty": 2}, {"code": "A", "qty": 3}],
        " 少冰 ", MENU, None,
    )
    assert n == "小明" and note == "少冰"
    assert items == [{"code": "A", "qty": 4}, {"code": "B", "qty": 2}]
    assert sub == 4 * 1550 + 2 * 3350


def test_aggregate_items_sums_across_entries_sorted():
    entries = [
        {"items": [{"code": "B", "qty": 2}, {"code": "A", "qty": 1}]},
        {"items": [{"code": "A", "qty": 4}]},
    ]
    assert group.aggregate_items(entries) == [
        {"productCode": "A", "quantity": 5},
        {"productCode": "B", "quantity": 2},
    ]
