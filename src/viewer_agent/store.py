"""SQLite-backed memory.

Two kinds of data live here:
  * the viewer event history (the "user data store" the agent reads from, and the dependency the replay can degrade)
  * the agent's own durable state: one state document per viewer, every decision and every published action
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .times import iso, ist_day, parse_ts

EVENT_COLUMNS = ("event_id", "user_id", "event", "occurred_at", "sent_at", "received_at", "session_id",
                 "app_dialect", "app_language", "platform", "app_version")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event TEXT NOT NULL,
    occurred_at TEXT, sent_at TEXT, received_at TEXT NOT NULL, session_id TEXT,
    app_dialect TEXT, app_language TEXT, platform TEXT, app_version TEXT, properties TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_user ON events (user_id, received_at);

CREATE TABLE IF NOT EXISTS viewers (
    user_id TEXT PRIMARY KEY, active_goal TEXT, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS viewers_goal ON viewers (active_goal);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, trigger_event_id TEXT NOT NULL,
    status TEXT NOT NULL, decided_at TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_trigger ON decisions (trigger_event_id);
CREATE INDEX IF NOT EXISTS decisions_user ON decisions (user_id, decided_at);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_id TEXT NOT NULL, type TEXT NOT NULL,
    published_at TEXT NOT NULL, send_at TEXT, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS actions_user ON actions (user_id, published_at);
"""


class StoreTimeout(Exception):
    """Raised instead of a result while the data store is degraded (see Store.degrade)."""


