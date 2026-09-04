import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from app.catalog_scraper import init_catalog_db, parse_catalog, run_catalog_refresh, save_page


def card(url="/a/show/123", city="Астана", price="42 900 000 ₸",
         title="2-комнатная квартира · 60,5 м² · 3/8 этаж"):
    return f'''<div class="a-card">
        <a class="a-card__title" href="{url}">{title}</a>
        <div class="a-card__price">{price}</div>
        <div class="a-card__subtitle">Есильский р-н, Улы Дала 61</div>
        <div class="a-card__text-preview">монолитный дом, 2019 г.п.</div>
        <div class="a-card__stats-item">{city}</div>
        </div>'''


def response(html, status=200):
    result = requests.Response()
    result._content = html.encode("utf-8")
    result.encoding = "utf-8"
    result.status_code = status
    result.close = Mock()
    return result


class CatalogParserTests(unittest.TestCase):
    def test_visible_fields_and_explicit_partial_provenance(self):
        rows, skipped = parse_catalog(card(), "astana")
        self.assertEqual(skipped, 0)
        row = rows[0]
        self.assertEqual((row["asking_price"], row["area_m2"], row["rooms"]), (42900000, 60.5, 2))
        self.assertEqual((row["floor"], row["total_floors"]), (3, 8))
        self.assertEqual(row["source"], "catalog")
        self.assertFalse(row["model_eligible"])
        self.assertNotIn("lat", row)

    def test_missing_total_floor_is_not_invented(self):
        rows, _ = parse_catalog(card(title="2-комнатная квартира · 60 м² · 3 этаж"), "astana")
        self.assertEqual(rows[0]["floor"], 3)
        self.assertIsNone(rows[0]["total_floors"])

    def test_wrong_city_malformed_link_and_ambiguous_prices_are_excluded(self):
        for html in (card(city="Алматы"), card(city=""), card(url="https://example.org/a/show/1"),
                     card(price="от 42 000 000 ₸"), card(price="40 000 000 - 45 000 000 ₸"),
                     card(price="0 ₸"), card(title="Жилой комплекс")):
            with self.subTest(html=html):
                self.assertEqual(parse_catalog(html, "astana"), ([], 1))

    def test_duplicate_promotions_do_not_duplicate_observations(self):
        rows, skipped = parse_catalog(card() + card(url="/a/show/123/"), "astana")
        self.assertEqual((len(rows), skipped), (1, 0))


class CatalogStorageTests(unittest.TestCase):
    def test_history_changes_only_on_actual_price_change(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        init_catalog_db(connection)
        rows, _ = parse_catalog(card(), "astana")
        self.assertEqual(save_page(connection, rows, 1), (1, 0))
        first_seen = connection.execute("SELECT first_seen_at FROM catalog_listings").fetchone()[0]
        self.assertEqual(save_page(connection, rows, 2), (0, 0))
        rows[0]["asking_price"] = 40000000
        self.assertEqual(save_page(connection, rows, 3), (0, 1))
        self.assertEqual(connection.execute("SELECT asking_price FROM catalog_price_history ORDER BY id").fetchall(),
                         [(42900000,), (40000000,)])
        row = connection.execute("SELECT first_seen_at, raw_json FROM catalog_listings").fetchone()
        self.assertEqual(row[0], first_seen)
        self.assertFalse(json.loads(row[1])["model_eligible"])
        self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='listings'").fetchone())

    def test_failed_page_is_rolled_back(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        init_catalog_db(connection)
        rows, _ = parse_catalog(card(), "astana")
        with self.assertRaises(KeyError):
            save_page(connection, rows + [{}], 1)
        self.assertEqual(connection.execute("SELECT count(*) FROM catalog_listings").fetchone()[0], 0)


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.sqlite3"
        self.scraper = Mock()

    def run_job(self, responses, pages):
        self.scraper.session.get.side_effect = responses
        with patch("app.catalog_scraper.ApartmentScraper", return_value=self.scraper) as factory, \
             patch("app.catalog_scraper.time.sleep"), patch("builtins.print"):
            result = run_catalog_refresh(db_path=self.path, city="astana", pages=pages)
        factory.assert_called_once_with(retry_total=0)
        self.scraper.parse_apartment_page.assert_not_called()
        self.scraper.reset_session.assert_not_called()
        self.scraper.session.close.assert_called_once()
        return result

    def test_only_catalog_requests_and_deduplication_across_pages(self):
        result = self.run_job([response(card()), response(card() + card(url="/a/show/456"))], 2)
        self.assertEqual((result["status"], result["listings_saved"]), ("completed", 2))
        urls = [call.args[0] for call in self.scraper.session.get.call_args_list]
        self.assertEqual(urls, ["https://krisha.kz/prodazha/kvartiry/astana/",
                                "https://krisha.kz/prodazha/kvartiry/astana/?page=2"])
        for call in self.scraper.session.get.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_access_denial_stops_without_following_links(self):
        result = self.run_job([response("", 468)], 100)
        self.assertEqual((result["status"], result["listings_saved"]), ("failed", 0))
        self.assertEqual(self.scraper.session.get.call_count, 1)
        self.assertIn("HTTP 468", result["error"])

    def test_later_denial_preserves_earlier_observations_as_partial(self):
        result = self.run_job([response(card()), response("", 403)], 100)
        self.assertEqual((result["status"], result["listings_saved"]), ("partial", 1))
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT count(*) FROM catalog_listings").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT status FROM catalog_runs").fetchone()[0], "partial")

    def test_empty_or_challenge_page_is_not_success(self):
        result = self.run_job([response("<h1>Captcha</h1>")], 1)
        self.assertEqual(result["status"], "failed")

    def test_network_error_does_not_expose_proxy_credentials(self):
        result = self.run_job([requests.ConnectionError("password=SECRET")], 1)
        self.assertEqual(result["error"], "ConnectionError")

    def test_invalid_options_are_rejected_before_network_or_database(self):
        with self.assertRaises(ValueError):
            run_catalog_refresh(db_path=self.path, city="astana", pages=100, delay=0)
        self.assertFalse(self.path.exists())
