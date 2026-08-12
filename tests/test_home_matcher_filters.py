from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.home_matcher import HomeSearchPreferences, _passes_hard_filters


class HomeMatcherPriceFilterTest(unittest.TestCase):
    def test_minimum_and_maximum_price_are_both_enforced(self) -> None:
        item = {"listed_price": 30_000_000}
        self.assertTrue(
            _passes_hard_filters(
                item,
                HomeSearchPreferences(min_price=20_000_000, max_price=40_000_000),
            )
        )
        self.assertFalse(
            _passes_hard_filters(
                item,
                HomeSearchPreferences(min_price=31_000_000),
            )
        )
        self.assertFalse(
            _passes_hard_filters(
                item,
                HomeSearchPreferences(max_price=29_000_000),
            )
        )


if __name__ == "__main__":
    unittest.main()
