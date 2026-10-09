"""HIT CAS HTTP login, following the school's public login.js/encrypt.js."""

import base64
import os
import secrets
import tempfile
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .sources import LoginRequired, MonitorError, Notice, is_login, list_url, parse_notices
from .storage import private_dir

AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
ALLOWED_HOSTS = {"ids.hit.edu.cn", "info.hitsz.edu.cn"}
LOGIN_COOLDOWN = 900
LOGIN_URL = "https://ids.hit.edu.cn/authserver/login?" + urlencode({
    "service": "http://info.hitsz.edu.cn/system/resource/code/auth/caslogin.jsp?owner=1834232703"
})


def encrypt_password(password: str, salt: str) -> str:
    """CryptoJS AES-CBC/PKCS7(randomString(64) + password, salt, random IV)."""
    key = salt.strip().encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise LoginRequired("认证页密码加密参数已变化，请检查登录适配器。")
    prefix = "".join(secrets.choice(AES_CHARS) for _ in range(64))
    iv = "".join(secrets.choice(AES_CHARS) for _ in range(16)).encode()
    padder = padding.PKCS7(128).padder()
    payload = padder.update((prefix + password).encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(payload) + encryptor.finalize()).decode()


def login_form(html: str, login_url: str) -> tuple[str, dict, str]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form#pwdFromId")
    if form is None:
        raise LoginRequired("未找到学校密码登录表单，可能需要二次认证或页面已变化。")
    salt = form.select_one("#pwdEncryptSalt, #pwdDefaultEncryptSalt")
    if salt is None or not salt.get("value"):
        raise LoginRequired("认证页缺少动态加密盐，未提交密码。")
    fields = {
        node["name"]: node.get("value", "")
        for node in form.select('input[type="hidden"][name]')
    }
    if not fields.get("execution"):
        raise LoginRequired("认证页缺少 execution 参数，未提交密码。")
    action = urlsplit(urljoin(login_url, form.get("action") or login_url))
    if action.scheme != "https" or action.hostname != "ids.hit.edu.cn":
        raise LoginRequired("登录表单地址异常，未提交密码。")
    query = parse_qs(action.query)
    service = parse_qs(urlsplit(login_url).query).get("service")
    if not service:
        raise LoginRequired("认证页缺少门户 service 参数，请从通知列表进入。")
    service_url = urlsplit(service[0])
    if service_url.hostname != "info.hitsz.edu.cn":
        raise LoginRequired("登录回调不属于哈工深通知门户，未提交密码。")
    # login.js appends service to the form action in the browser.
    query["service"] = service
    action = urlunsplit(action._replace(query=urlencode(query, doseq=True), fragment=""))
    return action, fields, salt["value"]


