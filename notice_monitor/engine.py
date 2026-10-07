"""Small async scheduler; fetching and delivery have independent failure state."""

import asyncio
import logging
import time
from pathlib import Path

from .auth import fetch_notices
from .sources import HitszSource, LoginRequired, MonitorError
from .storage import Store


def format_notice(source_name: str, notice) -> str:
    return f"【{source_name}】\n{notice['title']}\n日期：{notice['date'] or '未标注'}\n{notice['url']}"


class Monitor:
    def __init__(self, store: Store, directory: Path, sources: list[HitszSource], sender,
                 interval: int = 180, max_pages: int = 3, timeout: int = 25,
                 push_on_first: bool = False, credentials_file: Path | None = None,
                 logger=None, fetcher=None):
        self.store, self.directory, self.sources, self.sender = store, directory, sources, sender
        self.interval = max(30, int(interval))
        self.max_pages = min(10, max(1, int(max_pages)))
        self.timeout = min(120, max(5, int(timeout)))
        self.push_on_first, self.credentials_file = push_on_first, credentials_file
        self.logger = logger or logging.getLogger(__name__)
        self.fetcher = fetcher
        self.lock = asyncio.Lock()
        self.task = None
        self.next_fetch = {}
        self.failures = {}
        self.store.register(sources)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="campus-notice-monitor")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        # Wait for a manual check, if any, before its database is closed.
        async with self.lock:
            pass

    async def _fetch(self, source):
        if self.fetcher:
            return await self.fetcher(source)
        # Shield and join the worker on cancellation: do not leave a login thread
        # writing cookies while the plugin is being reloaded.
        task = asyncio.create_task(asyncio.to_thread(
            fetch_notices, self.directory, source, self.max_pages,
            self.timeout, self.credentials_file,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    async def run_once(self, force: bool = False) -> dict:
        async with self.lock:
            result = {"discovered": 0, "sent": 0, "errors": []}
            for source in self.sources:
                if not force and not self.store.has_subscribers(source.id):
                    continue
                if force or time.monotonic() >= self.next_fetch.get(source.id, 0):
                    try:
                        notices = await self._fetch(source)
                        result["discovered"] += self.store.ingest(source.id, notices, self.push_on_first)
                        self.failures[source.id] = 0
                        self.next_fetch[source.id] = time.monotonic() + self.interval
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, MonitorError) else f"抓取失败（{type(exc).__name__}）"
                        self.store.fetch_error(source.id, message)
                        result["errors"].append(f"{source.name}：{message}")
                        self.logger.warning("%s: %s", source.name, message)
                        count = self.failures.get(source.id, 0) + 1
                        self.failures[source.id] = count
                        delay = min(3600, self.interval * 2 ** min(count, 5))
                        if isinstance(exc, LoginRequired):
                            delay = max(900, delay)
                        self.next_fetch[source.id] = time.monotonic() + delay
                # A failed fetch must not block delivery of already queued notices.
                for delivery in self.store.pending(source.id):
                    try:
                        sent = await asyncio.wait_for(
                            self.sender(delivery["target"], format_notice(source.name, delivery)),
                            timeout=30,
                        )
                        if sent is False:
                            raise RuntimeError("adapter rejected send")
                    except Exception as exc:
                        message = f"推送失败（{type(exc).__name__}），已保留待重试。"
                        self.store.delivery_failed(delivery["id"], delivery["attempts"], message)
                        result["errors"].append(message)
                        self.logger.warning(message)
                    else:
                        self.store.delivered(delivery["id"])
                        result["sent"] += 1
            return result

    async def _loop(self):
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                self.logger.error("通知监控轮询异常（%s），稍后重试。", type(exc).__name__)
            await asyncio.sleep(min(self.interval, 60))
