"""SQLite database of watched tickets: latest verified state + a log of every change.

A leg is one real one-way ticket (route, departure time, airline, flight number, stops). The watcher
re-checks it with the provider every WATCH_INTERVAL_SECONDS and writes what it saw here. Only real
changes (price up/down, gone, back) go to the `changes` log, so the file stays small even when
checks run every 10 seconds.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import FlightOption

SCHEMA = """
CREATE TABLE IF NOT EXISTS legs (
  key TEXT PRIMARY KEY,
  origin TEXT NOT NULL, destination TEXT NOT NULL,
  origin_airport TEXT, destination_airport TEXT,
  departure_at TEXT NOT NULL, airline TEXT, flight_number TEXT, stops INTEGER NOT NULL,
  currency TEXT NOT NULL, first_price REAL NOT NULL, booking_url TEXT,
  added_at TEXT NOT NULL, search_id TEXT, pinned INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending', price REAL, price_seen_on TEXT,
  last_checked_at TEXT, last_change_at TEXT,
  checks INTEGER NOT NULL DEFAULT 0, changes INTEGER NOT NULL DEFAULT 0,
  alt_price REAL, alt_url TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
  old_price REAL, new_price REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS changes_key_at ON changes(key, at);
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, ms INTEGER NOT NULL, requests INTEGER NOT NULL,
  legs INTEGER NOT NULL, changed INTEGER NOT NULL, errors INTEGER NOT NULL
);
-- Telegram alerts: a browser's secret code is linked to a chat by /start <code>;
-- tg_subs are the tickets that browser watches, with the state it was last told about.
CREATE TABLE IF NOT EXISTS tg_links (
  code TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, lang TEXT NOT NULL DEFAULT 'ru', linked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tg_links_chat ON tg_links(chat_id);
CREATE TABLE IF NOT EXISTS tg_subs (
  code TEXT NOT NULL, key TEXT NOT NULL, created_at TEXT NOT NULL,
  notified_status TEXT, notified_price REAL, notified_at TEXT,
  PRIMARY KEY (code, key)
);
CREATE INDEX IF NOT EXISTS tg_subs_key ON tg_subs(key);
"""
KEEP_CYCLES = 2000


def leg_key(f: FlightOption) -> str:
    return f.watch_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(legs)")}
            if "touched_at" not in cols:  # databases created before multi-user watching
                self._db.execute("ALTER TABLE legs ADD COLUMN touched_at TEXT")
                self._db.execute("UPDATE legs SET touched_at = added_at")
            self._db.commit()

    # ── what to watch ────────────────────────────────────────────────────────
    def track_search(self, search_id: str, legs: list[FlightOption], keep_seconds: float = 3 * 3600,
                     pin_seconds: float = 7 * 86400, max_legs: int = 200) -> None:
        """Watch the legs of a new search next to other visitors' recent ones.

        Unpinned legs stop being watched `keep_seconds` after the last search that returned them,
        pinned ones after `pin_seconds`; beyond `max_legs` the least recently searched go first.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            for f in legs:
                self._upsert(f, search_id)
            self._db.execute("UPDATE legs SET active = 0 WHERE pinned = 0 AND touched_at < ?",
                             ((now - timedelta(seconds=keep_seconds)).isoformat(timespec="seconds"),))
            self._db.execute("UPDATE legs SET pinned = 0, active = 0 WHERE pinned = 1 AND touched_at < ?",
                             ((now - timedelta(seconds=pin_seconds)).isoformat(timespec="seconds"),))
            self._db.execute(
                """UPDATE legs SET active = 0 WHERE active = 1 AND pinned = 0 AND key NOT IN (
                     SELECT key FROM legs WHERE active = 1 ORDER BY pinned DESC, touched_at DESC LIMIT ?)""",
                (max_legs,))
            self._db.commit()

    def pin(self, keys: list[str], pinned: bool = True) -> int:
        with self._lock:
            if pinned:
                cur = self._db.executemany(
                    "UPDATE legs SET pinned = 1, touched_at = ?, active = 1 WHERE key = ?",
                    [(_now(), k) for k in keys])
            else:  # stays pinned while another browser still watches it
                cur = self._db.executemany(
                    """UPDATE legs SET pinned = 0, touched_at = ? WHERE key = ?
                       AND NOT EXISTS (SELECT 1 FROM tg_subs s WHERE s.key = legs.key)""",
                    [(_now(), k) for k in keys])
            self._db.commit()
            return cur.rowcount

    def _upsert(self, f: FlightOption, search_id: str) -> None:
        now = _now()
        self._db.execute(
            """INSERT INTO legs (key, origin, destination, origin_airport, destination_airport, departure_at,
                   airline, flight_number, stops, currency, first_price, booking_url, added_at, search_id,
                   price, price_seen_on, last_checked_at, status, touched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'available', ?)
               ON CONFLICT(key) DO UPDATE SET active = 1, search_id = excluded.search_id,
                   touched_at = excluded.touched_at""",
            (leg_key(f), f.origin_city, f.destination_city, f.origin_airport, f.destination_airport,
             f.departure_at.isoformat(), f.airline, f.flight_number, f.stops, f.currency, f.price,
             f.booking_url, now, search_id, f.price,
             f.price_seen_on.isoformat() if f.price_seen_on else None,
             f.checked_at.isoformat(timespec="seconds"), now))

    def active_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM legs WHERE active = 1").fetchone()[0]

    def active_legs(self) -> list[sqlite3.Row]:
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = self._db.execute("SELECT * FROM legs WHERE active = 1").fetchall()
        return [r for r in rows if datetime.fromisoformat(r["departure_at"]) > now]

    # ── results of a check ───────────────────────────────────────────────────
    def record(self, key: str, *, found: FlightOption | None, alt: FlightOption | None,
               error: str | None = None) -> str | None:
        """Store one check. Returns the change kind (price_up/price_down/gone/back) or None."""
        at = _now()
        with self._lock:
            row = self._db.execute("SELECT * FROM legs WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            if error:
                self._db.execute("UPDATE legs SET last_error = ?, last_checked_at = ? WHERE key = ?",
                                 (error, at, key))
                self._db.commit()
                return None
            kind = None
            if found is not None:
                old = row["price"]
                if row["status"] == "gone":
                    kind = "back"
                elif old is not None and found.price > old + 0.5:
                    kind = "price_up"
                elif old is not None and found.price < old - 0.5:
                    kind = "price_down"
                self._db.execute(
                    """UPDATE legs SET status = 'available', price = ?, price_seen_on = ?, booking_url = ?,
                           last_checked_at = ?, checks = checks + 1, alt_price = NULL, alt_url = NULL,
                           last_error = NULL WHERE key = ?""",
                    (found.price, found.price_seen_on.isoformat() if found.price_seen_on else row["price_seen_on"],
                     found.booking_url, at, key))
                new_price = found.price
            else:
                if row["status"] != "gone":
                    kind = "gone"
                self._db.execute(
                    """UPDATE legs SET status = 'gone', last_checked_at = ?, checks = checks + 1,
                           alt_price = ?, alt_url = ?, last_error = NULL WHERE key = ?""",
                    (at, alt.price if alt else None, alt.booking_url if alt else None, key))
                new_price = None
            if kind:
                note = None
                if kind == "gone" and alt is not None:
                    note = f"{alt.price:.0f}"  # the page shows it as "cheapest on this date now"
                self._db.execute(
                    "INSERT INTO changes (key, at, kind, old_price, new_price, note) VALUES (?,?,?,?,?,?)",
                    (key, at, kind, row["price"], new_price, note))
                self._db.execute("UPDATE legs SET changes = changes + 1, last_change_at = ? WHERE key = ?",
                                 (at, key))
            self._db.commit()
            return kind

    def log_cycle(self, started_at: datetime, ms: int, requests: int, legs: int, changed: int, errors: int):
        with self._lock:
            self._db.execute(
                "INSERT INTO cycles (started_at, ms, requests, legs, changed, errors) VALUES (?,?,?,?,?,?)",
                (started_at.isoformat(timespec="seconds"), ms, requests, legs, changed, errors))
            self._db.execute("DELETE FROM cycles WHERE id <= (SELECT MAX(id) FROM cycles) - ?", (KEEP_CYCLES,))
            self._db.commit()

    # ── reading ──────────────────────────────────────────────────────────────
    def snapshot(self, keys: list[str] | None = None, changes_per_leg: int = 5) -> list[dict]:
        """Watched legs with their recent changes; `keys` limits it to one visitor's tickets."""
        with self._lock:
            if keys is None:
                rows = self._db.execute("SELECT * FROM legs WHERE active = 1 ORDER BY pinned DESC, departure_at")
            else:
                marks = ",".join("?" * len(keys))
                rows = self._db.execute(f"SELECT * FROM legs WHERE active = 1 AND key IN ({marks}) "
                                        "ORDER BY pinned DESC, departure_at", keys) if keys else []
            legs = [dict(r) for r in rows]
            for leg in legs:
                leg["history"] = [dict(r) for r in self._db.execute(
                    "SELECT at, kind, old_price, new_price, note FROM changes WHERE key = ? ORDER BY id DESC LIMIT ?",
                    (leg["key"], changes_per_leg)).fetchall()]
        return legs

    # ── Telegram alerts ──────────────────────────────────────────────────────
    def tg_subscribe(self, code: str, keys: list[str], max_tickets: int = 40) -> int:
        """Remember that this browser watches these legs; the baseline is their state right now."""
        added = 0
        with self._lock:
            have = self._db.execute("SELECT COUNT(*) FROM tg_subs WHERE code = ?", (code,)).fetchone()[0]
            for k in keys:
                if have + added >= max_tickets:
                    break
                cur = self._db.execute(
                    """INSERT OR IGNORE INTO tg_subs (code, key, created_at, notified_status, notified_price)
                       SELECT ?, key, ?, status, price FROM legs WHERE key = ? AND active = 1""",
                    (code, _now(), k))
                added += cur.rowcount
            self._db.commit()
        return added

    def tg_unsubscribe(self, code: str, keys: list[str]) -> int:
        with self._lock:
            cur = self._db.executemany("DELETE FROM tg_subs WHERE code = ? AND key = ?", [(code, k) for k in keys])
            self._db.commit()
            return cur.rowcount

    def tg_link(self, code: str, chat_id: int, lang: str) -> None:
        """Link a browser to a chat. Alerts start from the tickets' current state, not from old changes."""
        with self._lock:
            self._db.execute(
                """INSERT INTO tg_links (code, chat_id, lang, linked_at) VALUES (?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at""",
                (code, chat_id, lang, _now()))
            self._db.execute(
                """UPDATE tg_subs SET notified_at = NULL,
                       notified_status = (SELECT status FROM legs WHERE legs.key = tg_subs.key),
                       notified_price = (SELECT price FROM legs WHERE legs.key = tg_subs.key)
                   WHERE code = ?""", (code,))
            self._db.commit()

    def tg_unlink(self, *, code: str | None = None, chat_id: int | None = None) -> int:
        with self._lock:
            if code is not None:
                cur = self._db.execute("DELETE FROM tg_links WHERE code = ?", (code,))
            else:
                cur = self._db.execute("DELETE FROM tg_links WHERE chat_id = ?", (chat_id,))
            self._db.commit()
            return cur.rowcount

    def tg_is_linked(self, code: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM tg_links WHERE code = ?", (code,)).fetchone() is not None

    def tg_code_lang(self, code: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT lang FROM tg_links WHERE code = ?", (code,)).fetchone()
        return row["lang"] if row else None

    def tg_chat_lang(self, chat_id: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT lang FROM tg_links WHERE chat_id = ? ORDER BY linked_at DESC",
                                   (chat_id,)).fetchone()
        return row["lang"] if row else None

    def tg_set_lang(self, code: str, lang: str) -> None:
        with self._lock:
            self._db.execute("UPDATE tg_links SET lang = ? WHERE code = ?", (lang, code))
            self._db.commit()

    def tg_chat_legs(self, chat_id: int) -> list[dict]:
        """The watched legs of every browser linked to this chat (for /list)."""
        with self._lock:
            rows = self._db.execute(
                """SELECT DISTINCT g.* FROM tg_links l JOIN tg_subs s ON s.code = l.code JOIN legs g ON g.key = s.key
                   WHERE l.chat_id = ? AND g.active = 1 ORDER BY g.departure_at""", (chat_id,)).fetchall()
        now = datetime.now(timezone.utc)
        return [dict(r) for r in rows if datetime.fromisoformat(r["departure_at"]) > now]

    def tg_due(self, confirm_seconds: float, min_interval_seconds: float) -> list[dict]:
        """Subscriptions whose leg now differs from what the chat was last told.

        A new state must have held for `confirm_seconds` (prices in the cache flicker) and a ticket
        gets at most one message per `min_interval_seconds`; changes in between fold into the next one.
        """
        now = datetime.now(timezone.utc)
        settled = (now - timedelta(seconds=confirm_seconds)).isoformat(timespec="seconds")
        quiet = (now - timedelta(seconds=min_interval_seconds)).isoformat(timespec="seconds")
        with self._lock:
            # Housekeeping: tickets no longer watched (expired pins).
            self._db.execute("DELETE FROM tg_subs WHERE key NOT IN (SELECT key FROM legs WHERE active = 1)")
            rows = self._db.execute(
                """SELECT s.code, s.notified_status, s.notified_price, s.notified_at, l.chat_id, l.lang, g.*
                   FROM tg_subs s JOIN tg_links l ON l.code = s.code JOIN legs g ON g.key = s.key
                   WHERE g.status IN ('available', 'gone')
                     AND (g.status IS NOT s.notified_status
                          OR (g.status = 'available' AND ABS(g.price - COALESCE(s.notified_price, g.price)) >= 0.5))
                     AND (g.last_change_at IS NULL OR g.last_change_at <= ?)
                     AND (s.notified_at IS NULL OR s.notified_at <= ?)
                   ORDER BY l.chat_id, g.departure_at""", (settled, quiet)).fetchall()
            self._db.commit()
        return [dict(r) for r in rows if datetime.fromisoformat(r["departure_at"]) > now]

    def tg_mark(self, code: str, key: str, status: str, price: float | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tg_subs SET notified_status = ?, notified_price = ?, notified_at = ? WHERE code = ? AND key = ?",
                (status, price, _now(), code, key))
            self._db.commit()

    # ── state that must survive a restart (free hosts wipe the disk) ─────────
    def export_state(self, changes_per_leg: int = 5) -> dict:
        """Pinned or Telegram-watched legs (with recent changes) and the Telegram links."""
        with self._lock:
            legs = [dict(r) for r in self._db.execute(
                """SELECT * FROM legs WHERE active = 1
                   AND (pinned = 1 OR key IN (SELECT key FROM tg_subs)) ORDER BY key""")]
            changes = []
            for leg in legs:
                changes += [dict(r) for r in self._db.execute(
                    "SELECT key, at, kind, old_price, new_price, note FROM changes WHERE key = ? ORDER BY id DESC LIMIT ?",
                    (leg["key"], changes_per_leg))][::-1]
            links = [dict(r) for r in self._db.execute("SELECT * FROM tg_links ORDER BY code")]
            subs = [dict(r) for r in self._db.execute("SELECT * FROM tg_subs ORDER BY code, key")]
        return {"version": 1, "legs": legs, "changes": changes, "tg_links": links, "tg_subs": subs}

    def is_empty(self) -> bool:
        with self._lock:
            return not any(self._db.execute(q).fetchone()[0] for q in (
                "SELECT COUNT(*) FROM legs WHERE pinned = 1", "SELECT COUNT(*) FROM tg_links",
                "SELECT COUNT(*) FROM tg_subs"))

    def import_state(self, state: dict) -> int:
        """Load what export_state saved. Returns the number of rows restored."""
        n = 0
        with self._lock:
            for table, rows in (("legs", state.get("legs", [])), ("tg_links", state.get("tg_links", [])),
                                ("tg_subs", state.get("tg_subs", []))):
                cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for row in rows:
                    row = {k: v for k, v in row.items() if k in cols}  # tolerate older/newer backups
                    marks = ",".join("?" * len(row))
                    self._db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(row)}) VALUES ({marks})",
                                     list(row.values()))
                    n += 1
            for c in state.get("changes", []):
                self._db.execute("INSERT INTO changes (key, at, kind, old_price, new_price, note) VALUES (?,?,?,?,?,?)",
                                 (c["key"], c["at"], c["kind"], c.get("old_price"), c.get("new_price"), c.get("note")))
            self._db.commit()
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()
