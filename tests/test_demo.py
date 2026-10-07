"""Small offline checks only: no live credentials, network or message sends."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notice_monitor.auth import PortalClient, encrypt_password, login_form
from notice_monitor.engine import Monitor
from notice_monitor.sources import HitszSource, LoginRequired, MonitorError, Notice, ParseError
from notice_monitor.storage import Store


def notice(identifier: int, published="2026-10-07"):
    return Notice(f"wbnews:{identifier}", f"测试通知标题 {identifier}",
                  f"https://info.hitsz.edu.cn/content.jsp?wbnewsid={identifier}", published)


class DemoTests(unittest.TestCase):
    def test_parser_deduplicates_filters_and_detects_login(self):
        source = HitszSource()
        html = """<div class="Newslist">
          <li><a href="content.jsp?wbnewsid=21&wbtreeid=1053">通知没有日期</a></li>
          <li><a title=" 完整 通知标题 " href="content.jsp?wbnewsid=21&wbtreeid=1053#x">标题</a><span>2026年10月7日</span></li>
          <li><a href="content.jsp?wbnewsid=20">较早发布通知</a><span>2026/10/06</span></li>
          <a href="https://evil.example/content.jsp?wbnewsid=99">错误域名通知</a>
          <a href="javascript:alert(1)">导航链接</a>
        </div>"""
        parsed = source.parse(html)
        self.assertEqual([n.id for n in parsed], ["wbnews:21", "wbnews:20"])
        self.assertEqual(parsed[0].date, "2026-10-07")
        self.assertEqual(parsed[0].title, "完整 通知标题")
        self.assertNotIn("#", parsed[0].url)
        self.assertIn("PAGENUM=2", source.page_url(2))
        with self.assertRaises(ParseError):
            source.parse("<html>维护中</html>")
        with self.assertRaises(LoginRequired):
            source.parse('<form id="pwdFromId"></form>')

    def test_encryption_matches_school_javascript(self):
        # Generated from school's encrypt.js with randomString(n) => "A" * n.
        expected = (
            "C5sV2ktEoPUVHc/EwB811XB+8Q2GjXiGjtyRUKn0tZBPMOykE8qp5hBRsHxL1HNb"
            "9wqYtOXhfT0V4GBzEezbb+w10dLqPmMlVrqo0eQ/WAna8QfWJfoL55fvWzM+WCMp"
        )
        with patch("notice_monitor.auth.secrets.choice", return_value="A"):
            self.assertEqual(encrypt_password("Demo-password-123", "0123456789abcdef"), expected)

    def test_login_form_keeps_cas_service_and_rejects_foreign_action(self):
        html = """<form id="pwdFromId" action="/authserver/login">
            <input type="hidden" name="execution" value="fresh-token">
            <input type="hidden" name="cllt" value="userNameLogin">
            <input id="pwdEncryptSalt" value="0123456789abcdef">
            <input name="passwordText" type="password">
        </form>"""
        url = "https://ids.hit.edu.cn/authserver/login?service=http%3A%2F%2Finfo.hitsz.edu.cn%2Fcallback"
        action, fields, salt = login_form(html, url)
        self.assertIn("service=http%3A%2F%2Finfo.hitsz.edu.cn%2Fcallback", action)
        self.assertEqual(fields["execution"], "fresh-token")
        self.assertNotIn("passwordText", fields)
        self.assertEqual(len(salt), 16)
        with self.assertRaises(LoginRequired):
            login_form(html.replace('/authserver/login"', 'https://evil.example/login"'), url)

    def test_baseline_and_delivery_survive_restart(self):
        source = HitszSource()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = Store(directory)
            store.register([source])
            store.subscribe(source.id, "owner")
            store.ingest(source.id, [notice(1)])
            self.assertEqual(len(store.pending(source.id)), 0)
            # A new ID with an older date is still new; pinned items do not hide it.
            store.ingest(source.id, [notice(1), notice(2, "2026-10-01")])
            self.assertEqual(len(store.pending(source.id)), 1)
            store.close()
            store = Store(directory)
            try:
                store.ingest(source.id, [notice(1), notice(2)])
                row = store.pending(source.id)[0]
                store.delivered(row["id"])
                store.ingest(source.id, [notice(1), notice(2)])
                self.assertEqual(len(store.pending(source.id)), 0)
                store.subscribe(source.id, "new-owner")
                store.ingest(source.id, [notice(1), notice(2)])
                self.assertEqual(len(store.pending(source.id)), 0)
            finally:
                store.close()

    def test_failed_send_retries_even_when_fetch_fails(self):
        async def exercise(directory):
            source = HitszSource()
            store = Store(directory)
            rows = [notice(1)]
            network_down = False
            owner_fails = True
            successes = []

            async def fetcher(_):
                if network_down:
                    raise MonitorError("模拟网络失败")
                return rows

            async def sender(target, text):
                if target == "owner" and owner_fails:
                    raise RuntimeError("simulated delivery failure")
                successes.append(target)
                return True

            monitor = Monitor(store, directory, [source], sender, fetcher=fetcher)
            try:
                store.subscribe(source.id, "owner")
                store.subscribe(source.id, "second-owner")
                await monitor.run_once(force=True)
                rows.append(notice(2))
                with patch("notice_monitor.storage.time.time", return_value=1000):
                    result = await monitor.run_once(force=True)
                self.assertEqual(result["sent"], 1)
                self.assertEqual(successes, ["second-owner"])
                self.assertEqual(store.status(source.id, "owner")["pending"], 1)
                owner_fails, network_down = False, True
                with patch("notice_monitor.storage.time.time", return_value=1061):
                    result = await monitor.run_once(force=True)
                self.assertEqual(result["sent"], 1)
                self.assertEqual(successes, ["second-owner", "owner"])
                self.assertTrue(result["errors"])
                self.assertEqual(store.status(source.id, "owner")["pending"], 0)
            finally:
                store.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_login_redirect_never_forwards_post_to_foreign_host(self):
        import requests

        response = requests.Response()
        response.status_code = 307
        response.url = "https://ids.hit.edu.cn/authserver/login"
        response.headers["Location"] = "https://evil.example/collect"
        response._content = b""
        response._content_consumed = True
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp))
            try:
                with patch.object(client.session, "request", return_value=response) as request:
                    with self.assertRaises(MonitorError):
                        client._request("POST", response.url, data={"password": "fake-ciphertext"})
                    self.assertEqual(request.call_count, 1)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