class PortalClient:
    """Reuse the school session, renewing it on authentication expiry."""

    def __init__(self, directory: Path, username: str, password: str):
        self.directory = directory
        private_dir(directory)
        self.username, self.password = username, password
        self.login_after = 0
        self.cookie_file = directory / "cookies.txt"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 HITSZNoticeMonitor/0.2",
            "Accept": "text/html,application/xhtml+xml,application/json",
        })
        self.session.max_redirects = 10
        self.cookies = MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            self.cookie_file.chmod(0o600)
            try:
                self.cookies.load(ignore_discard=True, ignore_expires=False)
            except (OSError, ValueError):
                self.cookies.clear()  # A broken session cache can be recreated by login.
        self.session.cookies = self.cookies

    def close(self):
        self.session.close()

    def _request(self, method: str, url: str, **kwargs):
        # Check each redirect, including 307/308, before forwarding credentials.
        for _ in range(11):
            parsed = urlsplit(url)
            if (parsed.hostname not in ALLOWED_HOSTS
                or parsed.scheme not in {"http", "https"}
                or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443}):
                raise MonitorError("学校请求跳转到了未支持的地址。")
            if parsed.hostname == "ids.hit.edu.cn" and parsed.scheme != "https":
                raise LoginRequired("认证平台未使用 HTTPS，已停止登录。")
            if method == "POST" and (parsed.scheme != "https" or parsed.hostname != "ids.hit.edu.cn"):
                raise LoginRequired("拒绝向非学校 HTTPS 认证地址提交密码。")
            try:
                response = self.session.request(
                    method, url, allow_redirects=False, timeout=(10, 25), **kwargs
                )
            except requests.RequestException as exc:
                # requests exceptions may include usernames, service tickets or URLs.
                raise MonitorError(f"学校网络请求失败（{type(exc).__name__}），请检查网络或代理。") from None
            if response.is_redirect:
                url = urljoin(response.url, response.headers["Location"])
                if response.status_code == 303 or (method == "POST" and response.status_code in {301, 302}):
                    method = "GET"
                    kwargs.pop("data", None)
                kwargs.pop("params", None)
                response.close()
                continue
            if response.status_code in {401, 403}:
                if method == "GET" and parsed.hostname == "info.hitsz.edu.cn":
                    return response  # Let fetch() renew an expired portal session.
                raise LoginRequired(f"学校拒绝访问（HTTP {response.status_code}），请检查账号和门户权限。")
            if response.status_code >= 400:
                raise MonitorError(f"学校返回 HTTP {response.status_code}，稍后重试。")
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = "utf-8"
            return response
        raise MonitorError("学校登录重定向次数过多。")

    def _save_cookies(self):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".cookies-")
        os.close(fd)
        try:
            self.cookies.save(name, ignore_discard=True, ignore_expires=False)
            os.replace(name, self.cookie_file)
        finally:
            Path(name).unlink(missing_ok=True)

    def _login(self, response):
        if time.monotonic() < self.login_after:
            raise LoginRequired("自动登录处于 15 分钟冷却期，请检查账号密码或认证要求。")
        action, fields, salt = login_form(response.text, response.url)
        self.login_after = time.monotonic() + LOGIN_COOLDOWN
        captcha = self._request(
            "GET", "https://ids.hit.edu.cn/authserver/checkNeedCaptcha.htl",
            params={"username": self.username}, headers={"Referer": response.url},
        )
        try:
            required = captcha.json()["isNeed"]
        except (ValueError, KeyError, TypeError):
            raise LoginRequired("无法确认验证码状态，未提交密码，请检查认证页面。") from None
        if required is not False:
            raise LoginRequired("学校要求验证码/滑块，当前无法自动登录；请先在学校认证平台处理。")
        fields.update(username=self.username, password=encrypt_password(self.password, salt),
                      _eventId="submit", cllt="userNameLogin", dllt="generalLogin", rememberMe="true")
        result = self._request("POST", action, data=fields, headers={"Referer": response.url})
        if is_login(result.url, result.text):
            raise LoginRequired("自动登录未完成，请检查账号密码、验证码或二次认证；15 分钟后重试。")

    def fetch(self) -> list[Notice]:
        notices = {}
        logged_in = False
        for page in range(1, 4):
            response = self._request("GET", list_url(page))
            if response.status_code in {401, 403} or is_login(response.url, response.text):
                if logged_in:
                    raise LoginRequired("登录后仍无法访问通知列表，请检查门户权限。")
                if response.status_code in {401, 403}:
                    self.cookies.clear()
                    response = self._request("GET", LOGIN_URL)
                if is_login(response.url, response.text):
                    self._login(response)
                logged_in = True
                response = self._request("GET", list_url(page))
            if response.status_code in {401, 403}:
                raise LoginRequired("登录后仍无法访问通知列表，请检查门户权限。")
            parsed = parse_notices(response.text, response.url)
            # Keep a verified session even if a subsequent list page fails.
            self._save_cookies()
            if logged_in:
                self.login_after = 0
            previous_count = len(notices)
            for notice in parsed:
                old = notices.get(notice.id)
                if old is None or (not old.date and notice.date):
                    notices[notice.id] = notice
            if len(notices) == previous_count:
                break  # Some portals repeat the last page for out-of-range PAGENUM.
        return sorted(notices.values(), key=lambda n: (n.date, n.id), reverse=True)
