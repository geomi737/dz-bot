import contextlib
import io
import json
import os
import ssl
import unittest
from email.message import Message
from datetime import date, datetime
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

from src.dnevnik import fetcher
from src.dnevnik.client import DnevnikClient, DnevnikError

TOKEN = "DO_NOT_PRINT_TEST_TOKEN"


class TransportFixture:
    def setUp(self):
        self.requests = []
        self.responses = []
        self.addCleanup(patch.stopall)
        # Никакой реальной сети: обработку ответов и редиректов делает urllib.
        patch("socket.create_connection", side_effect=AssertionError("Network forbidden")).start()
        patch.object(HTTPSHandler, "https_open", autospec=True, side_effect=self.open_https).start()
        self.client = DnevnikClient(TOKEN)

    def open_https(self, handler, request):
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.requests.append(request)
        spec = self.responses.pop(0)
        if isinstance(spec, Exception):
            raise spec
        headers = Message()
        headers["Content-Type"] = "application/json"
        if "location" in spec:
            headers["Location"] = spec["location"]
        body = spec.get("body", json.dumps(spec.get("json", {})).encode())
        response = addinfourl(io.BytesIO(body), headers, request.full_url, spec.get("status", 200))
        response.msg = "Test response"
        return response


class TransportTests(TransportFixture, unittest.TestCase):
    def test_only_https_get_and_header_token_with_encoded_dates(self):
        self.responses = [{"json": []}]
        with patch.dict(os.environ, {"https_proxy": "http://untrusted.invalid:8080"}):
            client = DnevnikClient(TOKEN)
            self.assertEqual(client.get_person_homework(10, 20, "2026-10-30T00:00:00", "2026-10-30T23:59:59"), [])
        request = self.requests[0]
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.dnevnik.ru", "/v2/persons/20/school/10/homeworks"))
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn(TOKEN, request.full_url)
        self.assertEqual(request.get_header("Access-token"), TOKEN)
        self.assertNotIn("Access-token", request.headers)
        self.assertEqual(parse_qs(url.query)["startDate"], ["2026-10-30T00:00:00"])

    def test_redirects_never_send_a_second_request(self):
        for code in (301, 302, 303, 307, 308):
            for target in ("https://untrusted.invalid/", "http://untrusted.invalid/", "https://api.dnevnik.ru/new", "file:///tmp/data"):
                with self.subTest(code=code, target=target):
                    self.requests.clear()
                    self.responses = [{"status": code, "location": target}]
                    with self.assertRaises(DnevnikError):
                        self.client.get_profile()
                    self.assertEqual(len(self.requests), 1)

    def test_http_and_network_errors_do_not_expose_secrets(self):
        for failure in (
            {"status": 401, "body": TOKEN.encode()},
            {"status": 403, "body": TOKEN.encode()},
            {"status": 500, "body": TOKEN.encode()},
            URLError(TOKEN),
            TimeoutError(TOKEN),
            ssl.SSLCertVerificationError(TOKEN),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.responses = [failure]
                with self.assertRaises(DnevnikError) as caught:
                    self.client.get_profile()
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_and_error_responses_are_not_homework(self):
        for response in (
            {"body": b"<html>not JSON</html>"},
            {"body": b"\xff"},
            {"json": None},
            {"json": {"type": "parameterInvalid", "description": TOKEN}},
            {"json": {"error": TOKEN}},
            {"json": []},  # Профиль пользователя должен быть объектом.
        ):
            with self.subTest(response=response):
                self.responses = [response]
                with self.assertRaises(DnevnikError) as caught:
                    self.client.get_profile()
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_response_size_is_limited(self):
        self.responses = [{"body": b" " * 100}]
        with patch("src.dnevnik.client.MAX_RESPONSE_BYTES", 32):
            with self.assertRaisesRegex(DnevnikError, "размер"):
                self.client.get_profile()

    def test_ids_cannot_inject_a_path_and_invalid_tokens_are_rejected(self):
        with self.assertRaises(DnevnikError):
            self.client.get_person_homework("//untrusted.invalid/", 20, "start", "end")
        self.assertEqual(self.requests, [])
        for token in ("", "abc\r\nInjected: header", "abc def"):
            with self.subTest(token=repr(token)):
                with self.assertRaises(DnevnikError):
                    DnevnikClient(token)


class FetcherTests(TransportFixture, unittest.TestCase):
    def run_main(self, env, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(fetcher, "load_env"), patch("sys.argv", ["fetcher.py", *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fetcher.main()
        return code, out.getvalue(), err.getvalue()

    def test_profile_schools_and_homework_flow(self):
        self.responses = [
            {"json": {"id": 999, "personId": 20}},
            {"json": [{"id": 10}]},
            {"json": {"homeworks": [{"subject": {"name": "Алгебра"}, "text": "№ 1"}]}},
        ]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Алгебра — № 1", out)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(
            [urlsplit(r.full_url).path for r in self.requests],
            ["/v2/users/me", "/v2/users/me/schools", "/v2/persons/20/school/10/homeworks"],
        )

    def test_real_api_works_are_joined_with_subjects(self):
        self.responses = [{"json": {
            "works": [
                {"subjectId": 101, "text": "Параграф 9, подготовить пересказ"},
                {"subjectId": 102, "text": ""},
            ],
            "subjects": [
                {"id_str": "101", "name": "Информатика"},
                {"id": 102, "name": "Алгебра"},
            ],
        }}]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_SCHOOL_ID": "10", "DNEVNIK_PERSON_ID": "20"})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Информатика — Параграф 9, подготовить пересказ", out)
        self.assertIn("Алгебра — не задано", out)

    def test_partial_id_override_and_numeric_school_list(self):
        self.responses = [
            {"json": [10]},
            {"json": {"works": [{"subjectId": 101, "text": "№ 1"}], "subjects": [{"id": 101, "name": "Алгебра"}]}},
        ]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_PERSON_ID": "20"})
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Алгебра — № 1", out)
        self.assertEqual(urlsplit(self.requests[0].full_url).path, "/v2/users/me/schools")

    def test_missing_subject_is_an_error_instead_of_no_homework(self):
        self.responses = [{"json": {"works": [{"subjectId": 101, "text": "№ 1"}], "subjects": []}}]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_SCHOOL_ID": "10", "DNEVNIK_PERSON_ID": "20"})
        self.assertEqual((code, out), (1, ""))
        self.assertIn("название предмета", err)

    def test_empty_tomorrow_is_reported_with_date_without_searching_other_days(self):
        self.responses = [{"json": []}]
        # Пятница после 10:00: запрашиваем субботу, а не понедельник.
        at = datetime.fromisoformat("2026-11-06T10:00:00+03:00").timestamp()
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_SCHOOL_ID": "10", "DNEVNIK_PERSON_ID": "20"}, "--at", str(at))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("ДЗ в дневнике пока нет", out)
        self.assertIn("субботу, 7 ноября 2026", out)
        self.assertEqual(len(self.requests), 1)
        params = parse_qs(urlsplit(self.requests[0].full_url).query)
        self.assertEqual(params["startDate"], ["2026-11-07T00:00:00"])
        self.assertEqual(params["endDate"], ["2026-11-07T23:59:59"])

    def test_before_cutoff_requests_only_today(self):
        self.responses = [{"json": []}]
        at = datetime.fromisoformat("2026-11-04T09:59:59+03:00").timestamp()
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_SCHOOL_ID": "10", "DNEVNIK_PERSON_ID": "20"}, "--at", str(at))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("среду, 4 ноября 2026", out)
        params = parse_qs(urlsplit(self.requests[0].full_url).query)
        self.assertEqual(params["startDate"], ["2026-11-04T00:00:00"])

    def test_html_and_attachment_links_reach_the_formatted_message(self):
        self.responses = [{"json": {"homeworks": [{
            "subject": "Химия",
            "text": '<p>Прочитать &amp; решить:</p><a href="https://example.org/task?a=1&amp;b=2">задание</a><br>https://example.org/book',
            "attachments": [{"url": "https://example.org/file.pdf"}],
        }]}}]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN, "DNEVNIK_SCHOOL_ID": "10", "DNEVNIK_PERSON_ID": "20"})
        self.assertEqual((code, err), (0, ""))
        for value in ("Химия", "Прочитать & решить:", "задание (https://example.org/task?a=1&b=2)", "https://example.org/book", "https://example.org/file.pdf"):
            self.assertIn(value, out)
        self.assertNotIn("<a", out)

    def test_login_password_do_not_trigger_authentication(self):
        code, out, err = self.run_main({"DNEVNIK_LOGIN": "login", "DNEVNIK_PASSWORD": TOKEN})
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(TOKEN, err)
        self.assertEqual(self.requests, [])

    def test_cli_error_is_sanitized_and_nonzero(self):
        self.responses = [URLError(TOKEN)]
        code, out, err = self.run_main({"DNEVNIK_TOKEN": TOKEN})
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn(TOKEN, err)
        self.assertNotIn("Traceback", err)

    def test_demo_needs_no_credentials_or_network(self):
        code, out, err = self.run_main({}, "--demo")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Алгебра — № 1", out)
        self.assertEqual(self.requests, [])

    def test_unknown_homework_shape_is_an_error(self):
        with self.assertRaises(DnevnikError):
            fetcher.fetch_day(type("Client", (), {"get_person_homework": lambda *args: {"unexpected": []}})(), 10, 20, fetcher.datetime.now().date())


