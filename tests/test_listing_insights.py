from __future__ import annotations

import unittest

from app.listing_insights import build_comparable_insight


class ComparableInsightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = {
            "url": "target",
            "city": "astana",
            "rooms": 2,
            "area_m2": 60,
            "listed_price_per_m2": 500_000,
            "residential_complex": "ЖК Тест",
            "district_slug": "yesil",
            "construction_year": 2021,
            "lat": 51.13,
            "lon": 71.43,
        }

    def test_same_complex_and_room_candidate_ranks_first(self) -> None:
        candidates = [
            self.candidate("district", complex_name="Другой ЖК", area=62, price=510_000),
            self.candidate("exact", complex_name="жк тест", area=59, price=490_000),
            self.candidate("large", complex_name="ЖК Тест", area=82, price=470_000, rooms=3),
        ]
        insight = build_comparable_insight(self.target, candidates)
        self.assertIsNotNone(insight)
        self.assertEqual(insight["items"][0]["url"], "exact")
        self.assertIn("тот же ЖК", insight["items"][0]["similarity_reasons"])
        self.assertIn("столько же комнат", insight["items"][0]["similarity_reasons"])

    def test_cross_city_and_self_are_never_returned(self) -> None:
        candidates = [
            self.candidate("target", complex_name="ЖК Тест", area=60, price=500_000),
            {**self.candidate("almaty", complex_name="ЖК Тест", area=60, price=500_000), "city": "almaty"},
            self.candidate("valid", complex_name="ЖК Тест", area=61, price=505_000),
        ]
        insight = build_comparable_insight(self.target, candidates)
        self.assertEqual([item["url"] for item in insight["items"]], ["valid"])

    def test_summary_uses_selected_asking_prices(self) -> None:
        candidates = [
            self.candidate("a", complex_name="ЖК Тест", area=58, price=450_000),
            self.candidate("b", complex_name="ЖК Тест", area=60, price=500_000),
            self.candidate("c", complex_name="ЖК Тест", area=62, price=550_000),
        ]
        insight = build_comparable_insight(self.target, candidates)
        self.assertEqual(insight["median_price_per_m2"], 500_000)
        self.assertEqual(insight["median_total_for_target_area"], 30_000_000)
        self.assertEqual(insight["min_price_per_m2"], 450_000)
        self.assertEqual(insight["max_price_per_m2"], 550_000)

    def candidate(
        self,
        url: str,
        *,
        complex_name: str,
        area: float,
        price: float,
        rooms: int = 2,
    ) -> dict:
        return {
            "url": url,
            "city": "astana",
            "short_title": f"{rooms}-комнатная · {area} м²",
            "rooms": rooms,
            "area_m2": area,
            "listed_price": area * price,
            "listed_price_per_m2": price,
            "residential_complex": complex_name,
            "district_slug": "yesil",
            "construction_year": 2020,
            "lat": 51.131,
            "lon": 71.431,
        }


if __name__ == "__main__":
    unittest.main()
