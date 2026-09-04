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
    recover_abandoned_refreshes,
    start_refresh_run,
    upsert_listing_prediction,
    utc_now,
)
from app.prediction_service import PredictionService
from app.cities import city_config, infer_listing_city, normalize_city_slug
from scrape import ApartmentScraper


# 468 is a nonstandard rejection observed on Krisha listing requests from the VPS.
# Stop this run instead of retrying other listings or rotating sessions.
UPSTREAM_STOP_STATUSES = frozenset({401, 403, 429, 468})


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
    city: str = "astana",
    kind: str = "manual",
    start_page: int = 1,
    pages: int = 50,
    min_delay: float = 1.0,
    max_delay: float = 2.0,
    stale_after_missed: int = 3,
    max_listings: int = 0,
    empty_page_retries: int = 3,
    max_consecutive_empty_pages: int = 3,
    max_consecutive_listing_failures: int = 10,
) -> RefreshResult:
    root_path = Path(root)
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    end_page = start_page + pages - 1
    connection = connect(db_path)
    init_db(connection)
    recovered = recover_abandoned_refreshes(connection)
    if recovered:
        print(f"[WARN] Recovered {recovered} interrupted refresh run(s).")
    running_refresh = fetch_running_refresh(connection)
    if running_refresh:
        connection.close()
        raise RuntimeError(
            f"Refresh уже выполняется: run #{running_refresh['id']}."
        )
    refresh_started_at = utc_now()
    run_id = start_refresh_run(
        connection,
        city=city_slug,
        kind=kind,
        start_page=start_page,
        end_page=end_page,
    )
    scraper = None
    pages_seen = 0
    urls_seen = 0
    processed = 0
    failed = 0
    status = "completed"
    error = None
    consecutive_empty_pages = 0
    empty_pages = 0
    consecutive_listing_failures = 0
    attempted = 0
    seen_urls: set[str] = set()
    failure_examples: list[str] = []

    try:
        # Initialization failures must close the run as failed, not leave it running.
        scraper = ApartmentScraper()
        prediction_service = PredictionService(root_path)
        for page in range(start_page, end_page + 1):
            stop_requested = False
            page_url = (
                f"{scraper.base_url}/prodazha/kvartiry/"
                f"{selected_city['krisha_slug']}/?page={page}"
            )
            print(f"[INFO] Page {page}: {page_url}")
            urls = []
            for attempt in range(1, max(empty_page_retries, 1) + 1):
                urls = iter_unique_urls(scraper.get_listing_urls(page_url))
                if urls:
                    break
                if attempt < max(empty_page_retries, 1):
                    if scraper.last_fetch_status in UPSTREAM_STOP_STATUSES:
                        break
                    retry_delay = min(10 * attempt, 30)
                    print(
                        f"[WARN] No URLs on page {page}; retry "
                        f"{attempt}/{empty_page_retries} in {retry_delay}s."
                    )
                    scraper.reset_session()
                    time.sleep(retry_delay)
            if not urls:
                empty_pages += 1
                consecutive_empty_pages += 1
                page_error = scraper.last_fetch_error or "No listing links in response"
                if len(failure_examples) < 3:
                    failure_examples.append(f"Page {page}: {page_error}")
                print(
                    f"[WARN] Page {page} remained empty after retries "
                    f"({consecutive_empty_pages}/{max_consecutive_empty_pages} consecutive)."
                )
                if (
                    consecutive_empty_pages >= max(max_consecutive_empty_pages, 1)
                    or scraper.last_fetch_status in UPSTREAM_STOP_STATUSES
                ):
                    print("[WARN] Too many consecutive empty pages; stopping refresh.")
                    status = "partial"
                    error = (
                        "Refresh остановлен после нескольких пустых страниц Krisha; "
                        f"последняя страница: {page}. {page_error}"
                    )
                    break
                continue

            consecutive_empty_pages = 0
            pages_seen += 1
            urls = [url for url in urls if url not in seen_urls]
            seen_urls.update(urls)
            urls_seen += len(urls)
            print(f"[INFO] Found {len(urls)} listing URLs.")

            for index, url in enumerate(urls, start=1):
                if max_listings and attempted >= max_listings:
                    print("[INFO] Max listing limit reached.")
                    stop_requested = True
                    break

                print(f"[INFO] Fetching {index}/{len(urls)}: {url}")
                attempted += 1
                stage = "parse"
                try:
                    raw_listing = scraper.parse_apartment_page(url)
                    if not raw_listing:
                        raise ValueError(scraper.last_parse_error or "No listing data")
                    raw_listing["scrape_city"] = city_slug
                    listing_city = infer_listing_city(raw_listing, default=city_slug)
                    if listing_city != city_slug:
                        print(
                            f"[WARN] Skipping cross-city listing from {listing_city}: {url}"
                        )
                        continue
                    stage = "predict"
                    prediction = prediction_service.predict_raw_listing(
                        raw_listing,
                        url=url,
                    )
                    stage = "store"
                    upsert_listing_prediction(
                        connection,
                        raw_listing=raw_listing,
                        prediction=prediction,
                    )
                    processed += 1
                    consecutive_listing_failures = 0
                except Exception as exc:
                    # A failed write must not retain a SQLite writer lock.
                    connection.rollback()
                    failed += 1
                    consecutive_listing_failures += 1
                    detail = f"{stage}: {type(exc).__name__}: {exc}"[:1000]
                    if len(failure_examples) < 3:
                        failure_examples.append(f"{url}: {detail}")
                    print(f"[WARN] Failed listing {url}: {detail}")
                    blocked = stage == "parse" and scraper.last_fetch_status in UPSTREAM_STOP_STATUSES
                    if blocked or consecutive_listing_failures >= max(1, max_consecutive_listing_failures):
                        status = "partial" if processed else "failed"
                        error = (
                            f"Refresh остановлен: {consecutive_listing_failures} ошибок подряд. "
                            f"Последняя ошибка: {detail}"
                        )
                        if blocked:
                            error = (
                                f"Krisha отклонила запрос: HTTP {scraper.last_fetch_status}. "
                                "Обновление остановлено; проверьте доступ с сервера "
                                "и согласуйте автоматическую загрузку с источником. "
                                f"Последняя ошибка: {detail}"
                            )
                        stop_requested = True
                        break
                finally:
                    # Failed responses need pacing too; never hammer a failing upstream.
                    if max_delay > 0:
                        time.sleep(random.uniform(min_delay, max_delay))

            if stop_requested:
                break
        if not processed:
            status = "failed"
            error = error or "Не обработано ни одного объявления."
        elif failed or empty_pages:
            status = "partial"
        if failure_examples:
            error = ((error + " " if error else "") + " | ".join(failure_examples))[:3500]
    except Exception as exc:
        status = "failed"
        error = str(exc)
        raise
    finally:
        if scraper is not None:
            scraper.session.close()
        try:
            # Only a complete, error-free weekly scan may age unseen listings.
            if kind == "weekly" and status == "completed" and start_page == 1 and not max_listings:
                mark_refresh_started(
                    connection, city=city_slug, seen_since=refresh_started_at,
                )
                mark_stale_listings(
                    connection,
                    city=city_slug,
                    stale_after_missed=stale_after_missed,
                )
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
            try:
                create_monitoring_snapshot(connection, run_id=run_id)
            except Exception as exc:
                print(f"[WARN] Could not create monitoring snapshot: {exc}")
        finally:
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
