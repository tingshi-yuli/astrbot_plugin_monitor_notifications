"""Focused regressions for authenticated snapshots and QQ detail delivery."""

import asyncio
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_core import response

from notice_monitor.auth import LOGIN_URL, PortalClient
from notice_monitor.details import parse_detail, wants_detail
from notice_monitor.qq import DeliveryRejected, DeliveryUnknown, QQTransport, plain
from notice_monitor.service import NoticeService, heading
from notice_monitor.sources import LoginRequired, Notice
from notice_monitor.storage import Archive, State

NOTICE = Notice(
    "wbnews:9631",
    "道路施工封闭通知",
    "https://info.hitsz.edu.cn/content.jsp?wbnewsid=9631",
    "2026-10-10",
)
TARGET = "QQ:FriendMessage:123456"
HTML = """<div id="vsb_content"><p>第一段：10月12日至25日封闭。</p>
<!-- hidden comment --><img src="/photo.png" alt="路线图">
<table><tr><th>地点</th><th>时间</th></tr><tr><td colspan="2">北侧通道</td></tr></table>
<p>尾段，请<a href="https://example.edu/register">报名</a>。</p>
<script>secretScript()</script></div>
<a href="/system/_content/download.jsp?urlfile=test">施工说明.pdf</a>"""


class DetailTests(unittest.TestCase):
    def test_body_order_tables_links_and_attachments(self):
        record = parse_detail(HTML, NOTICE, NOTICE.url)
        blocks = record["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image", "text", "text"])
        self.assertIn("第一段", blocks[0]["text"])
        self.assertEqual(blocks[1]["url"], "https://info.hitsz.edu.cn/photo.png")
        self.assertIn("地点 | 时间", blocks[2]["text"])
        self.assertIn("colspan=2", blocks[2]["text"])
        self.assertIn("https://example.edu/register", blocks[-1]["text"])
        self.assertEqual(record["attachments"][0]["name"], "施工说明.pdf")
        self.assertNotIn("secretScript", str(record))
        self.assertNotIn("hidden comment", str(record))
        with self.assertRaises(LoginRequired):
            parse_detail(HTML, NOTICE, LOGIN_URL)

    def test_detail_cas_callback_returns_to_original_and_only_downloads_inline_image(
        self,
    ):
        callback = "http://info.hitsz.edu.cn/system/resource/code/auth/caslogin.jsp"
        urls = []
        expired = True
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp), "fake", "fake")

            def request(method, url, **kwargs):
                nonlocal expired
                self.assertEqual(method, "GET")
                urls.append(url)
                if url == NOTICE.url and expired:
                    expired = False
                    return response(url, status=302, location=callback)
                if url == callback:
                    return response(url, "<script>location='list.jsp'</script>")
                if url.endswith("photo.png"):
                    result = response(url)
                    result._content = b"\x89PNG\r\n\x1a\nimage-fixture"
                    result.headers["Content-Type"] = "image/png"
                    result._content_consumed = True
                    return result
                self.assertEqual(url, NOTICE.url)
                return response(url, HTML)

            try:
                with patch.object(client.session, "request", side_effect=request):
                    record = client.fetch_detail(NOTICE)
                self.assertEqual(urls.count(NOTICE.url), 2)
                self.assertFalse(any("download.jsp" in url for url in urls))
                image = record["blocks"][1]
                self.assertTrue((Path(tmp) / "images" / image["file"]).is_file())
                self.assertEqual(
                    (Path(tmp) / "images" / image["file"]).stat().st_mode & 0o777, 0o600
                )
            finally:
                client.close()

    def test_clear_detail_requests_only(self):
        aliases = ["详细", "全文", "原文", "展开", "打包", "看看详情"]
        for text in ["详细", "请把这条通知的完整内容发我", "请发我全文", "看看详情！"]:
            self.assertTrue(wants_detail(text, aliases), text)
        for text in [
            "",
            "总结一下",
            "不要发全文",
            "详细解释学校为什么这么做",
            "这个全文不错",
        ]:
            self.assertFalse(wants_detail(text, aliases), text)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.client = Mock(directory=self.directory)
        self.client.fetch_detail.return_value = parse_detail(HTML, NOTICE, NOTICE.url)
        self.transport = Mock()
        self.transport.send_text = AsyncMock(return_value="42")
        self.transport.send_bundle = AsyncMock()
        self.transport.read_own_text = AsyncMock(return_value="")
        self.summary = AsyncMock(
            return_value="10 月 12 日至 25 日北侧通道施工封闭，请绕行。"
        )
        self.state = State(self.directory)
        self.state.observe([NOTICE])
        self.service = NoticeService(
            self.client,
            Archive(self.directory),
            self.state,
            self.transport,
            TARGET,
            self.summary,
        )

    async def test_summary_reply_snapshot_and_restart_mapping(self):
        await self.service.send_notice(asdict(NOTICE))
        pushed = self.transport.send_text.call_args.args[0]
        for expected in [
            NOTICE.title,
            NOTICE.date,
            NOTICE.url,
            "AI 摘要",
            "未解读图片及附件",
        ]:
            self.assertIn(expected, pushed)
        self.assertNotIn("引用本消息", pushed)
        self.assertNotIn("回复", pushed)
        self.assertIn("尾段", self.summary.call_args.args[0])
        archive = Archive(self.directory)
        self.assertEqual(archive.resolve(TARGET, "42"), NOTICE.id)
        self.assertIsNone(archive.resolve("QQ:FriendMessage:999", "42"))
        self.service.archive = archive
        resolved = await self.service.resolve_reply("42", "9999")
        self.assertEqual(resolved, NOTICE)
        await self.service.send_detail(resolved, "9999")
        contents = self.transport.send_bundle.call_args.args[0]
        pack = str(contents)
        for expected in ["第一段", "尾段", "北侧通道", "施工说明.pdf", "图片未打包"]:
            self.assertIn(expected, pack)
        self.assertNotIn(
            "请绕行", pack
        )  # Detail uses the source, not its generated summary.
        self.client.fetch_detail.assert_called_once()
        await self.service.send_notice(asdict(NOTICE))
        self.transport.send_text.assert_awaited_once()

    async def test_ai_disabled_timeout_and_detail_login_failure_still_push(self):
        self.service.summary_enabled = False
        await self.service.send_notice(asdict(NOTICE))
        self.summary.assert_not_awaited()
        self.assertNotIn("AI 摘要", self.transport.send_text.call_args.args[0])
        for number, detail_error in [(9632, False), (9633, True)]:
            notice = Notice(f"wbnews:{number}", NOTICE.title, NOTICE.url, NOTICE.date)
            self.service.summary_enabled = True
            self.client.fetch_detail.return_value = parse_detail(
                HTML, notice, notice.url
            )
            self.summary.side_effect = asyncio.TimeoutError
            if detail_error:
                self.client.fetch_detail.side_effect = LoginRequired("学校要求二次认证")
            await self.service.send_notice(asdict(notice))
            text = self.transport.send_text.call_args.args[0]
            self.assertIn("AI 摘要：暂不可用", text)
            self.assertIn(NOTICE.title, text)
            if detail_error:
                self.assertIn("二次认证", text)

    async def test_old_notification_recovery_requires_known_id(self):
        self.transport.read_own_text.return_value = heading(asdict(NOTICE))
        self.assertEqual(await self.service.resolve_reply("12", "9999"), NOTICE)
        unknown = Notice(
            "wbnews:99999", "其他通知", NOTICE.url.replace("9631", "99999"), ""
        )
        self.transport.read_own_text.return_value = heading(asdict(unknown))
        self.assertIsNone(await self.service.resolve_reply("13", "9999"))

    async def test_full_text_is_not_truncated(self):
        record = parse_detail(
            "<div id='vsb_content'>" + "完整正文" * 2000 + "末尾</div>",
            NOTICE,
            NOTICE.url,
        )
        contents = self.service._contents(record)
        restored = "".join(node[0]["data"]["text"] for node in contents[1:])
        self.assertEqual(restored, "完整正文" * 2000 + "末尾")
        self.assertTrue(all(len(node[0]["data"]["text"]) <= 1500 for node in contents))


class QQTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_uses_real_sequence_and_anchors_the_current_request(self):
        # IDs and timestamps cannot establish message order. Newer messages
        # arriving during the lookup must not change this request's predecessor.
        messages = [
            {
                "message_id": 900,
                "real_seq": "103",
                "message_seq": 900,
                "time": 1000,
                "message_type": "private",
                "sender": {"user_id": 9999},
            },
            {
                "message_id": -50,
                "real_seq": "102",
                "message_seq": -50,
                "time": 1000,
                "message_type": "private",
                "sender": {"user_id": 123},
            },
            {
                "message_id": 400,
                "real_seq": "101",
                "message_seq": 400,
                "time": 1000,
                "message_type": "private",
                "sender": {"user_id": 9999},
            },
        ]
        for history in (messages, list(reversed(messages))):
            call = AsyncMock(return_value={"messages": history})
            previous = await QQTransport(call, 123).previous_message_id("-50", "9999")
            self.assertEqual(previous, "400")
            self.assertEqual(call.call_args.args, ("get_friend_msg_history",))
            self.assertEqual(call.call_args.kwargs["user_id"], 123)
            self.assertFalse(call.call_args.kwargs["parse_mult_msg"])

    async def test_history_keeps_intervening_messages_and_rejects_gaps(self):
        bot = {
            "message_id": 40,
            "real_seq": "100",
            "message_type": "private",
            "sender": {"user_id": 9999},
        }
        request = {
            "message_id": 42,
            "real_seq": "102",
            "message_type": "private",
            "sender": {"user_id": 123},
        }
        for sender, expected in ((123, None), (9999, "41")):
            other = {
                "message_id": 41,
                "real_seq": "101",
                "message_type": "private",
                "sender": {"user_id": sender},
                "message": [{"type": "image", "data": {}}],
            }
            call = AsyncMock(return_value={"messages": [bot, other, request]})
            self.assertEqual(
                await QQTransport(call, 123).previous_message_id(42, 9999), expected
            )
        for history in (
            [bot, request],  # Sequence 101 is missing (e.g. a recalled message).
            [{k: v for k, v in bot.items() if k != "real_seq"}, request],
            [bot, {**bot, "message_id": 39}, request],
            [bot, {**request, "message_type": "group"}],
            [bot, {**request, "sender": {"user_id": 456}}],
            [bot, {**request, "self_id": 8888}],
        ):
            with self.subTest(history=history):
                call = AsyncMock(return_value={"messages": history})
                self.assertIsNone(
                    await QQTransport(call, 123).previous_message_id(42, 9999)
                )

    async def test_history_sync_retry_and_failure_are_bounded(self):
        bot = {
            "message_id": 40,
            "real_seq": "100",
            "message_type": "private",
            "sender": {"user_id": 9999},
        }
        request = {
            "message_id": 41,
            "real_seq": "101",
            "message_type": "private",
            "sender": {"user_id": 123},
        }
        with patch("notice_monitor.qq.asyncio.sleep", new_callable=AsyncMock) as sleep:
            call = AsyncMock(
                side_effect=[{"messages": [bot]}, {"messages": [bot, request]}]
            )
            self.assertEqual(
                await QQTransport(call, 123).previous_message_id(41, 9999), "40"
            )
            self.assertEqual(call.await_count, 2)
            sleep.assert_awaited_once_with(0.3)
            for failure in (
                DeliveryUnknown("timeout"),
                DeliveryRejected("unsupported"),
            ):
                call = AsyncMock(side_effect=failure)
                self.assertIsNone(
                    await QQTransport(call, 123).previous_message_id(41, 9999)
                )
                self.assertLessEqual(call.await_count, 2)
            call = AsyncMock(return_value={"messages": [bot]})
            self.assertIsNone(
                await QQTransport(call, 123).previous_message_id(41, 9999)
            )
            self.assertEqual(call.await_count, 2)

    async def test_forward_receipts_and_explicit_fallback(self):
        call = AsyncMock(
            side_effect=[
                DeliveryRejected("unsupported"),
                {"message_id": 1},
                {"message_id": 2},
            ]
        )
        transport = QQTransport(call, 123456)
        contents = [[plain("first")], [plain("last")]]
        await transport.send_bundle(contents, "9999")
        self.assertEqual(
            [c.args[0] for c in call.call_args_list],
            ["send_private_forward_msg", "send_private_msg", "send_private_msg"],
        )
        nodes = call.call_args_list[0].kwargs["messages"]
        self.assertEqual(nodes[0]["data"]["uin"], "9999")
        self.assertEqual(call.call_args_list[-1].kwargs["message"], contents[-1])

    async def test_unknown_delivery_never_falls_back(self):
        for result in [{"status": "ok", "data": {}}, {"status": "async", "retcode": 1}]:
            call = AsyncMock(return_value=result)
            with self.assertRaises(DeliveryUnknown):
                await QQTransport(call, 123).send_bundle([[plain("body")]], "9999")
            call.assert_awaited_once()
        call = AsyncMock(side_effect=asyncio.TimeoutError)
        with self.assertRaises(DeliveryUnknown):
            await QQTransport(call, 123).send_bundle([[plain("body")]], "9999")
        call.assert_awaited_once()

    async def test_quote_sender_verification(self):
        call = AsyncMock(
            return_value={
                "message_type": "private",
                "sender": {"user_id": 444},
                "message": [plain(heading(asdict(NOTICE)))],
            }
        )
        transport = QQTransport(call, 123)
        self.assertEqual(await transport.read_own_text("42", "9999"), "")
        call.return_value["sender"]["user_id"] = 9999
        self.assertIn("哈工深新通知", await transport.read_own_text("42", "9999"))
