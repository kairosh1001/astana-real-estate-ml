from __future__ import annotations

import sys
from pathlib import Path
import unittest

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_astana_models import (
    assign_split,
    property_groups,
    regression_metrics,
    room_segment,
)


class AstanaComparisonTests(unittest.TestCase):
    def test_duplicate_property_groups_share_a_split(self) -> None:
        frame = pd.DataFrame(
            {
                "city": ["astana", "astana"],
                "h3_res_9": ["cell", "cell"],
                "city_residential_complex": ["astana__test", "astana__test"],
                "rooms": [2, 2],
                "area_m2": [55.0, 55.01],
                "current_floor": [5, 5],
                "total_floors": [12, 12],
                "year_of_construction": [2020, 2020],
            }
        )
        self.assertEqual(property_groups(frame).nunique(), 1)
        self.assertEqual(assign_split(frame).nunique(), 1)

    def test_regression_metrics_use_log_target_and_kzt_scale(self) -> None:
        actual = np.log(np.array([400_000.0, 600_000.0]))
        predictions = {
            "q10": np.log(np.array([350_000.0, 500_000.0])),
            "q50": np.log(np.array([400_000.0, 600_000.0])),
            "q90": np.log(np.array([450_000.0, 700_000.0])),
        }
        metrics = regression_metrics(actual, predictions)
        self.assertAlmostEqual(metrics["log_rmse"], 0.0)
        self.assertAlmostEqual(metrics["mae_kzt_per_m2"], 0.0)
        self.assertAlmostEqual(metrics["q10_q90_coverage_pct"], 100.0)

    def test_room_segment_caps_large_apartments(self) -> None:
        self.assertEqual(room_segment(1), "1")
        self.assertEqual(room_segment(5), "5+")
        self.assertEqual(room_segment(9), "5+")


if __name__ == "__main__":
    unittest.main()
