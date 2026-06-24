from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('source','destination')),
            title TEXT NOT NULL,
            peer TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            UNIQUE(kind, peer)
        );

        CREATE TABLE IF NOT EXISTS forward_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'once' CHECK(mode IN ('once','repeat')),
            interval_seconds INTEGER NOT NULL DEFAULT 1800,
            enabled INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS group_sources (
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            PRIMARY KEY(group_id, source_id)
        );

        CREATE TABLE IF NOT EXISTS group_destinations (
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            destination_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            PRIMARY KEY(group_id, destination_id)
        );

        CREATE TABLE IF NOT EXISTS group_source_state (
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(group_id, source_id)
        );

        CREATE TABLE IF NOT EXISTS group_health (
            group_id INTEGER PRIMARY KEY REFERENCES forward_groups(id) ON DELETE CASCADE,
            last_success_at INTEGER,
            last_error_at INTEGER,
            last_error TEXT,
            last_repeat_at INTEGER,
            running_note TEXT
        );

        CREATE TABLE IF NOT EXISTS message_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            telegram_message_id INTEGER NOT NULL,
            message_date TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(group_id, source_id, telegram_message_id)
        );

        CREATE TABLE IF NOT EXISTS send_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            telegram_message_id INTEGER NOT NULL,
            destination_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            cycle_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            run_after INTEGER NOT NULL,
            last_error TEXT,
            locked_by TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(group_id, source_id, telegram_message_id, destination_id, cycle_key)
        );

        CREATE INDEX IF NOT EXISTS idx_send_jobs_pending
            ON send_jobs(status, run_after, id);

        CREATE TABLE IF NOT EXISTS scheduled_starts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES forward_groups(id) ON DELETE CASCADE,
            start_at INTEGER NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scheduled_starts
            ON scheduled_starts(done, start_at);

        CREATE TABLE IF NOT EXISTS panel_sessions (
            user_id INTEGER PRIMARY KEY,
            action TEXT NOT NULL,
            payload TEXT,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            note TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            starts_at INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            note TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );
        """
        with self.lock:
            self.conn.executescript(schema)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, params).fetchall())

    def add_entity(self, kind: str, title: str, peer: str) -> int:
        ts = now_ts()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO entities(kind, title, peer, enabled, created_at)
                VALUES(?, ?, ?, 1, ?)
                ON CONFLICT(kind, peer) DO UPDATE SET title=excluded.title, enabled=1
                """,
                (kind, title.strip(), peer.strip(), ts),
            )
            row = self.conn.execute("SELECT id FROM entities WHERE kind=? AND peer=?", (kind, peer.strip())).fetchone()
            return int(row["id"])

    def list_entities(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.fetchall("SELECT * FROM entities WHERE kind=? ORDER BY id DESC", (kind,))
        return self.fetchall("SELECT * FROM entities ORDER BY kind, id DESC")

    def add_group(self, name: str) -> int:
        ts = now_ts()
        cur = self.execute(
            "INSERT INTO forward_groups(name, created_at, updated_at) VALUES(?, ?, ?)",
            (name.strip(), ts, ts),
        )
        group_id = int(cur.lastrowid)
        self.execute("INSERT OR IGNORE INTO group_health(group_id) VALUES(?)", (group_id,))
        return group_id

    def list_groups(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM forward_groups ORDER BY id DESC")

    def get_group(self, group_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM forward_groups WHERE id=?", (group_id,))

    def update_group(self, group_id: int, **values: Any) -> None:
        allowed = {"name", "mode", "interval_seconds", "enabled"}
        pairs = [(key, value) for key, value in values.items() if key in allowed]
        if not pairs:
            return
        assignments = ", ".join(f"{key}=?" for key, _ in pairs)
        params = [value for _, value in pairs]
        params.extend([now_ts(), group_id])
        self.execute(f"UPDATE forward_groups SET {assignments}, updated_at=? WHERE id=?", tuple(params))

    def toggle_group(self, group_id: int) -> bool:
        row = self.get_group(group_id)
        if not row:
            raise ValueError("گروه پیدا نشد.")
        enabled = 0 if row["enabled"] else 1
        self.update_group(group_id, enabled=enabled)
        if enabled:
            with self.lock:
                self.conn.execute("DELETE FROM group_source_state WHERE group_id=?", (group_id,))
                self.conn.execute("DELETE FROM message_cache WHERE group_id=?", (group_id,))
                self.conn.execute("DELETE FROM send_jobs WHERE group_id=? AND status IN ('pending','running')", (group_id,))
            self.execute("UPDATE group_health SET running_note=? WHERE group_id=?", ("روشن شد", group_id))
        else:
            self.execute("UPDATE group_health SET running_note=? WHERE group_id=?", ("خاموش شد", group_id))
        return bool(enabled)

    def reset_group(self, group_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM group_source_state WHERE group_id=?", (group_id,))
            self.conn.execute("DELETE FROM message_cache WHERE group_id=?", (group_id,))
            self.conn.execute("DELETE FROM send_jobs WHERE group_id=? AND status IN ('pending','running')", (group_id,))
            self.conn.execute(
                "UPDATE group_health SET last_error=NULL, last_error_at=NULL, running_note=? WHERE group_id=?",
                ("ریست شد؛ از پیام‌های جدید به بعد ادامه می‌دهد", group_id),
            )

    def link_entity(self, group_id: int, entity_id: int, kind: str) -> None:
        table = "group_sources" if kind == "source" else "group_destinations"
        column = "source_id" if kind == "source" else "destination_id"
        self.execute(f"INSERT OR IGNORE INTO {table}(group_id, {column}) VALUES(?, ?)", (group_id, entity_id))

    def unlink_entity(self, group_id: int, entity_id: int, kind: str) -> None:
        table = "group_sources" if kind == "source" else "group_destinations"
        column = "source_id" if kind == "source" else "destination_id"
        self.execute(f"DELETE FROM {table} WHERE group_id=? AND {column}=?", (group_id, entity_id))

    def group_sources(self, group_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT e.* FROM entities e
            JOIN group_sources gs ON gs.source_id=e.id
            WHERE gs.group_id=? AND e.enabled=1
            ORDER BY e.id
            """,
            (group_id,),
        )

    def group_destinations(self, group_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT e.* FROM entities e
            JOIN group_destinations gd ON gd.destination_id=e.id
            WHERE gd.group_id=? AND e.enabled=1
            ORDER BY e.id
            """,
            (group_id,),
        )

    def active_groups(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM forward_groups WHERE enabled=1 ORDER BY id")

    def get_last_message_id(self, group_id: int, source_id: int) -> int:
        row = self.fetchone(
            "SELECT last_message_id FROM group_source_state WHERE group_id=? AND source_id=?",
            (group_id, source_id),
        )
        return int(row["last_message_id"]) if row else 0

    def set_last_message_id(self, group_id: int, source_id: int, message_id: int) -> None:
        self.execute(
            """
            INSERT INTO group_source_state(group_id, source_id, last_message_id, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(group_id, source_id)
            DO UPDATE SET last_message_id=excluded.last_message_id, updated_at=excluded.updated_at
            """,
            (group_id, source_id, message_id, now_ts()),
        )

    def remember_message(self, group_id: int, source_id: int, message_id: int, message_date: str) -> None:
        self.execute(
            """
            INSERT OR IGNORE INTO message_cache(group_id, source_id, telegram_message_id, message_date, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (group_id, source_id, message_id, message_date, now_ts()),
        )

    def enqueue_message(self, group_id: int, source_id: int, message_id: int, cycle_key: str, run_after: int | None = None) -> int:
        destinations = self.group_destinations(group_id)
        ts = now_ts()
        count = 0
        for destination in destinations:
            self.execute(
                """
                INSERT OR IGNORE INTO send_jobs(
                    group_id, source_id, telegram_message_id, destination_id, cycle_key,
                    status, run_after, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (group_id, source_id, message_id, int(destination["id"]), cycle_key, run_after or ts, ts, ts),
            )
            count += 1
        return count

    def enqueue_repeat_due_messages(self, group_id: int, cycle_key: str, today_prefix: str) -> int:
        rows = self.fetchall(
            """
            SELECT source_id, telegram_message_id FROM message_cache
            WHERE group_id=? AND message_date LIKE ?
            ORDER BY telegram_message_id
            """,
            (group_id, f"{today_prefix}%"),
        )
        total = 0
        for row in rows:
            total += self.enqueue_message(group_id, int(row["source_id"]), int(row["telegram_message_id"]), cycle_key)
        self.execute("UPDATE group_health SET last_repeat_at=? WHERE group_id=?", (now_ts(), group_id))
        return total

    def claim_next_job(self, worker_name: str) -> sqlite3.Row | None:
        ts = now_ts()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    """
                    SELECT * FROM send_jobs
                    WHERE status='pending' AND run_after<=?
                    ORDER BY run_after, id
                    LIMIT 1
                    """,
                    (ts,),
                ).fetchone()
                if not row:
                    self.conn.execute("COMMIT")
                    return None
                self.conn.execute(
                    """
                    UPDATE send_jobs
                    SET status='running', attempts=attempts+1, locked_by=?, updated_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (worker_name, ts, int(row["id"])),
                )
                self.conn.execute("COMMIT")
                return self.fetchone("SELECT * FROM send_jobs WHERE id=?", (int(row["id"]),))
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def finish_job(self, job_id: int, ok: bool, error: str | None = None, run_after: int | None = None, max_attempts: int = 5) -> None:
        row = self.fetchone("SELECT attempts, group_id FROM send_jobs WHERE id=?", (job_id,))
        if not row:
            return
        ts = now_ts()
        if ok:
            self.execute(
                "UPDATE send_jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?",
                (ts, job_id),
            )
            self.mark_success(int(row["group_id"]))
            return

        attempts = int(row["attempts"])
        status = "failed" if attempts >= max_attempts else "pending"
        self.execute(
            """
            UPDATE send_jobs
            SET status=?, last_error=?, run_after=?, locked_by=NULL, updated_at=?
            WHERE id=?
            """,
            (status, error or "خطای نامشخص", run_after or (ts + 60), ts, job_id),
        )
        self.mark_error(int(row["group_id"]), error or "خطای نامشخص")

    def mark_success(self, group_id: int) -> None:
        self.execute(
            """
            INSERT INTO group_health(group_id, last_success_at, running_note)
            VALUES(?, ?, ?)
            ON CONFLICT(group_id)
            DO UPDATE SET last_success_at=excluded.last_success_at, running_note=excluded.running_note
            """,
            (group_id, now_ts(), "آخرین ارسال موفق بود"),
        )

    def mark_error(self, group_id: int, error: str) -> None:
        self.execute(
            """
            INSERT INTO group_health(group_id, last_error_at, last_error, running_note)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(group_id)
            DO UPDATE SET last_error_at=excluded.last_error_at, last_error=excluded.last_error, running_note=excluded.running_note
            """,
            (group_id, now_ts(), error[:1000], "آخرین اجرا خطا داشت"),
        )

    def group_status(self, group_id: int) -> dict[str, Any]:
        group = self.get_group(group_id)
        health = self.fetchone("SELECT * FROM group_health WHERE group_id=?", (group_id,))
        jobs = self.fetchall("SELECT status, COUNT(*) count FROM send_jobs WHERE group_id=? GROUP BY status", (group_id,))
        return {
            "group": dict(group) if group else None,
            "health": dict(health) if health else {},
            "jobs": {row["status"]: row["count"] for row in jobs},
            "sources": [dict(row) for row in self.group_sources(group_id)],
            "destinations": [dict(row) for row in self.group_destinations(group_id)],
        }

    def set_panel_action(self, user_id: int, action: str, payload: dict[str, Any] | None = None) -> None:
        self.execute(
            """
            INSERT INTO panel_sessions(user_id, action, payload, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET action=excluded.action, payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (user_id, action, json.dumps(payload or {}, ensure_ascii=False), now_ts()),
        )

    def pop_panel_action(self, user_id: int) -> tuple[str, dict[str, Any]] | None:
        row = self.fetchone("SELECT action, payload FROM panel_sessions WHERE user_id=?", (user_id,))
        if not row:
            return None
        self.execute("DELETE FROM panel_sessions WHERE user_id=?", (user_id,))
        return str(row["action"]), json.loads(row["payload"] or "{}")

    def add_customer(self, name: str, note: str = "") -> int:
        cur = self.execute(
            "INSERT INTO customers(name, note, created_at) VALUES(?, ?, ?)",
            (name.strip(), note.strip(), now_ts()),
        )
        return int(cur.lastrowid)

    def list_customers(self) -> list[sqlite3.Row]:
        return self.fetchall("SELECT * FROM customers ORDER BY id DESC")

    def add_subscription(self, customer_id: int, days: int, note: str = "") -> int:
        starts = now_ts()
        ends = starts + max(1, days) * 86400
        cur = self.execute(
            """
            INSERT INTO subscriptions(customer_id, starts_at, ends_at, note, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (customer_id, starts, ends, note.strip(), now_ts()),
        )
        return int(cur.lastrowid)

    def list_subscriptions(self) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT s.*, c.name customer_name FROM subscriptions s
            JOIN customers c ON c.id=s.customer_id
            ORDER BY s.id DESC
            """
        )

    def schedule_staggered_start(self, group_ids: list[int], gap_seconds: int) -> None:
        base = now_ts()
        with self.lock:
            for index, group_id in enumerate(group_ids):
                self.conn.execute("UPDATE forward_groups SET enabled=0, updated_at=? WHERE id=?", (base, group_id))
                self.conn.execute(
                    "INSERT INTO scheduled_starts(group_id, start_at, created_at) VALUES(?, ?, ?)",
                    (group_id, base + index * gap_seconds, base),
                )

    def due_scheduled_starts(self) -> list[sqlite3.Row]:
        return self.fetchall(
            """
            SELECT ss.*, fg.name FROM scheduled_starts ss
            JOIN forward_groups fg ON fg.id=ss.group_id
            WHERE ss.done=0 AND ss.start_at<=?
            ORDER BY ss.start_at, ss.id
            """,
            (now_ts(),),
        )

    def recover_running_jobs(self) -> int:
        cur = self.execute(
            """
            UPDATE send_jobs
            SET status='pending', locked_by=NULL, run_after=?, updated_at=?,
                last_error='اجرای قبلی هنگام ارسال قطع شد؛ کار دوباره در صف قرار گرفت'
            WHERE status='running'
            """,
            (now_ts(), now_ts()),
        )
        return int(cur.rowcount or 0)

    def complete_scheduled_start(self, schedule_id: int, group_id: int) -> None:
        ts = now_ts()
        with self.lock:
            self.conn.execute("UPDATE forward_groups SET enabled=1, updated_at=? WHERE id=?", (ts, group_id))
            self.conn.execute("UPDATE scheduled_starts SET done=1 WHERE id=?", (schedule_id,))
            self.conn.execute(
                """
                INSERT INTO group_health(group_id, running_note)
                VALUES(?, ?)
                ON CONFLICT(group_id) DO UPDATE SET running_note=excluded.running_note
                """,
                (group_id, "طبق زمان‌بندی فاصله‌ای روشن شد"),
            )
