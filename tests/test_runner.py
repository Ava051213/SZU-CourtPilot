import threading
import time
import unittest
from unittest.mock import patch

from szu_booking.api import DateNotOpen, Identity, Target
from szu_booking.config import Config
from szu_booking.runner import Prepared, Runner, _looks_like_quota


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

    def test_slot_priority_falls_through_to_next_slot(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": True, "text": "已满员"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(
            time_slots=["19:00-20:00", "20:00-21:00"],
            retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})(),
        )
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertTrue(result["success"])
        self.assertEqual(result["slot"], "20:00-21:00")
        self.assertEqual(client.submitted[0]["slot"], "20:00-21:00")

    def test_duplicate_booking_stops_immediately(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [
            {"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False},
            {"WID": "2", "CDMC": "羽毛球A3", "CGBM": "001", "disabled": False},
        ]
        client = FakeClient(
            times, rooms, responses=[{"code": "1", "msg": "该场地您已预约，请勿重复预约"}]
        )
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertFalse(result["success"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(len(client.submitted), 1)

    def test_submit_exception_is_reported_as_uncertain(self):
        times = [{"CODE": "19:00-20:00", "disabled": False, "text": "可预约"}]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)

        def boom(**kwargs):
            raise TimeoutError("read timed out")

        client.submit = boom
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])

    def test_date_not_open_is_retried_until_deadline(self):
        client = FakeClient([], [])

        def not_open(*args, **kwargs):
            raise DateNotOpen("该日期暂未开放")

        client.times = not_open
        config = Config(retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client)
        result = self.runner._attempt(prepared, config, time.time() + 0.3)
        self.assertFalse(result["success"])
        self.assertIn("暂未开放", result["message"])
        self.assertEqual(client.submitted, [])

    def test_start_rejects_invalid_config(self):
        with self.assertRaises(ValueError):
            self.runner.start_once(Config(time_slots=[]))

    def test_multiple_slots_collects_requested_count(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": False, "text": "可预约"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(needed_count=2, retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 2)
        self.assertTrue(result["success"])
        self.assertFalse(result["partial"])
        self.assertEqual(
            [b["slot"] for b in result["bookings"]], ["19:00-20:00", "20:00-21:00"]
        )
        self.assertEqual(len(client.submitted), 2)

    def test_needed_count_one_stops_after_first_booking(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": False, "text": "可预约"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(needed_count=1, retry=type("R", (), {"interval_ms": 50, "max_seconds": 2})())
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertTrue(result["success"])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(result["slot"], "19:00-20:00")

    def test_partial_result_when_one_slot_stays_full(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": True, "text": "已满员"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(times, rooms)
        config = Config(needed_count=2, retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 0.3)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["missed"], ["19:00-20:00"])
        self.assertEqual(len(result["bookings"]), 1)

    def test_quota_error_halts_the_whole_run(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": False, "text": "可预约"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [{"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False}]
        client = FakeClient(
            times, rooms, responses=[{"code": "1", "msg": "超出当天预约次数上限"}]
        )
        config = Config(needed_count=2, retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 1)
        self.assertFalse(result["success"])
        self.assertIn("次数", result["exhausted"])
        self.assertEqual(len(client.submitted), 1)

    def test_slots_rotate_instead_of_starving_the_second(self):
        times = [
            {"CODE": "19:00-20:00", "disabled": False, "text": "可预约"},
            {"CODE": "20:00-21:00", "disabled": False, "text": "可预约"},
        ]
        rooms = [
            {"WID": "1", "CDMC": "羽毛球A2", "CGBM": "001", "disabled": False},
            {"WID": "2", "CDMC": "羽毛球A3", "CGBM": "001", "disabled": False},
        ]

        class Routing(FakeClient):
            def submit(self, **kwargs):
                self.submitted.append(kwargs)
                if kwargs["slot"] == "19:00-20:00":
                    return {"code": "1", "msg": "该场地已被预约"}
                return {"code": "0", "msg": "预约成功"}

        client = Routing(times, rooms)
        config = Config(needed_count=2, retry=type("R", (), {"interval_ms": 50, "max_seconds": 1})())
        prepared = make_prepared(client, ["19:00-20:00", "20:00-21:00"])
        result = self.runner._attempt(prepared, config, time.time() + 0.4)
        self.assertEqual(client.submitted[0]["slot"], "19:00-20:00")
        self.assertEqual(client.submitted[1]["slot"], "20:00-21:00")
        self.assertTrue(result["partial"])

    def test_start_rejects_when_already_running(self):
        gate = threading.Event()
        self.runner._thread = threading.Thread(target=gate.wait, daemon=True)
        self.runner._thread.start()
        try:
            with self.assertRaises(RuntimeError):
                self.runner.start_once(Config())
        finally:
            gate.set()
            self.runner.join(2)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.runner = Runner(lambda _: None)

    @patch("szu_booking.runner.notify")
    def test_success_triggers_notification(self, fake_notify):
        self.runner._finish({"success": True, "message": "预约已提交"})
        fake_notify.assert_called_once()
        self.assertEqual(self.runner.phase, "done")

    @patch("szu_booking.runner.notify")
    def test_dry_run_stays_silent(self, fake_notify):
        self.runner._finish({"success": True, "dry_run": True, "message": "演练"})
        fake_notify.assert_not_called()

    @patch("szu_booking.runner.notify")
    def test_uncertain_result_notifies_and_marks_failed(self, fake_notify):
        self.runner._finish({"success": False, "uncertain": True, "message": "结果未知"})
        fake_notify.assert_called_once()
        self.assertEqual(self.runner.phase, "failed")

    @patch("szu_booking.runner.notify")
    def test_plain_failure_only_logs(self, fake_notify):
        self.runner._finish({"success": False, "message": "未抢到场地"})
        fake_notify.assert_not_called()

    @patch("szu_booking.runner.notify")
    def test_partial_result_notifies_and_marks_phase(self, fake_notify):
        self.runner._finish({"success": False, "partial": True, "message": "部分成功"})
        fake_notify.assert_called_once()
        self.assertEqual(self.runner.phase, "partial")

    @patch("szu_booking.runner.notify")
    def test_fail_sets_error_phase(self, fake_notify):
        self.runner._fail(RuntimeError("boom"))
        self.assertEqual(self.runner.phase, "error")
        self.assertFalse(self.runner.last_result["success"])


class QuotaDetectionTests(unittest.TestCase):
    def test_quota_wording_is_detected(self):
        self.assertTrue(_looks_like_quota("超出当天预约次数上限"))
        self.assertTrue(_looks_like_quota("超过每日最多预约次数"))

    def test_ordinary_failures_are_not_quota(self):
        self.assertFalse(_looks_like_quota("该场地已被预约"))
        self.assertFalse(_looks_like_quota("19:00-20:00 已满员"))
        self.assertFalse(_looks_like_quota("该场地您已预约，请勿重复预约"))


if __name__ == "__main__":
    unittest.main()
