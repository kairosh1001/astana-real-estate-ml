from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.refresh_service import run_refresh


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh Krisha listing predictions.")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--pages", type=int, default=100)
    parser.add_argument("--kind", choices=["daily", "weekly", "manual"], default="manual")
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", str(ROOT / "data" / "krisha.sqlite3")),
    )
    parser.add_argument("--min-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=2.0)
    parser.add_argument("--stale-after-missed", type=int, default=3)
    parser.add_argument("--max-listings", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args()
    result = run_refresh(
        root=ROOT,
        db_path=args.db,
        kind=args.kind,
        start_page=args.start_page,
        pages=args.pages,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        stale_after_missed=args.stale_after_missed,
        max_listings=args.max_listings,
    )
    print(result)


if __name__ == "__main__":
    main()
