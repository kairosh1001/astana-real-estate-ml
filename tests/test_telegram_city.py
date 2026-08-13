from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.telegram_bot import format_digest, select_digest_listings


class TelegramCityDigestTest(unittest.TestCase):
    def test_invalid_scope_defaults_to_astana(self) -> None:
        astana = [
            {"city": "astana", "discount_vs_asking_pct_conservative": value / 100}
            for value in range(20, 10, -1)
        ]
        almaty = [
            {"city": "almaty", "discount_vs_asking_pct_conservative": value / 100}
            for value in range(30, 20, -1)
        ]
        selected = select_digest_listings(astana, almaty, "both")
        self.assertEqual(selected, astana[:10])

    def test_digest_names_selected_market(self) -> None:
        text = format_digest([], "https://kvartiry-ai.kz", notification_city="almaty")
        self.assertIn("Алматы", text)


if __name__ == "__main__":
    unittest.main()
