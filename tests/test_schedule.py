"""Date selection: which day the run targets, and when the scheduler fires."""

import unittest
from datetime import datetime

from szu_booking.config import Config, RetryConfig, ScheduleConfig, resolve_target_date
from szu_booking.runner import Runner


def make_config(*, weekdays=None, time_str="12:30:00", advance=60, max_seconds=90):
    config = Config()
    config.schedule = ScheduleConfig(
        enabled=True,
        time=time_str,
        advance_seconds=advance,
        weekdays=[1, 2, 3, 4, 5, 6, 7] if weekdays is None else weekdays,
    )
    config.retry = RetryConfig(max_seconds=max_seconds)
    return config


class ResolveTargetDateTests(unittest.TestCase):
    def test_tomorrow_crosses_month_and_year(self):
        config = Config(date_mode="tomorrow")
        self.assertEqual(
            resolve_target_date(config, datetime(2026, 10, 31, 23, 30)), "2026-11-01"
        )
        self.assertEqual(
            resolve_target_date(config, datetime(2026, 12, 31, 23, 30)), "2027-01-01"
        )

    def test_today_uses_current_date(self):
        config = Config(date_mode="today")
        self.assertEqual(resolve_target_date(config, datetime(2026, 10, 21, 0, 5)), "2026-10-21")
        self.assertEqual(resolve_target_date(config, datetime(2026, 10, 21, 23, 55)), "2026-10-21")

    def test_custom_returns_configured_date(self):
        config = Config(date_mode="custom", target_date="2026-11-05")
        self.assertEqual(resolve_target_date(config, datetime(2026, 10, 21, 12, 0)), "2026-11-05")


class NextTargetTests(unittest.TestCase):
    """Runner._next_target decides the exact instant the race starts."""

    def setUp(self):
        self.runner = Runner(lambda _: None)

    def target(self, now, **kwargs):
        return self.runner._next_target(make_config(**kwargs), now)

    def test_waits_for_today_when_slot_is_ahead(self):
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 12, 0)), datetime(2026, 10, 21, 12, 30)
        )

    def test_rolls_to_tomorrow_after_window_closed(self):
        # 12:30 + 90s grace has passed, so today is gone.
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 12, 31, 31)), datetime(2026, 10, 22, 12, 30)
        )
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 20, 0)), datetime(2026, 10, 22, 12, 30)
        )

    def test_inside_grace_window_fires_immediately(self):
        # 12:30:00 + 90s grace still allows a late start; target becomes "now".
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 12, 31)), datetime(2026, 10, 21, 12, 31)
        )

    def test_prewarm_window_still_waits_for_exact_instant(self):
        # 30s before 12:30 we are already inside the advance window, but the
        # target must stay 12:30:00 rather than collapsing to now.
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 12, 29, 30)), datetime(2026, 10, 21, 12, 30)
        )

    def test_weekday_filter_skips_the_weekend(self):
        # Friday evening: next weekday-only run is Monday.
        self.assertEqual(
            self.target(datetime(2026, 10, 23, 13, 0), weekdays=[1, 2, 3, 4, 5]),
            datetime(2026, 10, 26, 12, 30),
        )

    def test_weekend_only_schedule(self):
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 12, 0), weekdays=[6, 7]),
            datetime(2026, 10, 24, 12, 30),
        )

    def test_no_matching_weekday_falls_back_to_next_day(self):
        now = datetime(2026, 10, 21, 12, 0)
        self.assertEqual(self.target(now, weekdays=[]), datetime(2026, 10, 22, 12, 0))

    def test_custom_clock_time_is_respected(self):
        self.assertEqual(
            self.target(datetime(2026, 10, 21, 7, 0), time_str="07:30:00"),
            datetime(2026, 10, 21, 7, 30),
        )


if __name__ == "__main__":
    unittest.main()
