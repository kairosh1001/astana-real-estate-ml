from __future__ import annotations

import argparse
from datetime import datetime, timezone
import random
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrape import ApartmentScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Astana rental listings from Krisha.")
    parser.add_argument("--period", choices=["monthly", "daily"], required=True)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--max-listings", type=int, default=0)
    parser.add_argument("--min-delay", type=float, default=0.5)
    parser.add_argument("--max-delay", type=float, default=1.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--append", action="store_true", help="Append a new timestamped snapshot to the output CSV.")
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    category = f"rent_{args.period}"
    output = args.output or ROOT / "data" / f"rent_{args.period}_raw.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    scraper = ApartmentScraper(timeout=20)
    previous = pd.read_csv(output).to_dict(orient="records") if args.append and output.exists() else []
    rows: list[dict] = []
    seen: set[str] = set()
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for page in range(args.start_page, args.start_page + args.pages):
            page_url = scraper.category_page_url(category, page)
            urls = scraper.get_listing_urls(page_url)
            print(f"[INFO] {args.period} page {page}: {len(urls)} URLs")
            if not urls:
                break
            for url in urls:
                if url in seen:
                    continue
                if args.max_listings and len(rows) >= args.max_listings:
                    break
                seen.add(url)
                row = scraper.parse_apartment_page(url)
                if not row:
                    print(f"[WARN] Failed: {url}")
                    continue
                if row.get("rental_period") != args.period:
                    print(f"[WARN] Rejected wrong rental period: {url}")
                    continue
                row["scraped_at"] = scraped_at
                rows.append(row)
                if args.max_delay > 0:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
            combined = pd.DataFrame([*previous, *rows]).drop_duplicates(
                subset=["url", "scraped_at"],
                keep="last",
            )
            combined.to_csv(output, index=False, encoding="utf-8-sig")
            print(f"[CHECKPOINT] {len(rows)} new, {len(combined)} total snapshots -> {output}")
            if args.max_listings and len(rows) >= args.max_listings:
                break
    finally:
        scraper.session.close()

    print(f"[OK] Saved {len(rows)} new {args.period} listings to {output}")


if __name__ == "__main__":
    main()
