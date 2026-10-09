"""A single fetch-and-notify loop; no commands or subscription management."""

import asyncio

from .sources import LoginRequired, MonitorError


class Monitor:
    def __init__(self, state, fetch, send, interval, logger):
        self.state, self.fetch, self.send = state, fetch, send
        self.interval, self.logger = interval, logger

    async def _fetch(self):
        task = asyncio.create_task(asyncio.to_thread(self.fetch))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Join the HTTP worker before closing its session or reloading.
            try:
                await task
            except Exception:
                pass
            raise

    async def check(self):
        delay = self.interval
        try:
            self.state.observe(await self._fetch())
        except Exception as exc:
            message = str(exc) if isinstance(exc, MonitorError) else f"抓取失败（{type(exc).__name__}）"
            self.logger.warning("校园通知：%s", message)
            if isinstance(exc, LoginRequired):
                delay = max(delay, 900)
        # Retry saved notices even when the school is temporarily unavailable.
        for notice in list(self.state.pending.values()):
            text = f"【哈工深新通知】\n{notice['title']}\n日期：{notice['date'] or '未标注'}\n{notice['url']}"
            try:
                sent = await asyncio.wait_for(self.send(text), timeout=30)
                if sent is False:
                    raise RuntimeError("Message platform rejected the send")
                self.state.delivered(notice["id"])
            except Exception as exc:
                self.logger.warning("校园通知推送失败（%s），下一轮重试。", type(exc).__name__)
                break
        return delay

    async def run(self):
        while True:
            await asyncio.sleep(await self.check())
