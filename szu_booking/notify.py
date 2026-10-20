"""Small, dependency-free notification helpers."""

from __future__ import annotations

import json
import threading
from typing import Callable

import requests


def notify(title: str, message: str, log: Callable[[str], None] = print) -> None:
    """Best-effort desktop beep + user-configured webhook."""
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass

    try:
        from .config import Config

        url = Config.load().notify.webhook_url.strip()
    except Exception:
        url = ""
    if not url:
        return

    def worker() -> None:
        try:
            if "sctapi.ftqq.com" in url:
                response = requests.post(
                    url,
                    data={"title": title, "desp": message},
                    timeout=10,
                )
            elif "api.day.app" in url:
                response = requests.post(
                    url.rstrip("/"),
                    json={"title": title, "body": message},
                    timeout=10,
                )
            else:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "text": message}, ensure_ascii=False),
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            if response.status_code >= 400:
                log(f"通知发送失败：HTTP {response.status_code}")
        except Exception as exc:
            log(f"通知发送失败：{exc}")

    threading.Thread(target=worker, daemon=True).start()
