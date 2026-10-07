"""SQLite baseline and per-recipient outbox; failed sends remain retryable."""

import sqlite3
import time
from pathlib import Path

from .auth import private_dir
from .sources import HitszSource, Notice


class Store:
    def __init__(self, directory: Path):
        private_dir(directory)
        path = directory / "notices.sqlite3"
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                last_success REAL, last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS notices (
                source TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                url TEXT NOT NULL, date TEXT NOT NULL, first_seen REAL NOT NULL,
                PRIMARY KEY (source, id)
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                source TEXT NOT NULL, target TEXT NOT NULL,
                initialized INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, target)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, notice_id TEXT NOT NULL, target TEXT NOT NULL,
                sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                UNIQUE (source, notice_id, target),
                FOREIGN KEY (source, notice_id) REFERENCES notices(source, id),
                FOREIGN KEY (source, target) REFERENCES subscriptions(source, target) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(sent_at, retry_at);
        """)

    def close(self):
        self.db.close()

    def register(self, sources: list[HitszSource]):
        with self.db:
            for source in sources:
                self.db.execute(
                    "INSERT INTO sources(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                    (source.id, source.name),
                )

    def subscribe(self, source: str, target: str):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO subscriptions(source,target) VALUES(?,?)", (source, target))

    def unsubscribe(self, source: str, target: str):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE source=? AND target=?", (source, target))

    def has_subscribers(self, source: str) -> bool:
        return self.db.execute("SELECT 1 FROM subscriptions WHERE source=? LIMIT 1", (source,)).fetchone() is not None

    def ingest(self, source: str, notices: list[Notice], push_on_first: bool = False) -> int:
        if not notices:
            raise ValueError("An empty fetch cannot establish a baseline")
        fresh = []
        now = time.time()
        # Observations, baselines and pending deliveries commit together.
        with self.db:
            for notice in notices:
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?,?)",
                    (source, notice.id, notice.title, notice.url, notice.date, now),
                )
                if cursor.rowcount:
                    fresh.append(notice)
                else:
                    self.db.execute(
                        "UPDATE notices SET title=?,url=?,date=? WHERE source=? AND id=?",
                        (notice.title, notice.url, notice.date, source, notice.id),
                    )
            targets = self.db.execute("SELECT * FROM subscriptions WHERE source=?", (source,)).fetchall()
            for target in targets:
                candidates = fresh if target["initialized"] else (notices if push_on_first else [])
                for notice in sorted(candidates, key=lambda n: (n.date, n.id)):
                    self.db.execute(
                        "INSERT OR IGNORE INTO deliveries(source,notice_id,target) VALUES(?,?,?)",
                        (source, notice.id, target["target"]),
                    )
                self.db.execute("UPDATE subscriptions SET initialized=1 WHERE source=? AND target=?",
                                (source, target["target"]))
            self.db.execute("UPDATE sources SET last_success=?,last_error='' WHERE id=?", (now, source))
        return len(fresh)

    def fetch_error(self, source: str, error: str):
        with self.db:
            self.db.execute("UPDATE sources SET last_error=? WHERE id=?", (error, source))

    def pending(self, source: str, limit: int = 20):
        return self.db.execute("""
            SELECT d.id, d.target, d.attempts, n.title, n.url, n.date
            FROM deliveries d JOIN notices n ON n.source=d.source AND n.id=d.notice_id
            WHERE d.source=? AND d.sent_at IS NULL AND d.retry_at<=?
            ORDER BY d.id LIMIT ?
        """, (source, time.time(), limit)).fetchall()

    def delivered(self, delivery_id: int):
        with self.db:
            self.db.execute("UPDATE deliveries SET sent_at=?,error='' WHERE id=?", (time.time(), delivery_id))

    def delivery_failed(self, delivery_id: int, attempts: int, error: str):
        delay = min(3600, 60 * 2 ** min(attempts, 6))
        with self.db:
            self.db.execute("UPDATE deliveries SET attempts=attempts+1,retry_at=?,error=? WHERE id=?",
                            (time.time() + delay, error, delivery_id))

    def latest(self, source: str, limit: int = 5):
        return self.db.execute("SELECT * FROM notices WHERE source=? ORDER BY date DESC,id DESC LIMIT ?",
                               (source, limit)).fetchall()

    def status(self, source: str, target: str) -> dict:
        row = dict(self.db.execute("SELECT * FROM sources WHERE id=?", (source,)).fetchone())
        sub = self.db.execute("SELECT initialized FROM subscriptions WHERE source=? AND target=?",
                              (source, target)).fetchone()
        row["subscribed"] = sub is not None
        row["baseline_ready"] = bool(sub and sub["initialized"])
        row["pending"] = self.db.execute(
            "SELECT count(*) FROM deliveries WHERE source=? AND target=? AND sent_at IS NULL",
            (source, target),
        ).fetchone()[0]
        failure = self.db.execute(
            "SELECT error FROM deliveries WHERE source=? AND target=? AND sent_at IS NULL AND error!='' ORDER BY id LIMIT 1",
            (source, target),
        ).fetchone()
        row["delivery_error"] = failure[0] if failure else ""
        return row
