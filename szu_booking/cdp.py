"""Minimal Chrome DevTools Protocol client.

We deliberately avoid Selenium/Playwright: Chrome is already installed on the
target machine, and a tiny CDP client keeps the setup to ``pip install -r``
with no browser-driver downloads.  The client is synchronous and is intended
to be used by one worker thread at a time.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import urlopen

import websocket


class CDPError(RuntimeError):
    pass


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise CDPError("未找到 Chrome/Edge，请安装 Chrome 或设置 CHROME_PATH")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Chrome:
    """Launch (or attach to) a Chrome instance and drive one page target."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        headless: bool = True,
        port: int | None = None,
        window_size: str = "1440,1000",
        chrome_path: str | None = None,
        extra_args: Iterable[str] | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir).resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.port = port or _free_port()
        self.window_size = window_size
        self.chrome_path = chrome_path or find_chrome()
        self.extra_args = list(extra_args or [])
        self.proc: subprocess.Popen[bytes] | None = None
        self.ws: websocket.WebSocket | None = None
        self._id = 0
        self._events: list[dict[str, Any]] = []
        self._closed = False

    # ------------------------------------------------------------------ launch
    def start(self, url: str = "about:blank", timeout: float = 30.0) -> None:
        args = [
            self.chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-features=Translate,OptimizationHints",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={self.window_size}",
            "--remote-allow-origins=*",
        ]
        if os.environ.get("SZUB_CHROME_NO_SANDBOX") == "1":
            # Only for running inside a restrictive sandbox (e.g. during
            # development).  Never enable this for a normal desktop run.
            args += ["--no-sandbox", "--disable-crash-reporter", "--disable-breakpad"]
        args += self.extra_args
        if self.headless:
            args += ["--headless=new", "--disable-gpu"]
        args.append(url)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                    info = json.loads(r.read().decode("utf-8"))
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise CDPError("Chrome 启动失败（进程已退出）")
                time.sleep(0.25)
        if info is None:
            self.close()
            raise CDPError("Chrome 调试端口启动超时")

        targets = self._http_json("/json/list")
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            # Open a page target explicitly.
            try:
                self._http_put(f"/json/new?{url}")
            except Exception:
                pass
            time.sleep(0.4)
            pages = [t for t in self._http_json("/json/list") if t.get("type") == "page"]
            if not pages:
                self.close()
                raise CDPError("无法创建浏览器页面")
        ws_url = pages[0]["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(
            ws_url, timeout=15, suppress_origin=True, max_size=64 * 1024 * 1024
        )
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")
        try:
            self.call(
                "Emulation.setDeviceMetricsOverride",
                width=int(self.window_size.split(",")[0]),
                height=int(self.window_size.split(",")[1]),
                deviceScaleFactor=1,
                mobile=False,
            )
        except CDPError:
            pass

    def _http_json(self, path: str) -> Any:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def _http_put(self, path: str) -> Any:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="PUT"
        )
        with urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    # --------------------------------------------------------------------- CDP
    def call(self, method: str, timeout: float = 30.0, **params: Any) -> Any:
        if self.ws is None:
            raise CDPError("浏览器未连接")
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise CDPError(f"{method}: {data['error']}")
                return data.get("result", {})
            if "method" in data:
                self._events.append(data)
        raise CDPError(f"{method}: 超时")

    def wait_event(self, method: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for i, ev in enumerate(self._events):
                if ev.get("method") == method:
                    return self._events.pop(i)
            self.ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if raw:
                data = json.loads(raw)
                if "method" in data:
                    self._events.append(data)
        raise CDPError(f"等待事件 {method} 超时")

    def eval(self, expression: str, *, await_promise: bool = False, timeout: float = 30.0) -> Any:
        res = self.call(
            "Runtime.evaluate",
            timeout=timeout,
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CDPError(f"JS 执行失败: {text}")
        return res.get("result", {}).get("value")

    def navigate(self, url: str, timeout: float = 45.0) -> None:
        self.call("Page.navigate", url=url)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval("document.readyState") == "complete":
                    return
            except CDPError:
                pass
            time.sleep(0.2)
        raise CDPError(f"页面加载超时: {url}")

    def current_url(self) -> str:
        return str(self.eval("location.href") or "")

    def html(self) -> str:
        return str(self.eval("document.documentElement.outerHTML") or "")

    def screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.call("Page.captureScreenshot", format="png", fromSurface=True)
        path.write_bytes(base64.b64decode(data["data"]))
        return path

    def wait_for(
        self,
        js_expression: str,
        *,
        timeout: float = 30.0,
        interval: float = 0.25,
        description: str = "",
    ) -> Any:
        deadline = time.time() + timeout
        last: Any = None
        while time.time() < deadline:
            try:
                last = self.eval(js_expression)
                if last:
                    return last
            except CDPError:
                pass
            time.sleep(interval)
        raise CDPError(f"等待条件超时{('：' + description) if description else ''}")

    # ----------------------------------------------------------------- cookies
    def get_cookies(self) -> list[dict[str, Any]]:
        result = self.call("Storage.getCookies", timeout=10)
        return list(result.get("cookies", []))

    def restore_cookies(self, cookies: Iterable[dict[str, Any]], days: int = 30) -> int:
        """Re-inject exported cookies, turning session cookies into persistent ones."""
        self.call("Network.enable", timeout=10)
        now = int(time.time())
        count = 0
        for c in cookies:
            domain = str(c.get("domain") or "")
            if not domain:
                continue
            path = str(c.get("path") or "/")
            secure = bool(c.get("secure"))
            url = f"{'https' if secure else 'http'}://{domain.lstrip('.')}{path}"
            expires = c.get("expires")
            if not expires or expires <= 0:
                expires = now + days * 86400
            params: dict[str, Any] = {
                "name": c["name"],
                "value": c.get("value", ""),
                "url": url,
                "path": path,
                "secure": secure,
                "httpOnly": bool(c.get("httpOnly")),
                "expires": expires,
            }
            if c.get("sameSite"):
                params["sameSite"] = c["sameSite"]
            if domain.startswith("."):
                params["domain"] = domain
            try:
                self.call("Network.setCookie", timeout=10, **params)
                count += 1
            except CDPError:
                continue
        return count

    def save_cookies(self, path: str | Path, days: int = 30) -> int:
        cookies = self.get_cookies()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        for c in cookies:
            if not c.get("expires") or c["expires"] <= 0:
                c["expires"] = now + days * 86400
        path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(cookies)


    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.ws is not None:
                self.call("Browser.close", timeout=3)
        except Exception:
            pass
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                else:
                    self.proc.terminate()
            except Exception:
                pass
        self.proc = None
