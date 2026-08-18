from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import (
    connect,
    delete_telegram_admin_chat,
    fetch_telegram_admin_chats_for_report,
    fetch_telegram_admin_report,
    init_db,
    is_telegram_admin_chat,
    mark_telegram_admin_report_sent,
    set_telegram_admin_reports,
    upsert_telegram_admin_chat,
)
from scripts.telegram_bot import format_admin_report


class TelegramAdminDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def test_admin_chat_pairing_schedule_and_revocation(self) -> None:
        upsert_telegram_admin_chat(self.connection, chat_id=101)
        self.assertTrue(is_telegram_admin_chat(self.connection, chat_id=101))
        self.assertEqual(
            fetch_telegram_admin_chats_for_report(
                self.connection,
                report_date="2026-08-18",
            )[0]["chat_id"],
            101,
        )

        mark_telegram_admin_report_sent(
            self.connection,
            chat_id=101,
            report_date="2026-08-18",
        )
        self.assertEqual(
            fetch_telegram_admin_chats_for_report(
                self.connection,
                report_date="2026-08-18",
            ),
            [],
        )
        self.assertTrue(
            set_telegram_admin_reports(
                self.connection,
                chat_id=101,
                enabled=False,
            )
        )
        self.assertEqual(
            fetch_telegram_admin_chats_for_report(
                self.connection,
                report_date="2026-08-19",
            ),
            [],
        )
        self.assertTrue(delete_telegram_admin_chat(self.connection, chat_id=101))
        self.assertFalse(is_telegram_admin_chat(self.connection, chat_id=101))

    def test_report_aggregates_traffic_accounts_and_cities(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.connection.execute(
            """
            INSERT INTO request_events (
                created_at, method, path, status_code, duration_ms,
                client_hash, user_agent, referer
            ) VALUES (?, 'GET', '/listing-details', 500, 250, 'visitor-1', '', '')
            """,
            (now,),
        )
        self.connection.execute(
            """
            INSERT INTO users (
                email, email_normalized, display_name, password_hash,
                accepted_terms_version, accepted_terms_at, created_at, updated_at
            ) VALUES ('owner@example.com', 'owner@example.com', 'Owner', 'hash',
                      '2026-08-01', ?, ?, ?)
            """,
            (now, now, now),
        )
        self.connection.execute(
            """
            INSERT INTO listings (
                url, city, raw_json, first_seen_at, last_seen_at, last_checked_at,
                status, discount_vs_asking_pct_conservative
            ) VALUES ('https://krisha.kz/a/show/1', 'astana', '{}', ?, ?, ?, 'active', 0.1)
            """,
            (now, now, now),
        )
        self.connection.execute(
            """
            INSERT INTO refresh_runs (
                city, started_at, finished_at, kind, start_page, end_page,
                listings_processed, status
            ) VALUES ('astana', ?, ?, 'daily', 1, 1, 1, 'completed')
            """,
            (now, now),
        )
        self.connection.commit()

        report = fetch_telegram_admin_report(self.connection)

        self.assertEqual(report["traffic"]["visitors_24h"], 1)
        self.assertEqual(report["traffic"]["server_errors_24h"], 1)
        self.assertEqual(report["accounts"]["registered_users"], 1)
        self.assertEqual(report["cities"]["astana"]["active_listings"], 1)
        self.assertEqual(report["cities"]["astana"]["new_listings_24h"], 1)


class TelegramAdminFormattingTest(unittest.TestCase):
    def test_report_is_compact_and_contains_operational_sections(self) -> None:
        report = {
            "traffic": {
                "requests_24h": 120,
                "visitors_24h": 15,
                "predictions_24h": 9,
                "rate_limited_24h": 2,
                "server_errors_24h": 1,
                "avg_duration_ms_24h": 142.5,
                "top_pages": [
                    {"path": "/listing-details", "requests": 40, "visitors": 8}
                ],
            },
            "accounts": {
                "registered_users": 3,
                "active_sessions": 2,
                "saved_listings": 7,
            },
            "cities": {
                "astana": {
                    "active_listings": 100,
                    "stale_listings": 4,
                    "below_market_active": 12,
                    "new_listings_24h": 8,
                    "latest_refresh": {
                        "status": "completed",
                        "listings_processed": 50,
                        "listings_failed": 1,
                        "finished_at": "2026-08-18T02:00:00+00:00",
                    },
                },
                "almaty": {
                    "active_listings": 80,
                    "stale_listings": 5,
                    "below_market_active": 9,
                    "new_listings_24h": 6,
                    "latest_refresh": None,
                },
            },
        }
        text = format_admin_report(
            report,
            {"available": True, "status_code": 200, "latency_ms": 91},
            public_url="https://kvartiry-ai.kz",
        )
        for needle in (
            "Ежедневный статус",
            "Трафик за 24 часа",
            "Посетители: <b>15</b>",
            "Ошибки 5xx: <b>1</b>",
            "Зарегистрировано: <b>3</b>",
            "Астана",
            "Алматы",
            "Последние обновления",
        ):
            self.assertIn(needle, text)
        self.assertLess(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
