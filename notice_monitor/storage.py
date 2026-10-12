"""Local deduplication, pending notices and login-alert cooldown."""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .sources import MonitorError


def private_dir(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def write_private_json(path: Path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".notice-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class State:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.path = directory / "state.json"
        self.data = {"initialized": False, "seen": [], "pending": {}}
        if self.path.exists():
            try:
                self.path.chmod(0o600)
                data = json.loads(self.path.read_text(encoding="utf-8"))
                valid = (
                    isinstance(data["initialized"], bool)
                    and isinstance(data["seen"], list)
                    and all(isinstance(n, str) for n in data["seen"])
                    and isinstance(data["pending"], dict)
                    and isinstance(data.get("login_alert_at", 0), (int, float))
                    and all(
                        n["id"] == key
                        and all(
                            isinstance(n[k], str)
                            for k in ("id", "title", "date", "url")
                        )
                        for key, n in data["pending"].items()
                    )
                )
                if not valid:
                    raise ValueError
                self.data = data
            except (OSError, ValueError, KeyError, TypeError):
                raise MonitorError(
                    "通知去重文件 state.json 无法读取；保留文件并停止，避免丢失待推送通知。"
                ) from None

    @property
    def pending(self):
        return self.data["pending"]

    def _save(self, data):
        write_private_json(self.path, data)
        self.data = data

    def observe(self, notices):
        if not notices:
            raise MonitorError("通知列表为空，未改变去重记录。")
        seen = set(self.data["seen"])
        pending = dict(self.pending)
        for notice in sorted(notices, key=lambda n: (n.date, n.id)):
            if self.data["initialized"] and notice.id not in seen:
                pending[notice.id] = asdict(notice)
            seen.add(notice.id)
        # A successful check ends the failure episode, allowing the next
        # independent login failure to alert immediately.
        self._save(
            {
                "initialized": True,
                "seen": sorted(seen),
                "pending": pending,
                "login_alert_at": 0,
            }
        )

    def login_alerted(self, timestamp):
        self._save({**self.data, "login_alert_at": timestamp})

    def delivered(self, notice_id):
        pending = dict(self.pending)
        pending.pop(notice_id, None)
        self._save({**self.data, "pending": pending})


class Archive:
    """Local article snapshots and QQ reply mappings, separate from deduplication."""

    def __init__(self, directory: Path):
        self.directory = directory / "articles"
        private_dir(self.directory)
        self.index_path = self.directory / "replies.json"
        try:
            self.replies = (
                json.loads(self.index_path.read_text(encoding="utf-8"))
                if self.index_path.exists()
                else {}
            )
            if not isinstance(self.replies, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in self.replies.items()
            ):
                raise ValueError
        except (OSError, ValueError):
            raise MonitorError("通知引用索引无法读取，请检查本地缓存。") from None

    def path(self, notice_id):
        prefix, number = notice_id.split(":", 1)
        if prefix != "wbnews" or not number.isdigit():
            raise ValueError("Invalid notice ID")
        return self.directory / f"{number}.json"

    def get(self, notice_id):
        path = self.path(notice_id)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["notice"]["id"] != notice_id or not isinstance(
                record["blocks"], list
            ):
                raise ValueError
            return record
        except (ValueError, KeyError, TypeError):
            raise MonitorError("通知正文缓存损坏，请检查本地缓存文件。") from None

    def save(self, record):
        write_private_json(self.path(record["notice"]["id"]), record)

    def bind(self, target, message_id, notice_id):
        updated = {**self.replies, f"{target}\n{message_id}": notice_id}
        write_private_json(self.index_path, updated)
        self.replies = updated

    def resolve(self, target, message_id):
        return self.replies.get(f"{target}\n{message_id}")

    def already_sent(self, target, notice_id):
        return any(
            key.startswith(target + "\n") and value == notice_id
            for key, value in self.replies.items()
        )
