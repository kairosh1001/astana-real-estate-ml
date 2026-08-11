from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline_v2 import UNIVERSAL_FEATURE_COLUMNS
from scripts.optimize_model_v2 import add_derived_features, assign_split, feature_sets


class OptimizedFeatureProfileTest(unittest.TestCase):
    def test_feature_set_sizes_match_validated_experiment(self) -> None:
        sets = feature_sets(list(UNIVERSAL_FEATURE_COLUMNS))
        self.assertEqual(len(sets["full"]), 55)
        self.assertEqual(len(sets["compact"]), 34)
        self.assertEqual(len(sets["derived_compact"]), 41)
        self.assertNotIn(
            "dist_to_city_center_normalized", sets["derived_compact"]
        )

    def test_derived_features_cover_large_rooms_and_missing_floors(self) -> None:
        frame = pd.DataFrame(
            {
                "rooms": [2, 6],
                "area_m2": [60.0, 240.0],
                "current_floor": [1.0, pd.NA],
                "total_floors": [10.0, pd.NA],
                "year_of_construction": [2020.0, 2000.0],
            }
        )
        derived = add_derived_features(frame)
        self.assertEqual(derived["rooms_segment"].tolist(), ["2", "5+"])
        self.assertEqual(derived["area_per_room"].tolist(), [30.0, 40.0])
        self.assertEqual(derived["is_first_floor"].tolist(), [1, 0])

    def test_property_group_split_is_deterministic(self) -> None:
        row = {
            "city": "almaty",
            "h3_res_9": "8928312340fffff",
            "city_residential_complex": "almaty__test",
            "rooms": 3,
            "area_m2": 85.0,
            "current_floor": 4,
            "total_floors": 12,
            "year_of_construction": 2022,
        }
        frame = pd.DataFrame([row, row])
        split = assign_split(frame)
        self.assertEqual(split.iloc[0], split.iloc[1])


if __name__ == "__main__":
    unittest.main()
