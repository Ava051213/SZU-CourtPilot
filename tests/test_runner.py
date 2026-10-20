import time
import unittest

from szu_booking.api import Identity, Target
from szu_booking.config import Config
from szu_booking.runner import Prepared, Runner


class FakeClient:
    def __init__(self, times, rooms, responses=None):
        self._times = times
        self._rooms = rooms
        self.responses = list(responses or [{"code": "0", "msg": "预约成功"}])
        self.submitted = []
        self.room_calls = 0

    def times(self, campus_code, date, sport_code):
        return self._times

    def rooms(self, **kwargs):
        self.room_calls += 1
        return self._rooms

    def submit(self, **kwargs):
        self.submitted.append(kwargs)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def make_prepared(client, slots=None):
    target = Target(
        campus={"XQDM": "1", "XQDM_DISPLAY": "粤海校区"},
        sport={"XMDM": "001", "XMMC": "羽毛球", "DCFS": "1.0"},
        venues=[
            {"CGBM": "001", "CGMC": "运动广场东馆羽毛球场"},
            {"CGBM": "002", "CGMC": "运动广场东馆羽毛球场（教工专场）"},
        ],
    )
    return Prepared(
        client=client,
        identity=Identity("2024000000", "测试同学"),
        target=target,
        date="2026-10-20",
        venues=target.venues,
        slots=slots or ["19:00-20:00"],
    )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.runner = Runner(self.logs.append)

    def test_success_and_court_preference(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [
            {"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False},
            {"WID": "2", "CDMC": "羽毛球A3", "CGBM": "001", "disabled": False},
        ]
        client = FakeClient(times, rooms)
        config = Config(courts=["A3"], retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 2)
        self.assertTrue(result["success"])
        self.assertEqual(client.submitted[0]["room"]["CDMC"], "羽毛球A3")

    def test_full_slot_times_out(self):
        times = [{"CODE": "19:00-20:00", "disabled": True, "text": "已满员"}]
        client = FakeClient(times, [])
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 0.3)
        self.assertFalse(result["success"])
        self.assertIn("已满员", result["message"])

    def test_business_failure_then_success(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [
            {"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False},
            {"WID": "2", "CDMC": "羽毛球A3", "CGBM": "001", "disabled": False},
        ]
        client = FakeClient(
            times,
            rooms,
            responses=[
                {"code": "1", "msg": "该场地已被预约"},
                {"code": "0", "msg": "预约成功"},
            ],
        )
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 2)
        self.assertTrue(result["success"])
        self.assertEqual(len(client.submitted), 2)

    def test_unknown_venue_is_filtered(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [{"WID": "1", "CDMC": "其他场地", "CGBM": "999", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 0.3)
        self.assertFalse(result["success"])
        self.assertEqual(client.submitted, [])

    def test_strict_courts_does_not_fallback(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(
            courts=["D1"],
            strict_courts=True,
            retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})(),
        )
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 0.3)
        self.assertFalse(result["success"])
        self.assertIn("严格", result["message"])
        self.assertEqual(client.submitted, [])

    def test_non_strict_courts_falls_back_to_any_court(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(
            courts=["D1"],
            strict_courts=False,
            retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})(),
        )
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertTrue(result["success"])
        self.assertEqual(client.submitted[0]["room"]["CDMC"], "羽毛球A2")

    def test_dry_run_never_submits(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client)
        self.runner.dry_run = True
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(client.submitted, [])


if __name__ == "__main__":
    unittest.main()
