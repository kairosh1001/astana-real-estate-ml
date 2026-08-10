from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrape import ApartmentScraper


BASE_PATH = "/prodazha/kvartiry/almaty/"
DEFAULT_OUTPUT = ROOT / "data" / "almaty_sale_raw.csv"
DEFAULT_TARGET = 20_000
SCHEMA_VERSION = 1

# Krisha caps a search result at 1,000 pages. Splitting by room count keeps the
# searches disjoint and lets the collector pass the cap without price ranges
# that could become stale while a long crawl is running.
ROOM_PARTITIONS = (
    ("rooms_1", "1"),
    ("rooms_2", "2"),
    ("rooms_3", "3"),
    ("rooms_4", "4"),
    ("rooms_5_plus", "5.100"),
)


class AlmatyApartmentScraper(ApartmentScraper):
    """Apartment scraper that can skip unnecessary ЖК developer requests."""

    def __init__(self, *, timeout: int, fetch_developers: bool) -> None:
        super().__init__(timeout=timeout)
        self.fetch_developers = fetch_developers

    def fetch_complex_developer(self, complex_url: str) -> str | None:
        if not self.fetch_developers:
            return None
        return super().fetch_complex_developer(complex_url)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect at least 20,000 unique Almaty apartment-sale listings "
            "from Krisha.kz with checkpoints and automatic resume."
        )
    )
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--state",
        type=Path,
        help="Resume-state JSON path (default: next to --output).",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--min-delay", type=float, default=0.8)
    parser.add_argument("--max-delay", type=float, default=1.5)
    parser.add_argument("--min-page-delay", type=float, default=2.0)
    parser.add_argument("--max-page-delay", type=float, default=4.0)
    parser.add_argument("--checkpoint-every", type=int, default=200)
    parser.add_argument("--max-pages-per-partition", type=int, default=1000)
    parser.add_argument("--failure-retry-passes", type=int, default=2)
    parser.add_argument(
        "--fetch-developers",
        action="store_true",
        help=(
            "Also visit ЖК pages to fill missing developer names. Disabled by "
            "default because it adds many requests and is not used by the model."
        ),
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.target < DEFAULT_TARGET:
        raise ValueError(f"--target must be at least {DEFAULT_TARGET}")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.checkpoint_every <= 0:
        raise ValueError("--checkpoint-every must be positive")
    if args.max_pages_per_partition <= 0:
        raise ValueError("--max-pages-per-partition must be positive")
    if args.failure_retry_passes < 0:
        raise ValueError("--failure-retry-passes cannot be negative")
    for minimum, maximum, label in (
        (args.min_delay, args.max_delay, "listing delay"),
        (args.min_page_delay, args.max_page_delay, "page delay"),
    ):
        if minimum < 0 or maximum < minimum:
            raise ValueError(f"Invalid {label} bounds")


def canonical_listing_url(base_url: str, href: str) -> str:
    parsed = urlparse(urljoin(base_url, href))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def listing_id_from_url(url: str) -> int | None:
    match = re.search(r"/(\d+)/?$", urlparse(url).path)
    return int(match.group(1)) if match else None


def build_partition_url(base_url: str, room_value: str, page: int) -> str:
    query = urlencode(
        [("das[live.rooms]", room_value), ("page", str(page))]
    )
    return f"{base_url}{BASE_PATH}?{query}"


def parse_listing_page(base_url: str, page_url: str, html: str) -> tuple[list[str], int | None]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for link in soup.select(".a-card__title[href]"):
        href = link.get("href")
        if not href:
            continue
        url = canonical_listing_url(base_url, href)
        if url not in seen:
            seen.add(url)
            urls.append(url)

    page_numbers: list[int] = []
    expected_path = urlparse(page_url).path.rstrip("/")
    for link in soup.select("a[href]"):
        parsed = urlparse(urljoin(page_url, link.get("href") or ""))
        if parsed.path.rstrip("/") != expected_path:
            continue
        for raw_page in parse_qs(parsed.query).get("page", []):
            try:
                page_numbers.append(int(raw_page))
            except (TypeError, ValueError):
                continue
    return urls, max(page_numbers, default=None)


def load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path, dtype=object, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if "url" not in frame.columns:
        raise ValueError(f"Existing output has no url column: {path}")
    return deduplicate(frame)


def deduplicate(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    result["url"] = result["url"].astype("string").str.strip()
    result = result[result["url"].notna() & result["url"].ne("")]
    if "scraped_at" in result.columns:
        result["_scraped_order"] = pd.to_datetime(
            result["scraped_at"], errors="coerce", utc=True
        )
        result = result.sort_values("_scraped_order", kind="stable")
        result = result.drop(columns="_scraped_order")
    return result.drop_duplicates(subset="url", keep="last").reset_index(drop=True)


def ordered_columns(frame: pd.DataFrame) -> list[str]:
    priority = [
        "url",
        "listing_id",
        "title",
        "price",
        "scrape_city",
        "scraped_at",
        "scrape_partition",
        "scrape_schema_version",
    ]
    return [column for column in priority if column in frame.columns] + sorted(
        column for column in frame.columns if column not in priority
    )


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    frame.to_csv(
        temporary,
        index=False,
        encoding="utf-8-sig",
        columns=ordered_columns(frame),
    )
    os.replace(temporary, path)


def atomic_write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "partitions": {
            name: {"next_page": 1, "last_page": None, "complete": False}
            for name, _ in ROOM_PARTITIONS
        },
        "failed_urls": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    state = default_state()
    if not path.exists():
        return state
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        raise ValueError(f"Invalid resume state: {path}")
    for name, _ in ROOM_PARTITIONS:
        saved_partition = (saved.get("partitions") or {}).get(name)
        if isinstance(saved_partition, dict):
            state["partitions"][name].update(saved_partition)
    failed = saved.get("failed_urls")
    if isinstance(failed, dict):
        state["failed_urls"] = failed
    return state


def merge_rows(existing: pd.DataFrame, rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return existing
    return deduplicate(
        pd.concat([existing, pd.DataFrame(rows)], ignore_index=True, sort=False)
    )


def unique_urls(frame: pd.DataFrame) -> set[str]:
    if frame.empty or "url" not in frame.columns:
        return set()
    return set(frame["url"].dropna().astype(str))


def quality_summary(frame: pd.DataFrame) -> dict[str, Any]:
    def missing(column: str) -> int:
        if column not in frame.columns:
            return len(frame)
        values = frame[column].astype("string").str.strip()
        return int(values.isna().sum() + values.isin(["", "N/A"]).sum())

    return {
        "unique_listings": len(unique_urls(frame)),
        "missing_price": missing("price"),
        "missing_lat": missing("lat"),
        "missing_lon": missing("lon"),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args()
    validate_args(args)
    output = args.output.resolve()
    state_path = (args.state or output.with_suffix(".state.json")).resolve()
    existing = load_existing(output)
    state = load_state(state_path)
    saved_urls = unique_urls(existing)
    pending_rows: list[dict[str, Any]] = []
    pending_urls: set[str] = set()
    failed_urls: dict[str, int] = {
        str(url): int(attempts)
        for url, attempts in state.get("failed_urls", {}).items()
    }
    scraper = AlmatyApartmentScraper(
        timeout=args.timeout,
        fetch_developers=args.fetch_developers,
    )

    def total_unique() -> int:
        return len(saved_urls | pending_urls)

    def save_checkpoint() -> None:
        nonlocal existing, pending_rows, saved_urls
        existing = merge_rows(existing, pending_rows)
        if not existing.empty:
            atomic_write_csv(existing, output)
        saved_urls = unique_urls(existing)
        pending_rows = []
        pending_urls.clear()
        state["failed_urls"] = dict(sorted(failed_urls.items()))
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["unique_listings"] = len(saved_urls)
        atomic_write_json(state, state_path)
        print(f"[CHECKPOINT] {len(saved_urls):,} unique listings -> {output}")

    def fetch_detail(url: str, partition_name: str) -> bool:
        row = scraper.parse_apartment_page(url)
        if not row:
            failed_urls[url] = failed_urls.get(url, 0) + 1
            return False
        row["url"] = canonical_listing_url(scraper.base_url, str(row.get("url") or url))
        row["listing_id"] = listing_id_from_url(row["url"])
        row["scrape_city"] = "almaty"
        row["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["scrape_partition"] = partition_name
        row["scrape_schema_version"] = SCHEMA_VERSION
        pending_rows.append(row)
        pending_urls.add(row["url"])
        failed_urls.pop(url, None)
        return True

    print(f"[INFO] Existing unique listings: {len(saved_urls):,}")
    print(f"[INFO] Target: {args.target:,}; output: {output}")
    if len(saved_urls) >= args.target:
        print("[OK] Target is already satisfied; nothing to scrape.")
        print(json.dumps(quality_summary(existing), ensure_ascii=False, indent=2))
        scraper.session.close()
        return

    interrupted = False
    try:
        for partition_name, room_value in ROOM_PARTITIONS:
            if total_unique() >= args.target:
                break
            progress = state["partitions"][partition_name]
            if progress.get("complete"):
                continue
            page = max(1, int(progress.get("next_page") or 1))
            stale_pages = 0
            while page <= args.max_pages_per_partition and total_unique() < args.target:
                page_url = build_partition_url(scraper.base_url, room_value, page)
                html = scraper.fetch_page(page_url)
                if not html:
                    raise RuntimeError(
                        f"Could not fetch search page {partition_name}/{page}. "
                        "Progress was saved; rerun the same command to resume."
                    )
                page_urls, advertised_last_page = parse_listing_page(
                    scraper.base_url, page_url, html
                )
                if advertised_last_page is not None:
                    progress["last_page"] = min(
                        int(advertised_last_page), args.max_pages_per_partition
                    )
                if not page_urls:
                    progress["complete"] = True
                    print(f"[INFO] {partition_name}: empty page {page}; segment complete")
                    break

                new_inventory_urls = [
                    url
                    for url in page_urls
                    if url not in saved_urls and url not in pending_urls
                ]
                stale_pages = stale_pages + 1 if not new_inventory_urls else 0
                print(
                    f"[INFO] {partition_name} page {page}/"
                    f"{progress.get('last_page') or '?'}: {len(page_urls)} URLs, "
                    f"{len(new_inventory_urls)} new; total={total_unique():,}"
                )

                for url in new_inventory_urls:
                    if total_unique() >= args.target:
                        break
                    if fetch_detail(url, partition_name):
                        print(f"[OK] {total_unique():,}/{args.target:,} {url}")
                    else:
                        print(f"[WARN] Detail parse failed: {url}")
                    if args.max_delay:
                        time.sleep(random.uniform(args.min_delay, args.max_delay))

                page += 1
                progress["next_page"] = page
                last_page = progress.get("last_page")
                if last_page is not None and page > int(last_page):
                    progress["complete"] = True
                if stale_pages >= 3:
                    progress["complete"] = True
                    print(f"[WARN] {partition_name}: 3 pages without new URLs; segment stopped")
                if (
                    len(pending_rows) >= args.checkpoint_every
                    or progress.get("complete")
                    or total_unique() >= args.target
                ):
                    save_checkpoint()
                if progress.get("complete"):
                    break
                if args.max_page_delay:
                    time.sleep(
                        random.uniform(args.min_page_delay, args.max_page_delay)
                    )

        for retry_pass in range(1, args.failure_retry_passes + 1):
            if total_unique() >= args.target or not failed_urls:
                break
            retry_urls = list(failed_urls)
            print(f"[INFO] Failure retry pass {retry_pass}: {len(retry_urls):,} URLs")
            for url in retry_urls:
                if total_unique() >= args.target:
                    break
                if fetch_detail(url, "retry"):
                    print(f"[RECOVERED] {total_unique():,}/{args.target:,} {url}")
                if args.max_delay:
                    time.sleep(random.uniform(max(2.0, args.min_delay), max(4.0, args.max_delay)))
                if len(pending_rows) >= args.checkpoint_every:
                    save_checkpoint()
    except KeyboardInterrupt:
        interrupted = True
        print("\n[INFO] Ctrl+C received; saving progress before exit...")
    finally:
        try:
            save_checkpoint()
        finally:
            scraper.session.close()

    summary = quality_summary(existing)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if interrupted:
        print(f"[STOPPED] Progress saved. Rerun the same command to continue: {state_path}")
        return
    if summary["unique_listings"] < args.target:
        raise RuntimeError(
            f"Collected {summary['unique_listings']:,} unique listings, below the "
            f"{args.target:,} target. Rerun the same command: completed segments "
            "will be skipped and pending failures retried."
        )
    print(f"[DONE] Collected {summary['unique_listings']:,} unique Almaty listings")


if __name__ == "__main__":
    main()
