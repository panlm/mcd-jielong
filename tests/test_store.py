import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import group
from app.store import Store

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def mk(store, sid="s1", **kw):
    fields = dict(
        sid=sid, created_at=NOW.isoformat(), title="t", cutoff_at=NOW.isoformat(),
        cap_fen=None, menu_json="{}", state=group.OPEN,
    )
    fields.update(kw)
    return store.create_session(**fields)


def test_session_roundtrip_and_update(store):
    assert mk(store) == "s1"
    s = store.get_session("s1")
    assert s["title"] == "t" and s["state"] == group.OPEN and s["order_id"] is None
    store.update_session("s1", pay_url="u", order_status="x")
    s = store.get_session("s1")
    assert s["pay_url"] == "u" and s["order_status"] == "x"
    assert store.get_session("nope") is None
    with pytest.raises(ValueError):
        store.update_session("s1", bogus=1)


def test_transition_is_atomic(store):
    mk(store, state=group.LOCKED)
    assert store.transition("s1", {group.LOCKED}, group.ORDERING) is True
    assert store.transition("s1", {group.LOCKED}, group.ORDERING) is False
    assert store.get_session("s1")["state"] == group.ORDERING


def test_transition_concurrent_single_winner(store):
    mk(store, state=group.LOCKED)
    results = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        results.append(store.transition("s1", {group.LOCKED}, group.ORDERING))

    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count(True) == 1


def test_entries_crud_and_seq(store):
    mk(store)
    e1 = store.add_entry("s1", "a", [{"code": "A", "qty": 1}], "n", "h1")
    e2 = store.add_entry("s1", "b", [{"code": "B", "qty": 2}], "", "h2")
    es = store.list_entries("s1")
    assert [e["seq"] for e in es] == [1, 2]
    assert es[0]["items"] == [{"code": "A", "qty": 1}] and es[0]["note"] == "n"
    assert {"id", "name", "created_at", "updated_at"} <= es[0].keys()
    assert "secret_hash" not in es[0]
    assert store.count_entries("s1") == 2
    assert store.delete_entry(e1, "h1") is True
    assert store.count_entries("s1") == 1
    assert [e["id"] for e in store.list_entries("s1")] == [e2]
    e3 = store.add_entry("s1", "c", [{"code": "A", "qty": 1}], "", "h3")
    assert store.list_entries("s1")[-1]["seq"] == 3 and e3 != e1


def test_entry_update_requires_secret(store):
    mk(store)
    eid = store.add_entry("s1", "a", [{"code": "A", "qty": 1}], "n", "good")
    before = store.list_entries("s1")
    assert store.update_entry(eid, "bad", name="z", items=[{"code": "B", "qty": 9}]) is False
    assert store.delete_entry(eid, "bad") is False
    assert store.list_entries("s1") == before
    assert store.update_entry(eid, "good", name="z", items=[{"code": "B", "qty": 9}]) is True
    e = store.list_entries("s1")[0]
    assert e["name"] == "z" and e["items"] == [{"code": "B", "qty": 9}] and e["note"] == "n"
    assert store.delete_entry(eid, "good") is True
    assert store.update_entry(eid, "good", name="q") is False
    assert store.delete_entry(eid, "good") is False


def test_purge_older_than(store):
    mk(store, "old", created_at=(NOW - timedelta(hours=3)).isoformat())
    mk(store, "new", created_at=(NOW - timedelta(hours=1)).isoformat())
    store.add_entry("old", "a", [{"code": "A", "qty": 1}], "", "h")
    store.add_entry("new", "a", [{"code": "A", "qty": 1}], "", "h")
    assert store.purge_older_than(2 * 3600, NOW) == 1
    assert store.get_session("old") is None and store.get_session("new") is not None
    assert store.count_entries("old") == 0 and store.count_entries("new") == 1


