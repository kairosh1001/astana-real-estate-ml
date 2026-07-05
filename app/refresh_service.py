from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

from app.database import (
    connect,
    create_monitoring_snapshot,
    finish_refresh_run,
    fetch_running_refresh,
    init_db,
    iter_unique_urls,
    mark_refresh_started,
    mark_stale_listings,
    start_refresh_run,
    upsert_listing_prediction,
)
from app.prediction_service import PredictionService
from scrape import ApartmentScraper


@dataclass(frozen=True)
class RefreshResult:
    run_id: int
    status: str
    pages_seen: int
    urls_seen: int
    listings_processed: int
    listings_failed: int
    error: str | None = None


def run_refresh(
    *,
    root: Path | str,
    db_path: Path | str,
    kind: str = "manual",
    start_page: int = 1,
    pages: int = 50,
    min_delay: float = 1.0,
    max_delay: float = 2.0,
    stale_after_missed: int = 3,
    max_listings: int = 0,
) -> RefreshResult:
    root_path = Path(root)
    end_page = start_page + pages - 1
    connection = connect(db_path)
    init_db(connection)
    running_refresh = fetch_running_refresh(connection)
    if running_refresh:
        connection.close()
        raise RuntimeError(
            f"Refresh уже выполняется: run #{running_refresh['id']}."
        )
    run_id = start_refresh_run(
        connection,
        kind=kind,
        start_page=start_page,
        end_page=end_page,
    )
    mark_refresh_started(connection)

    scraper = ApartmentScraper()
    prediction_service = PredictionService(root_path)
    pages_seen = 0
    urls_seen = 0
    processed = 0
    failed = 0
    status = "completed"
    error = None

    try:
        for page in range(start_page, end_page + 1):
            stop_requested = False
            page_url = f"{scraper.base_url}/prodazha/kvartiry/astana/?page={page}"
            print(f"[INFO] Page {page}: {page_url}")
            urls = iter_unique_urls(scraper.get_listing_urls(page_url))
            if not urls:
                print(f"[WARN] No URLs found on page {page}; stopping refresh.")
                break

            pages_seen += 1
            urls_seen += len(urls)
            print(f"[INFO] Found {len(urls)} listing URLs.")

            for index, url in enumerate(urls, start=1):
                if max_listings and processed >= max_listings:
                    print("[INFO] Max listing limit reached.")
                    stop_requested = True
                    break

                print(f"[INFO] Fetching {index}/{len(urls)}: {url}")
                raw_listing = scraper.parse_apartment_page(url)
                if not raw_listing:
                    failed += 1
                    print(f"[WARN] Failed to parse listing: {url}")
                    continue

                try:
                    prediction = prediction_service.predict_raw_listing(
                        raw_listing,
                        url=url,
                    )
                    upsert_listing_prediction(
                        connection,
                        raw_listing=raw_listing,
                        prediction=prediction,
                    )
                    processed += 1
                except Exception as exc:
                    failed += 1
                    print(f"[WARN] Failed to predict listing {url}: {exc}")

                if max_delay > 0:
                    time.sleep(random.uniform(min_delay, max_delay))

            if stop_requested:
                break
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        scraper.session.close()
        mark_stale_listings(connection, stale_after_missed=stale_after_missed)
        finish_refresh_run(
            connection,
            run_id,
            pages_seen=pages_seen,
            urls_seen=urls_seen,
            listings_processed=processed,
            listings_failed=failed,
            status=status,
            error=error,
        )
        create_monitoring_snapshot(connection, run_id=run_id)
        connection.close()
        print(
            "[INFO] Refresh complete: "
            f"pages={pages_seen}, urls={urls_seen}, processed={processed}, failed={failed}"
        )

    return RefreshResult(
        run_id=run_id,
        status=status,
        pages_seen=pages_seen,
        urls_seen=urls_seen,
        listings_processed=processed,
        listings_failed=failed,
        error=error,
    )
