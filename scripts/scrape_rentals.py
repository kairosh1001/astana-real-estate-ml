from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import random
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrape import ApartmentScraper


SCRAPE_SCHEMA_VERSION = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Astana rental listings from Krisha.")
    parser.add_argument("--period", choices=["monthly", "daily"], required=True)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument(
        "--pages",
        type=int,
        default=10,
        help="Number of pages to crawl; use 0 to follow pagination through the final page.",
    )
    parser.add_argument("--max-listings", type=int, default=0)
    parser.add_argument("--min-delay", type=float, default=0.5)
    parser.add_argument("--max-delay", type=float, default=1.0)
    parser.add_argument("--failure-retry-passes", type=int, default=1)
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.90,
        help="Fail after checkpointing if fewer attempted detail pages were parsed successfully.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--status-output", type=Path)
    parser.add_argument("--append", action="store_true", help="Append daily snapshots to the output CSV.")
    return parser.parse_args()


def _snapshot_entity(frame: pd.DataFrame) -> pd.Series:
    listing_id = pd.to_numeric(frame.get("listing_id"), errors="coerce")
    entity = frame.get("url", pd.Series(pd.NA, index=frame.index)).astype("string").map(
        lambda value: f"url:{value}"
    )
    known = listing_id.notna()
    entity.loc[known] = listing_id.loc[known].astype("int64").astype(str).map(lambda value: f"id:{value}")
    return entity


