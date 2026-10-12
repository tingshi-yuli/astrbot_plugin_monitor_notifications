"""Extract the displayed article, keeping text and inline images in order."""

import re
import time
from dataclasses import asdict
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .sources import LoginRequired, Notice, ParseError, is_login


def portal_url(value: str, base: str) -> str:
    url = urljoin(base, value)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "info.hitsz.edu.cn"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise ParseError("正文资源地址不属于学校通知门户。")
    return url


def display_url(value: str, base: str) -> str:
    """Keep ordinary outbound article links as text, without requesting them."""
    url = urljoin(base, value)
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https", "mailto", "tel"} and not (
        parsed.username or parsed.password
    ):
        return url
    return ""


def parse_detail(html: str, notice: Notice, response_url: str) -> dict:
    if is_login(response_url, html):
        raise LoginRequired("通知详情需要重新登录。")
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#vsb_content, #vsb_content_2, .v_news_content")
    if root is None:
        raise ParseError("未找到通知正文，无法生成摘要或完整内容。")
    for node in root.select("script, style, noscript, iframe, form"):
        node.decompose()
    blocks, attachments, buffer = [], [], []

    def flush():
        text = "".join(buffer).replace("\xa0", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            blocks.append({"type": "text", "text": text})
        buffer.clear()

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            buffer.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "img":
            flush()
            src = node.get("src") or node.get("data-src") or ""
            try:
                url = portal_url(src, response_url) if src else ""
            except ParseError:
                url = ""
            blocks.append({"type": "image", "url": url, "alt": node.get("alt", "")})
            return
        if node.name == "table":
            flush()
            rows = []
            for row in node.select("tr"):
                cells = row.find_all(["td", "th"], recursive=False)
                values = []
                for cell in cells:
                    value = cell.get_text(" ", strip=True)
                    for anchor in cell.select("a[href]"):
                        link = display_url(anchor["href"], response_url)
                        if link:
                            value += f" ({link})"
                    spans = [
                        f"{name}={cell[name]}"
                        for name in ("rowspan", "colspan")
                        if cell.get(name)
                    ]
                    values.append(
                        value + (" [" + ", ".join(spans) + "]" if spans else "")
                    )
                rows.append(" | ".join(values))
            blocks.append({"type": "text", "text": "表格：\n" + "\n".join(rows)})
            # Images inside table cells also belong to the displayed article.
            for img in node.select("img"):
                walk(img)
            return
        boundary = node.name in {
            "p",
            "div",
            "section",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "blockquote",
        }
        if boundary:
            buffer.append("\n")
        if node.name == "br":
            buffer.append("\n")
        for child in node.children:
            walk(child)
        if node.name == "a" and node.get("href"):
            url = display_url(node["href"], response_url)
            if url:
                buffer.append(f" ({url})")
        if boundary:
            buffer.append("\n")

    walk(root)
    flush()
    # Attachment links can live outside the article container. Never fetch them.
    seen = set()
    for anchor in soup.select("a[href]"):
        href = anchor["href"]
        if not (
            re.search(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z)(?:[?#]|$)", href, re.I)
            or "download.jsp" in href.lower()
        ):
            continue
        url = display_url(href, response_url)
        if not url:
            continue
        if url not in seen:
            attachments.append(
                {"name": anchor.get_text(" ", strip=True) or "附件", "url": url}
            )
            seen.add(url)
    if not blocks:
        raise ParseError("通知正文为空，无法生成摘要或完整内容。")
    return {
        "notice": asdict(notice),
        "blocks": blocks,
        "attachments": attachments,
        "fetched_at": time.time(),
        "summary": "",
    }


def wants_detail(text: str, triggers: list[str]) -> bool:
    normalized = re.sub(r"[\s，,。.!！?？]", "", text)
    if normalized and normalized in {
        re.sub(r"[\s，,。.!！?？]", "", trigger) for trigger in triggers
    }:
        return True
    return bool(
        re.fullmatch(
            r"(?:请|麻烦)?(?:把|将)?(?:这条|这份|这个|该)?(?:通知|消息)?(?:的)?"
            r"(?:详细内容|完整内容|全文|原文)(?:发给我|发我|给我|看看|展开)?",
            normalized,
        )
        or re.fullmatch(
            r"(?:请|麻烦)?(?:给我|发我|发给我|看看)(?:这条通知的)?(?:全文|原文|完整内容|详细内容)",
            normalized,
        )
    )


def quoted_notice(text: str) -> Notice | None:
    """Recover a pre-upgrade notification only after its sender is verified."""
    lines = text.strip().splitlines()
    if len(lines) < 3 or lines[0] != "【哈工深新通知】":
        return None
    match = re.search(r"https?://info\.hitsz\.edu\.cn/content\.jsp\?[^\s<>]+", text)
    if not match:
        return None
    query = parse_qs(urlsplit(match[0]).query)
    number = query.get("wbnewsid", [""])[0]
    if not number.isdigit():
        return None
    date = next(
        (line.removeprefix("日期：") for line in lines if line.startswith("日期：")), ""
    )
    return Notice(f"wbnews:{number}", lines[1], portal_url(match[0], match[0]), date)
