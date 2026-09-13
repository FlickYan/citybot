"""SQLite persistence: who is subscribed, how they want to be pinged, and
which notifications already went out (so a restart never double-sends).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     INTEGER PRIMARY KEY,
    tz          TEXT    NOT NULL,
    lead_times  TEXT    NOT NULL,
    prematch    INTEGER NOT NULL DEFAULT 1,
    postmatch   INTEGER NOT NULL DEFAULT 1,
    weekly      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sent (
    chat_id  INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    sent_at  TEXT    NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE INDEX IF NOT EXISTS idx_sent_at ON sent (sent_at);
"""


@dataclass
class Subscriber:
    chat_id: int
    tz: str
    lead_times: list[int]
    prematch: bool
    postmatch: bool
    weekly: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Subscriber":
        return cls(
            chat_id=row["chat_id"],
            tz=row["tz"],
            lead_times=json.loads(row["lead_times"]),
            prematch=bool(row["prematch"]),
            postmatch=bool(row["postmatch"]),
            weekly=bool(row["weekly"]),
        )


class Database:
    def __init__(self, path: str) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(file, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --------------------------------------------------------------- subscribers

    def subscribe(self, chat_id: int, tz: str, lead_times: list[int]) -> bool:
        """Returns True if this created a new subscription."""
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if existing:
                return False
            self._conn.execute(
                "INSERT INTO subscribers (chat_id, tz, lead_times, created_at)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, tz, json.dumps(lead_times), _now_iso()),
            )
            self._conn.commit()
            return True

    def unsubscribe(self, chat_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?", (chat_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get(self, chat_id: int) -> Subscriber | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return Subscriber.from_row(row) if row else None

    def all(self) -> list[Subscriber]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM subscribers").fetchall()
        return [Subscriber.from_row(r) for r in rows]

    def set_timezone(self, chat_id: int, tz: str) -> None:
        self._update(chat_id, "tz", tz)

    def set_lead_times(self, chat_id: int, lead_times: list[int]) -> None:
        self._update(chat_id, "lead_times", json.dumps(lead_times))

    def set_flag(self, chat_id: int, flag: str, value: bool) -> None:
        if flag not in {"prematch", "postmatch", "weekly"}:
            raise ValueError(f"unknown flag {flag!r}")
        self._update(chat_id, flag, int(value))

    def _update(self, chat_id: int, column: str, value: object) -> None:
        # `column` is never user-supplied: callers pass a literal from the set above.
        with self._lock:
            self._conn.execute(
                f"UPDATE subscribers SET {column} = ? WHERE chat_id = ?",
                (value, chat_id),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- dedupe log

    def was_sent(self, chat_id: int, key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sent WHERE chat_id = ? AND key = ?", (chat_id, key)
            ).fetchone()
        return row is not None

    def mark_sent(self, chat_id: int, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sent (chat_id, key, sent_at) VALUES (?, ?, ?)",
                (chat_id, key, _now_iso()),
            )
            self._conn.commit()

    def prune_sent(self, older_than_days: int = 60) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM sent WHERE sent_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
