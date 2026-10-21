"""Configuration model and persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
COOKIE_PATH = DATA_DIR / "cookies.json"
LOG_PATH = DATA_DIR / "app.log"


@dataclass
class ScheduleConfig:
    enabled: bool = True
    time: str = "12:30:00"
    advance_seconds: int = 60
    weekdays: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])


@dataclass
class RetryConfig:
    max_seconds: int = 90
    interval_ms: int = 250
    request_timeout: int = 15


@dataclass
class NotifyConfig:
    webhook_url: str = ""
    beep: bool = True


@dataclass
class BrowserConfig:
    headless: bool = False
    profile_dir: str = "data/chrome-profile"


@dataclass
class Config:
    campus: str = "粤海校区"
    sport: str = "羽毛球"
    booking_type: str = "包场"
    date_mode: str = "tomorrow"  # tomorrow | today | custom
    target_date: str = ""
    time_slots: list[str] = field(
        default_factory=lambda: ["19:00-20:00", "20:00-21:00", "18:00-19:00"]
    )
    # How many bookings the run must collect, taken in time_slots order.
    # 1 means "any one slot is enough"; len(time_slots) means "all of them".
    needed_count: int = 1
    venues: list[str] = field(default_factory=lambda: ["运动广场东馆羽毛球场"])
    courts: list[str] = field(default_factory=list)
    strict_courts: bool = False
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        def sub(kind, value):
            if isinstance(value, dict):
                allowed = {f for f in kind.__dataclass_fields__}  # type: ignore[attr-defined]
                return kind(**{k: v for k, v in value.items() if k in allowed})
            return kind()

        def as_list(value, default: list[str]) -> list[str]:
            if value is None:
                return list(default)
            if isinstance(value, str):
                return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
            return [str(x) for x in value]

        base = cls()
        return cls(
            campus=str(data.get("campus", base.campus)),
            sport=str(data.get("sport", base.sport)),
            booking_type=str(data.get("booking_type", base.booking_type)),
            date_mode=str(data.get("date_mode", base.date_mode)),
            target_date=str(data.get("target_date", base.target_date)),
            time_slots=as_list(data.get("time_slots"), base.time_slots),
            needed_count=_as_int(data.get("needed_count"), base.needed_count),
            venues=as_list(data.get("venues"), base.venues),
            courts=as_list(data.get("courts"), base.courts),
            strict_courts=bool(data.get("strict_courts", base.strict_courts)),
            schedule=sub(ScheduleConfig, data.get("schedule")),
            retry=sub(RetryConfig, data.get("retry")),
            notify=sub(NotifyConfig, data.get("notify")),
            browser=sub(BrowserConfig, data.get("browser")),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.date_mode not in ("tomorrow", "today", "custom"):
            errors.append("date_mode 必须是 tomorrow/today/custom")
        if self.date_mode == "custom":
            try:
                __import__("datetime").date.fromisoformat(self.target_date)
            except ValueError:
                errors.append("指定日期必须是 YYYY-MM-DD 格式")
        if not self.time_slots:
            errors.append("至少选择一个时间段")
        for slot in self.time_slots:
            if not _valid_slot(slot):
                errors.append(f"时间段格式错误：{slot}（应为 HH:MM-HH:MM）")
        if self.needed_count < 1:
            errors.append("需要约到的场次数至少为 1")
        elif self.time_slots and self.needed_count > len(self.time_slots):
            errors.append(
                f"需要约到的场次数（{self.needed_count}）不能超过已选时间段数量（{len(self.time_slots)}）"
            )
        if self.strict_courts and not self.courts:
            errors.append("严格场地模式需要至少选择一个场地偏好")
        if not _valid_clock(self.schedule.time):
            errors.append("定时时间格式错误（应为 HH:MM:SS）")
        if self.schedule.advance_seconds < 0:
            errors.append("提前启动秒数不能为负")
        if self.retry.interval_ms < 50:
            errors.append("重试间隔不能小于 50ms")
        return errors

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls.from_dict(data)


def _as_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _valid_clock(value: str) -> bool:
    try:
        h, m, *rest = value.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and (not rest or 0 <= int(rest[0]) <= 59)
    except (ValueError, AttributeError):
        return False


def _valid_slot(value: str) -> bool:
    if "-" not in value:
        return False
    start, end = value.split("-", 1)
    if not (_valid_clock(start) and _valid_clock(end)):
        return False
    sh, sm = (int(x) for x in start.split(":")[:2])
    eh, em = (int(x) for x in end.split(":")[:2])
    return (eh * 60 + em) > (sh * 60 + sm)


def resolve_target_date(config: Config, now: datetime | None = None) -> str:
    """Return the booking date implied by the config."""
    now = now or datetime.now()
    if config.date_mode == "custom":
        return config.target_date
    if config.date_mode == "today":
        return now.date().isoformat()
    return (now.date() + timedelta(days=1)).isoformat()

