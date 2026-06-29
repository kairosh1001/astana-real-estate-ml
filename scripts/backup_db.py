from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "krisha.sqlite3"
DEFAULT_BACKUP_DIR = ROOT / "backups"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up the Krisha SQLite database.")
    parser.add_argument(
        "--db",
        default=os.getenv("DB_PATH", str(DEFAULT_DB)),
        help="SQLite database path to back up.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_BACKUP_DIR),
        help="Directory where backup files are written.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=14,
        help="Number of newest backup files to keep. Use 0 to keep all.",
    )
    return parser.parse_args()


def backup_database(db_path: Path, out_dir: Path) -> Path:
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    backup_path = out_dir / f"{db_path.stem}-{timestamp}.sqlite3"

    try:
        source = sqlite3.connect(db_path)
        try:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        raise SystemExit(
            "Could not back up the SQLite database. "
            "If this is Windows with a Docker bind mount, run the backup script "
            "from the host Python environment instead of inside Docker. "
            f"SQLite error: {exc}"
        ) from exc

    return backup_path


def prune_backups(out_dir: Path, db_stem: str, keep: int) -> None:
    if keep <= 0:
        return

    backups = sorted(
        out_dir.glob(f"{db_stem}-*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[keep:]:
        old_backup.unlink()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    out_dir = Path(args.out_dir)

    backup_path = backup_database(db_path, out_dir)
    prune_backups(out_dir, db_path.stem, args.keep)
    print(f"[OK] Backup written: {backup_path}")


if __name__ == "__main__":
    main()
