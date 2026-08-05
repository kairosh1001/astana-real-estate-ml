from __future__ import annotations

import argparse
import os
from pathlib import Path
import random
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import connect, init_db, upsert_rental_prediction
from app.rental_prediction_service import RentalPredictionService
from scrape import ApartmentScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh monthly or daily rental predictions.")
    parser.add_argument("--period", choices=["monthly", "daily"], required=True)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--max-listings", type=int, default=0)
    parser.add_argument("--min-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=2.0)
    parser.add_argument("--dataset", type=Path, help="Import a previously scraped rental CSV instead of crawling.")
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(ROOT / "data" / "krisha.sqlite3")))
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    scraper = ApartmentScraper(timeout=20)
    model = RentalPredictionService(ROOT, args.period)
    connection = connect(args.db)
    init_db(connection)
    processed = failed = 0
    try:
        if args.dataset:
            rows = pd.read_csv(args.dataset).to_dict(orient="records")
            for raw in rows:
                try:
                    prediction = model.predict_raw_listing(raw)
                    upsert_rental_prediction(connection, raw_listing=raw, prediction=prediction)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    print(f"[WARN] {raw.get('url', 'unknown')}: {exc}")
            print(f"[OK] period={args.period} processed={processed} failed={failed} pilot={not model.production_ready}")
            return
        for page in range(args.start_page, args.start_page + args.pages):
            urls = scraper.get_listing_urls(scraper.category_page_url(f"rent_{args.period}", page))
            print(f"[INFO] {args.period} page {page}: {len(urls)} URLs")
            if not urls:
                break
            for url in urls:
                if args.max_listings and processed >= args.max_listings:
                    break
                raw = scraper.parse_apartment_page(url)
                if not raw or raw.get("rental_period") != args.period:
                    failed += 1
                    continue
                try:
                    prediction = model.predict_raw_listing(raw)
                    upsert_rental_prediction(connection, raw_listing=raw, prediction=prediction)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    print(f"[WARN] {url}: {exc}")
                if args.max_delay > 0:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
            if args.max_listings and processed >= args.max_listings:
                break
    finally:
        scraper.session.close()
        connection.close()
    print(f"[OK] period={args.period} processed={processed} failed={failed} pilot={not model.production_ready}")


if __name__ == "__main__":
    main()
