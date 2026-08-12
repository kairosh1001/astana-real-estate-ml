from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.cities import city_config, district_options, infer_listing_city, normalize_city_slug
from app.prediction_service import ListingPrediction


DEFAULT_DB_PATH = Path("data") / "krisha.sqlite3"
DISTRICT_OPTIONS = district_options("astana")
APARTMENT_CONDITION_OPTIONS = [
    {"slug": "fresh_repair", "label": "Свежий ремонт", "value": "свежий ремонт"},
    {
        "slug": "tidy_repair",
        "label": "Не новый, но аккуратный ремонт",
        "value": "не новый, но аккуратный ремонт",
    },
    {"slug": "rough_finish", "label": "Черновая отделка", "value": "черновая отделка"},
    {"slug": "needs_repair", "label": "Требует ремонта", "value": "требует ремонта"},
    {"slug": "open_plan", "label": "Свободная планировка", "value": "свободная планировка"},
]

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
            city TEXT NOT NULL DEFAULT 'astana',
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
            city TEXT NOT NULL DEFAULT 'astana',
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
            notification_city TEXT NOT NULL DEFAULT 'astana',
            last_digest_date TEXT
        );
        """
    )
    _ensure_column(connection, "refresh_runs", "city", "TEXT NOT NULL DEFAULT 'astana'")
    _ensure_column(connection, "listings", "city", "TEXT NOT NULL DEFAULT 'astana'")
    _ensure_column(
        connection,
        "telegram_subscribers",
        "notification_city",
        "TEXT NOT NULL DEFAULT 'astana'",
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_city_status ON listings(city, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_runs_city_id ON refresh_runs(city, id DESC)"
    )
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    notification_city: str | None = None,
) -> None:
    now = utc_now()
    selected_city = valid_notification_city(notification_city) if notification_city else None
    connection.execute(
        """
        INSERT INTO telegram_subscribers (
            chat_id, created_at, updated_at, notifications_enabled,
            notification_city
        )
        VALUES (?, ?, ?, ?, COALESCE(?, 'astana'))
        ON CONFLICT(chat_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            notifications_enabled = excluded.notifications_enabled,
            notification_city = COALESCE(?, telegram_subscribers.notification_city)
        """,
        (
            chat_id,
            now,
            now,
            int(notifications_enabled),
            selected_city,
            selected_city,
        ),
    )
    connection.commit()


def valid_notification_city(value: object) -> str:
    cleaned = str(value or "").strip().casefold()
    return cleaned if cleaned in {"astana", "almaty", "both"} else "astana"


def set_telegram_notification_city(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    notification_city: str,
) -> None:
    selected_city = valid_notification_city(notification_city)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO telegram_subscribers (
            chat_id, created_at, updated_at, notifications_enabled,
            notification_city
        )
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            updated_at = excluded.updated_at,
            notifications_enabled = 1,
            notification_city = excluded.notification_city
        """,
        (chat_id, now, now, selected_city),
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
        SELECT chat_id, notification_city, last_digest_date
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
    city: str = "astana",
    kind: str,
    start_page: int,
    end_page: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO refresh_runs (started_at, city, kind, start_page, end_page)
        VALUES (?, ?, ?, ?, ?)
        """,
        (utc_now(), normalize_city_slug(city), kind, start_page, end_page),
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


def recover_abandoned_refreshes(
    connection: sqlite3.Connection,
    *,
    max_age_hours: int = 18,
) -> int:
    threshold = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat(timespec="seconds")
    cursor = connection.execute(
        """
        UPDATE refresh_runs
        SET finished_at = ?,
            status = 'failed',
            error = COALESCE(error, 'Запуск прерван и автоматически закрыт перед следующим refresh.')
        WHERE status = 'running'
          AND started_at < ?
        """,
        (utc_now(), threshold),
    )
    connection.commit()
    return int(cursor.rowcount)


def mark_refresh_started(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
) -> None:
    connection.execute(
        """
        UPDATE listings
        SET missed_refreshes = missed_refreshes + 1
        WHERE status = 'active' AND city = ?
        """,
        (normalize_city_slug(city),),
    )
    connection.commit()


def mark_stale_listings(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
    stale_after_missed: int = 3,
) -> None:
    connection.execute(
        """
        UPDATE listings
        SET status = 'stale'
        WHERE missed_refreshes >= ? AND city = ?
        """,
        (stale_after_missed, normalize_city_slug(city)),
    )
    connection.commit()


def upsert_listing_prediction(
    connection: sqlite3.Connection,
    *,
    raw_listing: dict,
    prediction: ListingPrediction,
) -> None:
    now = utc_now()
    city = infer_listing_city(raw_listing)
    raw_json = json.dumps(raw_listing, ensure_ascii=False, sort_keys=True)
    values = asdict(prediction)
    connection.execute(
        """
        INSERT INTO listings (
            url, city, title, raw_json, first_seen_at, last_seen_at, last_checked_at,
            missed_refreshes, status, listed_price, area_m2, listed_price_per_m2,
            pred_price_per_m2_q10, pred_price_per_m2_q50, pred_price_per_m2_q90,
            pred_total_q50, discount_vs_asking_pct_conservative,
            discount_vs_asking_pct_median, interval_width_pct
        )
        VALUES (
            :url, :city, :title, :raw_json, :now, :now, :now,
            0, 'active', :listed_price, :area_m2, :listed_price_per_m2,
            :pred_price_per_m2_q10, :pred_price_per_m2_q50,
            :pred_price_per_m2_q90, :pred_total_q50,
            :discount_vs_asking_pct_conservative,
            :discount_vs_asking_pct_median, :interval_width_pct
        )
        ON CONFLICT(url) DO UPDATE SET
            city = excluded.city,
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
            "city": city,
            "now": now,
        },
    )
    observed = _parse_iso_datetime(now) or datetime.now(timezone.utc)
    astana_observed = observed + timedelta(hours=5)
    astana_day_start = astana_observed.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(hours=5)
    astana_next_day = astana_day_start + timedelta(days=1)
    existing_history = connection.execute(
        """
        SELECT id
        FROM listing_price_history
        WHERE url = ?
          AND observed_at >= ?
          AND observed_at < ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            prediction.url,
            astana_day_start.isoformat(timespec="seconds"),
            astana_next_day.isoformat(timespec="seconds"),
        ),
    ).fetchone()
    if existing_history:
        connection.execute(
            """
            UPDATE listing_price_history
            SET observed_at = ?,
                listed_price = ?,
                listed_price_per_m2 = ?,
                status = 'active'
            WHERE id = ?
            """,
            (
                now,
                prediction.listed_price,
                prediction.listed_price_per_m2,
                existing_history["id"],
            ),
        )
    else:
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
    city: str = "astana",
    limit: int = 50,
    offset: int = 0,
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    apartment_condition: str | None = None,
    new_build: bool = False,
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
            city,
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
        WHERE city = ?
          AND discount_vs_asking_pct_conservative > 0
          {status_clause}
        ORDER BY discount_vs_asking_pct_conservative DESC
        """,
        (normalize_city_slug(city),),
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
    if apartment_condition:
        items = [
            item
            for item in items
            if item.get("apartment_condition_slug") == apartment_condition
        ]
    if new_build:
        items = [item for item in items if item.get("is_new_build") is True]
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


def fetch_home_match_candidates(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT
            url,
            city,
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
        WHERE city = ?
          AND status = 'active'
          AND listed_price IS NOT NULL
          AND area_m2 IS NOT NULL
        """,
        (normalize_city_slug(city),),
    ).fetchall()
    return [_prepare_undervalued_item(dict(row)) for row in rows]


def count_undervalued(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    apartment_condition: str | None = None,
    new_build: bool = False,
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
            city=city,
            limit=100000,
            offset=0,
            districts=districts,
            rooms=rooms,
            max_price=max_price,
            min_year=min_year,
            max_year=max_year,
            residential_complex=residential_complex,
            developer=developer,
            apartment_condition=apartment_condition,
            new_build=new_build,
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
            city,
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
    *,
    city: str = "astana",
) -> dict | None:
    query = _clean_text(residential_complex)
    if not query:
        return None

    rows = connection.execute(
        """
        SELECT raw_json, listed_price_per_m2, discount_vs_asking_pct_conservative
        FROM listings
        WHERE city = ?
          AND status = 'active'
          AND listed_price_per_m2 IS NOT NULL
        """,
        (normalize_city_slug(city),),
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


def fetch_market_dashboard(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
) -> dict:
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    rows = connection.execute(
        """
        SELECT url, city, title, raw_json, first_seen_at, last_seen_at, status,
               listed_price, area_m2, listed_price_per_m2,
               pred_price_per_m2_q50,
               discount_vs_asking_pct_conservative
        FROM listings
        WHERE city = ?
        """,
        (city_slug,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    active_items = []
    stale_count = 0

    for source_row in rows:
        row = dict(source_row)
        if row.get("status") != "active":
            stale_count += 1
            continue
        item = _market_item_from_row(row, now=now)
        if item:
            active_items.append(item)

    city_summary = _market_segment(selected_city["name"], active_items)
    city_median = city_summary.get("median_price_per_m2") or 0

    district_groups: dict[str, list[dict]] = {}
    complex_groups: dict[str, list[dict]] = {}
    room_groups: dict[str, list[dict]] = {}
    condition_groups: dict[str, list[dict]] = {}
    property_type_groups: dict[str, list[dict]] = {}
    for item in active_items:
        district_groups.setdefault(item["district_label"], []).append(item)
        if item["complex_name"]:
            complex_groups.setdefault(item["complex_name"], []).append(item)
        room_label = f"{item['rooms']}-комн." if item["rooms"] else "Не указано"
        room_groups.setdefault(room_label, []).append(item)
        condition_groups.setdefault(item["condition_label"], []).append(item)
        property_label = "Новостройки" if item["is_new_build"] else "Вторичный рынок"
        property_type_groups.setdefault(property_label, []).append(item)

    districts = [_market_segment(name, items) for name, items in district_groups.items()]
    districts.sort(key=lambda item: item["median_price_per_m2"], reverse=True)
    max_district_price = max(
        (item["median_price_per_m2"] for item in districts),
        default=1,
    )
    max_district_count = max((item["count"] for item in districts), default=1)
    for item in districts:
        item["slug"] = next(
            (
                option["slug"]
                for option in district_options(city_slug)
                if option["label"] == item["name"]
            ),
            "",
        )
        item["price_index"] = (
            item["median_price_per_m2"] / city_median * 100 if city_median else 0
        )
        item["price_bar_pct"] = item["median_price_per_m2"] / max_district_price * 100
        item["inventory_bar_pct"] = item["count"] / max_district_count * 100

    complexes = [_market_segment(name, items) for name, items in complex_groups.items()]
    complexes.sort(
        key=lambda item: (item["below_market_share"], item["count"]),
        reverse=True,
    )

    rooms = [_market_segment(name, items) for name, items in room_groups.items()]
    rooms.sort(key=lambda item: _room_sort_key(item["name"]))
    max_room_count = max((item["count"] for item in rooms), default=1)
    for item in rooms:
        item["bar_pct"] = item["count"] / max_room_count * 100

    conditions = [
        _market_segment(name, items) for name, items in condition_groups.items()
    ]
    conditions.sort(key=lambda item: item["count"], reverse=True)
    max_condition_price = max(
        (item["median_price_per_m2"] for item in conditions),
        default=1,
    )
    for item in conditions:
        item["bar_pct"] = item["median_price_per_m2"] / max_condition_price * 100

    property_types = [
        _market_segment(name, items) for name, items in property_type_groups.items()
    ]
    property_types.sort(key=lambda item: item["count"], reverse=True)

    price_buckets = _market_price_buckets(active_items)
    max_bucket_count = max((item["count"] for item in price_buckets), default=1) or 1
    for item in price_buckets:
        item["bar_pct"] = item["count"] / max_bucket_count * 100

    recent_7d = sum(
        1
        for item in active_items
        if _datetime_within_days(item.get("first_seen_at"), now, 7)
    )
    recent_30d = sum(
        1
        for item in active_items
        if _datetime_within_days(item.get("first_seen_at"), now, 30)
    )
    last_seen_values = [
        item["last_seen_at"] for item in active_items if item.get("last_seen_at")
    ]

    historical = _market_history(
        connection,
        now=now,
        urls=[str(row["url"]) for row in rows],
    )
    insights = _market_insights(districts)

    return {
        "city": city_summary,
        "districts": districts,
        "complexes": complexes[:20],
        "rooms": rooms,
        "conditions": conditions,
        "property_types": property_types,
        "price_buckets": price_buckets,
        "historical": historical,
        "insights": insights,
        "coverage": {
            "total_listings": len(rows),
            "active_listings": len(active_items),
            "stale_listings": stale_count,
            "known_district_share": _share(
                sum(1 for item in active_items if item["district_slug"]),
                len(active_items),
            ),
            "recent_7d": recent_7d,
            "recent_30d": recent_30d,
            "as_of": max(last_seen_values) if last_seen_values else None,
        },
    }


def fetch_district_analytics(
    connection: sqlite3.Connection,
    district_slug: str,
    *,
    city: str = "astana",
) -> dict | None:
    city_slug = normalize_city_slug(city)
    valid_slug = valid_district_slug(district_slug, city=city_slug)
    if not valid_slug:
        return None
    return _fetch_market_entity_analytics(
        connection,
        entity_kind="district",
        entity_name=district_label_for_slug(valid_slug, city=city_slug),
        matcher=lambda raw: normalize_district(
            raw.get("Город"), city=city_slug
        ) == valid_slug,
        city=city_slug,
    )


def fetch_complex_analytics(
    connection: sqlite3.Connection,
    residential_complex: str,
    *,
    city: str = "astana",
) -> dict | None:
    complex_name = _clean_text(residential_complex)
    if not complex_name:
        return None
    analytics = _fetch_market_entity_analytics(
        connection,
        entity_kind="complex",
        entity_name=complex_name,
        matcher=lambda raw: _clean_text(raw.get("Жилой комплекс")).casefold()
        == complex_name.casefold(),
        city=city,
    )
    if not analytics or not analytics["coverage"]["known_listings"]:
        return None
    return analytics


def _fetch_market_entity_analytics(
    connection: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_name: str,
    matcher,
    city: str = "astana",
) -> dict:
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    rows = connection.execute(
        """
        SELECT url, city, title, raw_json, first_seen_at, last_seen_at, status,
               listed_price, area_m2, listed_price_per_m2,
               pred_price_per_m2_q10, pred_price_per_m2_q50,
               pred_price_per_m2_q90, pred_total_q50,
               discount_vs_asking_pct_conservative,
               discount_vs_asking_pct_median, interval_width_pct
        FROM listings
        WHERE city = ?
        """,
        (city_slug,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    city_items = []
    entity_items = []
    entity_listing_rows = []
    entity_urls = []

    for source_row in rows:
        row = dict(source_row)
        raw_listing = _load_raw_listing(row.get("raw_json"))
        matches = matcher(raw_listing)
        if matches:
            entity_urls.append(str(row["url"]))
        if row.get("status") != "active":
            continue
        item = _market_item_from_row(row, now=now, raw_listing=raw_listing)
        if not item:
            continue
        city_items.append(item)
        if matches:
            entity_items.append(item)
            entity_listing_rows.append(row)

    summary = _market_segment(entity_name, entity_items)
    city_summary = _market_segment(selected_city["name"], city_items)
    city_median = city_summary.get("median_price_per_m2") or 0
    summary["price_index"] = (
        summary["median_price_per_m2"] / city_median * 100
        if city_median and summary["count"]
        else None
    )
    summary["median_diff_pct"] = (
        summary["median_price_per_m2"] / city_median - 1
        if city_median and summary["count"]
        else None
    )

    room_groups: dict[str, list[dict]] = {}
    condition_groups: dict[str, list[dict]] = {}
    property_groups: dict[str, list[dict]] = {}
    complex_groups: dict[str, list[dict]] = {}
    district_groups: dict[str, list[dict]] = {}
    for item in entity_items:
        room_label = f"{item['rooms']}-комн." if item["rooms"] else "Не указано"
        room_groups.setdefault(room_label, []).append(item)
        condition_groups.setdefault(item["condition_label"], []).append(item)
        property_label = "Новостройки" if item["is_new_build"] else "Вторичный рынок"
        property_groups.setdefault(property_label, []).append(item)
        if item["complex_name"]:
            complex_groups.setdefault(item["complex_name"], []).append(item)
        if item["district_slug"]:
            district_groups.setdefault(item["district_label"], []).append(item)

    rooms = [_market_segment(name, group) for name, group in room_groups.items()]
    rooms.sort(key=lambda item: _room_sort_key(item["name"]))
    max_room_count = max((item["count"] for item in rooms), default=1)
    for item in rooms:
        item["bar_pct"] = item["count"] / max_room_count * 100

    conditions = [
        _market_segment(name, group) for name, group in condition_groups.items()
    ]
    conditions.sort(key=lambda item: item["count"], reverse=True)
    max_condition_price = max(
        (item["median_price_per_m2"] for item in conditions),
        default=1,
    )
    for item in conditions:
        item["bar_pct"] = item["median_price_per_m2"] / max_condition_price * 100

    property_types = [
        _market_segment(name, group) for name, group in property_groups.items()
    ]
    property_types.sort(key=lambda item: item["count"], reverse=True)

    complexes = [
        _market_segment(name, group) for name, group in complex_groups.items()
    ]
    complexes.sort(key=lambda item: (item["count"], item["median_price_per_m2"]), reverse=True)

    districts = [
        _market_segment(name, group) for name, group in district_groups.items()
    ]
    districts.sort(key=lambda item: item["count"], reverse=True)
    for item in districts:
        item["slug"] = next(
            (
                option["slug"]
                for option in district_options(city_slug)
                if option["label"] == item["name"]
            ),
            "",
        )

    price_buckets = _market_price_buckets(entity_items)
    max_bucket_count = max((item["count"] for item in price_buckets), default=1) or 1
    for item in price_buckets:
        item["bar_pct"] = item["count"] / max_bucket_count * 100

    prepared_listings = [
        _prepare_undervalued_item(dict(row)) for row in entity_listing_rows
    ]
    prepared_listings.sort(
        key=lambda item: (
            item.get("discount_vs_asking_pct_conservative") or -1,
            item.get("last_seen_at") or "",
        ),
        reverse=True,
    )
    last_seen_values = [
        item["last_seen_at"] for item in entity_items if item.get("last_seen_at")
    ]
    return {
        "entity": {"kind": entity_kind, "name": entity_name},
        "summary": summary,
        "city": city_summary,
        "rooms": rooms,
        "conditions": conditions,
        "property_types": property_types,
        "price_buckets": price_buckets,
        "complexes": complexes,
        "districts": districts,
        "historical": _market_history(connection, now=now, urls=entity_urls),
        "listings": prepared_listings[:100],
        "coverage": {
            "known_listings": len(entity_urls),
            "active_listings": len(entity_items),
            "stale_listings": max(len(entity_urls) - len(entity_items), 0),
            "as_of": max(last_seen_values) if last_seen_values else None,
        },
    }


def _market_item_from_row(
    row: dict,
    *,
    now: datetime,
    raw_listing: dict | None = None,
) -> dict | None:
    if row.get("listed_price_per_m2") is None:
        return None
    raw_listing = raw_listing or _load_raw_listing(row.get("raw_json"))
    city_slug = normalize_city_slug(row.get("city") or infer_listing_city(raw_listing))
    district_slug = normalize_district(raw_listing.get("Город"), city=city_slug)
    condition_slug = normalize_apartment_condition(
        raw_listing.get("Состояние квартиры")
    )
    condition_label = next(
        (
            option["label"]
            for option in APARTMENT_CONDITION_OPTIONS
            if option["slug"] == condition_slug
        ),
        "Состояние не указано",
    )
    first_seen = _parse_iso_datetime(row.get("first_seen_at"))
    age_days = max((now - first_seen).days, 0) if first_seen else None
    return {
        "url": row.get("url"),
        "district_slug": district_slug,
        "district_label": district_label_for_slug(district_slug, city=city_slug),
        "complex_name": _clean_text(raw_listing.get("Жилой комплекс")),
        "condition_label": condition_label,
        "rooms": _extract_rooms(row.get("title")),
        "is_new_build": _extract_new_build_flag(raw_listing),
        "listed_price": _extract_float(row.get("listed_price")),
        "area_m2": _extract_float(row.get("area_m2")),
        "price_per_m2": float(row["listed_price_per_m2"]),
        "pred_q50_per_m2": _extract_float(row.get("pred_price_per_m2_q50")),
        "q10_upside": _extract_float(
            row.get("discount_vs_asking_pct_conservative")
        ),
        "age_days": age_days,
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


def fetch_market_brief(
    connection: sqlite3.Connection,
    *,
    city: str = "astana",
) -> dict:
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    rows = connection.execute(
        """
        SELECT raw_json, listed_price, area_m2, listed_price_per_m2,
               discount_vs_asking_pct_conservative, last_seen_at
        FROM listings
        WHERE city = ?
          AND status = 'active'
          AND listed_price_per_m2 IS NOT NULL
        """,
        (city_slug,),
    ).fetchall()
    items = []
    district_groups: dict[str, list[dict]] = {}
    last_seen_values = []
    for source_row in rows:
        row = dict(source_row)
        raw_listing = _load_raw_listing(row["raw_json"])
        district_slug = normalize_district(
            raw_listing.get("Город"), city=city_slug
        )
        item = {
            "price_per_m2": float(row["listed_price_per_m2"]),
            "listed_price": _extract_float(row.get("listed_price")),
            "area_m2": _extract_float(row.get("area_m2")),
            "q10_upside": _extract_float(
                row.get("discount_vs_asking_pct_conservative")
            ),
            "age_days": None,
        }
        items.append(item)
        if district_slug:
            district_groups.setdefault(
                district_label_for_slug(district_slug, city=city_slug), []
            ).append(item)
        if row.get("last_seen_at"):
            last_seen_values.append(row["last_seen_at"])
    districts = [
        _market_segment(name, group) for name, group in district_groups.items()
    ]
    districts.sort(key=lambda item: item["median_price_per_m2"], reverse=True)
    return {
        "city": _market_segment(selected_city["name"], items),
        "district_count": len(districts),
        "highest_district": districts[0] if districts else None,
        "lowest_district": districts[-1] if districts else None,
        "as_of": max(last_seen_values) if last_seen_values else None,
    }


def _market_segment(name: str, items: list[dict]) -> dict:
    prices = [item["price_per_m2"] for item in items if item.get("price_per_m2")]
    total_prices = [item["listed_price"] for item in items if item.get("listed_price")]
    areas = [item["area_m2"] for item in items if item.get("area_m2")]
    ages = [float(item["age_days"]) for item in items if item.get("age_days") is not None]
    q10_upside = [
        item["q10_upside"]
        for item in items
        if item.get("q10_upside") is not None
    ]
    below_market = sum(1 for value in q10_upside if value > 0)
    if not prices:
        return {
            "name": name,
            "count": 0,
            "below_market": 0,
            "below_market_share": 0.0,
            "median_price_per_m2": 0.0,
            "q25_price_per_m2": 0.0,
            "q75_price_per_m2": 0.0,
            "p10_price_per_m2": 0.0,
            "p90_price_per_m2": 0.0,
            "min_price_per_m2": 0.0,
            "max_price_per_m2": 0.0,
            "median_total_price": None,
            "median_area_m2": None,
            "median_age_days": None,
            "median_q10_upside": None,
        }
    return {
        "name": name,
        "count": len(items),
        "below_market": below_market,
        "below_market_share": _share(below_market, len(items)),
        "median_price_per_m2": _median(prices),
        "q25_price_per_m2": _quantile(prices, 0.25),
        "q75_price_per_m2": _quantile(prices, 0.75),
        "p10_price_per_m2": _quantile(prices, 0.10),
        "p90_price_per_m2": _quantile(prices, 0.90),
        "min_price_per_m2": min(prices),
        "max_price_per_m2": max(prices),
        "median_total_price": _median(total_prices) if total_prices else None,
        "median_area_m2": _median(areas) if areas else None,
        "median_age_days": _median(ages) if ages else None,
        "median_q10_upside": _median(q10_upside) if q10_upside else None,
    }


def _market_price_buckets(items: list[dict]) -> list[dict]:
    buckets = [
        ("до 300 тыс.", 0, 300_000),
        ("300–400 тыс.", 300_000, 400_000),
        ("400–500 тыс.", 400_000, 500_000),
        ("500–600 тыс.", 500_000, 600_000),
        ("600–800 тыс.", 600_000, 800_000),
        ("от 800 тыс.", 800_000, float("inf")),
    ]
    result = []
    for label, lower, upper in buckets:
        count = sum(
            1
            for item in items
            if lower <= item["price_per_m2"] < upper
        )
        result.append({"label": label, "count": count})
    return result


def _market_history(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    urls: list[str] | None = None,
) -> dict:
    cutoff = (now - timedelta(days=180)).isoformat(timespec="seconds")
    params: list[object] = [cutoff]
    url_clause = ""
    if urls is not None:
        if not urls:
            url_clause = "AND 1 = 0"
        else:
            placeholders = ",".join("?" for _ in urls)
            url_clause = f"AND url IN ({placeholders})"
            params.extend(urls)
    rows = connection.execute(
        f"""
        SELECT url, observed_at, listed_price, listed_price_per_m2
        FROM listing_price_history
        WHERE observed_at >= ?
          AND listed_price_per_m2 IS NOT NULL
          {url_clause}
        ORDER BY observed_at ASC, id ASC
        """,
        params,
    ).fetchall()
    daily_latest: dict[tuple[str, str], dict] = {}
    by_url: dict[str, list[dict]] = {}
    for source_row in rows:
        row = dict(source_row)
        observed = _parse_iso_datetime(row.get("observed_at"))
        if not observed:
            continue
        astana_day = (observed + timedelta(hours=5)).date().isoformat()
        daily_latest[(astana_day, row["url"])] = row
        by_url.setdefault(row["url"], []).append(row)

    daily_prices: dict[str, list[float]] = {}
    for (day, _url), row in daily_latest.items():
        daily_prices.setdefault(day, []).append(float(row["listed_price_per_m2"]))
    daily = [
        {
            "date": day,
            "label": datetime.fromisoformat(day).strftime("%d.%m"),
            "median_price_per_m2": _median(prices),
            "listing_count": len(prices),
        }
        for day, prices in sorted(daily_prices.items())
    ]

    available = len(daily) >= 8
    chart_points = []
    if available:
        values = [item["median_price_per_m2"] for item in daily]
        low = min(values)
        high = max(values)
        spread = high - low or 1
        for index, item in enumerate(daily):
            chart_points.append(
                {
                    **item,
                    "x": 55 + (890 * index / max(len(daily) - 1, 1)),
                    "y": 215 - ((item["median_price_per_m2"] - low) / spread * 175),
                }
            )
    else:
        low = min((item["median_price_per_m2"] for item in daily), default=None)
        high = max((item["median_price_per_m2"] for item in daily), default=None)

    eligible_urls = 0
    reductions = []
    increases = []
    for observations in by_url.values():
        priced = [item for item in observations if item.get("listed_price")]
        if len(priced) < 2:
            continue
        eligible_urls += 1
        first_price = float(priced[0]["listed_price"])
        last_price = float(priced[-1]["listed_price"])
        if first_price <= 0:
            continue
        change = (last_price - first_price) / first_price
        if change < 0:
            reductions.append(abs(change))
        elif change > 0:
            increases.append(change)

    change_pct = None
    if len(daily) >= 2 and daily[0]["median_price_per_m2"]:
        change_pct = (
            daily[-1]["median_price_per_m2"] / daily[0]["median_price_per_m2"] - 1
        )
    return {
        "available": available,
        "daily": daily,
        "chart_points": chart_points,
        "polyline": " ".join(
            f"{point['x']:.1f},{point['y']:.1f}" for point in chart_points
        ),
        "low": low,
        "high": high,
        "change_pct": change_pct,
        "observation_count": len(rows),
        "listing_count": len(by_url),
        "day_count": len(daily),
        "start_date": daily[0]["date"] if daily else None,
        "end_date": daily[-1]["date"] if daily else None,
        "eligible_price_change_count": eligible_urls,
        "price_cut_count": len(reductions),
        "price_cut_share": _share(len(reductions), eligible_urls),
        "median_price_cut": _median(reductions) if reductions else None,
        "price_increase_count": len(increases),
    }


def _market_insights(districts: list[dict]) -> list[dict]:
    reliable = [item for item in districts if item["count"] >= 3]
    candidates = reliable or districts
    if not candidates:
        return []
    premium = max(candidates, key=lambda item: item["median_price_per_m2"])
    opportunity = max(candidates, key=lambda item: item["below_market_share"])
    spread = max(
        candidates,
        key=lambda item: (
            (item["q75_price_per_m2"] - item["q25_price_per_m2"])
            / item["median_price_per_m2"]
        ),
    )
    return [
        {
            "label": "Самая высокая медиана",
            "name": premium["name"],
            "slug": premium.get("slug", ""),
            "value": premium["median_price_per_m2"],
            "kind": "price",
            "sample": premium["count"],
        },
        {
            "label": "Больше вариантов ниже рынка",
            "name": opportunity["name"],
            "slug": opportunity.get("slug", ""),
            "value": opportunity["below_market_share"],
            "kind": "percent",
            "sample": opportunity["count"],
        },
        {
            "label": "Самый широкий разброс",
            "name": spread["name"],
            "slug": spread.get("slug", ""),
            "value": (
                (spread["q75_price_per_m2"] - spread["q25_price_per_m2"])
                / spread["median_price_per_m2"]
            ),
            "kind": "percent",
            "sample": spread["count"],
        },
    ]


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _parse_iso_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _datetime_within_days(value: object, now: datetime, days: int) -> bool:
    parsed = _parse_iso_datetime(value)
    return bool(parsed and now - timedelta(days=days) <= parsed <= now)


def _room_sort_key(label: str) -> tuple[int, str]:
    match = re.match(r"(\d+)", label)
    return (int(match.group(1)), label) if match else (999, label)


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
    city_slug = normalize_city_slug(row.get("city") or infer_listing_city(raw_listing))
    district_slug = normalize_district(raw_listing.get("Город"), city=city_slug)
    district_label = district_label_for_slug(district_slug, city=city_slug)
    row["city"] = city_slug
    row["city_label"] = city_config(city_slug)["name"]
    row["district_slug"] = district_slug
    row["district_label"] = district_label
    row["rooms"] = _extract_rooms(row.get("title"))
    row["construction_year"] = _extract_int(raw_listing.get("Год постройки"))
    row["residential_complex"] = _clean_text(raw_listing.get("Жилой комплекс"))
    row["developer"] = _extract_developer(raw_listing)
    row["apartment_condition"] = _clean_text(
        raw_listing.get("Состояние квартиры")
    )
    row["apartment_condition_slug"] = normalize_apartment_condition(
        row["apartment_condition"]
    )
    row["is_new_build"] = _extract_new_build_flag(raw_listing)
    row["furnished"] = _clean_text(raw_listing.get("Квартира меблирована"))
    row["is_furnished"] = _extract_furnished_flag(row["furnished"])
    row["furnished_label"] = _format_furnished_label(row["furnished"])
    row["building_type"] = _clean_text(raw_listing.get("Тип дома"))
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


def normalize_district(value: object, *, city: str = "astana") -> str | None:
    city_slug = normalize_city_slug(city)
    cleaned = str(value or "").lower()
    cleaned = cleaned.replace("астана", "")
    if city_slug == "almaty":
        cleaned = cleaned.replace("алматы", "")
    cleaned = cleaned.replace("р-н", "")
    cleaned = cleaned.replace("район", "")
    cleaned = re.sub(r"[^а-яёa-z]+", " ", cleaned).strip()
    for option in city_config(city_slug)["districts"]:
        slug = option["slug"]
        aliases = option["aliases"]
        if cleaned in aliases:
            return slug
        if any(alias in cleaned.split() for alias in aliases):
            return slug
    return None


def district_label_for_slug(slug: str | None, *, city: str = "astana") -> str:
    for option in district_options(city):
        if option["slug"] == slug:
            return option["label"]
    return "Район не указан"


def valid_district_slug(
    value: str | None,
    *,
    city: str = "astana",
) -> str | None:
    if not value:
        return None
    slugs = {option["slug"] for option in district_options(city)}
    return value if value in slugs else None


def valid_district_slugs(
    values: list[str] | None,
    *,
    city: str = "astana",
) -> list[str]:
    if not values:
        return []
    slugs = {option["slug"] for option in district_options(city)}
    result = []
    for value in values:
        if value in slugs and value not in result:
            result.append(value)
    return result


def valid_apartment_condition_slug(value: str | None) -> str | None:
    if not value:
        return None
    slugs = {option["slug"] for option in APARTMENT_CONDITION_OPTIONS}
    return value if value in slugs else None


def normalize_apartment_condition(value: object) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    for option in APARTMENT_CONDITION_OPTIONS:
        if cleaned == option["value"]:
            return option["slug"]
    return None


def _extract_new_build_flag(raw_listing: dict) -> bool:
    value = raw_listing.get("Новостройка")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "да",
        "новостройка",
    }


def _extract_furnished_flag(value: object) -> bool | None:
    cleaned = _clean_text(value).casefold()
    if not cleaned:
        return None
    if "без мебели" in cleaned or "не меблирован" in cleaned:
        return False
    return any(
        marker in cleaned
        for marker in ("полностью", "частично", "меблирован", "с мебелью")
    )


def _format_furnished_label(value: object) -> str:
    cleaned = _clean_text(value)
    normalized = cleaned.casefold()
    if not normalized:
        return ""
    if "без мебели" in normalized or "не меблирован" in normalized:
        return "Без мебели"
    if "полностью" in normalized:
        return "Мебель: полностью меблирована"
    if "частично" in normalized:
        return "Мебель: частично меблирована"
    return f"Мебель: {cleaned}"


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
            city,
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
            city,
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


def fetch_status_summary(
    connection: sqlite3.Connection,
    *,
    city: str | None = None,
) -> dict:
    city_slug = normalize_city_slug(city) if city else None
    city_clause = "WHERE city = ?" if city_slug else ""
    params = (city_slug,) if city_slug else ()
    listing_counts = connection.execute(
        f"""
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
        {city_clause}
        """,
        params,
    ).fetchone()
    latest_refresh = connection.execute(
        f"""
        SELECT
            id,
            city,
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
        {city_clause}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
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
