"""HITSZ list parser adapted from HITA's CampusNoticeParser."""

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

DEFAULT_URL = "http://info.hitsz.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1053"
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")


class MonitorError(Exception):
    """An actionable, credential-free error safe to display to the owner."""


class LoginRequired(MonitorError):
    pass


class ParseError(MonitorError):
    pass


@dataclass(frozen=True)
class Notice:
    id: str
    title: str
    url: str
    date: str = ""


def parse_date(text: str) -> str:
    match = DATE_RE.search(text)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            pass
    return ""


def is_login(url: str, html: str = "") -> bool:
    parsed = urlsplit(url)
    if parsed.hostname == "ids.hit.edu.cn" or "/authserver/" in parsed.path.lower():
        return True
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one("form#pwdFromId, form#casLoginForm"))


def list_url(page: int = 1) -> str:
    return DEFAULT_URL if page == 1 else f"{DEFAULT_URL}&PAGENUM={page}"


def parse_notices(html: str, base_url: str = DEFAULT_URL) -> list[Notice]:
    if is_login(base_url, html):
        raise LoginRequired("学校门户会话已失效，需要登录。")
    root = BeautifulSoup(html, "html.parser").select_one(".Newslist")
    if root is None:
        raise ParseError("未找到 .Newslist 通知列表，可能是登录拦截或页面结构变化。")
    notices = {}
    for anchor in root.select("a[href]"):
        link = urlsplit(urljoin(base_url, anchor["href"]))
        query = {key.lower(): value for key, value in parse_qs(link.query).items()}
        raw_id = query.get("wbnewsid", [""])[0]
        if (
            link.scheme not in {"http", "https"}
            or link.hostname != "info.hitsz.edu.cn"
            or not raw_id.isdigit()
            or ("content.jsp" not in link.path.lower()
                and query.get("urltype", [""])[0].lower() != "news.newscontenturl")
        ):
            continue
        title = " ".join((anchor.get("title") or anchor.get_text(" ", strip=True)).split())
        if len(title) < 4:
            continue
        parent = anchor.parent
        span = parent.select_one("span")
        published = parse_date(span.get_text(" ") if span else "") or parse_date(parent.get_text(" "))
        tree = query.get("wbtreeid", ["1023"])[0]
        tree = tree if tree.isdigit() else "1023"
        canonical = (
            "https://info.hitsz.edu.cn/content.jsp?urltype=news.NewsContentUrl"
            f"&wbtreeid={tree}&wbnewsid={raw_id}"
        )
        notice = Notice(f"wbnews:{raw_id}", title, canonical, published)
        previous = notices.get(notice.id)
        if previous is None or (not previous.date and notice.date):
            notices[notice.id] = notice
    # Empty/error pages must never establish a successful baseline.
    if not notices:
        raise ParseError("通知列表中没有有效通知；保留已有记录，未更新基线。")
    return sorted(notices.values(), key=lambda n: (n.date, n.id), reverse=True)
