"""Runner._prepare: every guard that can abort a run before the race starts."""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from szu_booking.api import ApiError, AuthRequired, Identity, SzuClient
from szu_booking.config import Config
from szu_booking.runner import Runner


VENUE_DATA = {
    "campusList": [{"XQDM": "1", "XQDM_DISPLAY": "粤海校区"}],
    "xmList": [{"XQDM": "1", "XMDM": "001", "XMMC": "羽毛球", "DCFS": "1.0"}],
    "packageVenueList": [
        {"SSXQ": "1", "XM": "001", "CGBM": "001", "CGMC": "运动广场东馆羽毛球场"},
    ],
}

FAKE_COOKIE = Path("does-not-exist.json")


def fake_client(*, now, dates, venue_data=VENUE_DATA, auth_error=None):
    """A SzuClient without a network session: only the pure logic stays real."""
    client = SzuClient.__new__(SzuClient)  # skip __init__ so no requests.Session
    client.cookie_path = None
    client.timeout = 15
    client._time_offset = 0.0
    client.server_now = lambda: now
    client.dates = lambda: list(dates)
    client.venue_data = lambda: venue_data
    if auth_error is not None:

        def check_session():
            raise auth_error

        client.check_session = check_session
    else:
        client.check_session = lambda: Identity("2024000000", "测试同学")
    return client


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.runner = Runner(self.logs.append)

    def prepare(self, client, config):
        with patch("szu_booking.runner.SzuClient", return_value=client):
            return self.runner._prepare(config)

    def test_expired_session_aborts(self):
        client = fake_client(
            now=datetime(2026, 10, 21, 12, 0),
            dates=["2026-10-22"],
            auth_error=AuthRequired("未登录或登录已过期"),
        )
        with self.assertRaises(AuthRequired):
            self.prepare(client, Config())

    def test_past_date_is_rejected(self):
        client = fake_client(now=datetime(2026, 10, 21, 12, 0), dates=["2026-10-22"])
        config = Config(date_mode="custom", target_date="2020-01-01")
        with self.assertRaisesRegex(ApiError, "已经过去"):
            self.prepare(client, config)

    def test_all_slots_expired_today(self):
        today = datetime.now().date()
        client = fake_client(
            now=datetime.combine(today, datetime.min.time()).replace(hour=22),
            dates=[today.isoformat()],
        )
        config = Config(date_mode="today", time_slots=["19:00-20:00"])
        with self.assertRaisesRegex(ApiError, "均已过期"):
            self.prepare(client, config)

    def test_expired_slots_are_dropped_but_run_continues(self):
        today = datetime.now().date()
        client = fake_client(
            now=datetime.combine(today, datetime.min.time()).replace(hour=19, minute=30),
            dates=[today.isoformat()],
        )
        config = Config(
            date_mode="today",
            time_slots=["18:00-19:00", "20:00-21:00"],
        )
        prepared = self.prepare(client, config)
        self.assertEqual(prepared.slots, ["20:00-21:00"])
        self.assertTrue(any("已跳过" in line for line in self.logs))

    def test_unopened_date_strict_mode_raises(self):
        client = fake_client(now=datetime(2026, 10, 21, 12, 0), dates=["2026-10-21"])
        config = Config(date_mode="custom", target_date="2026-11-05")
        with self.assertRaisesRegex(ApiError, "尚未开放"):
            self.prepare(client, config)

    def test_unopened_date_lenient_mode_keeps_going(self):
        client = fake_client(now=datetime(2026, 10, 21, 12, 0), dates=["2026-10-21"])
        config = Config(date_mode="custom", target_date="2026-11-05")
        with patch("szu_booking.runner.SzuClient", return_value=client):
            prepared = self.runner._prepare(config, strict_date=False)
        self.assertEqual(prepared.date, "2026-11-05")
        self.assertTrue(any("继续重试" in line for line in self.logs))

    def test_empty_venue_filter_is_rejected(self):
        client = fake_client(now=datetime(2026, 10, 21, 12, 0), dates=["2026-10-22"])
        config = Config(venues=["不存在的场地"])
        with self.assertRaisesRegex(ApiError, "找不到场馆"):
            self.prepare(client, config)

    def test_successful_prepare_reports_target(self):
        client = fake_client(
            now=datetime(2026, 10, 21, 12, 0), dates=["2026-10-21", "2026-10-22"]
        )
        prepared = self.prepare(client, Config(date_mode="tomorrow"))
        self.assertEqual(prepared.date, "2026-10-22")
        self.assertEqual(prepared.slots, Config().time_slots)
        self.assertEqual([v["CGBM"] for v in prepared.venues], ["001"])
        self.assertTrue(any("目标：粤海校区" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
