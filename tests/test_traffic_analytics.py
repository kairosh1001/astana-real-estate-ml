from __future__ import annotations

import unittest
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import (
    connect,
    fetch_traffic_summary,
    init_db,
    record_request_event,
)


class TrafficAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path("data") / f"test-traffic-{uuid.uuid4().hex}.sqlite3"

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)

    def test_daily_chart_starts_tomorrow_and_setting_is_stable(self) -> None:
        expected_start = (
            (datetime.now(timezone.utc) + timedelta(hours=5)).date()
            + timedelta(days=1)
        ).isoformat()
        with closing(connect(self.db_path)) as connection:
            init_db(connection)
            first = fetch_traffic_summary(connection)
            self.assertEqual(first["daily_visitors_start_date"], expected_start)
            self.assertEqual(first["daily_visitors"], [])

            connection.execute(
                "UPDATE analytics_settings SET value = '2020-01-01' "
                "WHERE key = 'daily_visitors_start_date'"
            )
            connection.commit()
            init_db(connection)
            preserved = connection.execute(
                "SELECT value FROM analytics_settings "
                "WHERE key = 'daily_visitors_start_date'"
            ).fetchone()[0]
            self.assertEqual(preserved, "2020-01-01")

    def test_daily_chart_counts_unique_anonymous_visitors(self) -> None:
        today = (datetime.now(timezone.utc) + timedelta(hours=5)).date().isoformat()
        with closing(connect(self.db_path)) as connection:
            init_db(connection)
            connection.execute(
                "UPDATE analytics_settings SET value = ? "
                "WHERE key = 'daily_visitors_start_date'",
                (today,),
            )
            connection.commit()
            for client_hash in ("visitor-a", "visitor-a", "visitor-b"):
                record_request_event(
                    connection,
                    method="GET",
                    path="/",
                    status_code=200,
                    duration_ms=12,
                    client_hash=client_hash,
                    user_agent="test",
                    referer=None,
                )

            summary = fetch_traffic_summary(connection)
            self.assertEqual(summary["daily_visitors"][-1]["day"], today)
            self.assertEqual(summary["daily_visitors"][-1]["visitors"], 2)
            self.assertEqual(summary["daily_visitors"][-1]["height_pct"], 100)


if __name__ == "__main__":
    unittest.main()
