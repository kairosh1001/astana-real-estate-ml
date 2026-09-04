from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.database import connect, init_db, upsert_listing_prediction
from app.prediction_service import ListingPrediction
from app.refresh_service import run_refresh


def raw_listing(url: str) -> dict:
    return {
        "url": url,
        "title": "2-комнатная квартира, 60 м², 5/12 этаж",
        "price": 30_000_000,
        "Город": "Астана, Есиль р-н",
        "Площадь": "60 м²",
    }


def prediction(raw: dict, *, url: str) -> ListingPrediction:
    return ListingPrediction(
        url=url, title=raw["title"], listed_price=30_000_000,
        area_m2=60, listed_price_per_m2=500_000,
        pred_price_per_m2_q10=480_000, pred_price_per_m2_q50=520_000,
        pred_price_per_m2_q90=580_000, pred_total_q50=31_200_000,
        discount_vs_asking_pct_conservative=-0.04,
        discount_vs_asking_pct_median=0.04, interval_width_pct=0.19,
    )


class RefreshFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "refresh.sqlite3"
        self.scraper = Mock()
        self.scraper.base_url = "https://krisha.kz"
        self.scraper.last_fetch_error = None
        self.scraper.last_fetch_status = None
        self.scraper.last_parse_error = "Missing listing data"
        self.urls = [f"https://krisha.kz/a/show/{index}" for index in range(40)]
        self.scraper.get_listing_urls.return_value = self.urls
        self.scraper.parse_apartment_page.side_effect = raw_listing
        self.model = Mock()
        self.model.predict_raw_listing.side_effect = prediction

    def run_refresh(self, **kwargs):
        options = dict(
            root=".", db_path=self.db_path, pages=1, min_delay=0, max_delay=0,
            empty_page_retries=1, max_consecutive_listing_failures=3,
        )
        options.update(kwargs)
        with (
            patch("app.refresh_service.ApartmentScraper", return_value=self.scraper),
            patch("app.refresh_service.PredictionService", return_value=self.model),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return run_refresh(**options)

    def query(self, sql: str):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def seed_unseen_listing(self) -> None:
        connection = connect(self.db_path)
        try:
            init_db(connection)
            raw = raw_listing("https://krisha.kz/a/show/unseen")
            upsert_listing_prediction(
                connection, raw_listing=raw, prediction=prediction(raw, url=raw["url"]),
            )
            connection.execute(
                "UPDATE listings SET missed_refreshes = 2, last_checked_at = '2000-01-01'"
            )
            connection.commit()
        finally:
            connection.close()

    def test_all_parse_failures_stop_early_and_store_reason(self) -> None:
        self.scraper.parse_apartment_page.side_effect = None
        self.scraper.parse_apartment_page.return_value = None
        self.scraper.last_parse_error = "ReadTimeout"
        with patch("app.refresh_service.time.sleep") as sleep:
            result = self.run_refresh(min_delay=1, max_delay=1)
        self.assertEqual((result.status, result.listings_processed, result.listings_failed),
                         ("failed", 0, 3))
        self.assertIn("ReadTimeout", result.error)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(self.scraper.parse_apartment_page.call_count, 3)
        self.assertEqual(self.query("SELECT status FROM refresh_runs"), [("failed",)])
        self.assertIn("parse:", self.query("SELECT error FROM refresh_runs")[0][0])

    def test_access_denial_stops_without_more_listing_requests(self) -> None:
        self.scraper.parse_apartment_page.side_effect = None
        self.scraper.parse_apartment_page.return_value = None
        self.scraper.last_fetch_status = 403
        self.scraper.last_parse_error = "HTTP 403"
        result = self.run_refresh()
        self.assertEqual(result.listings_failed, 1)
        self.assertIn("HTTP 403", result.error)

    def test_prediction_errors_are_not_reported_as_success(self) -> None:
        self.model.predict_raw_listing.side_effect = ValueError("feature mismatch")
        result = self.run_refresh()
        self.assertEqual(result.status, "failed")
        self.assertIn("predict: ValueError: feature mismatch", result.error)

    def test_http_468_listing_stops_immediately_without_model_or_write(self) -> None:
        self.seed_unseen_listing()
        self.scraper.parse_apartment_page.side_effect = None
        self.scraper.parse_apartment_page.return_value = None
        self.scraper.last_fetch_status = 468
        self.scraper.last_parse_error = "HTTP 468"
        result = self.run_refresh(kind="weekly", pages=100)
        self.assertEqual((result.status, result.listings_processed, result.listings_failed),
                         ("failed", 0, 1))
        self.assertIn("Krisha отклонила запрос: HTTP 468", result.error)
        self.assertEqual(self.scraper.parse_apartment_page.call_count, 1)
        self.assertEqual(self.scraper.get_listing_urls.call_count, 1)
        self.scraper.reset_session.assert_not_called()
        self.model.predict_raw_listing.assert_not_called()
        self.assertEqual(self.query("SELECT status, missed_refreshes FROM listings"),
                         [("active", 2)])

    def test_http_468_category_stops_without_retries_or_session_rotation(self) -> None:
        self.scraper.get_listing_urls.return_value = []
        self.scraper.last_fetch_status = 468
        self.scraper.last_fetch_error = "HTTP 468"
        result = self.run_refresh(pages=100, empty_page_retries=3)
        self.assertEqual(result.status, "failed")
        self.assertIn("HTTP 468", result.error)
        self.assertEqual(self.scraper.get_listing_urls.call_count, 1)
        self.scraper.reset_session.assert_not_called()
        self.scraper.parse_apartment_page.assert_not_called()

    def test_partial_weekly_scan_does_not_age_existing_inventory(self) -> None:
        self.seed_unseen_listing()
        self.scraper.parse_apartment_page.side_effect = [raw_listing(self.urls[0]), None, None, None]
        result = self.run_refresh(kind="weekly")
        self.assertEqual((result.status, result.listings_processed), ("partial", 1))
        self.assertEqual(self.query(
            "SELECT status, missed_refreshes FROM listings WHERE url LIKE '%/unseen'"
        ), [("active", 2)])

    def test_successful_weekly_scan_ages_only_unseen_listings(self) -> None:
        self.seed_unseen_listing()
        self.scraper.get_listing_urls.return_value = self.urls[:1]
        result = self.run_refresh(kind="weekly")
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.query(
            "SELECT status, missed_refreshes FROM listings WHERE url LIKE '%/unseen'"
        ), [("stale", 3)])
        self.assertEqual(self.query(
            "SELECT status, missed_refreshes FROM listings WHERE url LIKE '%/0'"
        ), [("active", 0)])

    def test_empty_scan_is_failed_even_before_empty_page_threshold(self) -> None:
        self.scraper.get_listing_urls.return_value = []
        result = self.run_refresh()
        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.error)

    def test_smoke_limit_caps_attempts_not_successes(self) -> None:
        self.model.predict_raw_listing.side_effect = ValueError("bad model")
        result = self.run_refresh(max_listings=2)
        self.assertEqual(result.listings_failed, 2)
        self.assertEqual(self.scraper.parse_apartment_page.call_count, 2)

    def test_duplicate_urls_across_pages_are_processed_once(self) -> None:
        self.scraper.get_listing_urls.return_value = self.urls[:1]
        result = self.run_refresh(pages=2)
        self.assertEqual(result.listings_processed, 1)
        self.assertEqual(result.urls_seen, 1)

    def test_model_initialization_failure_closes_run(self) -> None:
        with (
            patch("app.refresh_service.PredictionService", side_effect=FileNotFoundError("model missing")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(FileNotFoundError),
        ):
            run_refresh(root=".", db_path=self.db_path, pages=1)
        self.assertEqual(self.query("SELECT status, error FROM refresh_runs"),
                         [("failed", "model missing")])

    def test_failed_write_is_rolled_back_before_next_success(self) -> None:
        connection = connect(self.db_path)
        connection.execute("CREATE TABLE partial_writes (value INTEGER)")
        connection.commit()
        connection.close()
        self.scraper.get_listing_urls.return_value = self.urls[:2]
        calls = 0

        def store(connection, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                connection.execute("INSERT INTO partial_writes VALUES (1)")
                raise sqlite3.OperationalError("simulated failed write")
            return upsert_listing_prediction(connection, **kwargs)

        with patch("app.refresh_service.upsert_listing_prediction", side_effect=store):
            result = self.run_refresh()
        self.assertEqual(result.status, "partial")
        self.assertIn("store: OperationalError", result.error)
        self.assertEqual(self.query("SELECT * FROM partial_writes"), [])


if __name__ == "__main__":
    unittest.main()
