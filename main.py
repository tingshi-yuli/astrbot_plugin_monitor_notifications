"""AstrBot adapter. The standalone demo does not import or start AstrBot."""

from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .notice_monitor import NAME, VERSION
from .notice_monitor.engine import Monitor, format_notice
from .notice_monitor.sources import DEFAULT_URL, build_sources
from .notice_monitor.storage import Store


@register(NAME, "youyi", "监听哈工深官网通知，自动登录并推送新增通知", VERSION)
class CampusNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.monitor = None
        self.store = None

    async def initialize(self):
        directory = Path(StarTools.get_data_dir(NAME))
        sources = build_sources(self.config.get("source_urls", [DEFAULT_URL]))
        credentials = Path(self.config.get("credentials_file", "credentials.json"))
        if not credentials.is_absolute():
            credentials = directory / credentials
        self.store = Store(directory)
        self.monitor = Monitor(
            self.store, directory, sources, self._send,
            interval=self.config.get("poll_interval_seconds", 180),
            max_pages=self.config.get("max_pages", 3),
            timeout=self.config.get("request_timeout_seconds", 25),
            push_on_first=self.config.get("push_on_first_run", False),
            credentials_file=credentials, logger=logger,
        )
        if self.config.get("enabled", True):
            self.monitor.start()
        logger.info("校园通知 demo 已初始化；订阅后开始轮询。")

    async def _send(self, target: str, text: str):
        return await self.context.send_message(target, MessageChain().message(text))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("校园通知", alias={"notice"})
    async def manage(self, event: AstrMessageEvent, action: str = "帮助"):
        """管理员私聊：校园通知 订阅/取消/检查/最新/状态/帮助。"""
        event.stop_event()
        if self.monitor is None:
            yield event.plain_result("插件尚未初始化，请检查插件加载日志。")
            return
        target = event.unified_msg_origin
        if action in {"订阅", "取消"}:
            async with self.monitor.lock:
                for source in self.monitor.sources:
                    if action == "订阅":
                        self.store.subscribe(source.id, target)
                    else:
                        self.store.unsubscribe(source.id, target)
            if action == "订阅":
                text = "已订阅到当前私聊。首次成功抓取将建立基线；可用“/校园通知 检查”立即抓取。"
                if self.config.get("push_on_first_run", False):
                    text = "已订阅到当前私聊；首次成功抓取会推送窗口内的现有通知。"
                if not self.config.get("enabled", True):
                    text += "当前自动轮询已关闭，请在插件配置中开启。"
            else:
                text = "已取消当前私聊的通知订阅及未发送任务。"
            yield event.plain_result(text)
        elif action == "检查":
            yield event.plain_result("正在检查通知并重试到期的待发送任务……")
            result = await self.monitor.run_once(force=True)
            text = f"检查完成：首次发现 {result['discovered']} 条，已推送 {result['sent']} 条。首次基线默认不推送。"
            if result["errors"]:
                text += "\n" + "\n".join(dict.fromkeys(result["errors"]))
            yield event.plain_result(text)
        elif action == "最新":
            for source in self.monitor.sources:
                rows = self.store.latest(source.id)
                if not rows:
                    yield event.plain_result(f"{source.name}暂无缓存，请先执行“/校园通知 检查”。")
                for row in rows:
                    yield event.plain_result(format_notice(source.name, row))
        elif action == "状态":
            lines = [f"自动轮询：{'开启' if self.config.get('enabled', True) else '关闭'}；间隔 {self.monitor.interval} 秒"]
            for source in self.monitor.sources:
                status = self.store.status(source.id, target)
                last = datetime.fromtimestamp(status["last_success"]).astimezone().isoformat(timespec="seconds") if status["last_success"] else "尚未成功"
                lines.append(
                    f"{source.name}：{'已订阅' if status['subscribed'] else '未订阅'}，"
                    f"基线{'已建立' if status['baseline_ready'] else '未建立'}，待推送 {status['pending']} 条\n最近成功：{last}"
                )
                for key in ("last_error", "delivery_error"):
                    if status[key]:
                        lines.append(status[key])
            yield event.plain_result("\n".join(lines))
        else:
            yield event.plain_result(
                "校园通知（管理员私聊）\n/校园通知 订阅\n/校园通知 取消\n"
                "/校园通知 检查\n/校园通知 最新\n/校园通知 状态\n"
                "账号密码通过本地 demo.py credentials 保存，勿在聊天中发送。"
            )

    async def terminate(self):
        if self.monitor:
            await self.monitor.stop()
            self.monitor = None
        if self.store:
            self.store.close()
            self.store = None
