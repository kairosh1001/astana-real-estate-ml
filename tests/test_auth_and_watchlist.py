from __future__ import annotations

import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.auth import (
    AuthValidationError,
    hash_password,
    normalize_display_name,
    normalize_email,
    session_token_hash,
    validate_password,
    verify_password,
)
from app.database import (
    connect,
    create_user,
    create_user_session,
    delete_user_session,
    fetch_saved_listing_urls,
    fetch_saved_listings,
    fetch_user_by_email,
    fetch_user_session,
    init_db,
    save_user_listing,
    unsave_user_listing,
    update_saved_listing_note,
    utc_now,
)


class AuthSecurityTests(unittest.TestCase):
    def test_email_and_name_normalization(self) -> None:
        self.assertEqual(normalize_email("  USER@Example.COM "), "user@example.com")
        self.assertEqual(normalize_display_name("  Айжан   Тестова "), "Айжан Тестова")
        with self.assertRaises(AuthValidationError):
            normalize_email("not-an-email")
        with self.assertRaises(AuthValidationError):
            normalize_display_name("A")

    def test_argon2_password_hash_does_not_expose_password(self) -> None:
        password = "very-secure-password"
        encoded = hash_password(password)
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertNotIn(password, encoded)
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        with self.assertRaises(AuthValidationError):
            validate_password("short")

    def test_session_tokens_are_stored_as_fixed_digests(self) -> None:
        digest = session_token_hash("raw-browser-token")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("raw-browser-token", digest)


class UserDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path("data") / f"test-auth-{uuid.uuid4().hex}.sqlite3"
        self.connection = connect(self.db_path)
        init_db(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        for suffix in ["", "-wal", "-shm"]:
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)

    def create_test_user(self, email: str = "buyer@example.com") -> dict:
        normalized = normalize_email(email)
        return create_user(
            self.connection,
            email=normalized,
            email_normalized=normalized,
            display_name="Покупатель",
            password_hash=hash_password("account-password-123"),
            accepted_terms_version="2026-08-16",
        )

    def test_schema_is_idempotent_and_email_is_unique(self) -> None:
        init_db(self.connection)
        user = self.create_test_user()
        loaded = fetch_user_by_email(self.connection, "buyer@example.com")
        self.assertEqual(loaded["id"], user["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_test_user("BUYER@example.com")

    def test_session_expiry_and_revocation(self) -> None:
        user = self.create_test_user()
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        create_user_session(
            self.connection,
            token_hash=session_token_hash("expired"),
            user_id=user["id"],
            csrf_token="expired-csrf",
            expires_at=expired_at,
            user_agent="unittest",
        )
        self.assertIsNone(
            fetch_user_session(self.connection, session_token_hash("expired"))
        )

        active_hash = session_token_hash("active")
        create_user_session(
            self.connection,
            token_hash=active_hash,
            user_id=user["id"],
            csrf_token="active-csrf",
            expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            user_agent="unittest",
        )
        self.assertEqual(fetch_user_session(self.connection, active_hash)["id"], user["id"])
        self.assertTrue(delete_user_session(self.connection, active_hash))
        self.assertIsNone(fetch_user_session(self.connection, active_hash))

    def test_saved_listings_are_isolated_and_track_price_changes(self) -> None:
        first_user = self.create_test_user()
        second_user = self.create_test_user("second@example.com")
        listing_url = "https://krisha.kz/a/show/123"
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO listings (
                url, city, title, raw_json, first_seen_at, last_seen_at,
                last_checked_at, status, listed_price, area_m2,
                listed_price_per_m2, pred_price_per_m2_q10,
                pred_price_per_m2_q50, pred_price_per_m2_q90,
                discount_vs_asking_pct_conservative
            )
            VALUES (?, 'astana', ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_url,
                "2-комнатная квартира, 60 м²",
                '{"Город":"Астана, Есиль р-н","Адрес":"Тест 1"}',
                now,
                now,
                now,
                20_000_000,
                60,
                333_333,
                350_000,
                380_000,
                420_000,
                0.05,
            ),
        )
        self.connection.commit()

        self.assertTrue(
            save_user_listing(
                self.connection,
                user_id=first_user["id"],
                listing_url=listing_url,
                saved_price=22_000_000,
                saved_title="Снимок заголовка",
                saved_city="astana",
            )
        )
        self.assertFalse(
            save_user_listing(
                self.connection,
                user_id=first_user["id"],
                listing_url=listing_url,
            )
        )
        self.assertEqual(fetch_saved_listing_urls(self.connection, second_user["id"]), [])
        item = fetch_saved_listings(self.connection, first_user["id"])[0]
        self.assertEqual(item["price_change"], -2_000_000)
        self.assertTrue(item["is_available"])

        self.assertTrue(
            update_saved_listing_note(
                self.connection,
                user_id=first_user["id"],
                listing_url=listing_url,
                note="Позвонить продавцу",
            )
        )
        self.assertEqual(
            fetch_saved_listings(self.connection, first_user["id"])[0]["note"],
            "Позвонить продавцу",
        )
        self.assertTrue(
            unsave_user_listing(
                self.connection,
                user_id=first_user["id"],
                listing_url=listing_url,
            )
        )

    def test_untracked_listing_can_still_be_saved(self) -> None:
        user = self.create_test_user()
        url = "https://krisha.kz/a/show/not-yet-scraped"
        self.assertTrue(
            save_user_listing(
                self.connection,
                user_id=user["id"],
                listing_url=url,
                saved_title="Проверено по ссылке",
                saved_city="almaty",
            )
        )
        item = fetch_saved_listings(self.connection, user["id"])[0]
        self.assertEqual(item["url"], url)
        self.assertFalse(item["is_available"])


if __name__ == "__main__":
    unittest.main()
