from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    status: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }
    for period in ("monthly", "daily"):
        scrape_status_path = ROOT / "data" / f"rent_{period}_scrape_status.json"
        dataset_path = ROOT / "data" / f"rent_{period}_raw.csv"
        scrape_status = json.loads(scrape_status_path.read_text(encoding="utf-8"))
        frame = pd.read_csv(dataset_path)
        scraped = pd.to_datetime(frame["scraped_at"], errors="coerce", utc=True)
        listing_ids = frame["listing_id"].astype("string")
        status[period] = {
            **scrape_status,
            "snapshot_rows": len(frame),
            "unique_listings": int(listing_ids.nunique()),
            "scrape_days_utc": int(scraped.dt.floor("D").nunique()),
            "latest_scrape": scraped.max().isoformat() if scraped.notna().any() else None,
            "description_coverage": float(frame.get("description", pd.Series(index=frame.index)).notna().mean()),
            "seller_type_coverage": float(frame.get("seller_type", pd.Series(index=frame.index)).notna().mean()),
        }
    output = ROOT / "data" / "rental_collection_status.json"
    output.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