class Store:
    def __init__(self, path: str | Path, shared: bool = False):
        # shared=True lets other threads read this connection (the demo app keeps one read-only copy of the
        # history and serves each request on a new thread); everything else uses one connection per thread
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=not shared)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._degraded_until: datetime | None = None
        self._degraded_timeout_s = 0.0
        self._degraded_timeout_rate = 0.0
        self._degraded_slow_s = 0.0
        self._reads = 0

    def close(self) -> None:
        self.conn.close()

    def snapshot(self) -> "Store":
        """An in-memory copy. A what-if run on the copy can never change the real state."""
        copy = Store(":memory:")
        self.conn.backup(copy.conn)
        return copy

    # ----------------------------------------------------------------- events
    def ingest_history(self, events_path: str | Path) -> dict:
        """Load the 30-day history once. Duplicate event_ids are dropped; a few counters come back for reporting."""
        stats: Counter = Counter()
        plays: Counter = Counter()
        names: set[str] = set()
        rows = []
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                stats["lines"] += 1
                names.add(e["event"])
                if e["event"] == "play_started" and e["properties"].get("title_id"):
                    plays[e["properties"]["title_id"]] += 1
                rows.append(self._event_row(e))
        with self.conn:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        stats["stored"] = cur.rowcount
        stats["duplicates_dropped"] = stats["lines"] - cur.rowcount
        return {"counts": dict(stats), "event_names": names, "plays_per_title": plays}

    def add_event(self, e: dict) -> bool:
        """Store a live event. Returns False when the same event_id was already seen."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self._event_row(e))
        return cur.rowcount == 1

    @staticmethod
    def _event_row(e: dict) -> tuple:
        return tuple(e.get(c) for c in EVENT_COLUMNS) + (json.dumps(e.get("properties") or {}),)

    def events_for(self, user_id: str, budget=None) -> list[dict]:
        """All known events for one viewer, oldest first by server receive time."""
        self._simulate_degradation(user_id, budget)
        rows = self.conn.execute(
            "SELECT * FROM events WHERE user_id = ? ORDER BY received_at", (user_id,)).fetchall()
        return [self._event_dict(r) for r in rows]

    def events_between(self, user_id: str, start: str, end: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE user_id = ? AND received_at > ? AND received_at <= ? ORDER BY received_at",
            (user_id, start, end)).fetchall()
        return [self._event_dict(r) for r in rows]

    @staticmethod
    def _event_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["properties"] = json.loads(d.pop("properties"))
        return d

    def event_names(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT event FROM events")}

    def latest_event_time(self) -> datetime | None:
        row = self.conn.execute("SELECT MAX(received_at) FROM events").fetchone()
        return parse_ts(row[0]) if row and row[0] else None

    # ----------------------------------------------------------------- degradation (replay harness)
    def degrade(self, until: datetime | None, timeout_s: float = 0.0, timeout_rate: float = 0.0,
                slow_s: float = 0.0) -> None:
        """Until `until`, a share of reads time out (costing the full timeout) and the rest are slow."""
        self._degraded_until = until
        self._degraded_timeout_s = timeout_s
        self._degraded_timeout_rate = timeout_rate
        self._degraded_slow_s = slow_s

    def _simulate_degradation(self, user_id: str, budget) -> None:
        if budget is None or self._degraded_until is None or budget.now >= self._degraded_until:
            return
        self._reads += 1
        digest = hashlib.sha256(f"{user_id}:{self._reads}".encode()).hexdigest()
        if int(digest[:8], 16) / 16 ** 8 < self._degraded_timeout_rate:
            budget.wait(self._degraded_timeout_s)
            raise StoreTimeout(f"user_data_store read timed out after {self._degraded_timeout_s:.0f}s")
        budget.wait(self._degraded_slow_s)

    # ----------------------------------------------------------------- viewer state
    def load_state(self, user_id: str, budget=None) -> dict | None:
        self._simulate_degradation(user_id, budget)
        row = self.conn.execute("SELECT state FROM viewers WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def save_state(self, user_id: str, state: dict, now: datetime) -> None:
        active = (state.get("active_goal") or {}).get("goal_id")
        with self.conn:
            self.conn.execute(
                "INSERT INTO viewers (user_id, active_goal, state, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET active_goal = excluded.active_goal, "
                "state = excluded.state, updated_at = excluded.updated_at",
                (user_id, active, json.dumps(state), iso(now)))

    def viewers_with_active_goal(self, goal_id: str) -> list[str]:
        rows = self.conn.execute("SELECT user_id FROM viewers WHERE active_goal = ?", (goal_id,)).fetchall()
        return [r["user_id"] for r in rows]

    def all_states(self):
        for r in self.conn.execute("SELECT user_id, state FROM viewers ORDER BY user_id"):
            yield r["user_id"], json.loads(r["state"])

    # ----------------------------------------------------------------- decisions and actions
    def find_decision(self, trigger_event_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT record FROM decisions WHERE trigger_event_id = ? AND status != 'DUPLICATE' "
            "ORDER BY decided_at LIMIT 1", (trigger_event_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def save_decision(self, record: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (record["decision_id"], record["viewer_id"], record["trigger"]["event_id"], record["status"],
                 record["published_at"], json.dumps(record)))

    def save_action(self, record: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
                (record["action_id"], record["decision_id"], record["user_id"], record["type"],
                 record["published_at"], record.get("send_at"), json.dumps(record)))

    def cancel_action(self, record: dict, at: datetime, reason: str) -> None:
        record["cancelled_at"], record["cancel_reason"] = iso(at), reason
        with self.conn:
            self.conn.execute("UPDATE actions SET record = ? WHERE action_id = ?",
                              (json.dumps(record), record["action_id"]))

    def decisions_for(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT record FROM decisions WHERE user_id = ? ORDER BY decided_at", (user_id,)).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def actions_for(self, user_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT record FROM actions WHERE user_id = ? ORDER BY published_at", (user_id,)).fetchall()
        return [json.loads(r["record"]) for r in rows]

    def live_pages(self, at: datetime, ttl_min: int, user_id: str | None = None) -> list[dict]:
        """Home pages published within the TTL window ending at `at`."""
        since = iso(at - timedelta(minutes=ttl_min))
        sql = "SELECT record FROM actions WHERE type = 'home_page' AND published_at > ? AND published_at <= ?"
        args: list = [since, iso(at)]
        if user_id:
            sql += " AND user_id = ?"
            args.append(user_id)
        return [json.loads(r["record"]) for r in self.conn.execute(sql, args)]

    def messages_on_day(self, user_id: str, day, types: tuple[str, ...] = ("push", "whatsapp")) -> list[dict]:
        """Messages of the given types (default: push and WhatsApp, which share one daily cap) whose send time falls
        on the given IST calendar day. Cancelled messages do not count."""
        marks = ",".join("?" * len(types))
        rows = self.conn.execute(
            f"SELECT record FROM actions WHERE user_id = ? AND type IN ({marks})", (user_id, *types)).fetchall()
        out = []
        for r in rows:
            rec = json.loads(r["record"])
            when = parse_ts(rec.get("send_at") or rec["published_at"])
            if when and ist_day(when) == day and not rec.get("cancelled_at"):
                out.append(rec)
        return out

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
