import json
import tempfile
import unittest
from pathlib import Path

from szu_booking.api import (
    ApiError,
    AuthRequired,
    DateNotOpen,
    Identity,
    SzuClient,
    room_label,
)


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


class FakeResponse:
    def __init__(self, payload=None, *, text="", url="https://ehall.szu.edu.cn/x.do", bad_json=False):
        self._payload = payload
        self.text = text
        self.url = url
        self.headers = {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.calls = []

    def request(self, method, url, data=None, timeout=None, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "data": data})
        return self.response


def client_with(response):
    session = FakeSession(response)
    return SzuClient(cookie_path=Path("does-not-exist.json"), session=session), session


ROOM_ARGS = {
    "campus_code": "1",
    "date": "2026-10-22",
    "sport_code": "001",
    "slot": "19:00-20:00",
}


class ResponseGuardTests(unittest.TestCase):
    def test_login_redirect_becomes_auth_required(self):
        client, _ = client_with(
            FakeResponse(url="https://authserver.szu.edu.cn/authserver/login")
        )
        with self.assertRaises(AuthRequired):
            client._request("GET", "sportVenue/getSportVenueData.do")

    def test_login_page_body_becomes_auth_required(self):
        client, _ = client_with(FakeResponse(text="<title>统一身份认证</title>"))
        with self.assertRaises(AuthRequired):
            client._request("GET", "x.do")

    def test_non_json_body_raises_api_error(self):
        client, _ = client_with(FakeResponse(text="<html>服务异常</html>", bad_json=True))
        with self.assertRaisesRegex(ApiError, "不是 JSON"):
            client._json("GET", "x.do")

    def test_date_header_sets_clock_offset(self):
        client, _ = client_with(FakeResponse())
        client.session.response.headers["Date"] = "Wed, 21 Oct 2026 04:30:00 GMT"
        client._request("GET", "x.do")
        self.assertIsNotNone(client._time_offset)


class IdentityTests(unittest.TestCase):
    HTML = (
        "<script>var USERID='2024000000';"
        'var USER_INFO={"userName":"张三","deptName":"计算机学院"};</script>'
    )

    def test_parses_user_id_and_profile(self):
        client, _ = client_with(FakeResponse(text=self.HTML))
        identity = client.identity()
        self.assertEqual(identity.user_id, "2024000000")
        self.assertEqual(identity.name, "张三")
        self.assertEqual(identity.dept, "计算机学院")

    def test_returns_none_without_user_id(self):
        client, _ = client_with(FakeResponse(text="<html>nothing here</html>"))
        self.assertIsNone(client.identity())

    def test_returns_none_on_login_redirect(self):
        client, _ = client_with(FakeResponse(url="https://authserver.szu.edu.cn/login"))
        self.assertIsNone(client.identity())

    def test_check_session_raises_when_logged_out(self):
        client, _ = client_with(FakeResponse(text="<html></html>"))
        with self.assertRaises(AuthRequired):
            client.check_session()


class TimesTests(unittest.TestCase):
    def test_list_payload_is_returned_as_is(self):
        client, _ = client_with(FakeResponse(payload=[{"CODE": "19:00-20:00"}]))
        rows = client.times("1", "2026-10-22", "001")
        self.assertEqual(rows[0]["CODE"], "19:00-20:00")

    def test_not_open_message_raises_date_not_open(self):
        client, _ = client_with(FakeResponse(payload={"code": "1", "msg": "该日期暂未开放"}))
        with self.assertRaises(DateNotOpen):
            client.times("1", "2026-10-22", "001")

    def test_other_message_raises_api_error(self):
        client, _ = client_with(FakeResponse(payload={"code": "1", "msg": "系统繁忙"}))
        with self.assertRaisesRegex(ApiError, "系统繁忙"):
            client.times("1", "2026-10-22", "001")


