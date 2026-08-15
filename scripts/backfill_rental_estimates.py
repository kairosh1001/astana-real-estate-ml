from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import connect, init_db, store_listing_rental_estimate
from app.rental_model_service import RentalModelService, rental_bundle_complete


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add rental estimates to saved sale listings.")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "krisha.sqlite3")
    parser.add_argument("--include-stale", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not rental_bundle_complete(ROOT):
        raise SystemExit("Rental model bundle is incomplete.")
    service = RentalModelService(ROOT)
    connection = connect(args.db)
    init_db(connection)
    status_sql = "" if args.include_stale else "WHERE status='active'"
    rows = connection.execute(
        f"SELECT url, raw_json, listed_price FROM listings {status_sql} ORDER BY url"
    ).fetchall()
    updated = failed = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_json"])
            estimate = service.estimate(raw, purchase_price=float(row["listed_price"])).to_dict()
            estimate["rental_model_version"] = estimate.pop("model_version")
            store_listing_rental_estimate(connection, url=row["url"], estimate=estimate)
            updated += 1
        except Exception as exc:
            failed += 1
            print(f"failed {row['url']}: {exc}")
        if updated % 100 == 0:
            connection.commit()
    connection.commit()
    connection.close()
    print(f"updated={updated} failed={failed} model={service.model_version}")


if __name__ == "__main__":
    main()
