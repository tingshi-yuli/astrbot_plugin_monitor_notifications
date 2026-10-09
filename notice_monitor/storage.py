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
                    and all(n["id"] == key and all(isinstance(n[k], str) for k in ("id", "title", "date", "url"))
                            for key, n in data["pending"].items())
                )
                if not valid:
                    raise ValueError
                self.data = data
            except (OSError, ValueError, KeyError, TypeError):
                raise MonitorError("通知去重文件 state.json 无法读取；保留文件并停止，避免丢失待推送通知。") from None

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
        self._save({"initialized": True, "seen": sorted(seen), "pending": pending, "login_alert_at": 0})

    def login_alerted(self, timestamp):
        self._save({**self.data, "login_alert_at": timestamp})

    def delivered(self, notice_id):
        pending = dict(self.pending)
        pending.pop(notice_id, None)
        self._save({**self.data, "pending": pending})
