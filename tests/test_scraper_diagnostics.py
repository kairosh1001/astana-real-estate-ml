import unittest
from unittest.mock import patch

import requests

from scrape import ApartmentScraper


class ScraperDiagnosticsTests(unittest.TestCase):
    def test_exhausted_http_retries_preserve_final_response(self) -> None:
        scraper = ApartmentScraper()
        self.addCleanup(scraper.session.close)
        retry = scraper.session.get_adapter("https://krisha.kz").max_retries
        self.assertFalse(retry.raise_on_status)

    def setUp(self):
        self.scraper = ApartmentScraper(retry_total=0)
        self.addCleanup(self.scraper.session.close)

    def test_http_failure_is_retained_for_refresh_report(self):
        response = requests.Response()
        response.status_code = 403
        with patch.object(self.scraper.session, "get", return_value=response):
            self.assertIsNone(self.scraper.parse_apartment_page("https://krisha.kz/a/show/123"))
        self.assertEqual(self.scraper.last_parse_error, "HTTP 403")
        self.assertEqual(self.scraper.last_fetch_status, 403)

    def test_network_failure_retains_type_without_request_secrets(self):
        with patch.object(self.scraper.session, "get", side_effect=requests.exceptions.ReadTimeout("private data")):
            self.assertIsNone(self.scraper.parse_apartment_page("https://krisha.kz/a/show/123"))
        self.assertEqual(self.scraper.last_parse_error, "ReadTimeout")

    def test_non_listing_html_is_rejected(self):
        self.assertIsNone(self.scraper.parse_apartment_html(
            "https://krisha.kz/a/show/123", "<html><h1>Access verification</h1></html>",
        ))
        self.assertIn("Missing listing title or price", self.scraper.last_parse_error)

    def test_null_optional_map_does_not_destroy_listing(self):
        html = '''<script id="jsdata">window.data = {"advert": {
            "title": "2-комнатная квартира, 60 м²", "price": 30000000, "map": null
        }};</script>'''
        item = self.scraper.parse_apartment_html("https://krisha.kz/a/show/123", html)
        self.assertIsNotNone(item)
        self.assertEqual(item["price"], 30_000_000)
        self.assertIsNone(item["lat"])
        self.assertIsNone(self.scraper.last_parse_error)

    def test_unexpected_parser_exception_is_retained(self):
        with patch.object(self.scraper, "extract_page_data", side_effect=ValueError("broken data")):
            self.assertIsNone(self.scraper.parse_apartment_html("https://krisha.kz/a/show/123", "<html/>"))
        self.assertEqual(self.scraper.last_parse_error, "ValueError: broken data")


if __name__ == "__main__":
    unittest.main()
