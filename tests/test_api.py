import unittest
from pathlib import Path

from szu_booking.api import SzuClient, room_label


SAMPLE = {
    "campusList": [
        {"XQDM": "1", "XQDM_DISPLAY": "粤海校区"},
        {"XQDM": "2", "XQDM_DISPLAY": "丽湖校区"},
    ],
    "xmList": [
        {"XQDM": "1", "XMDM": "001", "XMMC": "羽毛球", "DCFS": "1.0"},
        {"XQDM": "2", "XMDM": "001", "XMMC": "羽毛球", "DCFS": "1.0"},
        {"XQDM": "1", "XMDM": "002", "XMMC": "足球", "DCFS": "2.0"},
    ],
    "packageVenueList": [
        {"SSXQ": "1", "XM": "001", "CGBM": "001", "CGMC": "运动广场东馆羽毛球场"},
        {"SSXQ": "1", "XM": "001", "CGBM": "002", "CGMC": "运动广场东馆羽毛球场（教工专场）"},
        {"SSXQ": "2", "XM": "001,021", "CGBM": "104", "CGMC": "至畅体育馆"},
        {"SSXQ": "1", "XM": "002", "CGBM": "009", "CGMC": "足球场"},
    ],
}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.client = SzuClient(cookie_path=Path("does-not-exist.json"))

    def test_resolve_badminton(self):
        target = self.client.resolve_target(SAMPLE, "粤海校区", "羽毛球")
        self.assertEqual(target.campus["XQDM"], "1")
        self.assertEqual(target.sport["XMDM"], "001")
        self.assertEqual([v["CGBM"] for v in target.venues], ["001", "002"])

    def test_resolve_does_not_mix_campuses(self):
        target = self.client.resolve_target(SAMPLE, "丽湖校区", "羽毛球")
        self.assertEqual([v["CGBM"] for v in target.venues], ["104"])

    def test_unknown_campus_and_sport(self):
        with self.assertRaisesRegex(Exception, "找不到校区"):
            self.client.resolve_target(SAMPLE, "不存在校区", "羽毛球")
        with self.assertRaisesRegex(Exception, "找不到项目"):
            self.client.resolve_target(SAMPLE, "粤海校区", "不存在项目")

    def test_unsupported_booking_type(self):
        with self.assertRaisesRegex(Exception, "不支持包场"):
            self.client.resolve_target(SAMPLE, "粤海校区", "足球")

    def test_filter_venues_exact_and_partial(self):
        target = self.client.resolve_target(SAMPLE, "粤海校区", "羽毛球")
        exact = self.client.filter_venues(target, ["运动广场东馆羽毛球场"])
        self.assertEqual([v["CGBM"] for v in exact], ["001"])
        partial = self.client.filter_venues(target, ["教工"])
        self.assertEqual([v["CGBM"] for v in partial], ["002"])
        all_venues = self.client.filter_venues(target, [])
        self.assertEqual(len(all_venues), 2)

    def test_room_label(self):
        self.assertEqual(room_label({"CDMC": "羽毛球A2"}), "羽毛球A2")


class SubmitPayloadTests(unittest.TestCase):
    def test_submit_payload_shape(self):
        client = SzuClient(cookie_path=Path("does-not-exist.json"))
        captured = {}

        def fake_json(method, path, data=None):
            captured.update({"method": method, "path": path, "data": data})
            return {"code": "0"}

        client._json = fake_json  # type: ignore[assignment]
        from szu_booking.api import Identity

        response = client.submit(
            identity=Identity("2024000000", "测试同学"),
            room={"CGBM": "001", "WID": "abc", "XMDM": "001", "XQDM": "1", "DCFS": "1.0"},
            date="2026-10-20",
            slot="19:00-20:00",
        )
        self.assertEqual(response["code"], "0")
        payload = captured["data"]
        self.assertEqual(captured["path"], "sportVenue/insertVenueBookingInfo.do")
        self.assertEqual(payload["CDWID"], "abc")
        self.assertEqual(payload["YYKS"], "2026-10-20 19:00")
        self.assertEqual(payload["YYJS"], "2026-10-20 20:00")
        self.assertEqual(payload["YYRGH"], "2024000000")


if __name__ == "__main__":
    unittest.main()
