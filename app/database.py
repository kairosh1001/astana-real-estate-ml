from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.prediction_service import ListingPrediction


DEFAULT_DB_PATH = Path("data") / "krisha.sqlite3"
DISTRICT_OPTIONS = [
    {"slug": "yesil", "label": "Есиль"},
    {"slug": "nura", "label": "Нура"},
    {"slug": "saryarka", "label": "Сарыарка"},
    {"slug": "almaty", "label": "Алматы"},
    {"slug": "baikonyr", "label": "Байконур"},
    {"slug": "saraishyk", "label": "Сарайшык"},
]

_DISTRICT_ALIASES = {
    "yesil": {"есиль", "есильский"},
    "nura": {"нура"},
    "saryarka": {"сарыарка"},
    "almaty": {"алматы"},
    "baikonyr": {"байконур"},
    "saraishyk": {"сарайшык"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    journal_mode = os.getenv("SQLITE_JOURNAL_MODE", "WAL").upper()
    connection.execute(f"PRAGMA journal_mode={journal_mode}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            kind TEXT NOT NULL,
            start_page INTEGER NOT NULL,
            end_page INTEGER NOT NULL,
            pages_seen INTEGER NOT NULL DEFAULT 0,
            urls_seen INTEGER NOT NULL DEFAULT 0,
            listings_processed INTEGER NOT NULL DEFAULT 0,
            listings_failed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS listings (
            url TEXT PRIMARY KEY,
            title TEXT,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL,
            missed_refreshes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            listed_price REAL,
            area_m2 REAL,
            listed_price_per_m2 REAL,
            pred_price_per_m2_q10 REAL,
            pred_price_per_m2_q50 REAL,
            pred_price_per_m2_q90 REAL,
            pred_total_q50 REAL,
            discount_vs_asking_pct_conservative REAL,
            discount_vs_asking_pct_median REAL,
            interval_width_pct REAL
        );

        CREATE TABLE IF NOT EXISTS listing_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            listed_price REAL,
            listed_price_per_m2 REAL,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY(url) REFERENCES listings(url) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_price_history_url_observed
        ON listing_price_history(url, observed_at);

        CREATE TABLE IF NOT EXISTS model_monitoring_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            run_id INTEGER,
            total_listings INTEGER NOT NULL DEFAULT 0,
            active_listings INTEGER NOT NULL DEFAULT 0,
            below_market_active INTEGER NOT NULL DEFAULT 0,
            below_market_share REAL NOT NULL DEFAULT 0,
            median_listed_price_per_m2 REAL,
            median_pred_q50_per_m2 REAL,
            missing_year_share REAL NOT NULL DEFAULT 0,
            missing_coords_share REAL NOT NULL DEFAULT 0,
            unknown_district_share REAL NOT NULL DEFAULT 0,
            missing_complex_share REAL NOT NULL DEFAULT 0,
            scrape_failed_share REAL NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(run_id) REFERENCES refresh_runs(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_monitoring_snapshots_created
        ON model_monitoring_snapshots(created_at);

        CREATE TABLE IF NOT EXISTS prediction_cache (
            url TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            prediction_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_prediction_cache_created
        ON prediction_cache(created_at);

        CREATE TABLE IF NOT EXISTS request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            duration_ms REAL NOT NULL,
            client_hash TEXT NOT NULL,
            user_agent TEXT,
            referer TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_request_events_created
        ON request_events(created_at);

        CREATE INDEX IF NOT EXISTS idx_request_events_path_created
        ON request_events(path, created_at);

        CREATE TABLE IF NOT EXISTS feedback_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            email TEXT,
            message TEXT NOT NULL,
            client_hash TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_feedback_messages_created
        ON feedback_messages(created_at);

        CREATE TABLE IF NOT EXISTS telegram_subscribers (
            chat_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            last_digest_date TEXT
        );
        """
    )
    connection.commit()


def fetch_cached_prediction(
    connection: sqlite3.Connection,
    url: str,
    *,
    ttl_seconds: int,
) -> dict | None:
    row = connection.execute(
        """
        SELECT created_at, prediction_json
        FROM prediction_cache
        WHERE url = ?
        """,
        (url,),
    ).fetchone()
    if not row:
        return None

    try:
        created_at = datetime.fromisoformat(
            str(row["created_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if datetime.now(timezone.utc) - created_at > timedelta(seconds=ttl_seconds):
        return None

    try:
        payload = json.loads(row["prediction_json"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def store_cached_prediction(
    connection: sqlite3.Connection,
    *,
    url: str,
    prediction: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO prediction_cache (url, created_at, prediction_json)
        VALUES (?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            created_at = excluded.created_at,
            prediction_json = excluded.prediction_json
        """,
        (url, utc_now(), json.dumps(prediction, ensure_ascii=False, sort_keys=True)),
    )
    connection.commit()


def record_request_event(
    connection: sqlite3.Connection,
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_hash: str,
    user_agent: str | None,
    referer: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO request_events (
            created_at, method, path, status_code, duration_ms,
            client_hash, user_agent, referer
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            method[:12],
            path[:180],
            status_code,
            duration_ms,
            client_hash[:32],
            (user_agent or "")[:220],
            (referer or "")[:220],
        ),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(
        timespec="seconds"
    )
    connection.execute("DELETE FROM request_events WHERE created_at < ?", (cutoff,))
    connection.commit()


def fetch_traffic_summary(
    connection: sqlite3.Connection,
    *,
    limit: int = 30,
) -> dict:
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(
        timespec="seconds"
    )
    totals = connection.execute(
        """
        SELECT
            COUNT(*) AS requests_24h,
            COUNT(DISTINCT client_hash) AS visitors_24h,
            SUM(
                CASE
                    WHEN path IN ('/predict', '/predict-by-link', '/listing-details')
                    THEN 1 ELSE 0
                END
            ) AS predictions_24h,
            SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END) AS rate_limited_24h,
            AVG(duration_ms) AS avg_duration_ms_24h
        FROM request_events
        WHERE created_at >= ?
        """,
        (cutoff_24h,),
    ).fetchone()
    week = connection.execute(
        """
        SELECT
            COUNT(*) AS requests_7d,
            COUNT(DISTINCT client_hash) AS visitors_7d
        FROM request_events
        WHERE created_at >= ?
        """,
        (cutoff_7d,),
    ).fetchone()
    top_pages = connection.execute(
        """
        SELECT path, COUNT(*) AS requests, COUNT(DISTINCT client_hash) AS visitors
        FROM request_events
        WHERE created_at >= ?
        GROUP BY path
        ORDER BY requests DESC
        LIMIT ?
        """,
        (cutoff_24h, limit),
    ).fetchall()
    recent = connection.execute(
        """
        SELECT created_at, method, path, status_code, duration_ms, client_hash
        FROM request_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    slow = connection.execute(
        """
        SELECT created_at, method, path, status_code, duration_ms
        FROM request_events
        WHERE created_at >= ?
        ORDER BY duration_ms DESC
        LIMIT ?
        """,
        (cutoff_24h, min(limit, 10)),
    ).fetchall()
    return {
        **dict(totals),
        **dict(week),
        "top_pages": [dict(row) for row in top_pages],
        "recent_events": [dict(row) for row in recent],
        "slow_requests": [dict(row) for row in slow],
    }


def create_feedback_message(
    connection: sqlite3.Connection,
    *,
    email: str | None,
    message: str,
    client_hash: str,
) -> None:
    connection.execute(
        """
        INSERT INTO feedback_messages (created_at, email, message, client_hash)
        VALUES (?, ?, ?, ?)
        """,
        (utc_now(), email, message, client_hash[:32]),
    )
    connection.commit()


def fetch_feedback_messages(
    connection: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, created_at, email, message, client_hash
        FROM feedback_messages
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def delete_feedback_message(connection: sqlite3.Connection, feedback_id: int) -> bool:
    cursor = connection.execute(
        "DELETE FROM feedback_messages WHERE id = ?",
        (feedback_id,),
    )
    connection.commit()
    return cursor.rowcount > 0


def upsert_telegram_subscriber(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    notifications_enabled: bool = True,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO telegram_subscribers (
            chat_id, created_at, updated_at, notifications_enabled
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            notifications_enabled = excluded.notifications_enabled
        """,
        (chat_id, now, now, int(notifications_enabled)),
    )
    connection.commit()


def set_telegram_notifications(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    enabled: bool,
) -> None:
    connection.execute(
        """
        UPDATE telegram_subscribers
        SET updated_at = ?, notifications_enabled = ?
        WHERE chat_id = ?
        """,
        (utc_now(), int(enabled), chat_id),
    )
    connection.commit()


def fetch_telegram_subscribers_for_digest(
    connection: sqlite3.Connection,
    *,
    digest_date: str,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT chat_id, last_digest_date
        FROM telegram_subscribers
        WHERE notifications_enabled = 1
          AND (last_digest_date IS NULL OR last_digest_date != ?)
        ORDER BY created_at ASC
        """,
        (digest_date,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_telegram_digest_sent(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    digest_date: str,
) -> None:
    connection.execute(
        """
        UPDATE telegram_subscribers
        SET updated_at = ?, last_digest_date = ?
        WHERE chat_id = ?
        """,
        (utc_now(), digest_date, chat_id),
    )
    connection.commit()


def start_refresh_run(
    connection: sqlite3.Connection,
    *,
    kind: str,
    start_page: int,
    end_page: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO refresh_runs (started_at, kind, start_page, end_page)
        VALUES (?, ?, ?, ?)
        """,
        (utc_now(), kind, start_page, end_page),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_refresh_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    pages_seen: int,
    urls_seen: int,
    listings_processed: int,
    listings_failed: int,
    status: str = "completed",
    error: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE refresh_runs
        SET finished_at = ?,
            pages_seen = ?,
            urls_seen = ?,
            listings_processed = ?,
            listings_failed = ?,
            status = ?,
            error = ?
        WHERE id = ?
        """,
        (
            utc_now(),
            pages_seen,
            urls_seen,
            listings_processed,
            listings_failed,
            status,
            error,
            run_id,
        ),
    )
    connection.commit()


def mark_refresh_started(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE listings
        SET missed_refreshes = missed_refreshes + 1
        WHERE status = 'active'
        """
    )
    connection.commit()


def mark_stale_listings(
    connection: sqlite3.Connection,
    *,
    stale_after_missed: int = 3,
) -> None:
    connection.execute(
        """
        UPDATE listings
        SET status = 'stale'
        WHERE missed_refreshes >= ?
        """,
        (stale_after_missed,),
    )
    connection.commit()


def upsert_listing_prediction(
    connection: sqlite3.Connection,
    *,
    raw_listing: dict,
    prediction: ListingPrediction,
) -> None:
    now = utc_now()
    raw_json = json.dumps(raw_listing, ensure_ascii=False, sort_keys=True)
    values = asdict(prediction)
    connection.execute(
        """
        INSERT INTO listings (
            url, title, raw_json, first_seen_at, last_seen_at, last_checked_at,
            missed_refreshes, status, listed_price, area_m2, listed_price_per_m2,
            pred_price_per_m2_q10, pred_price_per_m2_q50, pred_price_per_m2_q90,
            pred_total_q50, discount_vs_asking_pct_conservative,
            discount_vs_asking_pct_median, interval_width_pct
        )
        VALUES (
            :url, :title, :raw_json, :now, :now, :now,
            0, 'active', :listed_price, :area_m2, :listed_price_per_m2,
            :pred_price_per_m2_q10, :pred_price_per_m2_q50,
            :pred_price_per_m2_q90, :pred_total_q50,
            :discount_vs_asking_pct_conservative,
            :discount_vs_asking_pct_median, :interval_width_pct
        )
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            raw_json = excluded.raw_json,
            last_seen_at = excluded.last_seen_at,
            last_checked_at = excluded.last_checked_at,
            missed_refreshes = 0,
            status = 'active',
            listed_price = excluded.listed_price,
            area_m2 = excluded.area_m2,
            listed_price_per_m2 = excluded.listed_price_per_m2,
            pred_price_per_m2_q10 = excluded.pred_price_per_m2_q10,
            pred_price_per_m2_q50 = excluded.pred_price_per_m2_q50,
            pred_price_per_m2_q90 = excluded.pred_price_per_m2_q90,
            pred_total_q50 = excluded.pred_total_q50,
            discount_vs_asking_pct_conservative =
                excluded.discount_vs_asking_pct_conservative,
            discount_vs_asking_pct_median =
                excluded.discount_vs_asking_pct_median,
            interval_width_pct = excluded.interval_width_pct
        """,
        {
            **values,
            "raw_json": raw_json,
            "now": now,
        },
    )
    connection.execute(
        """
        INSERT INTO listing_price_history (
            url, observed_at, listed_price, listed_price_per_m2, status
        )
        VALUES (?, ?, ?, ?, 'active')
        """,
        (
            prediction.url,
            now,
            prediction.listed_price,
            prediction.listed_price_per_m2,
        ),
    )
    connection.commit()


def fetch_undervalued(
    connection: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    polygon: list[tuple[float, float]] | None = None,
    new_since: str | None = None,
    min_discount_pct: float | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> list[dict]:
    status_clause = "" if include_stale else "AND status = 'active'"
    rows = connection.execute(
        f"""
        SELECT
            url,
            title,
            raw_json,
            status,
            first_seen_at,
            last_seen_at,
            listed_price,
            area_m2,
            listed_price_per_m2,
            pred_price_per_m2_q10,
            pred_price_per_m2_q50,
            pred_price_per_m2_q90,
            pred_total_q50,
            discount_vs_asking_pct_conservative,
            discount_vs_asking_pct_median,
            interval_width_pct
        FROM listings
        WHERE discount_vs_asking_pct_conservative > 0
          {status_clause}
        ORDER BY discount_vs_asking_pct_conservative DESC
        """,
    ).fetchall()
    items = [_prepare_undervalued_item(dict(row)) for row in rows]
    if districts:
        allowed_districts = set(districts)
        items = [
            item for item in items if item.get("district_slug") in allowed_districts
        ]
    if rooms:
        items = [item for item in items if item.get("rooms") == rooms]
    if max_price:
        items = [
            item
            for item in items
            if item.get("listed_price") is not None and item["listed_price"] <= max_price
        ]
    if min_year:
        items = [
            item
            for item in items
            if item.get("construction_year") and item["construction_year"] >= min_year
        ]
    if max_year:
        items = [
            item
            for item in items
            if item.get("construction_year") and item["construction_year"] <= max_year
        ]
    if residential_complex:
        complex_query = residential_complex.casefold()
        items = [
            item
            for item in items
            if complex_query in str(item.get("residential_complex") or "").casefold()
        ]
    if developer:
        developer_query = developer.casefold()
        items = [
            item
            for item in items
            if developer_query in str(item.get("developer") or "").casefold()
        ]
    if min_area:
        items = [
            item
            for item in items
            if item.get("area_m2") is not None and item["area_m2"] >= min_area
        ]
    if max_area:
        items = [
            item
            for item in items
            if item.get("area_m2") is not None and item["area_m2"] <= max_area
        ]
    if polygon and len(polygon) >= 3:
        items = [
            item
            for item in items
            if item.get("lat") is not None
            and item.get("lon") is not None
            and _point_in_polygon(item["lat"], item["lon"], polygon)
        ]
    if new_since:
        items = [
            item
            for item in items
            if _iso_datetime_at_or_after(item.get("first_seen_at"), new_since)
        ]
    if min_discount_pct:
        items = [
            item
            for item in items
            if item.get("discount_vs_asking_pct_conservative") is not None
            and item["discount_vs_asking_pct_conservative"] >= min_discount_pct
        ]
    items = _sort_undervalued_items(items, sort)
    return items[offset : offset + limit]


def count_undervalued(
    connection: sqlite3.Connection,
    *,
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    polygon: list[tuple[float, float]] | None = None,
    new_since: str | None = None,
    min_discount_pct: float | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> int:
    return len(
        fetch_undervalued(
            connection,
            limit=100000,
            offset=0,
            districts=districts,
            rooms=rooms,
            max_price=max_price,
            min_year=min_year,
            max_year=max_year,
            residential_complex=residential_complex,
            developer=developer,
            min_area=min_area,
            max_area=max_area,
            polygon=polygon,
            new_since=new_since,
            min_discount_pct=min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
    )


def fetch_listing_by_url(connection: sqlite3.Connection, url: str) -> dict | None:
    row = connection.execute(
        """
        SELECT
            url,
            title,
            raw_json,
            status,
            first_seen_at,
            last_seen_at,
            listed_price,
            area_m2,
            listed_price_per_m2,
            pred_price_per_m2_q10,
            pred_price_per_m2_q50,
            pred_price_per_m2_q90,
            pred_total_q50,
            discount_vs_asking_pct_conservative,
            discount_vs_asking_pct_median,
            interval_width_pct
        FROM listings
        WHERE url = ?
        """,
        (url,),
    ).fetchone()
    return _prepare_undervalued_item(dict(row)) if row else None


def fetch_listings_by_urls(
    connection: sqlite3.Connection,
    urls: list[str],
) -> list[dict]:
    result = []
    for url in urls[:5]:
        item = fetch_listing_by_url(connection, url)
        if item:
            result.append(item)
    return result


def fetch_price_history(
    connection: sqlite3.Connection,
    url: str,
    *,
    limit: int = 30,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT observed_at, listed_price, listed_price_per_m2, status
        FROM listing_price_history
        WHERE url = ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        (url, limit),
    ).fetchall()
    return list(reversed([dict(row) for row in rows]))


def fetch_complex_stats(
    connection: sqlite3.Connection,
    residential_complex: str | None,
) -> dict | None:
    query = _clean_text(residential_complex)
    if not query:
        return None

    rows = connection.execute(
        """
        SELECT raw_json, listed_price_per_m2, discount_vs_asking_pct_conservative
        FROM listings
        WHERE status = 'active'
          AND listed_price_per_m2 IS NOT NULL
        """
    ).fetchall()
    prices = []
    below_market = 0
    for row in rows:
        raw_listing = _load_raw_listing(row["raw_json"])
        complex_name = _clean_text(raw_listing.get("Жилой комплекс"))
        if complex_name.casefold() != query.casefold():
            continue
        prices.append(float(row["listed_price_per_m2"]))
        if (row["discount_vs_asking_pct_conservative"] or 0) > 0:
            below_market += 1

    if not prices:
        return None

    return {
        "name": query,
        "count": len(prices),
        "below_market": below_market,
        "min_price_per_m2": min(prices),
        "median_price_per_m2": _median(prices),
        "max_price_per_m2": max(prices),
    }


def fetch_market_dashboard(connection: sqlite3.Connection) -> dict:
    rows = connection.execute(
        """
        SELECT raw_json, listed_price_per_m2, discount_vs_asking_pct_conservative
        FROM listings
        WHERE status = 'active'
          AND listed_price_per_m2 IS NOT NULL
        """
    ).fetchall()
    district_prices: dict[str, list[float]] = {}
    district_below_market: dict[str, int] = {}
    complex_prices: dict[str, list[float]] = {}
    complex_below_market: dict[str, int] = {}

    for row in rows:
        raw_listing = _load_raw_listing(row["raw_json"])
        price = float(row["listed_price_per_m2"])
        is_below_market = (row["discount_vs_asking_pct_conservative"] or 0) > 0

        district_slug = normalize_district(raw_listing.get("Город"))
        district_label = district_label_for_slug(district_slug)
        district_prices.setdefault(district_label, []).append(price)
        if is_below_market:
            district_below_market[district_label] = (
                district_below_market.get(district_label, 0) + 1
            )

        complex_name = _clean_text(raw_listing.get("Жилой комплекс"))
        if complex_name:
            complex_prices.setdefault(complex_name, []).append(price)
            if is_below_market:
                complex_below_market[complex_name] = (
                    complex_below_market.get(complex_name, 0) + 1
                )

    districts = [
        {
            "name": name,
            "count": len(prices),
            "below_market": district_below_market.get(name, 0),
            "median_price_per_m2": _median(prices),
            "min_price_per_m2": min(prices),
            "max_price_per_m2": max(prices),
        }
        for name, prices in district_prices.items()
    ]
    districts.sort(key=lambda item: item["median_price_per_m2"], reverse=True)

    complexes = [
        {
            "name": name,
            "count": len(prices),
            "below_market": complex_below_market.get(name, 0),
            "median_price_per_m2": _median(prices),
            "min_price_per_m2": min(prices),
            "max_price_per_m2": max(prices),
        }
        for name, prices in complex_prices.items()
    ]
    complexes.sort(key=lambda item: (item["below_market"], item["count"]), reverse=True)

    return {
        "districts": districts,
        "complexes": complexes[:20],
    }


def create_monitoring_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: int | None = None,
) -> int:
    rows = connection.execute(
        """
        SELECT raw_json, listed_price_per_m2, pred_price_per_m2_q50,
               discount_vs_asking_pct_conservative, status
        FROM listings
        """
    ).fetchall()
    active_rows = [row for row in rows if row["status"] == "active"]
    active_count = len(active_rows)
    total_count = len(rows)
    below_market_count = sum(
        1
        for row in active_rows
        if (row["discount_vs_asking_pct_conservative"] or 0) > 0
    )
    listed_prices = [
        float(row["listed_price_per_m2"])
        for row in active_rows
        if row["listed_price_per_m2"] is not None
    ]
    pred_q50 = [
        float(row["pred_price_per_m2_q50"])
        for row in active_rows
        if row["pred_price_per_m2_q50"] is not None
    ]

    missing_year = 0
    missing_coords = 0
    unknown_district = 0
    missing_complex = 0
    for row in active_rows:
        raw_listing = _load_raw_listing(row["raw_json"])
        if not _extract_int(raw_listing.get("Год постройки")):
            missing_year += 1
        if _extract_float(raw_listing.get("lat")) is None or _extract_float(raw_listing.get("lon")) is None:
            missing_coords += 1
        if not normalize_district(raw_listing.get("Город")):
            unknown_district += 1
        if not _clean_text(raw_listing.get("Жилой комплекс")):
            missing_complex += 1

    failed_share = 0.0
    if run_id:
        run = connection.execute(
            """
            SELECT listings_processed, listings_failed, status
            FROM refresh_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run:
            attempted = (run["listings_processed"] or 0) + (run["listings_failed"] or 0)
            failed_share = (run["listings_failed"] or 0) / attempted if attempted else 0.0

    below_market_share = below_market_count / active_count if active_count else 0.0
    warnings = _monitoring_warnings(
        active_count=active_count,
        below_market_share=below_market_share,
        missing_year_share=_share(missing_year, active_count),
        missing_coords_share=_share(missing_coords, active_count),
        unknown_district_share=_share(unknown_district, active_count),
        missing_complex_share=_share(missing_complex, active_count),
        scrape_failed_share=failed_share,
    )
    cursor = connection.execute(
        """
        INSERT INTO model_monitoring_snapshots (
            created_at, run_id, total_listings, active_listings,
            below_market_active, below_market_share, median_listed_price_per_m2,
            median_pred_q50_per_m2, missing_year_share, missing_coords_share,
            unknown_district_share, missing_complex_share, scrape_failed_share,
            warnings_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            run_id,
            total_count,
            active_count,
            below_market_count,
            below_market_share,
            _median(listed_prices) if listed_prices else None,
            _median(pred_q50) if pred_q50 else None,
            _share(missing_year, active_count),
            _share(missing_coords, active_count),
            _share(unknown_district, active_count),
            _share(missing_complex, active_count),
            failed_share,
            json.dumps(warnings, ensure_ascii=False),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def fetch_monitoring_snapshots(
    connection: sqlite3.Connection,
    *,
    limit: int = 30,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            id,
            created_at,
            run_id,
            total_listings,
            active_listings,
            below_market_active,
            below_market_share,
            median_listed_price_per_m2,
            median_pred_q50_per_m2,
            missing_year_share,
            missing_coords_share,
            unknown_district_share,
            missing_complex_share,
            scrape_failed_share,
            warnings_json
        FROM model_monitoring_snapshots
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    snapshots = []
    for row in rows:
        snapshot = dict(row)
        try:
            snapshot["warnings"] = json.loads(snapshot.pop("warnings_json") or "[]")
        except json.JSONDecodeError:
            snapshot["warnings"] = []
        snapshots.append(snapshot)
    return snapshots


def _prepare_undervalued_item(row: dict) -> dict:
    raw_listing = _load_raw_listing(row.get("raw_json"))
    district_slug = normalize_district(raw_listing.get("Город"))
    district_label = district_label_for_slug(district_slug)
    row["district_slug"] = district_slug
    row["district_label"] = district_label
    row["rooms"] = _extract_rooms(row.get("title"))
    row["construction_year"] = _extract_int(raw_listing.get("Год постройки"))
    row["residential_complex"] = _clean_text(raw_listing.get("Жилой комплекс"))
    row["developer"] = _extract_developer(raw_listing)
    row["address"] = _extract_address(raw_listing)
    row["lat"] = _extract_float(raw_listing.get("lat"))
    row["lon"] = _extract_float(raw_listing.get("lon"))
    row["short_title"] = _short_listing_title(row.get("title"), row.get("area_m2"))
    row["listing_summary"] = _listing_summary_with_district(
        row["short_title"],
        district_label,
    )
    row.pop("raw_json", None)
    return row


def _extract_address(raw_listing: dict) -> str:
    for key in [
        "Адрес",
        "Улица",
        "Местоположение",
        "address",
        "addressTitle",
    ]:
        cleaned = _clean_text(raw_listing.get(key))
        if cleaned:
            return cleaned
    return ""


def _listing_summary_with_district(short_title: str, district_label: str) -> str:
    if not district_label or "не указан" in district_label.casefold():
        return short_title
    return f"{short_title}, {district_label.casefold()}"


def _extract_developer(raw_listing: dict) -> str:
    for key in [
        "Застройщик",
        "Застройщик ЖК",
        "Застройщик жилого комплекса",
        "developer",
        "builder",
    ]:
        cleaned = _clean_text(raw_listing.get(key))
        if cleaned:
            return cleaned
    return ""


def _sort_undervalued_items(items: list[dict], sort: str) -> list[dict]:
    sorters = {
        "q10_discount": (
            lambda item: item.get("discount_vs_asking_pct_conservative") or 0,
            True,
        ),
        "median_discount": (
            lambda item: item.get("discount_vs_asking_pct_median") or 0,
            True,
        ),
        "listed_price": (lambda item: item.get("listed_price") or float("inf"), False),
        "listed_price_asc": (
            lambda item: item.get("listed_price") or float("inf"),
            False,
        ),
        "listed_price_desc": (lambda item: item.get("listed_price") or 0, True),
        "price_per_m2": (
            lambda item: item.get("listed_price_per_m2") or float("inf"),
            False,
        ),
        "price_per_m2_asc": (
            lambda item: item.get("listed_price_per_m2") or float("inf"),
            False,
        ),
        "price_per_m2_desc": (
            lambda item: item.get("listed_price_per_m2") or 0,
            True,
        ),
        "newest": (lambda item: item.get("first_seen_at") or "", True),
        "area_asc": (lambda item: item.get("area_m2") or float("inf"), False),
        "area_desc": (lambda item: item.get("area_m2") or 0, True),
    }
    key, reverse = sorters.get(sort, sorters["q10_discount"])
    return sorted(items, key=key, reverse=reverse)


def _median(values: list[float]) -> float:
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _monitoring_warnings(
    *,
    active_count: int,
    below_market_share: float,
    missing_year_share: float,
    missing_coords_share: float,
    unknown_district_share: float,
    missing_complex_share: float,
    scrape_failed_share: float,
) -> list[str]:
    warnings = []
    if active_count == 0:
        warnings.append("Нет активных объявлений в базе.")
    if scrape_failed_share >= 0.10:
        warnings.append("Доля ошибок scrape выше 10%.")
    if below_market_share >= 0.20:
        warnings.append("Доля квартир ниже рынка необычно высокая.")
    if missing_year_share >= 0.30:
        warnings.append("У многих объявлений отсутствует год постройки.")
    if missing_coords_share >= 0.10:
        warnings.append("У части объявлений отсутствуют координаты.")
    if unknown_district_share >= 0.10:
        warnings.append("У части объявлений не распознан район.")
    if missing_complex_share >= 0.40:
        warnings.append("У многих объявлений не указан ЖК.")
    return warnings


def _load_raw_listing(raw_json: object) -> dict:
    if not raw_json:
        return {}
    try:
        loaded = json.loads(str(raw_json))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def normalize_district(value: object) -> str | None:
    cleaned = str(value or "").lower()
    cleaned = cleaned.replace("астана", "")
    cleaned = cleaned.replace("р-н", "")
    cleaned = cleaned.replace("район", "")
    cleaned = re.sub(r"[^а-яёa-z]+", " ", cleaned).strip()
    for slug, aliases in _DISTRICT_ALIASES.items():
        if cleaned in aliases:
            return slug
        if any(alias in cleaned.split() for alias in aliases):
            return slug
    return None


def district_label_for_slug(slug: str | None) -> str:
    for option in DISTRICT_OPTIONS:
        if option["slug"] == slug:
            return option["label"]
    return "Район не указан"


def valid_district_slug(value: str | None) -> str | None:
    if not value:
        return None
    slugs = {option["slug"] for option in DISTRICT_OPTIONS}
    return value if value in slugs else None


def valid_district_slugs(values: list[str] | None) -> list[str]:
    if not values:
        return []
    slugs = {option["slug"] for option in DISTRICT_OPTIONS}
    result = []
    for value in values:
        if value in slugs and value not in result:
            result.append(value)
    return result


def _short_listing_title(title: object, area_m2: object) -> str:
    title_text = str(title or "")
    rooms = _extract_rooms(title_text)

    try:
        area_value = float(area_m2)
    except (TypeError, ValueError):
        area_value = 0

    if area_value and area_value.is_integer():
        area = f"{area_value:.0f}"
    elif area_value:
        area = f"{area_value:.1f}"
    else:
        area = ""

    title_part = f"{rooms}-комнатная квартира" if rooms else "Квартира"
    return f"{title_part} · {area} м²" if area else title_part


def _extract_rooms(title: object) -> int | None:
    rooms_match = re.search(r"(\d+)\s*-\s*комнат", str(title or ""), flags=re.IGNORECASE)
    return int(rooms_match.group(1)) if rooms_match else None


def _extract_int(value: object) -> int | None:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else None


def _extract_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != "nan" else ""


def _iso_datetime_at_or_after(value: object, threshold: str) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed_threshold = datetime.fromisoformat(threshold.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed_threshold.tzinfo is None:
        parsed_threshold = parsed_threshold.replace(tzinfo=timezone.utc)
    return parsed >= parsed_threshold


def _point_in_polygon(
    lat: float,
    lon: float,
    polygon: list[tuple[float, float]],
) -> bool:
    inside = False
    previous_lat, previous_lon = polygon[-1]
    for current_lat, current_lon in polygon:
        crosses_latitude = (current_lat > lat) != (previous_lat > lat)
        if crosses_latitude:
            lon_delta = previous_lon - current_lon
            lat_delta = previous_lat - current_lat
            intersection_lon = lon_delta * (lat - current_lat) / lat_delta + current_lon
            if lon < intersection_lon:
                inside = not inside
        previous_lat, previous_lon = current_lat, current_lon
    return inside


def fetch_refresh_runs(
    connection: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            id,
            started_at,
            finished_at,
            kind,
            start_page,
            end_page,
            pages_seen,
            urls_seen,
            listings_processed,
            listings_failed,
            status,
            error
        FROM refresh_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_running_refresh(connection: sqlite3.Connection) -> dict | None:
    row = connection.execute(
        """
        SELECT
            id,
            started_at,
            finished_at,
            kind,
            start_page,
            end_page,
            pages_seen,
            urls_seen,
            listings_processed,
            listings_failed,
            status,
            error
        FROM refresh_runs
        WHERE status = 'running'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def fetch_status_summary(connection: sqlite3.Connection) -> dict:
    listing_counts = connection.execute(
        """
        SELECT
            COUNT(*) AS total_listings,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_listings,
            SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale_listings,
            SUM(
                CASE
                    WHEN status = 'active'
                     AND discount_vs_asking_pct_conservative > 0
                    THEN 1 ELSE 0
                END
            ) AS below_market_active
        FROM listings
        """
    ).fetchone()
    latest_refresh = connection.execute(
        """
        SELECT
            id,
            started_at,
            finished_at,
            kind,
            start_page,
            end_page,
            pages_seen,
            urls_seen,
            listings_processed,
            listings_failed,
            status,
            error
        FROM refresh_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    summary = dict(listing_counts)
    summary["latest_refresh"] = dict(latest_refresh) if latest_refresh else None
    return summary


def iter_unique_urls(urls: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result
