from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
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
    journal_mode = os.getenv("SQLITE_JOURNAL_MODE", "DELETE").upper()
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
    offset: int = 0,
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    polygon: list[tuple[float, float]] | None = None,
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
    min_area: float | None = None,
    max_area: float | None = None,
    polygon: list[tuple[float, float]] | None = None,
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
            min_area=min_area,
            max_area=max_area,
            polygon=polygon,
            include_stale=include_stale,
        )
    )


def _prepare_undervalued_item(row: dict) -> dict:
    raw_listing = _load_raw_listing(row.get("raw_json"))
    district_slug = normalize_district(raw_listing.get("Город"))
    district_label = district_label_for_slug(district_slug)
    row["district_slug"] = district_slug
    row["district_label"] = district_label
    row["rooms"] = _extract_rooms(row.get("title"))
    row["construction_year"] = _extract_int(raw_listing.get("Год постройки"))
    row["residential_complex"] = _clean_text(raw_listing.get("Жилой комплекс"))
    row["lat"] = _extract_float(raw_listing.get("lat"))
    row["lon"] = _extract_float(raw_listing.get("lon"))
    row["short_title"] = _short_listing_title(row.get("title"), row.get("area_m2"))
    row.pop("raw_json", None)
    return row


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