def test_entry_writes_conditional_on_session_state(store):
    from app.store import StateConflict
    mk(store, state=group.LOCKED)
    with pytest.raises(StateConflict):
        store.add_entry("s1", "a", [{"code": "c", "qty": 1}], "", "h", only_if_state={group.OPEN})
    assert store.list_entries("s1") == []
    eid = store.add_entry("s1", "a", [{"code": "c", "qty": 1}], "", "h")  # unconditional still works
    with pytest.raises(StateConflict):
        store.update_entry(eid, "h", only_if_state={group.OPEN}, name="b")
    with pytest.raises(StateConflict):
        store.delete_entry(eid, "h", only_if_state={group.OPEN})
    e = store.list_entries("s1")
    assert len(e) == 1 and e[0]["name"] == "a"
    store.transition("s1", {group.LOCKED}, group.OPEN)
    assert store.update_entry(eid, "h", only_if_state={group.OPEN}, name="b") is True
    assert store.update_entry(eid, "bad", only_if_state={group.OPEN}, name="c") is False  # secret
    assert store.delete_entry(eid, "h", only_if_state={group.OPEN}) is True
    assert store.add_entry("s1", "x", [], "", "h", only_if_state={group.OPEN}) > 0
    with pytest.raises(StateConflict):  # unknown session counts as "not in the state"
        store.add_entry("nope", "x", [], "", "h", only_if_state={group.OPEN})


def test_transition_all(store):
    mk(store, "a", state=group.ORDERING)
    mk(store, "b", state=group.ORDERING)
    mk(store, "c", state=group.LOCKED)
    assert store.transition_all({group.ORDERING}, group.UNCERTAIN) == 2
    assert [store.get_session(s)["state"] for s in "abc"] == [group.UNCERTAIN] * 2 + [group.LOCKED]
    assert store.transition_all(set(), group.OPEN) == 0


def test_old_database_gains_recent_checked_column(tmp_path):
    """Databases created before I2 get the column on open (default 0 = not checked)."""
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""
CREATE TABLE sessions (sid TEXT PRIMARY KEY, created_at TEXT, title TEXT, address_id TEXT,
  address_label TEXT, store_code TEXT, be_code TEXT, store_name TEXT, cutoff_at TEXT,
  cap_fen INTEGER, menu_json TEXT, state TEXT, quote_json TEXT, order_id TEXT, pay_url TEXT,
  order_status TEXT, expect_delivery_time TEXT, updated_at TEXT);
INSERT INTO sessions (sid, state) VALUES ('old1', 'uncertain');
""")
    db.commit()
    db.close()
    s = Store(path)
    assert s.get_session("old1")["recent_checked"] == 0
    Store(path)  # idempotent
    assert s.transition("old1", {"uncertain"}, "locked", only_if={"recent_checked": 1}) is False
    s.update_session("old1", recent_checked=1)
    assert s.transition("old1", {"uncertain"}, "locked", only_if={"recent_checked": 1}) is True


def test_transition_into_uncertain_clears_recent_checked(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.create_session(sid="a", state="ordering", recent_checked=1)
    s.create_session(sid="b", state="ordering", recent_checked=1)
    assert s.transition("a", {"ordering"}, "uncertain")
    assert s.get_session("a")["recent_checked"] == 0
    assert s.transition_all({"ordering"}, "uncertain") == 1
    assert s.get_session("b")["recent_checked"] == 0
    with pytest.raises(ValueError):
        s.transition("a", {"uncertain"}, "locked", only_if={"bogus": 1})


def test_host_delete_entry_soft_deletes_and_voids_quote(store):
    from app.store import StateConflict
    mk(store, "s1", state="locked")
    mk(store, "s2", state="ordering")
    store.update_session("s1", quote_json="{}")
    e1 = store.add_entry("s1", "a", [{"code": "A", "qty": 1}], "", "h")
    e2 = store.add_entry("s2", "b", [{"code": "A", "qty": 1}], "", "h")
    ok_states = {"open", "locked"}
    with pytest.raises(StateConflict):  # s2 is ORDERING: nothing written
        store.host_delete_entry("s2", e2, only_if_state=ok_states)
    assert [e["id"] for e in store.list_entries("s2")] == [e2]
    assert store.host_delete_entry("s1", e2, only_if_state=ok_states) is False  # not s1's entry
    assert [e["id"] for e in store.list_entries("s2")] == [e2]
    assert store.get_session("s1")["quote_json"] == "{}"  # a failed delete keeps the quote
    assert store.host_delete_entry("s1", e1, only_if_state=ok_states) is True
    assert store.list_entries("s1") == [] and store.get_session("s1")["quote_json"] is None
    assert store.host_delete_entry("s1", e1, only_if_state=ok_states) is False  # already gone
