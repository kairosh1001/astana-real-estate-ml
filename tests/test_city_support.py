from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import (
    connect,
    fetch_home_match_candidates,
    fetch_market_dashboard,
    fetch_telegram_subscribers_for_digest,
    fetch_undervalued,
    init_db,
    mark_refresh_started,
    normalize_district,
    set_telegram_notification_city,
    upsert_telegram_subscriber,
    upsert_listing_prediction,
)
from app.cities import infer_listing_city
from app.prediction_service import ListingPrediction


def _prediction(city: str) -> ListingPrediction:
    return ListingPrediction(
        url=f"https://krisha.kz/a/show/test-{city}",
        title="2-комнатная квартира, 60 м², 5/12 этаж",
        listed_price=30_000_000,
        area_m2=60,
        listed_price_per_m2=500_000,
        pred_price_per_m2_q10=550_000,
        pred_price_per_m2_q50=600_000,
        pred_price_per_m2_q90=700_000,
        pred_total_q50=36_000_000,
        discount_vs_asking_pct_conservative=0.1,
        discount_vs_asking_pct_median=0.2,
        interval_width_pct=0.25,
        city=city,
    )


class CityDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        init_db(self.connection)
        for city, location in (
            ("astana", "Астана, Есиль р-н"),
            ("almaty", "Алматы, Бостандыкский р-н"),
        ):
            prediction = _prediction(city)
            upsert_listing_prediction(
                self.connection,
                raw_listing={
                    "url": prediction.url,
                    "title": prediction.title,
                    "price": prediction.listed_price,
                    "scrape_city": city,
                    "Город": location,
                    "Площадь": "60 м²",
                    "lat": 51.13 if city == "astana" else 43.24,
                    "lon": 71.43 if city == "astana" else 76.94,
                },
                prediction=prediction,
            )

    def tearDown(self) -> None:
        self.connection.close()

    def test_city_columns_and_filters_keep_markets_separate(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        self.assertIn("city", columns)
        self.assertEqual(len(fetch_undervalued(self.connection, city="astana")), 1)
        self.assertEqual(len(fetch_undervalued(self.connection, city="almaty")), 1)
        self.assertEqual(
            len(fetch_home_match_candidates(self.connection, city="almaty")), 1
        )
        combined = fetch_undervalued(self.connection, city="both")
        self.assertEqual(len(combined), 2)
        self.assertEqual({item["city"] for item in combined}, {"astana", "almaty"})

    def test_listing_page_city_wins_over_search_page_city(self) -> None:
        self.assertEqual(
            infer_listing_city(
                {
                    "scrape_city": "almaty",
                    "Город": "Астана, Алматы р-н",
                    "lat": 51.15,
                    "lon": 71.50,
                }
            ),
            "astana",
        )

    def test_init_quarantines_existing_cross_city_rows(self) -> None:
        astana_url = _prediction("astana").url
        self.connection.execute(
            "UPDATE listings SET city = 'almaty', status = 'active' WHERE url = ?",
            (astana_url,),
        )
        self.connection.commit()

        init_db(self.connection)

        row = self.connection.execute(
            "SELECT city, status FROM listings WHERE url = ?",
            (astana_url,),
        ).fetchone()
        self.assertEqual(dict(row), {"city": "astana", "status": "stale"})

    def test_stale_accounting_is_city_scoped(self) -> None:
        mark_refresh_started(self.connection, city="almaty")
        rows = self.connection.execute(
            "SELECT city, missed_refreshes FROM listings ORDER BY city"
        ).fetchall()
        self.assertEqual(
            {row["city"]: row["missed_refreshes"] for row in rows},
            {"almaty": 1, "astana": 0},
        )

    def test_city_specific_districts_and_dashboard(self) -> None:
        self.assertEqual(
            normalize_district("Алматы, Бостандыкский р-н", city="almaty"),
            "bostandyk",
        )
        dashboard = fetch_market_dashboard(self.connection, city="almaty")
        self.assertEqual(dashboard["city"]["name"], "Алматы")
        self.assertEqual(dashboard["districts"][0]["slug"], "bostandyk")

    def test_telegram_city_preference_is_migrated_and_preserved(self) -> None:
        upsert_telegram_subscriber(self.connection, chat_id=42)
        set_telegram_notification_city(
            self.connection,
            chat_id=42,
            notification_city="both",
        )
        upsert_telegram_subscriber(
            self.connection,
            chat_id=42,
            notifications_enabled=True,
        )
        subscribers = fetch_telegram_subscribers_for_digest(
            self.connection,
            digest_date="2026-08-12",
        )
        self.assertEqual(subscribers[0]["notification_city"], "both")


if __name__ == "__main__":
    unittest.main()
