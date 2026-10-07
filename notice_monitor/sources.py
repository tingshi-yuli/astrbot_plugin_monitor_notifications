"""HITSZ list parser adapted from HITA's CampusNoticeParser."""

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

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


@dataclass(frozen=True)
class HitszSource:
    url: str = DEFAULT_URL

    def __post_init__(self):
        parsed = urlsplit(self.url)
        tree = parse_qs(parsed.query).get("wbtreeid", [""])[0]
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname != "info.hitsz.edu.cn"
            or parsed.path != "/list.jsp"
            or not tree.isdigit()
            or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
        ):
            raise MonitorError("当前适配器仅支持 info.hitsz.edu.cn/list.jsp 的栏目网址。")

    @property
    def id(self) -> str:
        return "hitsz:" + parse_qs(urlsplit(self.url).query)["wbtreeid"][0]

    @property
    def name(self) -> str:
        return "哈工深通知" if self.id == "hitsz:1053" else f"哈工深栏目 {self.id.split(':')[1]}"

    def page_url(self, page: int) -> str:
        parsed = urlsplit(self.url)
        query = dict(parse_qs(parsed.query, keep_blank_values=True))
        query.pop("PAGENUM", None)
        if page > 1:
            query["PAGENUM"] = [str(page)]
        return urlunsplit(parsed._replace(query=urlencode(query, doseq=True), fragment=""))

    def parse(self, html: str, base_url: str | None = None) -> list[Notice]:
        if is_login(base_url or self.url, html):
            raise LoginRequired("学校门户会话已失效，需要登录。")
        root = BeautifulSoup(html, "html.parser").select_one(".Newslist")
        if root is None:
            raise ParseError("未找到 .Newslist 通知列表，可能是登录拦截或页面结构变化。")
        notices = {}
        for anchor in root.select("a[href]"):
            link = urlsplit(urljoin(base_url or self.url, anchor["href"]))
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


def build_sources(urls: list[str]) -> list[HitszSource]:
    # Add other school adapters here without changing the delivery engine.
    sources = [HitszSource(url.strip()) for url in urls if url.strip()]
    if not sources:
        raise MonitorError("至少需要配置一个通知栏目。")
    if len({source.id for source in sources}) != len(sources):
        raise MonitorError("通知栏目重复，请检查 source_urls。")
    return sources
