import json
import unittest
from pathlib import Path

from szu_booking.config import Config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(Config().validate(), [])

    def test_bad_slot_is_rejected(self):
        config = Config(time_slots=["25:00-26:00", "19:00-18:00", "abc"])
        errors = config.validate()
        self.assertEqual(len(errors), 3)

    def test_custom_date_requires_iso_date(self):
        config = Config(date_mode="custom", target_date="2026/10/20")
        self.assertTrue(any("YYYY-MM-DD" in e for e in config.validate()))

    def test_strict_courts_requires_selection(self):
        config = Config(strict_courts=True, courts=[])
        self.assertTrue(any("严格场地" in e for e in config.validate()))
        self.assertFalse(Config(strict_courts=True, courts=["A2"]).validate())

    def test_save_load_roundtrip(self):
        path = Path("data") / "test-config-roundtrip.json"
        try:
            config = Config(campus="丽湖校区", sport="网球", time_slots=["19:00-20:00"])
            config.save(path)
            loaded = Config.load(path)
            self.assertEqual(loaded.campus, "丽湖校区")
            self.assertEqual(loaded.sport, "网球")
            self.assertEqual(loaded.time_slots, ["19:00-20:00"])
            self.assertEqual(loaded.schedule.time, "12:30:00")
        finally:
            path.unlink(missing_ok=True)

    def test_partial_dict_uses_defaults(self):
        config = Config.from_dict({"campus": "丽湖校区", "schedule": {"time": "12:25:00"}})
        self.assertEqual(config.campus, "丽湖校区")
        self.assertEqual(config.sport, "羽毛球")
        self.assertEqual(config.schedule.time, "12:25:00")
        self.assertEqual(config.schedule.advance_seconds, 60)

    def test_comma_separated_lists_are_normalized(self):
        config = Config.from_dict({"time_slots": "19:00-20:00，20:00-21:00", "venues": "A, B"})
        self.assertEqual(config.time_slots, ["19:00-20:00", "20:00-21:00"])
        self.assertEqual(config.venues, ["A", "B"])

    def test_asdict_is_json_serializable(self):
        json.dumps(Config().to_dict(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