class RoomsTests(unittest.TestCase):
    def test_rows_are_returned(self):
        payload = {"datas": {"getOpeningRoom": {"rows": [{"WID": "1"}]}}}
        client, _ = client_with(FakeResponse(payload=payload))
        rows = client.rooms(**ROOM_ARGS)
        self.assertEqual(rows, [{"WID": "1"}])

    def test_not_open_message_raises_date_not_open(self):
        client, _ = client_with(FakeResponse(payload={"code": "1", "msg": "该日期暂未开放"}))
        with self.assertRaises(DateNotOpen):
            client.rooms(**ROOM_ARGS)

    def test_business_error_raises_api_error(self):
        client, _ = client_with(FakeResponse(payload={"code": "1", "msg": "系统繁忙"}))
        with self.assertRaisesRegex(ApiError, "系统繁忙"):
            client.rooms(**ROOM_ARGS)

    def test_missing_datas_raises_api_error(self):
        client, _ = client_with(FakeResponse(payload={"code": "0"}))
        with self.assertRaisesRegex(ApiError, "格式异常"):
            client.rooms(**ROOM_ARGS)

    def test_rows_of_wrong_type_raises_api_error(self):
        payload = {"datas": {"getOpeningRoom": {"rows": "oops"}}}
        client, _ = client_with(FakeResponse(payload=payload))
        with self.assertRaisesRegex(ApiError, "格式异常"):
            client.rooms(**ROOM_ARGS)

    def test_venue_filter_is_omitted_for_all(self):
        payload = {"datas": {"getOpeningRoom": {"rows": []}}}
        client, session = client_with(FakeResponse(payload=payload))
        client.rooms(**ROOM_ARGS, venue_code="all")
        self.assertNotIn("CGBM", session.calls[-1]["data"])

    def test_venue_filter_is_forwarded(self):
        payload = {"datas": {"getOpeningRoom": {"rows": []}}}
        client, session = client_with(FakeResponse(payload=payload))
        client.rooms(**ROOM_ARGS, venue_code="001")
        self.assertEqual(session.calls[-1]["data"]["CGBM"], "001")


class SubmitEdgeTests(unittest.TestCase):
    def test_non_dict_response_raises_api_error(self):
        client = SzuClient(cookie_path=Path("does-not-exist.json"))
        client._json = lambda *a, **k: ["unexpected"]  # type: ignore[assignment]
        with self.assertRaisesRegex(ApiError, "格式异常"):
            client.submit(
                identity=Identity("2024000000", "测试同学"),
                room={"CGBM": "001"},
                date="2026-10-22",
                slot="19:00-20:00",
            )


class FilterVenuesTests(unittest.TestCase):
    def setUp(self):
        self.client = SzuClient(cookie_path=Path("does-not-exist.json"))
        self.target = self.client.resolve_target(SAMPLE, "粤海校区", "羽毛球")

    def test_overlapping_keywords_are_deduplicated(self):
        # 精确全名命中 001，"东馆" 又命中 001+002：001 只能出现一次。
        venues = self.client.filter_venues(
            self.target, ["运动广场东馆羽毛球场", "东馆"]
        )
        self.assertEqual([v["CGBM"] for v in venues], ["001", "002"])

    def test_repeated_keyword_is_idempotent(self):
        venues = self.client.filter_venues(self.target, ["东馆", "东馆"])
        self.assertEqual([v["CGBM"] for v in venues], ["001", "002"])

    def test_no_match_returns_empty_list(self):
        self.assertEqual(self.client.filter_venues(self.target, ["不存在"]), [])


class CookieLoadingTests(unittest.TestCase):
    def test_entries_without_name_are_skipped(self):
        cookies = [
            {"name": "JSESSIONID", "value": "abc", "domain": "ehall.szu.edu.cn", "path": "/"},
            {"value": "orphan"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            path.write_text(json.dumps(cookies), encoding="utf-8")
            client = SzuClient(cookie_path=path)
        self.assertEqual(len(client.session.cookies), 1)


if __name__ == "__main__":
    unittest.main()
