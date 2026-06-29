from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.prediction_service import ListingPrediction


DEFAULT_DB_PATH = Path("data") / "krisha.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
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
        """
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
    connection.commit()


def fetch_undervalued(
    connection: sqlite3.Connection,
    *,
    limit: int = 50,
    include_stale: bool = False,
) -> list[dict]:
    status_clause = "" if include_stale else "AND status = 'active'"
    rows = connection.execute(
        f"""
        SELECT
            url,
            title,
            status,
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
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


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


def iter_unique_urls(urls: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result