class DateAndTextTests(unittest.TestCase):
    def test_cutoff_midnight_month_year_and_leap_day(self):
        cases = [
            ("2026-11-04T00:00:00+03:00", date(2026, 11, 4)),
            ("2026-11-04T09:59:59+03:00", date(2026, 11, 4)),
            ("2026-11-04T10:00:00+03:00", date(2026, 11, 5)),
            ("2026-11-04T23:59:59+03:00", date(2026, 11, 5)),
            ("2026-11-30T10:00:00+03:00", date(2026, 12, 1)),
            ("2026-12-31T10:00:00+03:00", date(2027, 1, 1)),
            ("2028-02-28T10:00:00+03:00", date(2028, 2, 29)),
        ]
        with patch.dict(os.environ, {"BOT_TIMEZONE": "Europe/Moscow"}):
            for instant, expected in cases:
                with self.subTest(instant=instant):
                    self.assertEqual(fetcher.target_day(datetime.fromisoformat(instant).timestamp()), expected)

    def test_school_timezone_can_be_changed(self):
        at = datetime.fromisoformat("2026-11-04T06:00:00+00:00").timestamp()
        with patch.dict(os.environ, {"BOT_TIMEZONE": "Europe/Moscow"}):
            self.assertEqual(fetcher.target_day(at), date(2026, 11, 4))
        with patch.dict(os.environ, {"BOT_TIMEZONE": "Asia/Yekaterinburg"}):
            self.assertEqual(fetcher.target_day(at), date(2026, 11, 5))

    def test_invalid_time_or_timezone_is_controlled(self):
        with patch.dict(os.environ, {"BOT_TIMEZONE": "../bad-zone"}):
            with self.assertRaises(DnevnikError):
                fetcher.target_day()
        with patch.dict(os.environ, {"BOT_TIMEZONE": "Europe/Moscow"}):
            for at in (float("inf"), float("nan"), 1e100):
                with self.assertRaises(DnevnikError):
                    fetcher.target_day(at)

    def test_plain_urls_keep_query_parameters_and_anchor_does_not_duplicate_url(self):
        for url in ("https://example.org/?a=1&b=2", "https://example.org/?a=1&copy=2&notin=3"):
            self.assertEqual(fetcher.hw_text({"text": url}), url)
            self.assertEqual(fetcher.hw_text({"text": f'<a href="{url}">{url}</a>'}), url)
        self.assertEqual(fetcher.hw_text({"text": "№ 1", "files": [123, "456", {"id": 789}]}), "№ 1")


if __name__ == "__main__":
    unittest.main()
