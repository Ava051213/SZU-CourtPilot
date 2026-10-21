"""Local web dashboard for configuring and running the booking assistant."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .api import ApiError, AuthRequired, SzuClient
from .config import APP_DIR, CONFIG_PATH, Config, resolve_target_date
from .login import interactive_login, session_status
from .runner import Runner

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="SZU-CourtPilot")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class RunRequest(BaseModel):
    mode: str = "once"
    dry_run: bool = False


class ConfigPayload(BaseModel):
    data: dict[str, Any]


class State:
    def __init__(self) -> None:
        self.config = Config.load()
        self.runner = Runner(self.log)
        self.logs: deque[dict[str, Any]] = deque(maxlen=3000)
        self.seq = 0
        self.lock = threading.Lock()
        self.session: dict[str, Any] = {
            "logged_in": False,
            "checked": False,
            "message": "尚未检测",
        }
        self.login_running = False
        self.login_message = ""
        self.options: dict[str, Any] | None = None
        self.options_at = 0.0
        self.login_thread: threading.Thread | None = None

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.seq += 1
            entry = {"seq": self.seq, "time": stamp, "text": str(text)}
            self.logs.append(entry)
        try:
            with open(APP_DIR / "data" / "app.log", "a", encoding="utf-8") as fh:
                fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        except OSError:
            pass

    def refresh_session(self) -> None:
        result = session_status()
        result["checked"] = True
        with self.lock:
            self.session = result

    def start_login(self) -> None:
        if self.login_running:
            raise RuntimeError("登录窗口已经打开")
        self.login_running = True
        self.login_message = "正在打开登录窗口……"

        def work() -> None:
            try:
                interactive_login(self.config, self.log)
                self.login_message = "登录成功"
                self.refresh_session()
            except Exception as exc:
                self.login_message = f"登录失败：{exc}"
                self.log(self.login_message)
            finally:
                self.login_running = False

        self.login_thread = threading.Thread(target=work, daemon=True)
        self.login_thread.start()

    def load_options(self, force: bool = False) -> dict[str, Any]:
        if self.options and not force and time.time() - self.options_at < 300:
            return self.options
        client = SzuClient()
        try:
            data = client.venue_data()
        except Exception:
            return self._fallback_options()
        campuses = [c.get("XQDM_DISPLAY", "") for c in data.get("campusList", [])]
        sports: dict[str, list[str]] = {}
        venues: dict[str, list[str]] = {}
        campus_names = {str(c.get("XQDM")): str(c.get("XQDM_DISPLAY")) for c in data.get("campusList", [])}
        sport_names = {
            (str(x.get("XQDM")), str(x.get("XMDM"))): str(x.get("XMMC"))
            for x in data.get("xmList", [])
        }
        for campus in data.get("campusList", []):
            code = str(campus.get("XQDM"))
            name = str(campus.get("XQDM_DISPLAY"))
            sports[name] = sorted(
                {str(x.get("XMMC")) for x in data.get("xmList", []) if str(x.get("XQDM")) == code}
            )
        for venue in data.get("packageVenueList", []):
            name = str(venue.get("CGMC"))
            campus_code = str(venue.get("SSXQ"))
            campus_name = campus_names.get(campus_code, "")
            for sport_code in str(venue.get("XM", "")).split(","):
                sport_code = sport_code.strip()
                sport_name = sport_names.get((campus_code, sport_code), "")
                if sport_name:
                    venues.setdefault(f"{campus_name}|{sport_name}", []).append(name)
        result = {"campuses": campuses, "sports": sports, "venues": venues, "source": "api"}
        self.options = result
        self.options_at = time.time()
        return result

    def _fallback_options(self) -> dict[str, Any]:
        return {
            "campuses": ["粤海校区", "丽湖校区"],
            "sports": {
                "粤海校区": [
                    "羽毛球",
                    "足球",
                    "排球",
                    "网球",
                    "篮球",
                    "壁球",
                    "乒乓球",
                    "游泳",
                    "一楼重量型健身",
                    "二楼有氧健身",
                ],
                "丽湖校区": [
                    "羽毛球",
                    "足球",
                    "排球",
                    "网球",
                    "篮球",
                    "乒乓球",
                    "游泳",
                    "匹克球",
                    "桌球",
                    "瑜伽",
                ],
            },
            "venues": {},
            "source": "fallback",
        }


state = State()


@app.on_event("startup")
def _startup() -> None:
    state.log("程序已启动。首次使用请点击“打开登录窗口”。")
    threading.Thread(target=state.refresh_session, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {"config": state.config.to_dict(), "errors": state.config.validate()}


@app.post("/api/config")
def save_config(payload: ConfigPayload) -> dict[str, Any]:
    config = Config.from_dict(payload.data)
    errors = config.validate()
    if errors:
        raise HTTPException(status_code=400, detail="；".join(errors))
    config.save(CONFIG_PATH)
    state.config = config
    state.log("配置已保存。")
    return {"ok": True, "config": config.to_dict()}


@app.get("/api/status")
def status() -> dict[str, Any]:
    data = state.runner.status()
    data["session"] = state.session
    data["login"] = {"running": state.login_running, "message": state.login_message}
    data["now"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["config_errors"] = state.config.validate()
    return data


@app.post("/api/session/check")
def check_session() -> dict[str, Any]:
    state.refresh_session()
    return state.session


@app.post("/api/login")
def login() -> dict[str, Any]:
    try:
        state.start_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.get("/api/options")
def options(force: bool = False) -> dict[str, Any]:
    return state.load_options(force=force)


def _target_for(campus: str, sport: str):
    client = SzuClient()
    data = client.venue_data()
    target = client.resolve_target(data, campus, sport)
    return client, target


def _open_dates(client: SzuClient) -> list[str]:
    try:
        return client.dates()
    except AuthRequired:
        raise
    except Exception:
        return []


@app.get("/api/slots")
def slots(campus: str, sport: str, date_mode: str = "tomorrow", target_date: str = "") -> dict[str, Any]:
    """Time-slot grid for the picker.

    The target day is usually still closed before 12:30, but the picker must
    stay usable so the preferences can be prepared in advance.  When the day
    is closed we mirror the slot structure of the newest open day and report
    ``open: false`` instead of failing the request.
    """
    try:
        if date_mode == "custom":
            if not target_date:
                raise HTTPException(status_code=400, detail="请先选择指定日期")
            date = target_date
        else:
            date = resolve_target_date(Config(date_mode=date_mode), datetime.now())
        client, target = _target_for(campus, sport)
        dates = _open_dates(client)
        is_open = date in dates
        reference = "" if is_open else (max(dates) if dates else "")
        times: list[dict[str, Any]] = []
        message = ""
        if reference or is_open:
            try:
                times = client.times(
                    str(target.campus["XQDM"]),
                    date if is_open else reference,
                    str(target.sport["XMDM"]),
                )
            except ApiError as exc:
                message = str(exc)
        result = []
        for item in times:
            if is_open:
                result.append(
                    {
                        "code": str(item.get("CODE")),
                        "disabled": bool(item.get("disabled")),
                        "text": str(item.get("text") or ""),
                        "state": str(item.get("STATE_EXPLAIN") or ""),
                    }
                )
            else:
                result.append(
                    {"code": str(item.get("CODE")), "disabled": False, "text": "未开放", "state": ""}
                )
        return {
            "date": date,
            "dates": dates,
            "open": is_open,
            "reference": reference,
            "slots": result,
            "message": message,
        }
    except HTTPException:
        raise
    except AuthRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ApiError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取时间段失败：{exc}")


@app.get("/api/courts")
def courts(campus: str, sport: str, date: str, slot: str) -> dict[str, Any]:
    """Court names for a chosen date/slot.

    Court names are fixed per venue (A1-A8 .. D1-D8), so when the target day
    is not open yet the layout is read from the newest open day and every
    court comes back with ``available: null`` (unknown) instead of a 409.
    """
    try:
        client, target = _target_for(campus, sport)
        dates = _open_dates(client)
        is_open = date in dates
        reference = date if is_open else (max(dates) if dates else "")
        if not reference:
            raise ApiError("暂无可查询日期，无法读取场地")
        rooms = client.rooms(
            campus_code=str(target.campus["XQDM"]),
            date=reference,
            sport_code=str(target.sport["XMDM"]),
            slot=slot,
        )
        merged: dict[str, dict[str, Any]] = {}
        for room in rooms:
            name = str(room.get("CDMC") or "").strip()
            if not name:
                continue
            item = merged.setdefault(
                name,
                {"name": name, "available": 0, "total": 0, "venues": []},
            )
            item["total"] += 1
            if is_open and not room.get("disabled"):
                item["available"] += 1
            venue = str(room.get("CGBM_DISPLAY") or "")
            if venue and venue not in item["venues"]:
                item["venues"].append(venue)
        items = list(merged.values())
        if not is_open:
            for item in items:
                item["available"] = None
        return {
            "date": date,
            "slot": slot,
            "open": is_open,
            "reference": "" if is_open else reference,
            "courts": items,
        }
    except AuthRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ApiError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取场地失败：{exc}")


@app.post("/api/run")
def run(payload: RunRequest) -> dict[str, Any]:
    if state.runner.running:
        raise HTTPException(status_code=409, detail="已有任务在运行")
    if not state.session.get("logged_in"):
        state.refresh_session()
    if not state.session.get("logged_in"):
        raise HTTPException(status_code=400, detail="登录已失效，请先打开登录窗口")
    errors = state.config.validate()
    if errors:
        raise HTTPException(status_code=400, detail="；".join(errors))
    try:
        if payload.mode == "schedule":
            state.runner.start_schedule(state.config)
        else:
            state.runner.start_once(state.config, dry_run=payload.dry_run)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/stop")
def stop() -> dict[str, Any]:
    state.runner.stop()
    return {"ok": True}


@app.get("/api/logs")
def logs(after: int = 0) -> dict[str, Any]:
    with state.lock:
        items = [x for x in state.logs if x["seq"] > after]
        seq = state.seq
    return {"logs": items, "seq": seq}


@app.get("/api/screenshot")
def screenshot() -> FileResponse:
    path = APP_DIR / "data" / "login.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="暂无截图")
    return FileResponse(path, media_type="image/png")
