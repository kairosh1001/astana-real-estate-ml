"""Collect only visible, publicly returned catalog summaries, never detail pages.

These observations are intentionally isolated from valuation inputs: a catalog
summary is not a fully inspected apartment and must not replace one silently.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests

from app.cities import CITIES
from scrape import ApartmentScraper


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_catalog(html: str, city: str) -> tuple[list[dict], int]:
    expected_city = CITIES[city]["name"].casefold()
    soup = BeautifulSoup(html, "html.parser")
    rows, skipped, seen = [], 0, set()
    for card in soup.select(".a-card"):
        def txt(selector):
            element = card.select_one(selector)
            return " ".join(element.get_text(" ", strip=True).split()) if element else ""

        link = card.select_one(".a-card__title[href]")
        url = urljoin("https://krisha.kz", link.get("href", "")) if link else ""
        title = txt(".a-card__title")
        price_text = txt(".a-card__price")
        location = txt(".a-card__stats-item")
        price = re.fullmatch(r"(\d+)(?:₸|〒|тг\.?)", re.sub(r"\s+", "", price_text), re.I)
        rooms = re.search(r"(\d+)-комнатн", title)
        area = re.search(r"(\d+(?:[.,]\d+)?)\s*м[²2]", title)
        # Exclude cross-city promotions, ranges/from-prices, malformed URLs, and
        # incomplete summaries. Never guess which part of a price range is asking.
        if not (
            re.fullmatch(r"https://krisha\.kz/a/show/\d+/?", url)
            and location.casefold() == expected_city and price and rooms and area
        ):
            skipped += 1
            continue
        url = url.rstrip("/")
        if url in seen:
            continue
        asking_price = int(price.group(1))
        area_m2 = float(area.group(1).replace(",", "."))
        if asking_price <= 0 or area_m2 <= 0 or int(rooms.group(1)) <= 0:
            skipped += 1
            continue
        seen.add(url)
        floors = re.search(r"(\d+)(?:\s*/\s*(\d+))?\s*этаж", title)
        row = {
            "url": url, "city": city, "title": title,
            "asking_price": asking_price, "rooms": int(rooms.group(1)),
            "area_m2": area_m2,
            "floor": int(floors.group(1)) if floors else None,
            "total_floors": int(floors.group(2)) if floors and floors.group(2) else None,
            "address_summary": txt(".a-card__subtitle"),
            "description_summary": txt(".a-card__text-preview"),
            "source": "catalog", "data_completeness": "summary_only",
            "model_eligible": False,
        }
        rows.append(row)
    return rows, skipped


def init_catalog_db(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS catalog_runs (
            id INTEGER PRIMARY KEY, city TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL, pages_seen INTEGER DEFAULT 0,
            listings_saved INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, error TEXT
        );
        CREATE TABLE IF NOT EXISTS catalog_listings (
            city TEXT NOT NULL, url TEXT NOT NULL, asking_price INTEGER NOT NULL,
            title TEXT NOT NULL, raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY (city, url)
        );
        CREATE TABLE IF NOT EXISTS catalog_price_history (
            id INTEGER PRIMARY KEY, city TEXT NOT NULL, url TEXT NOT NULL,
            observed_at TEXT NOT NULL, asking_price INTEGER NOT NULL,
            run_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS catalog_history_url
            ON catalog_price_history(city, url, observed_at);
    """)


def save_page(connection: sqlite3.Connection, rows: list[dict], run_id: int) -> tuple[int, int]:
    observed_at, added, price_changes = now(), 0, 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            previous = connection.execute(
                "SELECT asking_price FROM catalog_listings WHERE city=? AND url=?",
                (row["city"], row["url"]),
            ).fetchone()
            connection.execute("""
                INSERT INTO catalog_listings
                (city, url, asking_price, title, raw_json, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(city, url) DO UPDATE SET
                    asking_price=excluded.asking_price, title=excluded.title,
                    raw_json=excluded.raw_json, last_seen_at=excluded.last_seen_at
            """, (row["city"], row["url"], row["asking_price"], row["title"],
                  json.dumps(row, ensure_ascii=False), observed_at, observed_at))
            if previous is None or previous[0] != row["asking_price"]:
                connection.execute("""
                    INSERT INTO catalog_price_history(city, url, observed_at, asking_price, run_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (row["city"], row["url"], observed_at, row["asking_price"], run_id))
                added += previous is None
                price_changes += previous is not None
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return added, price_changes


def run_catalog_refresh(*, db_path: str | Path, city: str, pages: int = 1,
                        delay: float = 5.0) -> dict:
    if city not in CITIES or not 1 <= pages <= 100 or delay < 2:
        raise ValueError("Use astana/almaty, 1-100 pages, and a delay of at least 2 seconds")
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    scraper = None
    result = dict(city=city, status="failed", pages_seen=0, listings_saved=0,
                  new_listings=0, price_changes=0, skipped=0, error=None)
    try:
        init_catalog_db(connection)
        run_id = connection.execute(
            "INSERT INTO catalog_runs(city, started_at, status) VALUES (?, ?, 'running')",
            (city, now()),
        ).lastrowid
        connection.commit()
        try:
            scraper = ApartmentScraper(retry_total=0)
            seen = set()
            for page in range(1, pages + 1):
                if page > 1:
                    time.sleep(delay)
                base = f"https://krisha.kz/prodazha/kvartiry/{city}/"
                url = base if page == 1 else f"{base}?page={page}"
                print(f"[INFO] Catalog {city} page {page}: {url}", flush=True)
                with scraper.session.get(url, timeout=20, allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"Catalog HTTP {response.status_code}; stopped without retries")
                    rows, skipped = parse_catalog(response.text, city)
                result["skipped"] += skipped
                if not rows:
                    raise RuntimeError("No valid catalog summaries; empty/changed page or access check")
                unique_rows = [row for row in rows if row["url"] not in seen]
                added, changes = save_page(connection, unique_rows, run_id)
                seen.update(row["url"] for row in unique_rows)
                result["pages_seen"] += 1
                result["listings_saved"] += len(unique_rows)
                result["new_listings"] += added
                result["price_changes"] += changes
                print(f"[INFO] Saved {len(unique_rows)} catalog summaries; skipped {skipped}", flush=True)
            # Completed means all requested catalog pages were scanned. Excluded
            # cards are counted separately; this never means full detail coverage.
            result["status"] = "completed"
        except Exception as exc:
            # Request exception strings can contain proxy credentials. Keep type only.
            result["error"] = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)[:1000]
            result["status"] = "partial" if result["listings_saved"] else "failed"
        finally:
            connection.execute("""
                UPDATE catalog_runs SET finished_at=?, status=?, pages_seen=?,
                    listings_saved=?, skipped=?, error=? WHERE id=?
            """, (now(), result["status"], result["pages_seen"], result["listings_saved"],
                  result["skipped"], result["error"], run_id))
            connection.commit()
        result["run_id"] = run_id
        return result
    finally:
        if scraper is not None:
            scraper.session.close()
        connection.close()
