#!/usr/bin/env python3
"""Standalone demo: no AstrBot instance or message platform required."""

import argparse
import asyncio
import getpass
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from notice_monitor.auth import PortalClient, fetch_notices, write_private_json
from notice_monitor.engine import Monitor
from notice_monitor.sources import DEFAULT_URL, MonitorError, build_sources
from notice_monitor.storage import Store


def arguments():
    parser = argparse.ArgumentParser(description="哈工深通知监控 demo（不会接入 AstrBot）")
    parser.add_argument("--data-dir", type=Path, default=Path(".demo-data"))
    parser.add_argument("--url", action="append", help="可重复指定同门户栏目网址")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=25)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("credentials", help="隐藏输入密码，保存到本地 credentials.json")
    commands.add_parser("fetch", help="登录并抓取一次，输出 JSON；不修改监控基线")
    cookies = commands.add_parser("import-cookies", help="导入手动登录后导出的 Cookie JSON")
    cookies.add_argument("file", type=Path)
    parse = commands.add_parser("parse", help="离线解析 HTML，不访问网络")
    parse.add_argument("file", type=Path)
    watch = commands.add_parser("watch", help="轮询通知，模拟推送到控制台")
    watch.add_argument("--interval", type=int, default=180)
    watch.add_argument("--push-existing", action="store_true", help="首次基线也推送窗口内通知")
    return parser.parse_args()


async def watch(args, sources):
    store = Store(args.data_dir)

    async def print_message(target, text):
        print(text + "\n", flush=True)
        return True

    monitor = Monitor(
        store, args.data_dir, sources, print_message,
        interval=args.interval, max_pages=args.max_pages, timeout=args.timeout,
        push_on_first=args.push_existing,
    )
    for source in sources:
        store.subscribe(source.id, "console")
    print("控制台监控已启动；默认首次只建立基线。Ctrl+C 退出。", flush=True)
    try:
        while True:
            result = await monitor.run_once()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            await asyncio.sleep(min(monitor.interval, 60))
    finally:
        await monitor.stop()
        store.close()


def main():
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "credentials":
            if not sys.stdin.isatty():
                raise MonitorError("请在交互终端运行 credentials，以隐藏输入密码。")
            username = input("学号/工号：").strip()
            password = getpass.getpass("统一身份认证密码（不回显）：")
            if not username or not password:
                raise MonitorError("账号和密码不能为空。")
            write_private_json(args.data_dir / "credentials.json", {"username": username, "password": password})
            (args.data_dir / "login_retry.json").unlink(missing_ok=True)
            # A changed account must not silently reuse the previous account's session.
            (args.data_dir / "cookies.txt").unlink(missing_ok=True)
            print(f"已保存到 {args.data_dir / 'credentials.json'}（明文，文件权限 0600）。")
            return 0
        if args.command == "import-cookies":
            client = PortalClient(args.data_dir)
            try:
                count = client.import_cookies(args.file)
            finally:
                client.close()
            print(f"已导入 {count} 个学校 Cookie；运行 fetch 检查会话是否有效。")
            return 0
        sources = build_sources(args.url or [DEFAULT_URL])
        if args.command == "parse":
            notices = sources[0].parse(args.file.read_text(encoding="utf-8"))
            print(json.dumps([asdict(notice) for notice in notices], ensure_ascii=False, indent=2))
        elif args.command == "fetch":
            for source in sources:
                notices = fetch_notices(
                    args.data_dir, source, min(10, max(1, args.max_pages)),
                    min(120, max(5, args.timeout)),
                )
                print(json.dumps([asdict(notice) for notice in notices], ensure_ascii=False, indent=2))
        else:
            asyncio.run(watch(args, sources))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, MonitorError) else f"操作失败（{type(exc).__name__}），请检查本地文件或配置。"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
