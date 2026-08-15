from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cities import infer_listing_city
from scrape import ApartmentScraper


ROOM_PARTITIONS = ("1", "2", "3", "4", "5.100")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape monthly Krisha rental listings for rental-value training."
    )
    parser.add_argument(
        "--cities",
        nargs="+",
        choices=("astana", "almaty"),
        default=["astana", "almaty"],
    )
    parser.add_argument(
        "--rooms",
        nargs="+",
        choices=ROOM_PARTITIONS,
        default=list(ROOM_PARTITIONS),
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=100,
        help="Maximum pages per city/room partition; use 0 to follow pagination.",
    )
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-listings", type=int, default=0)
    parser.add_argument("--min-delay", type=float, default=0.35)
    parser.add_argument("--max-delay", type=float, default=0.8)
    parser.add_argument("--append", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "rent_monthly_raw.csv",
    )
    parser.add_argument(
        "--status-output",
        type=Path,
        default=ROOT / "data" / "rent_monthly_scrape_status.json",
    )
    return parser.parse_args()


def _checkpoint(previous: pd.DataFrame, rows: list[dict], output: Path) -> pd.DataFrame:
    combined = pd.concat([previous, pd.DataFrame(rows)], ignore_index=True, sort=False)
    if combined.empty:
        return combined
    combined["_scraped_order"] = pd.to_datetime(
        combined.get("scraped_at"), errors="coerce", utc=True
    )
    combined = (
        combined.sort_values("_scraped_order", kind="stable")
        .drop_duplicates("url", keep="last")
        .drop(columns=["_scraped_order"])
        .reset_index(drop=True)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(output, index=False, encoding="utf-8-sig")
    return combined


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.pages < 0:
        raise ValueError("--pages must be zero or positive")
    if args.start_page < 1:
        raise ValueError("--start-page must be positive")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("Delay bounds are invalid")

    previous = (
        pd.read_csv(args.output)
        if args.append and args.output.exists()
        else pd.DataFrame()
    )
    already_known = set(previous.get("url", pd.Series(dtype="string")).dropna())
    scraper = ApartmentScraper(timeout=10, retry_total=1)
    rows: list[dict] = []
    failed_urls: set[str] = set()
    discovered_urls: set[str] = set()
    partition_stats: dict[str, dict] = {}
    started = datetime.now(timezone.utc)
    scraped_at = started.isoformat(timespec="seconds")
    combined = previous.copy()

    try:
        stop_requested = False
        for city in args.cities:
            if stop_requested:
                break
            for rooms in args.rooms:
                if stop_requested:
                    break
                partition = f"{city}_rooms_{rooms}"
                page = args.start_page
                final_page = (
                    args.start_page + args.pages - 1 if args.pages else None
                )
                last_page: int | None = None
                partition_urls: set[str] = set()
                parsed_before = len(rows)
                while True:
                    if final_page is not None and page > final_page:
                        break
                    if last_page is not None and page > last_page:
                        break
                    page_url = scraper.category_page_url(
                        "rent_monthly", page, city=city, rooms=rooms
                    )
                    try:
                        urls, advertised_last = scraper.get_listing_page(page_url)
                    except RuntimeError as exc:
                        print(f"[WARN] {partition} page {page}: {exc}")
                        scraper.reset_session()
                        break
                    if advertised_last is not None:
                        last_page = max(last_page or 0, advertised_last)
                    print(
                        f"[INFO] {partition} page {page}/{last_page or '?'}: "
                        f"{len(urls)} URLs"
                    )
                    if not urls:
                        break
                    new_on_page = 0
                    for url in urls:
                        discovered_urls.add(url)
                        partition_urls.add(url)
                        if url in already_known:
                            continue
                        if args.max_listings and len(rows) >= args.max_listings:
                            stop_requested = True
                            break
                        try:
                            raw = scraper.parse_apartment_page(url)
                        except Exception as exc:
                            print(f"[WARN] listing {url}: {exc}", flush=True)
                            scraper.reset_session()
                            failed_urls.add(url)
                            continue
                        if not raw:
                            failed_urls.add(url)
                            continue
                        raw["scrape_city"] = city
                        if raw.get("rental_period") != "monthly":
                            failed_urls.add(url)
                            continue
                        if infer_listing_city(raw, default=city) != city:
                            continue
                        raw["scraped_at"] = scraped_at
                        raw["scrape_partition"] = partition
                        rows.append(raw)
                        already_known.add(url)
                        new_on_page += 1
                        if args.max_delay > 0:
                            time.sleep(random.uniform(args.min_delay, args.max_delay))
                    combined = _checkpoint(previous, rows, args.output)
                    print(
                        f"[CHECKPOINT] {len(rows)} new rows, {len(combined)} unique "
                        f"listings -> {args.output}"
                    )
                    if stop_requested:
                        break
                    if new_on_page == 0 and all(url in already_known for url in urls):
                        if last_page is None or page >= last_page:
                            break
                    page += 1
                partition_stats[partition] = {
                    "advertised_last_page": last_page,
                    "urls_seen": len(partition_urls),
                    "parsed_rows": len(rows) - parsed_before,
                }
    finally:
        scraper.session.close()
        combined = _checkpoint(previous, rows, args.output)

    city_series = (
        combined["scrape_city"]
        if "scrape_city" in combined
        else pd.Series(dtype="string")
    )
    room_series = (
        pd.to_numeric(combined["rooms_structured"], errors="coerce")
        if "rooms_structured" in combined
        else pd.Series(dtype="float64")
    )
    city_counts = city_series.value_counts(dropna=False).to_dict()
    room_counts = room_series.value_counts(dropna=False).sort_index().to_dict()
    status = {
        "started_at": scraped_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": "monthly",
        "cities": args.cities,
        "room_partitions": args.rooms,
        "discovered_urls": len(discovered_urls),
        "new_rows": len(rows),
        "unique_listings": len(combined),
        "failed_urls": sorted(failed_urls),
        "city_counts": city_counts,
        "room_counts": {str(key): int(value) for key, value in room_counts.items()},
        "partitions": partition_stats,
    }
    args.status_output.parent.mkdir(parents=True, exist_ok=True)
    args.status_output.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
