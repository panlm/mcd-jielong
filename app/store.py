"""SQLite storage. One shared connection guarded by a lock (thread-safe).

Datetimes are stored as ISO-8601 strings with offset; callers parse with
datetime.fromisoformat.
"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from app import group

SESSION_FIELDS = (
    "sid", "created_at", "title", "address_id", "address_label", "store_code",
    "be_code", "store_name", "cutoff_at", "cap_fen", "menu_json", "state",
    "quote_json", "order_id", "pay_url", "order_status",
    "expect_delivery_time", "updated_at", "recent_checked",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY, created_at TEXT, title TEXT, address_id TEXT,
  address_label TEXT, store_code TEXT, be_code TEXT, store_name TEXT,
  cutoff_at TEXT, cap_fen INTEGER, menu_json TEXT, state TEXT,
  quote_json TEXT, order_id TEXT, pay_url TEXT, order_status TEXT,
  expect_delivery_time TEXT, updated_at TEXT,
  recent_checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL, seq INTEGER NOT NULL,
  name TEXT NOT NULL, items_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  secret_hash TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_sid ON entries(sid);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateConflict(Exception):
    """A conditional entry write found its session no longer in the required state."""


# SQL safety: some statements are built with f-strings, but only identifiers come from code —
# column names are checked against SESSION_FIELDS / a fixed entry-field set (_check), and IN-lists
# are runs of '?' placeholders. Every value is passed as a bound parameter, never interpolated.
# The `nosec B608` / `nosemgrep` markers below refer to this note.
class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(sessions)")}
            if "recent_checked" not in cols:  # databases created before I2
                self._db.execute(
                    "ALTER TABLE sessions ADD COLUMN recent_checked INTEGER NOT NULL DEFAULT 0")
            self._db.commit()

    # --- sessions ---
    def create_session(self, **fields) -> str:
        self._check(fields)
        fields.setdefault("created_at", _now())
        fields.setdefault("updated_at", fields["created_at"])
        cols = list(fields)
        sql = f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"  # nosec B608 - identifiers only from allowlists/'?' marks; values bound
        with self._lock:
            self._db.execute(sql, [fields[c] for c in cols])  # nosemgrep
            self._db.commit()
        return fields["sid"]

    def get_session(self, sid):
        with self._lock:
            row = self._db.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
        return dict(row) if row else None

    def update_session(self, sid, **fields) -> None:
        self._check(fields)
        fields.pop("sid", None)
        fields.setdefault("updated_at", _now())
        sets = ",".join(f"{c}=?" for c in fields)
        with self._lock:
            self._db.execute(f"UPDATE sessions SET {sets} WHERE sid=?", [*fields.values(), sid])  # nosec B608 - identifiers only from allowlists/'?' marks; values bound; nosemgrep
            self._db.commit()

    @staticmethod
    def _entering(to_state) -> str:
        """Extra SET clause for a state change. Entering UNCERTAIN always clears recent_checked
        (I2: the host must run 查最近订单 again for *this* uncertain outcome)."""
        return ", recent_checked=0" if to_state == group.UNCERTAIN else ""

    def transition(self, sid, from_states, to_state, only_if=None) -> bool:
        """Compare-and-set on state (plus optional column == value conditions in only_if)."""
        states = list(from_states)
        if not states:
            return False
        only_if = dict(only_if or {})
        self._check(only_if)
        marks = ",".join("?" * len(states))
        extra = "".join(f" AND {c}=?" for c in only_if)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE sessions SET state=?, updated_at=?{self._entering(to_state)}"  # nosec B608 - identifiers only from allowlists/'?' marks; values bound
                f" WHERE sid=? AND state IN ({marks}){extra}",
                [to_state, _now(), sid, *states, *only_if.values()],
            )
            self._db.commit()
            return cur.rowcount == 1

    def mark_recent_checked(self, sid, seen_updated_at) -> bool:
        """Set recent_checked=1 only if the session is still in the same UNCERTAIN episode the
        caller saw (state UNCERTAIN and updated_at unchanged). Does not bump updated_at."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE sessions SET recent_checked=1 WHERE sid=? AND state=? AND updated_at=?",
                (sid, group.UNCERTAIN, seen_updated_at))
            self._db.commit()
            return cur.rowcount == 1

    def count_sessions(self, states) -> int:
        states = list(states)
        if not states:
            return 0
        marks = ",".join("?" * len(states))
        with self._lock:
            return self._db.execute(  # nosemgrep
                f"SELECT COUNT(*) FROM sessions WHERE state IN ({marks})", states).fetchone()[0]  # nosec B608 - identifiers only from allowlists/'?' marks; values bound

    def transition_all(self, from_states, to_state) -> int:
        """Move every session in from_states to to_state; returns how many moved."""
        states = list(from_states)
        if not states:
            return 0
        marks = ",".join("?" * len(states))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE sessions SET state=?, updated_at=?{self._entering(to_state)}"  # nosec B608 - identifiers only from allowlists/'?' marks; values bound
                f" WHERE state IN ({marks})",
                [to_state, _now(), *states],
            )
            self._db.commit()
            return cur.rowcount

    @staticmethod
    def _check(fields):
        bad = set(fields) - set(SESSION_FIELDS)
        if bad:
            raise ValueError(f"unknown session fields: {sorted(bad)}")

    # --- entries ---
    # only_if_state: the write happens only if the session's stored state is in this set,
    # checked under the same lock as the write (so a concurrent lock/transition can't slip in
    # between); otherwise StateConflict is raised and nothing is written.
    def _require_state(self, sid, only_if_state) -> None:
        if only_if_state is None:
            return
        row = self._db.execute("SELECT state FROM sessions WHERE sid=?", (sid,)).fetchone()
        if row is None or row[0] not in set(only_if_state):
            raise StateConflict(sid)

    def _entry_sid(self, eid):
        row = self._db.execute("SELECT sid FROM entries WHERE id=?", (eid,)).fetchone()
        return row[0] if row else None

    def add_entry(self, sid, name, items, note, secret_hash, only_if_state=None) -> int:
        now = _now()
        with self._lock:
            self._require_state(sid, only_if_state)
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM entries WHERE sid=?", (sid,)
            ).fetchone()[0]
            cur = self._db.execute(
                "INSERT INTO entries (sid,seq,name,items_json,note,secret_hash,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sid, seq, name, json.dumps(items, ensure_ascii=False), note, secret_hash, now, now),
            )
            self._db.commit()
            return cur.lastrowid

    def update_entry(self, eid, secret_hash, only_if_state=None, **fields) -> bool:
        allowed = {"name", "items", "note"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown entry fields: {sorted(bad)}")
        cols, vals = [], []
        for k, v in fields.items():
            if k == "items":
                cols.append("items_json=?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                cols.append(f"{k}=?")
                vals.append(v)
        cols.append("updated_at=?")
        vals.append(_now())
        with self._lock:
            sid = self._entry_sid(eid)
            if sid is not None:
                self._require_state(sid, only_if_state)
            cur = self._db.execute(  # nosemgrep
                f"UPDATE entries SET {','.join(cols)} WHERE id=? AND secret_hash=? AND deleted=0",  # nosec B608 - identifiers only from allowlists/'?' marks; values bound
                [*vals, eid, secret_hash],
            )
            self._db.commit()
            return cur.rowcount == 1

    def delete_entry(self, eid, secret_hash, only_if_state=None) -> bool:
        with self._lock:
            sid = self._entry_sid(eid)
            if sid is not None:
                self._require_state(sid, only_if_state)
            cur = self._db.execute(
                "UPDATE entries SET deleted=1, updated_at=? WHERE id=? AND secret_hash=? AND deleted=0",
                (_now(), eid, secret_hash),
            )
            self._db.commit()
            return cur.rowcount == 1

    def host_delete_entry(self, sid, eid, only_if_state) -> bool:
        """I5: the host removes a live entry of `sid` (no secret needed). Soft delete plus
        quote_json=NULL in one transaction, only while the session's stored state is in
        only_if_state (StateConflict otherwise, nothing written). False if eid is not a live
        entry of sid."""
        now = _now()
        with self._lock:
            self._require_state(sid, only_if_state)
            cur = self._db.execute(
                "UPDATE entries SET deleted=1, updated_at=? WHERE id=? AND sid=? AND deleted=0",
                (now, eid, sid))
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute("UPDATE sessions SET quote_json=NULL, updated_at=? WHERE sid=?",
                             (now, sid))
            self._db.commit()
            return True

    def list_entries(self, sid):
        with self._lock:
            rows = self._db.execute(
                "SELECT id,seq,name,items_json,note,created_at,updated_at FROM entries"
                " WHERE sid=? AND deleted=0 ORDER BY seq",
                (sid,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["items"] = json.loads(d.pop("items_json"))
            out.append(d)
        return out

    def count_entries(self, sid) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM entries WHERE sid=? AND deleted=0", (sid,)
            ).fetchone()[0]

    def purge_older_than(self, seconds: int, now: datetime) -> int:
        """Delete sessions (and their entries) created more than `seconds` before now."""
        threshold = now - timedelta(seconds=seconds)
        with self._lock:
            rows = self._db.execute("SELECT sid, created_at FROM sessions").fetchall()
            old = [
                r["sid"] for r in rows
                if r["created_at"] and datetime.fromisoformat(r["created_at"]) < threshold
            ]
            for sid in old:
                self._db.execute("DELETE FROM entries WHERE sid=?", (sid,))
                self._db.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            self._db.commit()
        return len(old)