def deduplicate_daily_snapshots(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep one record per listing and UTC scrape day.

    Repeated daily snapshots are useful for chronological validation; repeated
    invocations on the same day are not and would overweight those listings.
    """
    if frame.empty:
        return frame
    result = frame.copy()
    scraped = pd.to_datetime(result.get("scraped_at"), errors="coerce", utc=True)
    result["_scraped_order"] = scraped
    result["_scrape_day"] = scraped.dt.strftime("%Y-%m-%d").fillna("unknown")
    result["_snapshot_entity"] = _snapshot_entity(result)
    result = (
        result.sort_values("_scraped_order", kind="stable")
        .drop_duplicates(["_snapshot_entity", "_scrape_day"], keep="last")
        .drop(columns=["_scraped_order", "_scrape_day", "_snapshot_entity"])
        .reset_index(drop=True)
    )
    return result


def complete_urls_for_day(previous: pd.DataFrame, scrape_day: str) -> set[str]:
    if previous.empty or "scraped_at" not in previous or "url" not in previous:
        return set()
    days = pd.to_datetime(previous["scraped_at"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
    versions = pd.to_numeric(
        previous.get("scrape_schema_version", pd.Series(0, index=previous.index)),
        errors="coerce",
    ).fillna(0)
    mask = days.eq(scrape_day) & versions.ge(SCRAPE_SCHEMA_VERSION)
    return set(previous.loc[mask, "url"].dropna().astype(str))


def save_checkpoint(previous: pd.DataFrame, rows: list[dict], output: Path) -> pd.DataFrame:
    new = pd.DataFrame(rows)
    combined = pd.concat([previous, new], ignore_index=True, sort=False)
    combined = deduplicate_daily_snapshots(combined)
    combined.to_csv(output, index=False, encoding="utf-8-sig")
    return combined


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.pages < 0:
        raise ValueError("--pages must be zero or positive")
    if not 0 <= args.min_success_rate <= 1:
        raise ValueError("--min-success-rate must be between 0 and 1")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        raise ValueError("Delay bounds are invalid")

    category = f"rent_{args.period}"
    output = args.output or ROOT / "data" / f"rent_{args.period}_raw.csv"
    status_output = args.status_output or ROOT / "data" / f"rent_{args.period}_scrape_status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    status_output.parent.mkdir(parents=True, exist_ok=True)

    scraper = ApartmentScraper(timeout=30)
    previous = pd.read_csv(output) if args.append and output.exists() else pd.DataFrame()
    started_at = datetime.now(timezone.utc)
    scraped_at = started_at.isoformat(timespec="seconds")
    scrape_day = started_at.strftime("%Y-%m-%d")
    already_complete = complete_urls_for_day(previous, scrape_day)
    rows: list[dict] = []
    inventory_urls: set[str] = set()
    invocation_seen: set[str] = set()
    failed_urls: list[str] = []
    attempted = 0
    skipped_complete = 0
    discovered_last_page: int | None = None
    pages_visited = 0
    combined = previous.copy()

    def fetch_listing(url: str) -> bool:
        nonlocal attempted
        attempted += 1
        row = scraper.parse_apartment_page(url)
        if not row:
            return False
        if row.get("rental_period") != args.period:
            print(f"[WARN] Rejected wrong rental period: {url}")
            return False
        row["scraped_at"] = scraped_at
        row["scrape_schema_version"] = SCRAPE_SCHEMA_VERSION
        rows.append(row)
        return True

    try:
        page = args.start_page
        fixed_last_page = args.start_page + args.pages - 1 if args.pages else None
        while True:
            if fixed_last_page is not None and page > fixed_last_page:
                break
            if fixed_last_page is None and discovered_last_page is not None and page > discovered_last_page:
                break

            page_url = scraper.category_page_url(category, page)
            urls, advertised_last_page = scraper.get_listing_page(page_url)
            pages_visited += 1
            if advertised_last_page is not None:
                discovered_last_page = max(discovered_last_page or 0, advertised_last_page)
            expected_last = fixed_last_page or discovered_last_page
            print(
                f"[INFO] {args.period} page {page}/{expected_last or '?'}: "
                f"{len(urls)} URLs"
            )
            if not urls:
                if expected_last is not None and page < expected_last:
                    raise RuntimeError(f"Category page {page} was empty before advertised page {expected_last}")
                break

            for url in urls:
                inventory_urls.add(url)
                if url in invocation_seen:
                    continue
                invocation_seen.add(url)
                if url in already_complete:
                    skipped_complete += 1
                    continue
                if args.max_listings and len(rows) >= args.max_listings:
                    break
                if not fetch_listing(url):
                    failed_urls.append(url)
                    print(f"[WARN] Failed detail page: {url}")
                if args.max_delay > 0:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))

            combined = save_checkpoint(previous, rows, output)
            print(
                f"[CHECKPOINT] {len(rows)} parsed, {len(failed_urls)} pending failures, "
                f"{len(combined)} daily snapshots -> {output}"
            )
            if args.max_listings and len(rows) >= args.max_listings:
                break
            page += 1

        for retry_pass in range(1, args.failure_retry_passes + 1):
            if not failed_urls:
                break
            pending = failed_urls
            failed_urls = []
            print(f"[INFO] Detail retry pass {retry_pass}: {len(pending)} URLs")
            for url in pending:
                if not fetch_listing(url):
                    failed_urls.append(url)
                if args.max_delay > 0:
                    time.sleep(random.uniform(max(1.0, args.min_delay), max(2.0, args.max_delay)))
            combined = save_checkpoint(previous, rows, output)
    finally:
        scraper.session.close()
        if rows or not previous.empty:
            combined = save_checkpoint(previous, rows, output)

    unique_parsed_urls = len({str(row.get("url")) for row in rows})
    unique_attempted_urls = unique_parsed_urls + len(set(failed_urls))
    success_rate = unique_parsed_urls / unique_attempted_urls if unique_attempted_urls else 1.0
    status = {
        "period": args.period,
        "started_at": scraped_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scrape_day_utc": scrape_day,
        "scrape_schema_version": SCRAPE_SCHEMA_VERSION,
        "pages_visited": pages_visited,
        "advertised_last_page": discovered_last_page,
        "inventory_urls_seen": len(inventory_urls),
        "skipped_complete_same_day": skipped_complete,
        "detail_attempts_including_retries": attempted,
        "parsed_rows": len(rows),
        "failed_urls": failed_urls,
        "success_rate": success_rate,
        "snapshot_rows": len(combined),
        "unique_listings": int(_snapshot_entity(combined).nunique()) if not combined.empty else 0,
    }
    status_output.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if success_rate < args.min_success_rate:
        raise RuntimeError(
            f"Only {success_rate:.1%} of attempted detail requests succeeded; "
            f"minimum is {args.min_success_rate:.1%}"
        )
    print(f"[OK] Saved {len(rows)} parsed {args.period} rows to {output}")


if __name__ == "__main__":
    main()
