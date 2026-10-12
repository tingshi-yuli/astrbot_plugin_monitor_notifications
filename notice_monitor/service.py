"""Notice snapshots, isolated AI summaries, and quote-to-detail delivery."""

import asyncio
import base64
import re
from dataclasses import asdict

from .details import quoted_notice
from .qq import plain
from .sources import MonitorError, Notice


def heading(notice):
    return (
        f"【哈工深新通知】\n{notice['title']}\n"
        f"日期：{notice['date'] or '未标注'}\n{notice['url']}"
    )


class NoticeService:
    def __init__(
        self, client, archive, state, transport, target, summarize, summary_enabled=True
    ):
        self.client, self.archive, self.state = client, archive, state
        self.transport, self.summarize = transport, summarize
        self.target = target
        self.summary_enabled = summary_enabled
        self.lock = asyncio.Lock()

    def remember(self, notice):
        record = self.archive.get(notice.id)
        if record is None:
            record = {
                "notice": asdict(notice),
                "blocks": [],
                "attachments": [],
                "fetched_at": 0,
                "summary": "",
            }
            self.archive.save(record)
        return record

    async def _detail(self, notice):
        record = self.remember(notice)
        if record["fetched_at"]:
            return record
        worker = asyncio.create_task(
            asyncio.to_thread(self.client.fetch_detail, notice)
        )
        try:
            record = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Never close the shared HTTP session while its worker is running.
            try:
                await worker
            except Exception:
                pass
            raise
        self.archive.save(record)
        return record

    async def _summary(self, record):
        if record.get("summary"):
            return record["summary"]
        text = "\n\n".join(b["text"] for b in record["blocks"] if b["type"] == "text")
        if not text.strip():
            return "AI 摘要：正文以图片为主，暂无可总结的文字。"
        if len(text) > 24000:
            return "AI 摘要：正文过长，暂不生成摘要；已保存完整正文。"
        prompt = (
            f"通知标题：{record['notice']['title']}\n日期：{record['notice']['date']}\n"
            f"<notice_text>\n{text}\n</notice_text>"
        )
        try:
            summary = await asyncio.wait_for(self.summarize(prompt), timeout=25)
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("Empty summary")
            # A failed/oversized model response must not block the actual notice.
            if len(summary) > 1200:
                raise ValueError("Oversized summary")
        except Exception:
            return "AI 摘要：暂不可用，已保存完整正文。"
        record["summary"] = "AI 摘要：\n" + summary.strip()
        self.archive.save(record)
        return record["summary"]

    async def send_notice(self, notice_data):
        async with self.lock:
            notice = Notice(**notice_data)
            if self.archive.already_sent(self.target, notice.id):
                return
            self.remember(notice)
            text = heading(notice_data)
            try:
                record = await self._detail(notice)
            except Exception as exc:
                reason = (
                    str(exc) if isinstance(exc, MonitorError) else "正文抓取或缓存失败"
                )
                text += f"\n\n正文暂不可用：{reason}"
                if self.summary_enabled:
                    text += "\nAI 摘要：暂不可用。"
            else:
                if self.summary_enabled:
                    text += "\n\n" + await self._summary(record)
                    if record["attachments"] or any(
                        b["type"] == "image" for b in record["blocks"]
                    ):
                        text += "\n（摘要仅依据正文文字，未解读图片及附件。）"
            message_id = await self.transport.send_text(text)
            self.archive.bind(self.target, message_id, notice.id)

    async def resolve_reply(self, message_id, self_id):
        notice_id = self.archive.resolve(self.target, message_id)
        if notice_id:
            record = self.archive.get(notice_id)
            return Notice(**record["notice"]) if record else None
        # v0.2 notifications have no local receipt. Ask QQ for the original
        # message, verify it was sent by this bot, and require a known notice ID.
        try:
            original = await self.transport.read_own_text(message_id, self_id)
            notice = quoted_notice(original)
        except (MonitorError, ValueError):
            return None
        if notice and notice.id in self.state.data["seen"]:
            self.remember(notice)
            self.archive.bind(self.target, message_id, notice.id)
            return notice
        return None

    def _contents(self, record):
        contents = []

        def add_text(text):
            for start in range(0, len(text), 1500):
                contents.append([plain(text[start : start + 1500])])

        add_text(heading(record["notice"]) + "\n\n以下为抓取时保存的通知正文。")
        for block in record["blocks"]:
            if block["type"] == "text":
                add_text(block["text"])
            elif block["type"] == "image":
                filename = block.get("file", "")
                path = self.client.directory / "images" / filename
                if re.fullmatch(r"[a-f0-9]{64}\.img", filename) and path.is_file():
                    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                    contents.append(
                        [{"type": "image", "data": {"file": "base64://" + encoded}}]
                    )
                else:
                    add_text(
                        "[正文图片未打包："
                        + block.get("error", "本地图片缓存不可用")
                        + "]\n"
                        + block.get("alt", "")
                        + "\n"
                        + block.get("url", "")
                    )
        if record["attachments"]:
            add_text(
                "附件清单（本阶段仅列名称和链接，未下载文件）：\n"
                + "\n\n".join(
                    item["name"] + "\n" + item["url"] for item in record["attachments"]
                )
            )
        return contents

    async def send_detail(self, notice, self_id):
        async with self.lock:
            record = await self._detail(notice)
            await self.transport.send_bundle(self._contents(record), self_id)
