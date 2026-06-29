from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def seed_listing(db_path: Path) -> None:
    from app.database import connect, init_db

    with connect(db_path) as connection:
        init_db(connection)
        connection.execute(
            """
            INSERT INTO refresh_runs (
                started_at, finished_at, kind, start_page, end_page,
                pages_seen, urls_seen, listings_processed, listings_failed, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-29T00:00:00+00:00",
                "2026-06-29T00:05:00+00:00",
                "daily",
                1,
                50,
                50,
                1050,
                1000,
                5,
                "completed",
            ),
        )
        connection.execute(
            """
            INSERT INTO listings (
                url, title, raw_json, first_seen_at, last_seen_at, last_checked_at,
                missed_refreshes, status, listed_price, area_m2,
                listed_price_per_m2, pred_price_per_m2_q10,
                pred_price_per_m2_q50, pred_price_per_m2_q90, pred_total_q50,
                discount_vs_asking_pct_conservative,
                discount_vs_asking_pct_median, interval_width_pct
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "https://krisha.kz/a/show/123",
                "Тестовая квартира",
                "{}",
                "2026-06-29T00:00:00+00:00",
                "2026-06-29T00:00:00+00:00",
                "2026-06-29T00:00:00+00:00",
                0,
                "active",
                20000000,
                40,
                500000,
                550000,
                600000,
                700000,
                24000000,
                0.10,
                0.20,
                0.25,
            ),
        )
        connection.commit()


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise SystemExit(f"Expected page to contain: {needle}")


def main() -> None:
    db_path = ROOT / "data" / "ui_check.sqlite3"
    db_path.parent.mkdir(exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()

    os.environ["DB_PATH"] = str(db_path)
    seed_listing(db_path)

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    home = client.get("/")
    if home.status_code != 200:
        raise SystemExit(f"Home page returned {home.status_code}")
    assert_contains(home.text, "Квартиры ниже рынка")
    assert_contains(home.text, "История обновлений")

    invalid_url = client.post("/predict", data={"url": "https://example.com/a/show/123"})
    if invalid_url.status_code != 400:
        raise SystemExit(f"Invalid URL check returned {invalid_url.status_code}")
    assert_contains(invalid_url.text, "Ссылка должна вести на krisha.kz")

    undervalued = client.get("/undervalued-page")
    if undervalued.status_code != 200:
        raise SystemExit(f"Undervalued page returned {undervalued.status_code}")

    for needle in [
        "Квартиры ниже рынка",
        "q10/м2",
        "q50/м2",
        "q90/м2",
        "Выгода q10",
        "Выгода q50",
        "Подробнее",
        "/predict?url=",
    ]:
        assert_contains(undervalued.text, needle)

    refresh_runs = client.get("/refresh-runs-page")
    if refresh_runs.status_code != 200:
        raise SystemExit(f"Refresh runs page returned {refresh_runs.status_code}")
    for needle in [
        "История обновлений",
        "ежедневное",
        "завершено",
        "Найдено URL",
        "Обработано",
    ]:
        assert_contains(refresh_runs.text, needle)

    print("[OK] UI checks passed.")


if __name__ == "__main__":
    main()
