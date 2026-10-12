"""A single fetch-and-notify loop; no commands or subscription management."""

import asyncio
import time

from .sources import LoginRequired, MonitorError

DEFAULT_INTERVAL = 3600
MIN_INTERVAL = 900
LOGIN_ALERT_INTERVAL = 24 * 3600


class Monitor:
    def __init__(self, state, fetch, send, interval, logger, deliver=None):
        self.state, self.fetch, self.send = state, fetch, send
        self.interval, self.logger = max(MIN_INTERVAL, int(interval)), logger
        self.deliver = deliver

    async def _send(self, text):
        if await asyncio.wait_for(self.send(text), timeout=30) is False:
            raise RuntimeError("Message platform rejected the send")

    async def _alert_failure(self, message, login_failure):
        # Keep the existing timestamp key to preserve alert cooldowns on upgrade.
        last_alert = self.state.data.get("login_alert_at", 0)
        if last_alert and 0 <= time.time() - last_alert < LOGIN_ALERT_INTERVAL:
            return
        text = (
            f"【哈工深通知：{'自动登录失败' if login_failure else '通知检查失败'}】\n"
            f"原因：{message}\n"
            "目前无法检查新通知，可能漏掉更新。请根据上述原因检查学校认证状态、网络或插件配置。\n"
            f"插件将在约 {self.interval / 60:g} 分钟后重试；持续失败每 24 小时提醒一次。"
        )
        try:
            await self._send(text)
            # Do not suppress a future alert until delivery has succeeded.
            self.state.login_alerted(time.time())
        except Exception as exc:
            self.logger.warning(
                "Campus notice failure alert could not be sent (%s); retrying next cycle.",
                type(exc).__name__,
            )

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
            message = (
                str(exc)
                if isinstance(exc, MonitorError)
                else f"抓取失败（{type(exc).__name__}）"
            )
            self.logger.warning("校园通知：%s", message)
            await self._alert_failure(message, isinstance(exc, LoginRequired))
        # Retry saved notices even when the school is temporarily unavailable.
        for notice in list(self.state.pending.values()):
            text = f"【哈工深新通知】\n{notice['title']}\n日期：{notice['date'] or '未标注'}\n{notice['url']}"
            try:
                if self.deliver:
                    await self.deliver(notice)
                else:
                    await self._send(text)
                self.state.delivered(notice["id"])
            except Exception as exc:
                self.logger.warning(
                    "校园通知推送失败（%s），下一轮重试。", type(exc).__name__
                )
                break
        return delay

    async def run(self):
        while True:
            await asyncio.sleep(await self.check())
