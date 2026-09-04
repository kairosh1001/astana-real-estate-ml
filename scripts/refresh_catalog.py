"""Save public catalog summaries and price changes without requesting detail pages."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog_scraper import run_catalog_refresh


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", choices=("astana", "almaty"), required=True)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--delay", type=float, default=5.0)
    # Deliberately not DB_PATH: keep incomplete summaries out of the valuation DB.
    parser.add_argument("--db", default=os.getenv("CATALOG_DB_PATH", str(ROOT / "data/catalog.sqlite3")))
    args = parser.parse_args()
    result = run_catalog_refresh(db_path=args.db, city=args.city, pages=args.pages, delay=args.delay)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
