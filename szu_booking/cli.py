"""Command-line interface (optional; the web dashboard is the main UI)."""

from __future__ import annotations

import argparse
import sys
import time

from .api import ApiError, AuthRequired, SzuClient
from .config import Config
from .login import interactive_login, session_status
from .runner import Runner


def cmd_login(args: argparse.Namespace) -> int:
    interactive_login(Config.load())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    result = session_status()
    print(result)
    return 0 if result.get("logged_in") else 1


def cmd_once(args: argparse.Namespace) -> int:
    config = Config.load()
    errors = config.validate()
    if errors:
        print("配置错误：" + "；".join(errors))
        return 2
    runner = Runner(print)
    runner.start_once(config, dry_run=args.dry_run)
    runner.join()
    result = runner.last_result or {}
    return 0 if result.get("success") else 1


def cmd_schedule(args: argparse.Namespace) -> int:
    config = Config.load()
    errors = config.validate()
    if errors:
        print("配置错误：" + "；".join(errors))
        return 2
    runner = Runner(print)
    runner.start_schedule(config)
    print("定时任务已启动，按 Ctrl+C 退出。")
    try:
        while runner.running:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop()
        runner.join(5)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = Config.load()
    client = SzuClient()
    try:
        identity = client.check_session()
        print(f"登录有效：{identity.name}（{identity.user_id}）")
        data = client.venue_data()
        target = client.resolve_target(data, config.campus, config.sport)
        print(f"校区：{target.campus['XQDM_DISPLAY']}（{target.campus['XQDM']}）")
        print(f"项目：{target.sport['XMMC']}（{target.sport['XMDM']}）")
        print("场馆：")
        for venue in target.venues:
            print(f"  - {venue['CGMC']}（{venue['CGBM']}）")
        print(f"可选日期：{'、'.join(client.dates())}")
        print(f"服务器时间：{client.server_now():%Y-%m-%d %H:%M:%S}")
        return 0
    except AuthRequired as exc:
        print(f"未登录：{exc}")
        return 1
    except ApiError as exc:
        print(f"接口错误：{exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="szu-courtpilot", description="SZU-CourtPilot · 荔园抢场")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="打开 Chrome 登录并保存会话").set_defaults(func=cmd_login)
    sub.add_parser("check", help="检查登录状态").set_defaults(func=cmd_check)
    once = sub.add_parser("once", help="立即尝试预约一次")
    once.add_argument("--dry-run", action="store_true", help="只查询空场，不提交")
    once.set_defaults(func=cmd_once)
    sub.add_parser("schedule", help="按配置时间循环抢场").set_defaults(func=cmd_schedule)
    sub.add_parser("doctor", help="检查配置与接口").set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
