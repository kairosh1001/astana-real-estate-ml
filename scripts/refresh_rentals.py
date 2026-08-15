from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import (
    connect,
    finish_rental_refresh_run,
    init_db,
    start_rental_refresh_run,
    upsert_rental_listing,
)
from scrape import ApartmentScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh current monthly-rent inventory.")
    parser.add_argument("--city", choices=["astana", "almaty"], default="astana")
    parser.add_argument("--pages", type=int, default=100)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "krisha.sqlite3")
    parser.add_argument("--min-delay", type=float, default=0.25)
    parser.add_argument("--max-delay", type=float, default=0.65)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    connection = connect(args.db)
    init_db(connection)
    run_id = start_rental_refresh_run(connection, city=args.city)
    scraper = ApartmentScraper(timeout=10, retry_total=1)
    pages_seen = urls_seen = processed = failed = 0
    seen: set[str] = set()
    try:
        connection.execute(
            "UPDATE rental_listings SET missed_refreshes=missed_refreshes+1 WHERE city=? AND status='active'",
            (args.city,),
        )
        for page in range(1, args.pages + 1):
            urls, advertised_last_page = scraper.get_listing_page(
                page,
                category="rent_monthly",
                city=args.city,
            )
            pages_seen += 1
            if not urls:
                break
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                urls_seen += 1
                try:
                    raw = scraper.parse_apartment_page(url)
                    if not raw or raw.get("rental_period") != "monthly":
                        failed += 1
                        continue
                    upsert_rental_listing(connection, raw_listing=raw, city=args.city)
                    processed += 1
                except Exception as exc:  # keep a long refresh useful after one bad card
                    failed += 1
                    print(f"failed {url}: {exc}")
                if processed % 25 == 0:
                    connection.commit()
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            print(f"page={page} urls={urls_seen} processed={processed} failed={failed}")
            if advertised_last_page and page >= advertised_last_page:
                break
        connection.execute(
            "UPDATE rental_listings SET status='stale' WHERE city=? AND missed_refreshes>=3",
            (args.city,),
        )
        connection.commit()
        finish_rental_refresh_run(
            connection, run_id, pages_seen=pages_seen, urls_seen=urls_seen,
            processed=processed, failed=failed,
        )
    except Exception as exc:
        connection.rollback()
        finish_rental_refresh_run(
            connection, run_id, pages_seen=pages_seen, urls_seen=urls_seen,
            processed=processed, failed=failed, status="failed", error=str(exc),
        )
        raise
    finally:
        scraper.session.close()
        connection.close()


if __name__ == "__main__":
    main()
