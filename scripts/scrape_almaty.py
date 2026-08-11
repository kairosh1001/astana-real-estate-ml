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


SUPPORTED_CITIES = ("almaty", "astana")
SCHEMA_VERSION = 2

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

# The collector works toward a useful room mix instead of stopping as soon as
# one global row count is reached. Existing rows count toward these targets.
DEFAULT_PARTITION_TARGETS = {
    "rooms_1": 10_000,
    "rooms_2": 12_000,
    "rooms_3": 10_000,
    "rooms_4": 6_000,
    "rooms_5_plus": 3_000,
}
DEFAULT_TOTAL_TARGET = sum(DEFAULT_PARTITION_TARGETS.values())


class CityApartmentScraper(ApartmentScraper):
    """City apartment scraper that can skip unnecessary ЖК developer requests."""

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
            "Collect a balanced room-count sample of city apartment-sale "
            "listings from Krisha.kz with checkpoints and automatic resume."
        )
    )
    parser.add_argument("--city", choices=SUPPORTED_CITIES, default="almaty")
    parser.add_argument(
        "--target",
        type=int,
        help=(
            "Optional balanced total target. It is distributed across room "
            f"partitions; the default quotas total {DEFAULT_TOTAL_TARGET:,}."
        ),
    )
    for partition_name, _ in ROOM_PARTITIONS:
        option = f"--{partition_name.replace('_', '-')}-target"
        parser.add_argument(
            option,
            dest=f"{partition_name}_target",
            type=int,
            help=f"Override the target for {partition_name}.",
        )
    parser.add_argument(
        "--only-partition",
        action="append",
        choices=[name for name, _ in ROOM_PARTITIONS],
        help=(
            "Collect only this room partition; repeat for multiple partitions. "
            "Other partitions and their state are left untouched."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output (default: data/<city>_sale_raw.csv).",
    )
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
    if args.target is not None and args.target < len(ROOM_PARTITIONS):
        raise ValueError(f"--target must be at least {len(ROOM_PARTITIONS)}")
    for partition_name, _ in ROOM_PARTITIONS:
        value = getattr(args, f"{partition_name}_target")
        if value is not None and value <= 0:
            raise ValueError(f"--{partition_name.replace('_', '-')}-target must be positive")
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


def scaled_partition_targets(total_target: int) -> dict[str, int]:
    """Scale the default room mix to an exact aggregate target."""
    weights = {
        name: DEFAULT_PARTITION_TARGETS[name] / DEFAULT_TOTAL_TARGET
        for name, _ in ROOM_PARTITIONS
    }
    raw = {name: total_target * weight for name, weight in weights.items()}
    targets = {name: max(1, int(value)) for name, value in raw.items()}
    difference = total_target - sum(targets.values())
    order = sorted(
        targets,
        key=lambda name: raw[name] - int(raw[name]),
        reverse=difference > 0,
    )
    step = 1 if difference > 0 else -1
    cursor = 0
    while difference:
        name = order[cursor % len(order)]
        if step > 0 or targets[name] > 1:
            targets[name] += step
            difference -= step
        cursor += 1
    return targets


def resolve_partition_targets(args: argparse.Namespace) -> dict[str, int]:
    targets = (
        scaled_partition_targets(args.target)
        if args.target is not None
        else dict(DEFAULT_PARTITION_TARGETS)
    )
    for partition_name, _ in ROOM_PARTITIONS:
        override = getattr(args, f"{partition_name}_target")
        if override is not None:
            targets[partition_name] = override
    return targets


def canonical_listing_url(base_url: str, href: str) -> str:
    parsed = urlparse(urljoin(base_url, href))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def listing_id_from_url(url: str) -> int | None:
    match = re.search(r"/(\d+)/?$", urlparse(url).path)
    return int(match.group(1)) if match else None


def build_partition_url(base_url: str, city: str, room_value: str, page: int) -> str:
    query = urlencode(
        [("das[live.rooms]", room_value), ("page", str(page))]
    )
    return f"{base_url}/prodazha/kvartiry/{city}/?{query}"


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


def partition_counts(frame: pd.DataFrame) -> dict[str, int]:
    counts = {name: 0 for name, _ in ROOM_PARTITIONS}
    if frame.empty or "scrape_partition" not in frame.columns:
        return counts
    observed = frame["scrape_partition"].astype("string").value_counts()
    for name in counts:
        counts[name] = int(observed.get(name, 0))
    return counts


def partition_status(count: int, target: int, inventory_complete: bool) -> str:
    if count >= target:
        return "quota_met"
    if inventory_complete:
        return "inventory_exhausted"
    return "incomplete"


def quality_summary(frame: pd.DataFrame) -> dict[str, Any]:
    def missing(column: str) -> int:
        if column not in frame.columns:
            return len(frame)
        values = frame[column].astype("string").str.strip()
        return int(values.isna().sum() + values.isin(["", "N/A"]).sum())

    return {
        "unique_listings": len(unique_urls(frame)),
        "partition_counts": partition_counts(frame),
        "missing_price": missing("price"),
        "missing_lat": missing("lat"),
        "missing_lon": missing("lon"),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args()
    validate_args(args)
    targets = resolve_partition_targets(args)
    selected_partitions = set(
        args.only_partition or [name for name, _ in ROOM_PARTITIONS]
    )
    output = (
        args.output or ROOT / "data" / f"{args.city}_sale_raw.csv"
    ).resolve()
    state_path = (args.state or output.with_suffix(".state.json")).resolve()
    existing = load_existing(output)
    state = load_state(state_path)
    saved_urls = unique_urls(existing)
    collected_by_partition = partition_counts(existing)
    pending_rows: list[dict[str, Any]] = []
    pending_urls: set[str] = set()
    failed_urls: dict[str, dict[str, Any]] = {}
    for url, saved_failure in state.get("failed_urls", {}).items():
        if isinstance(saved_failure, dict):
            failed_urls[str(url)] = {
                "attempts": int(saved_failure.get("attempts", 0)),
                "partition": str(saved_failure.get("partition") or "retry"),
            }
        else:
            # Backward compatibility with schema v1, which stored only attempts.
            failed_urls[str(url)] = {
                "attempts": int(saved_failure),
                "partition": "retry",
            }
    scraper = CityApartmentScraper(
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
        state["partition_targets"] = targets
        state["partition_counts"] = dict(collected_by_partition)
        atomic_write_json(state, state_path)
        print(f"[CHECKPOINT] {len(saved_urls):,} unique listings -> {output}")

    def fetch_detail(url: str, partition_name: str) -> bool:
        row = scraper.parse_apartment_page(url)
        if not row:
            previous = failed_urls.get(url, {})
            previous_partition = str(previous.get("partition") or partition_name)
            failed_urls[url] = {
                "attempts": int(previous.get("attempts", 0)) + 1,
                "partition": (
                    partition_name if partition_name != "retry" else previous_partition
                ),
            }
            return False
        row["url"] = canonical_listing_url(scraper.base_url, str(row.get("url") or url))
        row["listing_id"] = listing_id_from_url(row["url"])
        row["scrape_city"] = args.city
        row["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["scrape_partition"] = partition_name
        row["scrape_schema_version"] = SCHEMA_VERSION
        pending_rows.append(row)
        pending_urls.add(row["url"])
        if partition_name in collected_by_partition:
            collected_by_partition[partition_name] += 1
        failed_urls.pop(url, None)
        return True

    print(f"[INFO] Existing unique listings: {len(saved_urls):,}")
    print(f"[INFO] Balanced target: {sum(targets.values()):,}; output: {output}")
    for partition_name, _ in ROOM_PARTITIONS:
        print(
            f"[INFO] {partition_name}: {collected_by_partition[partition_name]:,}/"
            f"{targets[partition_name]:,}"
        )
    already_satisfied = all(
        collected_by_partition[name] >= targets[name]
        or bool(state["partitions"][name].get("complete"))
        for name, _ in ROOM_PARTITIONS
        if name in selected_partitions
    )
    if already_satisfied:
        print("[OK] Every room partition met its quota or exhausted its inventory.")
        print(json.dumps(quality_summary(existing), ensure_ascii=False, indent=2))
        scraper.session.close()
        return

    interrupted = False
    try:
        for partition_name, room_value in ROOM_PARTITIONS:
            if partition_name not in selected_partitions:
                print(f"[INFO] {partition_name}: not selected; skipped")
                continue
            progress = state["partitions"][partition_name]
            partition_target = targets[partition_name]
            if collected_by_partition[partition_name] >= partition_target:
                print(
                    f"[OK] {partition_name}: quota already met "
                    f"({collected_by_partition[partition_name]:,}/{partition_target:,})"
                )
                continue
            if progress.get("complete"):
                print(
                    f"[INFO] {partition_name}: inventory exhausted at "
                    f"{collected_by_partition[partition_name]:,}/{partition_target:,}"
                )
                continue
            page = max(1, int(progress.get("next_page") or 1))
            stale_pages = 0
            while (
                page <= args.max_pages_per_partition
                and collected_by_partition[partition_name] < partition_target
            ):
                page_url = build_partition_url(
                    scraper.base_url, args.city, room_value, page
                )
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
                    if collected_by_partition[partition_name] >= partition_target:
                        break
                    if fetch_detail(url, partition_name):
                        print(
                            f"[OK] {partition_name} "
                            f"{collected_by_partition[partition_name]:,}/"
                            f"{partition_target:,}; total={total_unique():,} {url}"
                        )
                    else:
                        print(f"[WARN] Detail parse failed: {url}")
                    if args.max_delay:
                        time.sleep(random.uniform(args.min_delay, args.max_delay))

                quota_met = collected_by_partition[partition_name] >= partition_target
                if quota_met:
                    # Revisit this page if a future run raises the quota. Already
                    # saved URLs will be skipped, so unprocessed cards are not lost.
                    progress["next_page"] = page
                else:
                    page += 1
                    progress["next_page"] = page
                last_page = progress.get("last_page")
                if not quota_met and last_page is not None and page > int(last_page):
                    progress["complete"] = True
                if stale_pages >= 3:
                    progress["complete"] = True
                    print(f"[WARN] {partition_name}: 3 pages without new URLs; segment stopped")
                if (
                    len(pending_rows) >= args.checkpoint_every
                    or progress.get("complete")
                    or quota_met
                ):
                    save_checkpoint()
                if progress.get("complete") or quota_met:
                    break
                if args.max_page_delay:
                    time.sleep(
                        random.uniform(args.min_page_delay, args.max_page_delay)
                    )

        for retry_pass in range(1, args.failure_retry_passes + 1):
            if not failed_urls:
                break
            retry_urls = list(failed_urls)
            print(f"[INFO] Failure retry pass {retry_pass}: {len(retry_urls):,} URLs")
            for url in retry_urls:
                retry_partition = str(failed_urls[url].get("partition") or "retry")
                if fetch_detail(url, retry_partition):
                    print(f"[RECOVERED] total={total_unique():,} {url}")
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
    summary["partition_targets"] = targets
    summary["partition_status"] = {
        name: (
            partition_status(
                summary["partition_counts"][name],
                targets[name],
                bool(state["partitions"][name].get("complete")),
            )
            if name in selected_partitions
            else "not_selected"
        )
        for name, _ in ROOM_PARTITIONS
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if interrupted:
        print(f"[STOPPED] Progress saved. Rerun the same command to continue: {state_path}")
        return
    unfinished = [
        name
        for name, status in summary["partition_status"].items()
        if status == "incomplete"
    ]
    if unfinished:
        raise RuntimeError(
            f"Room partitions remain incomplete: {unfinished}. Rerun the same "
            "command; completed quotas will be skipped and progress will resume."
        )
    print(
        f"[DONE] Collected {summary['unique_listings']:,} unique {args.city} listings "
        "with every room quota met or available inventory exhausted"
    )


if __name__ == "__main__":
    main()
