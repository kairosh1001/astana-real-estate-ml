from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.manual_prediction import ManualApartment, build_manual_raw_listing


class ManualPredictionTest(unittest.TestCase):
    def test_builds_v2_compatible_raw_listing(self) -> None:
        raw = build_manual_raw_listing(
            ManualApartment(
                city="astana",
                listed_price=35_000_000,
                area_m2=62.5,
                rooms=2,
                current_floor=5,
                total_floors=12,
                construction_year=2020,
                district="yesil",
                residential_complex="Test Residence",
                ceiling_height=3.0,
                furnished="full",
                apartment_condition="fresh_repair",
                building_type="monolith",
                is_new_build=True,
                middle_floor_only=True,
                lat=51.12,
                lon=71.42,
            )
        )
        self.assertEqual(raw["scrape_city"], "astana")
        self.assertIn("Есиль", raw["Город"])
        self.assertEqual(raw["Этаж"], "5 из 12")
        self.assertEqual(raw["Тип дома"], "монолитный")
        self.assertIn("2-комнатная", raw["title"])

    def test_middle_floor_checkbox_rejects_edge_floors(self) -> None:
        for floor, total in ((1, 9), (9, 9)):
            with self.subTest(floor=floor, total=total):
                with self.assertRaisesRegex(ValueError, "промежуточный этаж"):
                    build_manual_raw_listing(
                        ManualApartment(
                            city="almaty",
                            listed_price=40_000_000,
                            area_m2=55,
                            rooms=2,
                            current_floor=floor,
                            total_floors=total,
                            construction_year=2018,
                            middle_floor_only=True,
                        )
                    )

    def test_coordinates_must_match_selected_city(self) -> None:
        with self.assertRaisesRegex(ValueError, "пределами"):
            build_manual_raw_listing(
                ManualApartment(
                    city="astana",
                    listed_price=30_000_000,
                    area_m2=50,
                    rooms=2,
                    current_floor=3,
                    total_floors=9,
                    construction_year=2015,
                    lat=43.24,
                    lon=76.94,
                )
            )


if __name__ == "__main__":
    unittest.main()
