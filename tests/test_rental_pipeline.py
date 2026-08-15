from __future__ import annotations

import sqlite3
import unittest

from app.database import (
    fetch_undervalued,
    init_db,
    store_listing_rental_estimate,
    upsert_listing_prediction,
    upsert_rental_listing,
)
from app.prediction_service import ListingPrediction


class RentalStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_rental_inventory_is_separate_and_estimate_reaches_sale_card(self) -> None:
        rental = {
            "url": "https://krisha.kz/a/show/22",
            "title": "2-комнатная квартира, 54 м²",
            "price": 400_000,
            "area_m2_structured": 54,
        }
        upsert_rental_listing(self.connection, raw_listing=rental, city="almaty")
        self.connection.commit()
        row = self.connection.execute(
            "SELECT monthly_rent, area_m2, rent_per_m2 FROM rental_listings"
        ).fetchone()
        self.assertEqual(row["monthly_rent"], 400_000)
        self.assertEqual(row["area_m2"], 54)

        raw_sale = {
            "url": "https://krisha.kz/a/show/11",
            "title": "2-комнатная квартира, 54 м²",
            "price": 40_000_000,
            "Город": "Алматы, Медеуский р-н",
        }
        prediction = ListingPrediction(
            url=raw_sale["url"], title=raw_sale["title"], listed_price=40_000_000,
            area_m2=54, listed_price_per_m2=740_740,
            pred_price_per_m2_q10=800_000, pred_price_per_m2_q50=850_000,
            pred_price_per_m2_q90=900_000, pred_total_q50=45_900_000,
            discount_vs_asking_pct_conservative=.08,
            discount_vs_asking_pct_median=.15, interval_width_pct=.12, city="almaty",
        )
        upsert_listing_prediction(
            self.connection, raw_listing=raw_sale, prediction=prediction
        )
        store_listing_rental_estimate(
            self.connection,
            url=raw_sale["url"],
            estimate={
                "monthly_rent_q10": 320_000,
                "monthly_rent_q50": 360_000,
                "monthly_rent_q90": 410_000,
                "gross_yield_q50": .108,
                "payback_years_q50": 9.26,
            },
        )
        self.connection.commit()
        item = fetch_undervalued(self.connection, city="almaty", limit=1)[0]
        self.assertEqual(item["monthly_rent_q50"], 360_000)
        self.assertAlmostEqual(item["gross_yield_q50"], .108)


if __name__ == "__main__":
    unittest.main()
