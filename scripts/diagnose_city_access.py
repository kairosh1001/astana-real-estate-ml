"""Compare two listing responses from this host; no refresh or database writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrape import ApartmentScraper


def select_targets(db_path: str) -> dict[str, str]:
    # mode=ro prevents accidental database creation or schema/data changes.
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        targets = {}
        for city in ("astana", "almaty"):
            row = connection.execute(
                "SELECT url FROM listings WHERE city = ? AND status = 'active' "
                "ORDER BY last_checked_at DESC, url DESC LIMIT 1", (city,),
            ).fetchone()
            if row is None:
                raise ValueError(f"No active listing in database for {city}")
            targets[city] = row[0]
        validate_targets(targets)
        return targets
    finally:
        connection.close()


def validate_targets(targets: dict[str, str]) -> None:
    if set(targets) != {"astana", "almaty"}:
        raise ValueError("Exactly one URL for each city is required")
    for url in targets.values():
        if not re.fullmatch(r"https://krisha\.kz/a/show/[0-9]+/?", url):
            raise ValueError("Expected a public krisha.kz listing URL")


def inspect_response(response: requests.Response) -> dict:
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    # Only fixed labels are emitted, never cookies, challenge tokens or raw HTML.
    markers = [label for label, needles in {
        "captcha": ("captcha", "капча"),
        "javascript_check": ("enable javascript", "включите javascript"),
        "access_denied": ("access denied", "доступ ограничен", "доступ запрещен"),
        "human_check": ("verify you are human", "подтвердите, что вы человек"),
    }.items() if any(needle in text for needle in needles)]
    return {
        "http_status": response.status_code,
        "headers": {name: response.headers[name][:200] for name in
                    ("Server", "Content-Type", "Retry-After") if name in response.headers},
        "has_listing_title": bool(soup.select_one(".offer__advert-title h1")),
        "has_listing_price": bool(soup.select_one(".offer__price")),
        "protection_markers": markers,
        "body_bytes": len(response.content),
    }


def probe(session: requests.Session, targets: dict[str, str]) -> list[dict]:
    validate_targets(targets)
    results = []
    for index, (city, url) in enumerate(targets.items()):
        if index:
            time.sleep(2)
        row = {"city": city, "url": url,
               "checked_at_utc": datetime.now(timezone.utc).isoformat()}
        started = time.monotonic()
        try:
            # Deliberately inspect the other city once even if the first is rejected.
            # No retries, redirect following, session rotation or challenge solving.
            with session.get(url, timeout=15, allow_redirects=False) as response:
                row.update(inspect_response(response))
        except requests.RequestException as exc:
            row["network_error"] = type(exc).__name__
        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
        results.append(row)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(ROOT / "data/krisha.sqlite3")))
    args = parser.parse_args()
    targets = select_targets(args.db)
    scraper = ApartmentScraper(retry_total=0)
    try:
        results = probe(scraper.session, targets)
    finally:
        scraper.session.close()
    print(json.dumps({"same_session": True, "retries": 0, "results": results},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
