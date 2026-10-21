"""Booking orchestration: preparation, the 12:30 race, and daily scheduling."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta
from typing import Any, Callable

from .api import ApiError, AuthRequired, DateNotOpen, Identity, SzuClient, Target, room_label
from .config import Config, COOKIE_PATH, resolve_target_date
from .notify import notify


@dataclass
class Prepared:
    client: SzuClient
    identity: Identity
    target: Target
    date: str
    venues: list[dict[str, Any]]
    slots: list[str]
    data: dict[str, Any] = field(default_factory=dict)


_QUOTA_HINTS = ("超出", "超过", "上限", "次数", "最多", "限制", "限约")


def _looks_like_quota(message: str) -> bool:
    """Best-effort detection of a per-account booking cap.

    The API reports this only as free text, so the hints lean towards
    stopping: cutting a run short is recoverable, burning the whole window on
    a cap that will never lift is not.
    """
    return any(hint in message for hint in _QUOTA_HINTS)


class Runner:
    def __init__(self, log: Callable[[str], None] = print) -> None:
        self.log = log
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.mode: str | None = None
        self.started_at: str | None = None
        self.next_run: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.phase: str = "idle"
        self.dry_run = False

    # ------------------------------------------------------------------ status
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.mode,
            "phase": self.phase,
            "started_at": self.started_at,
            "next_run": self.next_run,
            "last_result": self.last_result,
            "dry_run": self.dry_run,
        }

    # ------------------------------------------------------------------ control
    def start_once(self, config: Config, *, dry_run: bool = False) -> None:
        self._start("once", config, dry_run=dry_run)

    def start_schedule(self, config: Config) -> None:
        self._start("schedule", config)

    def _start(self, mode: str, config: Config, *, dry_run: bool = False) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("已有任务在运行")
            errors = config.validate()
            if errors:
                raise ValueError("；".join(errors))
            self._stop.clear()
            self.mode = mode
            self.dry_run = dry_run
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_result = None
            self.phase = "starting"
            target = self._run_once if mode == "once" else self._run_schedule
            self._thread = threading.Thread(target=target, args=(config,), daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.phase = "stopping"
        self.log("收到停止指令，正在结束任务……")

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -------------------------------------------------------------------- dates
    def _target_date(self, config: Config, now: datetime) -> str:
        return resolve_target_date(config, now)

    def _prepare(self, config: Config, *, strict_date: bool = True) -> Prepared:
        client = SzuClient(COOKIE_PATH, timeout=config.retry.request_timeout)
        identity = client.check_session()
        self.log(f"登录有效：{identity.name}（{identity.user_id}）")
        data = client.venue_data()
        target = client.resolve_target(data, config.campus, config.sport)
        venues = client.filter_venues(target, config.venues)
        if not venues:
            names = "、".join(str(v.get("CGMC")) for v in target.venues)
            raise ApiError(f"找不到场馆 {config.venues}，可选：{names}")
        date = self._target_date(config, client.server_now())
        now = client.server_now()
        target_day = date_cls.fromisoformat(date)
        slots = list(config.time_slots)
        if target_day < now.date():
            raise ApiError(f"预约日期 {date} 已经过去，请改选今天或明天")
        if target_day == now.date():
            future = []
            for slot in slots:
                end = datetime.strptime(slot.split("-")[1], "%H:%M").time()
                if end > now.time():
                    future.append(slot)
            skipped = [s for s in slots if s not in future]
            if skipped:
                self.log(f"已跳过今天已过期的时间段：{'、'.join(skipped)}")
            if not future:
                raise ApiError("所选时间段今天均已过期或已结束，请把预约日期改为“明天”后重试")
            slots = future
        dates = client.dates()
        if date not in dates:
            message = f"预约日期 {date} 尚未开放（当前可选：{'、'.join(dates)}）"
            if strict_date:
                raise ApiError(message)
            self.log(message + "，到点后会继续重试。")
        self.log(
            f"目标：{config.campus} · {config.sport} · {date} · "
            f"场馆 {'、'.join(str(v.get('CGMC')) for v in venues)}"
        )
        needed = max(1, min(config.needed_count, len(slots)))
        self.log(f"时间段优先级：{' > '.join(slots)}（需 {needed} 场）")
        return Prepared(
            client=client,
            identity=identity,
            target=target,
            date=date,
            venues=venues,
            slots=slots,
            data=data,
        )

    # --------------------------------------------------------------------- once
    def _run_once(self, config: Config) -> None:
        self._execute_immediate(config)

    def _execute_immediate(self, config: Config) -> None:
        try:
            self.phase = "preparing"
            prepared = self._prepare(config)
            deadline = time.time() + max(5, config.retry.max_seconds)
            self.phase = "racing"
            result = self._attempt(prepared, config, deadline)
            self._finish(result)
        except Exception as exc:
            self._fail(exc)

    # ---------------------------------------------------------------- schedule
    def _run_schedule(self, config: Config) -> None:
        while not self._stop.is_set():
            try:
                self.phase = "waiting"
                now = datetime.now()
                target = self._next_target(config, now)
                self.next_run = target.strftime("%Y-%m-%d %H:%M:%S")
                prewarm = target - timedelta(seconds=config.schedule.advance_seconds)
                if prewarm > now:
                    self.phase = "waiting"
                    self.log(f"下一场：{self.next_run}（提前 {config.schedule.advance_seconds} 秒准备）")
                    if not self._wait_until(prewarm):
                        break
                else:
                    self.log(f"已进入抢场窗口：{self.next_run}")

                self.phase = "preparing"
                prepared = self._prepare(config, strict_date=False)
                # Wait for the exact opening instant.
                if not self._wait_until(target):
                    break
                self.phase = "racing"
                deadline = target.timestamp() + max(5, config.retry.max_seconds)
                self.log("时间到，开始抢场！")
                result = self._attempt(prepared, config, deadline)
                self._finish(result)
                if result.get("success") or result.get("partial") or result.get("uncertain"):
                    # Something is already booked (or its state is unknown), so
                    # stop here and let the user settle it instead of firing a
                    # second round inside the same window.
                    self.next_run = None
                    return
            except AuthRequired as exc:
                self._fail(exc, level="error")
                return
            except Exception as exc:
                self._fail(exc)
                if self._stop.is_set():
                    break
                if not self._wait_until(time.time() + 20):
                    break

    def _next_target(self, config: Config, now: datetime) -> datetime:
        hour, minute, second = (int(x) for x in config.schedule.time.split(":"))
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            if day.isoweekday() not in config.schedule.weekdays:
                continue
            candidate = datetime.combine(day, datetime.min.time()).replace(
                hour=hour, minute=minute, second=second
            )
            # Late start grace: if we are inside today's race window, go now.
            if candidate - timedelta(seconds=config.schedule.advance_seconds) <= now <= candidate + timedelta(
                seconds=config.retry.max_seconds
            ):
                return max(candidate, now)
            if candidate > now:
                return candidate
        return now + timedelta(days=1)

    def _wait_until(self, when: datetime | float) -> bool:
        if isinstance(when, datetime):
            when = when.timestamp()
        while not self._stop.is_set():
            remaining = when - time.time()
            if remaining <= 0:
                return True
            self._stop.wait(min(remaining, 0.5))
        return False

    # ------------------------------------------------------------------ attempt
    def _attempt(self, prepared: Prepared, config: Config, deadline: float) -> dict[str, Any]:
        attempts = 0
        last_message = ""
        interval = max(0.05, config.retry.interval_ms / 1000.0)
        allowed_venues = {str(v.get("CGBM")) for v in prepared.venues}
        court_filters = [c for c in config.courts if c.strip()]
        venue_order = {str(v.get("CGBM")): i for i, v in enumerate(prepared.venues)}
        announced: set[str] = set()
        tried: set[tuple[str, str]] = set()
        reported: set[str] = set()

        # The list order is the user's own priority ranking: never re-sort it.
        needed = max(1, min(config.needed_count, len(prepared.slots)))
        pending: list[str] = list(prepared.slots)
        bookings: list[dict[str, str]] = []
        skipped: dict[str, str] = {}
        halted = ""

        while time.time() < deadline and not self._stop.is_set():
            if len(bookings) >= needed or not pending:
                break
            attempts += 1
            # One times request per round: 3 slots should not mean 3x the
            # request rate at 12:30.
            try:
                times = prepared.client.times(
                    str(prepared.target.campus["XQDM"]),
                    prepared.date,
                    str(prepared.target.sport["XMDM"]),
                )
            except AuthRequired:
                raise
            except DateNotOpen as exc:
                last_message = str(exc)
                self.phase = "等待开放"
                if last_message not in reported:
                    reported.add(last_message)
                    self.log(f"时间段尚未开放：{last_message}")
                time.sleep(interval)
                continue
            except Exception as exc:
                last_message = f"查询时间段失败：{exc}"
                self.log(last_message)
                time.sleep(interval)
                continue
            by_code = {str(t.get("CODE")): t for t in times}

            # Give every unfinished slot its turn each round. Starving the
            # second slot behind a stuck first one is the only real risk here.
            more_slots_waiting = len(pending) > 1

            for slot in list(pending):
                if self._stop.is_set() or time.time() >= deadline or halted:
                    break
                if len(bookings) >= needed:
                    break
                item = by_code.get(slot)
                if item is None:
                    last_message = f"{slot} 不在可选时间段中"
                    continue
                if item.get("disabled"):
                    last_message = f"{slot} {item.get('text') or '暂不可预约'}"
                    self.phase = f"等待 {slot}"
                    continue

                if slot not in announced:
                    announced.add(slot)
                    self.log(f"{slot} 已开放，查询场地……")
                    self.phase = f"尝试 {slot}"
                try:
                    rooms = prepared.client.rooms(
                        campus_code=str(prepared.target.campus["XQDM"]),
                        date=prepared.date,
                        sport_code=str(prepared.target.sport["XMDM"]),
                        slot=slot,
                    )
                except AuthRequired:
                    raise
                except DateNotOpen as exc:
                    last_message = f"{slot} 尚未开放（{exc}）"
                    self.phase = f"等待 {slot} 开放"
                    if last_message not in reported:
                        reported.add(last_message)
                        self.log(last_message)
                    continue
                except Exception as exc:
                    last_message = f"查询场地失败：{exc}"
                    if last_message not in reported:
                        reported.add(last_message)
                        self.log(last_message)
                    continue

                available = [
                    r
                    for r in rooms
                    if not r.get("disabled") and str(r.get("CGBM")) in allowed_venues
                ]
                if court_filters:
                    preferred = []
                    for wanted in court_filters:
                        preferred.extend(r for r in available if wanted in room_label(r))
                    seen_wid = set()
                    filtered = []
                    for room in preferred:
                        if room.get("WID") not in seen_wid:
                            seen_wid.add(room.get("WID"))
                            filtered.append(room)
                    if config.strict_courts:
                        if not filtered:
                            last_message = f"{slot} 严格模式下没有指定场地的空位"
                            self.phase = f"等待 {slot} 指定场地"
                            continue
                        available = filtered
                    else:
                        available = filtered or available
                if not available:
                    last_message = f"{slot} 暂无可选场地"
                    self.phase = f"等待 {slot} 空位"
                    continue

                available.sort(key=lambda r: venue_order.get(str(r.get("CGBM")), 99))

                for room in available:
                    if self._stop.is_set() or time.time() >= deadline:
                        break
                    key = (slot, str(room.get("WID")))
                    if key in tried:
                        continue
                    label = room_label(room)
                    if self.dry_run:
                        text = f"演练：找到空场 {prepared.date} {slot} / {label}，未提交"
                        self.log(text)
                        bookings.append({"slot": slot, "court": label})
                        pending.remove(slot)
                        last_message = text
                        break
                    self.log(f"尝试提交：{slot} / {label}……")
                    try:
                        response = prepared.client.submit(
                            identity=prepared.identity,
                            room=room,
                            date=prepared.date,
                            slot=slot,
                        )
                    except AuthRequired:
                        raise
                    except Exception as exc:
                        # A timeout after POST is ambiguous: stop rather than
                        # risk creating a second booking.
                        settled = "、".join(f"{b['slot']} / {b['court']}" for b in bookings)
                        last_message = f"提交结果未知：{exc}。"
                        if settled:
                            last_message += f"已成交 {settled}，另一单请到“我的预约”核对"
                        else:
                            last_message += "请到“我的预约”确认"
                        self.log(last_message)
                        return self._result(
                            prepared,
                            bookings,
                            needed,
                            pending,
                            attempts,
                            last_message,
                            uncertain=True,
                            slot=slot,
                            court=label,
                        )
                    code = str(response.get("code"))
                    message = str(response.get("msg") or "")
                    if code == "0":
                        text = f"预约已提交：{prepared.date} {slot} / {label}，请在45分钟内支付"
                        self.log(text)
                        bookings.append({"slot": slot, "court": label})
                        pending.remove(slot)
                        last_message = text
                        break
                    last_message = message or f"预约失败（code={code}）"
                    tried.add(key)
                    self.log(f"提交失败：{last_message}")
                    if _looks_like_quota(last_message):
                        # Account-level cap: no other slot will pass either.
                        halted = last_message
                        break
                    if any(k in last_message for k in ("已预约", "重复预约")):
                        if bookings:
                            # The server refused a second booking for this user.
                            halted = last_message
                            break
                        skipped[slot] = last_message
                        pending.remove(slot)
                        break
                    if more_slots_waiting:
                        # Hand the rest of this round to the other slots.
                        break
            if halted:
                break
            if len(bookings) >= needed or not pending:
                break
            if self.dry_run:
                break
            if self._stop.is_set():
                break
            time.sleep(interval)

        return self._result(
            prepared,
            bookings,
            needed,
            pending,
            attempts,
            last_message,
            duplicate=bool(skipped) and not bookings,
            exhausted=halted,
        )

    def _result(
        self,
        prepared: Prepared,
        bookings: list[dict[str, str]],
        needed: int,
        pending: list[str],
        attempts: int,
        message: str,
        *,
        uncertain: bool = False,
        duplicate: bool = False,
        exhausted: str = "",
        slot: str = "",
        court: str = "",
    ) -> dict[str, Any]:
        enough = len(bookings) >= needed
        first = bookings[0] if bookings else {}
        if not message:
            message = "到达时限，未抢到场地"
        return {
            "success": enough and not uncertain,
            "partial": bool(bookings) and not enough,
            "uncertain": uncertain,
            "duplicate": duplicate,
            "exhausted": exhausted,
            "dry_run": self.dry_run,
            "date": prepared.date,
            "needed": needed,
            "bookings": list(bookings),
            "missed": [] if enough else list(pending),
            "slot": slot or first.get("slot", ""),
            "court": court or first.get("court", ""),
            "attempts": attempts,
            "message": message,
        }

    # -------------------------------------------------------------------- final
    def _finish(self, result: dict[str, Any]) -> None:
        if result.get("success"):
            self.phase = "done"
        elif result.get("uncertain"):
            # An unknown submission outranks a partial win: it needs checking.
            self.phase = "failed"
        elif result.get("partial"):
            self.phase = "partial"
        else:
            self.phase = "failed"
        self.last_result = result
        if result.get("dry_run"):
            self.log("演练结束：未提交任何预约。")
            return
        if result.get("success"):
            notify("深大场馆预约已提交", result.get("message", ""), self.log)
        elif result.get("uncertain"):
            notify("深大场馆预约结果未知", result.get("message", ""), self.log)
        elif result.get("partial"):
            notify("深大场馆预约部分成功", result.get("message", ""), self.log)
        else:
            self.log(f"本次结束：{result.get('message')}")

    def _fail(self, exc: Exception, level: str = "error") -> None:
        self.phase = "error"
        message = f"任务失败：{exc}"
        self.log(message)
        self.last_result = {"success": False, "message": message}
        notify("深大场馆预约任务失败", message, self.log)
