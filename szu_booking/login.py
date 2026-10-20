"""Interactive CAS login using a real Chrome window.

Passwords are never read or stored by this program.  The user logs in through
the normal university page (QR code, password, 7-day login, ...) and we keep
only the resulting session cookies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .api import SzuClient
from .cdp import Chrome
from .config import COOKIE_PATH, Config

LOGIN_URL = (
    "https://authserver.szu.edu.cn/authserver/login"
    "?service=https%3A%2F%2Fehall.szu.edu.cn%3A443%2Fqljfwapp%2Fsys%2FlwSzuCgyy%2Findex.do"
)
APP_URL = "https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy/index.do#/sportVenue"
SCREENSHOT = Path(__file__).resolve().parent.parent / "data" / "login.png"


def interactive_login(
    config: Config,
    log: Callable[[str], None] = print,
    *,
    timeout: int = 600,
    cookie_path: Path = COOKIE_PATH,
) -> str:
    """Open Chrome, wait for the user to finish CAS login, save cookies."""
    log("正在打开 Chrome 登录窗口……")
    profile = Path(config.browser.profile_dir)
    if not profile.is_absolute():
        profile = Path(__file__).resolve().parent.parent / profile
    chrome = Chrome(
        profile,
        headless=False,
        window_size="1440,1000",
    )
    try:
        chrome.start("about:blank")
        # Seed the existing session first.  If it is still valid, CAS will
        # usually redirect straight back and the user does not have to log in
        # again just to refresh the cookie file.
        if cookie_path.exists():
            try:
                cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
                if cookies:
                    chrome.restore_cookies(cookies)
            except (OSError, ValueError):
                pass
        chrome.navigate(LOGIN_URL)
        chrome.eval("window.focus()")
        log("请在弹出的 Chrome 窗口中登录（可扫码）。登录成功后窗口会自动关闭。")
        deadline = time.time() + timeout
        last_note = 0.0
        while time.time() < deadline:
            try:
                url = chrome.current_url()
                body = chrome.eval("document.body ? document.body.innerText : ''") or ""
            except Exception:
                time.sleep(2)
                continue
            if "ehall.szu.edu.cn" in url and "体育场馆预约" in body:
                log("检测到登录成功，正在保存会话……")
                break
            if time.time() - last_note > 20:
                log("等待登录中……")
                last_note = time.time()
            time.sleep(2)
        else:
            try:
                chrome.screenshot(SCREENSHOT)
                log(f"登录超时，截图已保存：{SCREENSHOT}")
            except Exception:
                pass
            raise TimeoutError("登录超时，请重试")

        count = chrome.save_cookies(cookie_path, days=30)
        # Also make the profile's session cookies persistent, so a browser
        # restart does not immediately lose the login.
        chrome.restore_cookies(chrome.get_cookies())
        log(f"已保存 {count} 个 Cookie。")
    finally:
        chrome.close()

    client = SzuClient(cookie_path)
    identity = client.check_session()
    log(f"登录有效：{identity.name}（{identity.user_id}）")
    return identity.user_id


def session_status(cookie_path: Path = COOKIE_PATH) -> dict[str, str | bool]:
    client = SzuClient(cookie_path)
    try:
        identity = client.check_session()
        return {"logged_in": True, "name": identity.name, "user_id": identity.user_id}
    except Exception as exc:
        return {"logged_in": False, "message": str(exc)}
