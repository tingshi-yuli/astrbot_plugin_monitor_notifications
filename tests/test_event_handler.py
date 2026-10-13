"""Exercise the real event handler when AstrBot's SDK is available."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

HAS_ASTRBOT = importlib.util.find_spec("astrbot") is not None


@unittest.skipUnless(HAS_ASTRBOT, "AstrBot SDK required (run inside its container)")
class EventHandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from astrbot.api.message_components import Image, Plain, Reply

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "_hitsz_event_test",
            root / "main.py",
            submodule_search_locations=[str(root)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.plugin_class = module.CampusNoticePlugin
        cls.Plain, cls.Reply, cls.Image = Plain, Reply, Image

    def setUp(self):
        self.plugin = self.plugin_class.__new__(self.plugin_class)
        self.plugin.target = "QQ:FriendMessage:123"
        self.plugin.triggers = ["详细", "全文", "原文", "展开", "打包"]
        self.plugin.stopping = False
        self.plugin.jobs = set()
        self.service = Mock()
        self.plugin.service = self.service
        self.service.transport.previous_message_id = AsyncMock(return_value="42")
        self.service.resolve_reply = AsyncMock(
            side_effect=lambda message_id, self_id: {
                "41": "notice-A",
                "42": "notice-B",
            }.get(str(message_id))
        )
        self.service.send_detail = AsyncMock()
        self.event = Mock(unified_msg_origin=self.plugin.target)
        self.event.message_obj = SimpleNamespace(message_id="100")
        self.event.get_self_id.return_value = "9999"

    async def test_direct_request_uses_the_adjacent_notice(self):
        self.event.get_messages.return_value = [self.Plain("详细")]
        await self.plugin.notice_detail(self.event)
        self.service.transport.previous_message_id.assert_awaited_once_with(
            "100", "9999"
        )
        self.service.send_detail.assert_awaited_once_with("notice-B", "9999")
        self.event.stop_event.assert_called_once()
        self.assertFalse(self.plugin.jobs)

    async def test_explicit_quote_wins_without_history_fallback(self):
        for message_id, expected in (("41", "notice-A"), ("missing", None)):
            with self.subTest(message_id=message_id):
                self.event.get_messages.return_value = [
                    self.Reply(id=message_id),
                    self.Plain("全文"),
                ]
                self.event.stop_event.reset_mock()
                self.service.send_detail.reset_mock()
                await self.plugin.notice_detail(self.event)
                self.service.transport.previous_message_id.assert_not_awaited()
                self.assertEqual(
                    self.event.stop_event.call_count, int(expected is not None)
                )
                if expected:
                    self.service.send_detail.assert_awaited_once_with(expected, "9999")
                else:
                    self.service.send_detail.assert_not_awaited()

    async def test_intervening_chat_unknown_history_and_other_sessions_do_not_trigger(
        self,
    ):
        for previous in (None, "other-plugin-message", "ordinary-bot-response"):
            with self.subTest(previous=previous):
                self.service.transport.previous_message_id.return_value = previous
                self.event.get_messages.return_value = [self.Plain("详细")]
                await self.plugin.notice_detail(self.event)
                self.service.send_detail.assert_not_awaited()
                self.event.stop_event.assert_not_called()
        self.service.transport.previous_message_id.reset_mock()
        for target, parts in (
            ("QQ:FriendMessage:456", [self.Plain("详细")]),
            (self.plugin.target, [self.Plain("总结一下")]),
            (self.plugin.target, [self.Plain("详细解释为什么")]),
            (self.plugin.target, [self.Plain("详细"), self.Image(file="test.png")]),
        ):
            with self.subTest(target=target, parts=parts):
                self.event.unified_msg_origin = target
                self.event.get_messages.return_value = parts
                await self.plugin.notice_detail(self.event)
                self.service.transport.previous_message_id.assert_not_awaited()
                self.service.send_detail.assert_not_awaited()
                self.event.stop_event.assert_not_called()
