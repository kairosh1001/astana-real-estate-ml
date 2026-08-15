from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrape import ApartmentScraper


class RentalScrapeTest(unittest.TestCase):
    def test_monthly_listing_uses_structured_price_and_period(self) -> None:
        html = """
        <html><body>
          <div class="offer__advert-title"><h1>2-комнатная квартира · 54 м²</h1></div>
          <div class="offer__price">400 000 ₸ за месяц</div>
          <div class="offer__parameters">
            <dl><dt>Квартира меблирована</dt><dd>полностью</dd></dl>
          </div>
          <script id="jsdata">
            window.data = {"advert": {
              "id": 123,
              "price": 400000,
              "sectionAlias": "arenda",
              "categoryAlias": "kvartiry",
              "rooms": 2,
              "square": 54,
              "userType": "owner",
              "addressTitle": "Достык",
              "map": {"lat": 43.23, "lon": 76.95},
              "photos": [{"src": "one"}, {"src": "two"}]
            }};
          </script>
        </body></html>
        """
        scraper = ApartmentScraper()
        try:
            item = scraper.parse_apartment_html(
                "https://krisha.kz/a/show/123", html
            )
        finally:
            scraper.session.close()

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["price"], 400000)
        self.assertEqual(item["rental_period"], "monthly")
        self.assertEqual(item["listing_id"], 123)
        self.assertEqual(item["rooms_structured"], 2)
        self.assertEqual(item["area_m2_structured"], 54)
        self.assertEqual(item["photo_count"], 2)
        self.assertEqual(item["Квартира меблирована"], "полностью")

    def test_city_and_room_are_encoded_in_category_url(self) -> None:
        scraper = ApartmentScraper()
        try:
            url = scraper.category_page_url(
                "rent_monthly", 7, city="almaty", rooms="5.100"
            )
        finally:
            scraper.session.close()

        self.assertIn("/arenda/kvartiry/almaty/", url)
        self.assertIn("page=7", url)
        self.assertIn("das%5Blive.rooms%5D=5.100", url)


if __name__ == "__main__":
    unittest.main()
