"""Automatically push new HITSZ notices to one configured conversation."""

import asyncio
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, StarTools, register

from .notice_monitor import NAME, VERSION
from .notice_monitor.auth import PortalClient
from .notice_monitor.engine import DEFAULT_INTERVAL, Monitor
from .notice_monitor.storage import State


@register(NAME, "youyi", "自动登录哈工深官网，检测新通知并推送提醒", VERSION)
class CampusNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.task = None
        self.client = None

    async def initialize(self):
        username = self.config.get("username", "").strip()
        password = self.config.get("password", "")
        target = self.config.get("target_umo", "").strip()
        if not username or not password or not target:
            logger.warning("校园通知：请在插件配置中填写学校账号、密码和推送会话，保存后重载插件。")
            return
        if len(target.split(":", 2)) != 3 or not all(target.split(":", 2)):
            raise ValueError("推送会话需填写 /sid 返回的完整 UMO，不能只填 QQ 号或用户 ID。")
        directory = Path(StarTools.get_data_dir(NAME))
        state = State(directory)
        interval = self.config.get("poll_interval_seconds", DEFAULT_INTERVAL)
        self.client = PortalClient(directory, username, password)

        async def send(text):
            return await self.context.send_message(target, MessageChain().message(text))

        monitor = Monitor(state, self.client.fetch, send, interval, logger)
        self.task = asyncio.create_task(monitor.run(), name="hitsz-notices")

    async def terminate(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.client:
            self.client.close()
            self.client = None
