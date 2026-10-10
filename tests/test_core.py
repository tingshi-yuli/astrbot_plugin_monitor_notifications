"""Small, offline checks for renewal and new-notice delivery only."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from notice_monitor.auth import LOGIN_URL, PortalClient, encrypt_password, login_form
from notice_monitor.engine import DEFAULT_INTERVAL, LOGIN_ALERT_INTERVAL, Monitor
from notice_monitor.sources import DEFAULT_URL, LoginRequired, MonitorError, Notice, ParseError, parse_notices
from notice_monitor.storage import State

LIST_HTML = '<div class="Newslist"><li><a href="content.jsp?wbnewsid=21">校园通知测试标题</a><span>2026-10-07</span></li></div>'
FORM_HTML = '<form id="pwdFromId" action="/authserver/login"><input name="execution" type="hidden" value="token-{token}"><input id="pwdEncryptSalt" value="0123456789abcdef"></form>'


def response(url, body="", status=200, location=None):
    result = requests.Response()
    result.url, result.status_code, result.encoding = url, status, "utf-8"
    result._content, result._content_consumed = body.encode(), True
    if location:
        result.headers["Location"] = location
    return result


class CoreTests(unittest.TestCase):
    def test_parser_and_empty_page(self):
        html = LIST_HTML.replace("</div>", '<li><a href="content.jsp?wbnewsid=21">重复的通知标题</a></li><a href="https://evil.example/content.jsp?wbnewsid=22">错误域名的标题</a></div>')
        notices = parse_notices(html)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].date, "2026-10-07")
        with self.assertRaises(ParseError):
            parse_notices("<html>网站维护中</html>")
        with self.assertRaises(LoginRequired):
            parse_notices(FORM_HTML.format(token=1), LOGIN_URL)

    def test_encryption_and_dynamic_form_match_school(self):
        # Vector from the school's encrypt.js with randomString(n) => "A" * n.
        expected = (
            "C5sV2ktEoPUVHc/EwB811XB+8Q2GjXiGjtyRUKn0tZBPMOykE8qp5hBRsHxL1HNb"
            "9wqYtOXhfT0V4GBzEezbb+w10dLqPmMlVrqo0eQ/WAna8QfWJfoL55fvWzM+WCMp"
        )
        with patch("notice_monitor.auth.secrets.choice", return_value="A"):
            self.assertEqual(encrypt_password("Demo-password-123", "0123456789abcdef"), expected)
        action, fields, salt = login_form(FORM_HTML.format(token=2), LOGIN_URL)
        self.assertIn("service=", action)
        self.assertEqual(fields["execution"], "token-2")
        self.assertEqual(len(salt), 16)

    def test_session_reuse_and_relogin_after_seven_days(self):
        now = time.time()
        expires = 0
        logins = 0
        forbidden = False
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            client = PortalClient(directory, "fake-student", "fake-password")

            def request(method, url, **kwargs):
                nonlocal expires, logins
                if "checkNeedCaptcha.htl" in url:
                    return response(url, '{"isNeed":false}')
                if method == "POST":
                    fields = kwargs["data"]
                    self.assertEqual(fields["execution"], f"token-{logins + 1}")
                    self.assertEqual(fields["username"], "fake-student")
                    self.assertNotEqual(fields["password"], "fake-password")
                    self.assertEqual(fields["rememberMe"], "true")
                    logins += 1
                    expires = now + 7 * 86400
                    client.cookies.set_cookie(requests.cookies.create_cookie(
                        "JSESSIONID", f"session-{logins}", domain="info.hitsz.edu.cn", expires=int(expires),
                    ))
                    return response(url, status=302, location=DEFAULT_URL)
                if "ids.hit.edu.cn" in url:
                    return response(url, FORM_HTML.format(token=logins + 1))
                if now >= expires:
                    return response(url, status=403 if forbidden else 302, location=None if forbidden else LOGIN_URL)
                return response(url, LIST_HTML)

            try:
                with patch.object(client.session, "request", side_effect=request):
                    self.assertEqual(len(client.fetch()), 1)
                    client.fetch()
                self.assertEqual(logins, 1)
                client.close()
                client = PortalClient(directory, "fake-student", "fake-password")
                self.assertIn("session-1", [cookie.value for cookie in client.cookies])
                with patch.object(client.session, "request", side_effect=request):
                    client.fetch()  # A restart reuses the persisted school session.
                    self.assertEqual(logins, 1)
                    now += 7 * 86400 + 1
                    client.fetch()  # Expiry redirects to a fresh login form.
                    self.assertEqual(logins, 2)
                    now += 7 * 86400 + 1
                    forbidden = True
                    client.fetch()  # 403 on an expired session also renews login.
                    self.assertEqual(logins, 3)
            finally:
                client.close()

    def test_captcha_stops_password_submission_and_cools_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp), "fake-student", "fake-password")
            try:
                page = response(LOGIN_URL, FORM_HTML.format(token=1))
                with patch.object(client, "_request", return_value=response(LOGIN_URL, '{"isNeed":true}')) as request:
                    with self.assertRaisesRegex(LoginRequired, "验证码"):
                        client._login(page)
                    with self.assertRaisesRegex(LoginRequired, "冷却"):
                        client._login(page)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(request.call_args.args[0], "GET")
            finally:
                client.close()

    def test_secondary_auth_redirect_reports_manual_verification(self):
        url = "https://ids.hit.edu.cn/authserver/reAuthCheck/reAuthLoginView.do"
        with self.assertRaisesRegex(LoginRequired, "学校要求二次认证"):
            login_form("<html>Extra verification</html>", url)
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp), "fake-student", "fake-password")
            try:
                page = response(LOGIN_URL, FORM_HTML.format(token=1))
                with patch.object(client, "_request", side_effect=[
                    response(LOGIN_URL, '{"isNeed":false}'),
                    response(url, "<html>Private account details</html>"),
                ]) as request:
                    with self.assertRaisesRegex(LoginRequired, "学校要求二次认证") as error:
                        client._login(page)
                    self.assertNotIn("Private account details", str(error.exception))
                    self.assertEqual(request.call_count, 2)
                    self.assertEqual(request.call_args.args[0], "POST")
            finally:
                client.close()

    def test_failed_push_survives_restart_and_failed_fetch(self):
        async def exercise(directory):
            old = Notice("wbnews:1", "原有的通知", "https://info.hitsz.edu.cn/1", "2026-10-07")
            new = Notice("wbnews:2", "新增的通知", "https://info.hitsz.edu.cn/2", "2026-10-01")
            state = State(directory)
            state.observe([old])
            self.assertFalse(state.pending)
            with self.assertRaises(MonitorError):
                state.observe([])
            state.observe([new, old])  # Even an older date with a new ID is new.
            self.assertEqual(list(state.pending), [new.id])
            state = State(directory)

            async def failed_send(_):
                return False

            def failed_fetch():
                raise MonitorError("模拟学校网络失败")

            monitor = Monitor(state, failed_fetch, failed_send, 180, Mock())
            await monitor.check()
            self.assertEqual(list(State(directory).pending), [new.id])
            delivered = []

            async def send(text):
                delivered.append(text)
                return True

            monitor.send = send
            await monitor.check()
            await monitor.check()
            state = State(directory)
            state.observe([old, new])
            self.assertFalse(state.pending)
            self.assertEqual(len(delivered), 1)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_post_redirect_cannot_leak_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp), "fake-student", "fake-password")
            try:
                result = response(LOGIN_URL, status=307, location="https://evil.example/collect")
                with patch.object(client.session, "request", return_value=result) as request:
                    with self.assertRaises(MonitorError):
                        client._request("POST", LOGIN_URL, data={"password": "fake-ciphertext"})
                    self.assertEqual(request.call_count, 1)
            finally:
                client.close()

    def test_login_alert_retry_cooldown_restart_and_recovery(self):
        async def exercise(directory):
            failing = True
            attempts = []

            def fetch():
                if failing:
                    raise LoginRequired("学校要求验证码")
                return parse_notices(LIST_HTML)

            async def send(text):
                attempts.append(text)
                return len(attempts) != 1  # First alert cannot be delivered.

            monitor = Monitor(State(directory), fetch, send, 180, Mock())
            self.assertEqual(monitor.interval, 900)
            with patch("notice_monitor.engine.time.time", return_value=1000):
                self.assertEqual(await monitor.check(), 900)
            self.assertFalse(State(directory).data.get("login_alert_at", 0))
            with patch("notice_monitor.engine.time.time", return_value=1001):
                await monitor.check()
            self.assertEqual(len(attempts), 2)
            self.assertEqual(State(directory).data["login_alert_at"], 1001)

            monitor = Monitor(State(directory), fetch, send, DEFAULT_INTERVAL, Mock())
            with patch("notice_monitor.engine.time.time", return_value=1002):
                self.assertEqual(await monitor.check(), 3600)
            self.assertEqual(len(attempts), 2)  # No repeated alert after restart.
            with patch("notice_monitor.engine.time.time", return_value=1001 + LOGIN_ALERT_INTERVAL):
                await monitor.check()
            self.assertEqual(len(attempts), 3)

            failing = False
            await monitor.check()
            self.assertEqual(State(directory).data["login_alert_at"], 0)
            failing = True
            with patch("notice_monitor.engine.time.time", return_value=1002 + LOGIN_ALERT_INTERVAL):
                await monitor.check()
            self.assertEqual(len(attempts), 4)
            self.assertIn("自动登录失败", attempts[-1])
            self.assertIn("学校要求验证码", attempts[-1])
            self.assertIn("60 分钟", attempts[-1])

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_authentication_network_errors_are_login_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = PortalClient(Path(tmp), "fake-student", "fake-password")
            try:
                with patch.object(client.session, "request", side_effect=requests.Timeout("private details")):
                    with self.assertRaises(LoginRequired) as error:
                        client._request("GET", LOGIN_URL)
                    self.assertNotIn("private details", str(error.exception))
                with patch.object(client, "_request", return_value=response(LOGIN_URL, FORM_HTML.format(token=1))):
                    with patch.object(client, "_login", side_effect=MonitorError("认证过程中网络失败")):
                        with self.assertRaisesRegex(LoginRequired, "重新登录失败"):
                            client.fetch()
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
