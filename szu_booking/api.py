"""HTTP client for the SZU sports-venue application.

The booking page is a Vue app that talks to a small JSON API.  Driving that
API directly is both faster and much less brittle than clicking a browser at
12:30, so the runner uses this client for every read and for submission.
The browser is only used once, to obtain the CAS session cookies.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

BASE = "https://ehall.szu.edu.cn/qljfwapp/sys/lwSzuCgyy"
INDEX_URL = f"{BASE}/index.do"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class AuthRequired(RuntimeError):
    """Raised when the saved session can no longer access the app."""


class ApiError(RuntimeError):
    """Raised for a business/API error returned by the server."""


@dataclass
class Identity:
    user_id: str
    name: str
    dept: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class Target:
    campus: dict[str, Any]
    sport: dict[str, Any]
    venues: list[dict[str, Any]]


class SzuClient:
    def __init__(
        self,
        cookie_path: str | Path | None = None,
        *,
        timeout: int = 15,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        if cookie_path is None:
            from .config import COOKIE_PATH

            cookie_path = COOKIE_PATH
        self.cookie_path = Path(cookie_path) if cookie_path else None
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": INDEX_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        self._time_offset: float | None = None
        if self.cookie_path and self.cookie_path.exists():
            self.load_cookies(self.cookie_path)

    # ------------------------------------------------------------------ cookies
    def load_cookies(self, path: str | Path) -> int:
        cookies = json.loads(Path(path).read_text(encoding="utf-8"))
        count = 0
        for c in cookies:
            if not c.get("name"):
                continue
            try:
                self.session.cookies.set(
                    c["name"],
                    c.get("value", ""),
                    domain=c.get("domain"),
                    path=c.get("path", "/"),
                )
                count += 1
            except Exception:
                continue
        return count

    # ------------------------------------------------------------------ request
    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        allow_redirects: bool = True,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
        response = self.session.request(
            method,
            url,
            data=data,
            timeout=self.timeout,
            allow_redirects=allow_redirects,
        )
        if self._time_offset is None and response.headers.get("Date"):
            try:
                server = parsedate_to_datetime(response.headers["Date"]).timestamp()
                self._time_offset = server - time.time()
            except (TypeError, ValueError):
                pass
        if "authserver" in response.url or "统一身份认证" in response.text[:2000]:
            raise AuthRequired("登录已失效，请重新登录")
        return response

    def _json(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        response = self._request(method, path, data=data)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:300].replace("\n", " ")
            raise ApiError(f"服务器返回的不是 JSON：{snippet}") from exc

    # ----------------------------------------------------------------- identity
    def identity(self) -> Identity | None:
        """Return the logged-in user, or None if the session is not valid."""
        try:
            response = self._request("GET", INDEX_URL)
        except AuthRequired:
            return None
        if "authserver" in response.url:
            return None
        html = response.text
        match = re.search(r"USERID='([^']*)'", html)
        if not match:
            return None
        user_id = match.group(1)
        name = ""
        dept = ""
        info_match = re.search(r"USER_INFO=(\{.*?\});", html, re.S)
        if info_match:
            try:
                info = json.loads(info_match.group(1))
                name = str(info.get("userName") or info.get("name") or "")
                dept = str(info.get("deptName") or "")
            except ValueError:
                pass
        return Identity(user_id=user_id, name=name, dept=dept)

    def check_session(self) -> Identity:
        identity = self.identity()
        if identity is None:
            raise AuthRequired("未登录或登录已过期")
        return identity

    # -------------------------------------------------------------------- reads
    def venue_data(self) -> dict[str, Any]:
        data = self._json("GET", "sportVenue/getSportVenueData.do")
        if str(data.get("code")) != "0":
            raise ApiError(str(data.get("msg") or "读取场馆配置失败"))
        return data

    def dates(self) -> list[str]:
        data = self._json("POST", "sportVenue/getRqList.do")
        if not isinstance(data, list):
            raise ApiError("日期列表格式异常")
        return [str(x) for x in data]

    def times(self, campus_code: str, date: str, sport_code: str, booking_type: str = "1.0") -> list[dict[str, Any]]:
        data = self._json(
            "POST",
            "sportVenue/getTimeList.do",
            {
                "XQ": campus_code,
                "YYRQ": date,
                "YYLX": booking_type,
                "XMDM": sport_code,
            },
        )
        if not isinstance(data, list):
            raise ApiError(str(data.get("msg") if isinstance(data, dict) else "时间段数据异常"))
        return data

    def rooms(
        self,
        *,
        campus_code: str,
        date: str,
        sport_code: str,
        slot: str,
        booking_type: str = "1.0",
        venue_code: str | None = None,
    ) -> list[dict[str, Any]]:
        start, end = slot.split("-", 1)
        params = {
            "XMDM": sport_code,
            "YYRQ": date,
            "YYLX": booking_type,
            "KSSJ": start,
            "JSSJ": end,
            "XQDM": campus_code,
        }
        if venue_code and venue_code != "all":
            params["CGBM"] = venue_code
        data = self._json("POST", "modules/sportVenue/getOpeningRoom.do", params)
        try:
            rows = data["datas"]["getOpeningRoom"]["rows"]
        except (KeyError, TypeError) as exc:
            raise ApiError(f"场地列表格式异常：{str(data)[:200]}") from exc
        if not isinstance(rows, list):
            raise ApiError("场地列表格式异常")
        return rows

    # ------------------------------------------------------------------- submit
    def submit(
        self,
        *,
        identity: Identity,
        room: dict[str, Any],
        date: str,
        slot: str,
    ) -> dict[str, Any]:
        start, end = slot.split("-", 1)
        payload = {
            "DHID": "",
            "YYRGH": identity.user_id,
            "YYRXM": identity.name,
            "CYRS": "",
            "CQBM": "",
            "CGDM": room.get("CGBM", ""),
            "CDWID": room.get("WID", ""),
            "XMDM": room.get("XMDM", ""),
            "XQWID": room.get("XQDM", ""),
            "KYYSJD": slot,
            "YYRQ": date,
            "YYLX": room.get("DCFS") or "1.0",
            "YYKS": f"{date} {start}",
            "YYJS": f"{date} {end}",
            "PC_OR_PHONE": "pc",
        }
        data = self._json("POST", "sportVenue/insertVenueBookingInfo.do", payload)
        if not isinstance(data, dict):
            raise ApiError("预约接口返回格式异常")
        return data

    # ------------------------------------------------------------------- catalog
    def resolve_target(self, data: dict[str, Any], campus_name: str, sport_name: str) -> Target:
        campus = next(
            (c for c in data.get("campusList", []) if c.get("XQDM_DISPLAY") == campus_name),
            None,
        )
        if campus is None:
            campuses = "、".join(c.get("XQDM_DISPLAY", "") for c in data.get("campusList", []))
            raise ApiError(f"找不到校区“{campus_name}”，可选：{campuses}")

        sports = [
            x
            for x in data.get("xmList", [])
            if x.get("XQDM") == campus.get("XQDM") and x.get("XMMC") == sport_name
        ]
        if not sports:
            names = sorted(
                {
                    x.get("XMMC", "")
                    for x in data.get("xmList", [])
                    if x.get("XQDM") == campus.get("XQDM")
                }
            )
            raise ApiError(f"找不到项目“{sport_name}”，可选：{'、'.join(names)}")
        sport = sports[0]
        if "1.0" not in str(sport.get("DCFS") or ""):
            raise ApiError(f"“{sport_name}”不支持包场预约")
        venues = [
            v
            for v in data.get("packageVenueList", [])
            if v.get("SSXQ") == campus.get("XQDM")
            and str(campus.get("XQDM")) == str(v.get("SSXQ"))
            and str(sport.get("XMDM")) in str(v.get("XM", "")).split(",")
        ]
        return Target(campus=campus, sport=sport, venues=venues)

    def filter_venues(self, target: Target, names: list[str]) -> list[dict[str, Any]]:
        if not names:
            return list(target.venues)
        result = []
        for wanted in names:
            exact = [v for v in target.venues if v.get("CGMC") == wanted]
            if exact:
                result.extend(exact)
                continue
            partial = [v for v in target.venues if wanted in str(v.get("CGMC", ""))]
            result.extend(partial)
        # stable de-duplication
        seen = set()
        unique = []
        for venue in result:
            code = venue.get("CGBM")
            if code not in seen:
                seen.add(code)
                unique.append(venue)
        return unique

    # --------------------------------------------------------------------- time
    def server_now(self) -> datetime:
        if self._time_offset is None:
            try:
                self._request("GET", INDEX_URL, allow_redirects=False)
            except Exception:
                pass
        return datetime.fromtimestamp(time.time() + (self._time_offset or 0))


def room_label(room: dict[str, Any]) -> str:
    return str(room.get("CDMC") or room.get("WID") or "场地")
