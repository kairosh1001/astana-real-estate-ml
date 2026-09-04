import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from scripts.diagnose_city_access import compare_category_sequence, probe, select_targets


class CityAccessDiagnosticTests(unittest.TestCase):
    def test_sequence_uses_same_url_and_session_without_database(self):
        session = Mock()
        url = "https://krisha.kz/a/show/1013552697"
        ok = {"http_status": 200, "has_listing_title": True, "has_listing_price": True}
        with patch("scripts.diagnose_city_access.request_once", side_effect=[ok.copy(), ok.copy(), {"http_status": 468}]) as request, patch("scripts.diagnose_city_access.time.sleep"):
            rows = compare_category_sequence(session, "astana", url)
        self.assertEqual(len(rows), 3)
        self.assertEqual(request.call_args_list[0].args, (session, "astana", url))
        self.assertEqual(request.call_args_list[2].args, (session, "astana", url))
        self.assertEqual(request.call_args_list[1].args[2], "https://krisha.kz/prodazha/kvartiry/astana/?page=1")

    def test_sequence_stops_on_first_rejection_or_missing_listing(self):
        for response in ({"http_status": 468}, {"http_status": 200, "has_listing_title": False}):
            with self.subTest(response=response), patch("scripts.diagnose_city_access.request_once", return_value=response.copy()) as request:
                rows = compare_category_sequence(Mock(), "astana", "https://krisha.kz/a/show/1")
                self.assertEqual(len(rows), 1)
                self.assertEqual(request.call_count, 1)

    def test_two_requests_same_session_despite_first_rejection(self):
        session = Mock()
        responses = []
        for status, body in ((468, "<p>Access denied captcha SECRET</p>"),
                             (200, '<div class="offer__advert-title"><h1>Flat</h1></div>')):
            response = requests.Response()
            response.status_code = status
            response._content = body.encode()
            response.headers["Set-Cookie"] = "private=SECRET"
            response.close = Mock()
            responses.append(response)
        session.get.side_effect = responses
        targets = {"astana": "https://krisha.kz/a/show/1", "almaty": "https://krisha.kz/a/show/2"}
        with patch("scripts.diagnose_city_access.time.sleep"):
            results = probe(session, targets)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual([r["http_status"] for r in results], [468, 200])
        self.assertNotIn("SECRET", str(results))
        self.assertIn("access_denied", results[0]["protection_markers"])
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs, {"timeout": 15, "allow_redirects": False})

    def test_invalid_target_rejected_before_network(self):
        session = Mock()
        with self.assertRaises(ValueError):
            probe(session, {"astana": "http://localhost/", "almaty": "https://krisha.kz/a/show/2"})
        session.get.assert_not_called()

    def test_selection_does_not_create_or_modify_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                select_targets(str(path))
            self.assertFalse(path.exists())
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE listings (url TEXT, city TEXT, status TEXT, last_checked_at TEXT)")
            connection.executemany("INSERT INTO listings VALUES (?, ?, 'active', '2026-09-04')", [
                ("https://krisha.kz/a/show/1", "astana"),
                ("https://krisha.kz/a/show/2", "almaty"),
            ])
            connection.commit()
            connection.close()
            before = path.read_bytes()
            self.assertEqual(len(select_targets(str(path))), 2)
            self.assertEqual(path.read_bytes(), before)
