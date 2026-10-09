"""A single fetch-and-notify loop; no commands or subscription management."""

import asyncio
import time

from .sources import LoginRequired, MonitorError

DEFAULT_INTERVAL = 3600
MIN_INTERVAL = 900
LOGIN_ALERT_INTERVAL = 24 * 3600


class Monitor:
    def __init__(self, state, fetch, send, interval, logger):
        self.state, self.fetch, self.send = state, fetch, send
        self.interval, self.logger = max(MIN_INTERVAL, int(interval)), logger

    async def _send(self, text):
        if await asyncio.wait_for(self.send(text), timeout=30) is False:
            raise RuntimeError("Message platform rejected the send")

    async def _alert_login_failure(self, message):
        last_alert = self.state.data.get("login_alert_at", 0)
        if last_alert and 0 <= time.time() - last_alert < LOGIN_ALERT_INTERVAL:
            return
        text = (
            "【哈工深通知：自动登录失败】\n"
            f"原因：{message}\n"
            "目前无法检查新通知，可能漏掉更新。请检查本地插件配置中的账号密码，"
            "以及学校是否要求验证码或二次认证。\n"
            f"插件将在约 {self.interval / 60:g} 分钟后重试；持续失败每 24 小时提醒一次。"
        )
        try:
            await self._send(text)
            # Do not suppress a future alert until delivery has succeeded.
            self.state.login_alerted(time.time())
        except Exception as exc:
            self.logger.warning("登录失败提醒发送失败（%s），下一轮重试。", type(exc).__name__)

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
                await self._alert_login_failure(message)
        # Retry saved notices even when the school is temporarily unavailable.
        for notice in list(self.state.pending.values()):
            text = f"【哈工深新通知】\n{notice['title']}\n日期：{notice['date'] or '未标注'}\n{notice['url']}"
            try:
                await self._send(text)
                self.state.delivered(notice["id"])
            except Exception as exc:
                self.logger.warning("校园通知推送失败（%s），下一轮重试。", type(exc).__name__)
                break
        return delay

    async def run(self):
        while True:
            await asyncio.sleep(await self.check())
