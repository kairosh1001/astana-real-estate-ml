from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import requests

ROOT = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1]))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import (
    connect,
    fetch_cached_prediction,
    fetch_telegram_subscribers_for_digest,
    fetch_undervalued,
    init_db,
    mark_telegram_digest_sent,
    set_telegram_notifications,
    store_cached_prediction,
    upsert_telegram_subscriber,
)
from app.prediction_service import (
    ListingPrediction,
    PredictionService,
    validate_krisha_url,
)


ASTANA_TZ = timezone(timedelta(hours=5), name="Asia/Astana")
KRISHA_URL_RE = re.compile(r"https?://(?:www\.)?krisha\.kz/[^\s]+", re.IGNORECASE)


class TelegramBot:
    def __init__(
        self,
        *,
        token: str,
        root: Path,
        db_path: Path,
        public_url: str,
        digest_hour: int,
        prediction_cache_ttl_seconds: int,
    ) -> None:
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.root = root
        self.db_path = db_path
        self.public_url = public_url.rstrip("/")
        self.digest_hour = digest_hour
        self.prediction_cache_ttl_seconds = prediction_cache_ttl_seconds
        self.prediction_service = PredictionService(root)

        with connect(db_path) as connection:
            init_db(connection)

    def run(self, *, poll_interval: float = 2.0) -> None:
        offset = 0
        while True:
            try:
                for update in self.get_updates(offset=offset, timeout=25):
                    offset = max(offset, int(update["update_id"]) + 1)
                    self.handle_update(update)
                self.send_due_digests()
            except Exception as exc:
                print(f"[WARN] Telegram bot loop error: {exc}", flush=True)
                time.sleep(5)
            time.sleep(poll_interval)

    def get_updates(self, *, offset: int, timeout: int) -> list[dict]:
        response = requests.get(
            f"{self.api_url}/getUpdates",
            params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": '["message"]',
            },
            timeout=timeout + 5,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload)
        return payload.get("result") or []

    def handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        command = text.split(maxsplit=1)[0].lower()
        if command in {"/start", "/help"}:
            self.subscribe(chat_id)
            self.send_message(chat_id, self.help_text())
            return
        if command in {"/off", "/stop", "/notifications_off"}:
            self.set_notifications(chat_id, enabled=False)
            self.send_message(
                chat_id,
                "Уведомления выключены. Оценка ссылок всё ещё работает. "
                "Чтобы снова получать подборку, отправьте /on.",
            )
            return
        if command in {"/on", "/notifications_on"}:
            self.subscribe(chat_id)
            self.send_message(
                chat_id,
                "Уведомления включены. Я буду отправлять новые выгодные объявления за 24 часа.",
            )
            return

        url = extract_krisha_url(text)
        if not url:
            self.send_message(
                chat_id,
                "Пришлите ссылку на объявление Krisha, например https://krisha.kz/a/show/...",
            )
            return

        self.subscribe(chat_id)
        self.send_message(chat_id, "Оцениваю ссылку, это может занять до 10 секунд...")
        try:
            prediction = self.predict_url(url)
        except ValueError as exc:
            self.send_message(chat_id, str(exc))
            return
        except Exception as exc:
            self.send_message(
                chat_id,
                f"Не получилось оценить объявление: {html.escape(str(exc))}",
            )
            return

        self.send_message(chat_id, format_prediction(prediction, self.public_url))

    def subscribe(self, chat_id: int) -> None:
        with connect(self.db_path) as connection:
            upsert_telegram_subscriber(
                connection,
                chat_id=int(chat_id),
                notifications_enabled=True,
            )

    def set_notifications(self, chat_id: int, *, enabled: bool) -> None:
        with connect(self.db_path) as connection:
            upsert_telegram_subscriber(
                connection,
                chat_id=int(chat_id),
                notifications_enabled=enabled,
            )
            set_telegram_notifications(connection, chat_id=int(chat_id), enabled=enabled)

    def predict_url(self, url: str) -> ListingPrediction:
        normalized_url = normalize_krisha_url(url)
        validate_krisha_url(normalized_url)

        with connect(self.db_path) as connection:
            cached = fetch_cached_prediction(
                connection,
                normalized_url,
                ttl_seconds=self.prediction_cache_ttl_seconds,
            )
        if cached:
            return ListingPrediction(**cached)

        prediction = self.prediction_service.predict_by_url(normalized_url)
        with connect(self.db_path) as connection:
            store_cached_prediction(
                connection,
                url=normalized_url,
                prediction=asdict(prediction),
            )
        return prediction

    def send_due_digests(self) -> None:
        now = datetime.now(ASTANA_TZ)
        if now.hour < self.digest_hour:
            return

        digest_date = now.date().isoformat()
        with connect(self.db_path) as connection:
            subscribers = fetch_telegram_subscribers_for_digest(
                connection,
                digest_date=digest_date,
            )
            listings = fetch_undervalued(
                connection,
                limit=10,
                new_since=(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
                    timespec="seconds"
                ),
                include_stale=False,
            )

        if not subscribers:
            return

        message = format_digest(listings, self.public_url)
        for subscriber in subscribers:
            chat_id = int(subscriber["chat_id"])
            try:
                self.send_message(chat_id, message, disable_web_page_preview=True)
                with connect(self.db_path) as connection:
                    mark_telegram_digest_sent(
                        connection,
                        chat_id=chat_id,
                        digest_date=digest_date,
                    )
            except Exception as exc:
                print(f"[WARN] Failed to send digest to {chat_id}: {exc}", flush=True)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        disable_web_page_preview: bool = False,
    ) -> None:
        response = requests.post(
            f"{self.api_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=15,
        )
        response.raise_for_status()

    def help_text(self) -> str:
        site_text = f"\nСайт: {html.escape(self.public_url)}" if self.public_url else ""
        return (
            "Я оцениваю объявления Krisha по ML-модели и могу присылать новые "
            "выгодные варианты за 24 часа.\n\n"
            "Что можно отправить:\n"
            "• ссылку на объявление Krisha — я дам оценку;\n"
            "• /off — выключить ежедневные уведомления;\n"
            "• /on — включить уведомления;\n"
            "• /help — показать справку."
            f"{site_text}"
        )


def extract_krisha_url(text: str) -> str | None:
    match = KRISHA_URL_RE.search(text)
    return match.group(0).rstrip(".,)") if match else None


def normalize_krisha_url(url: str) -> str:
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    if netloc == "www.krisha.kz":
        netloc = "krisha.kz"
    path = parsed.path.rstrip("/")
    return urlunparse(("https", netloc, path, "", "", ""))


def format_prediction(prediction: ListingPrediction, public_url: str) -> str:
    q10_gain = prediction.pred_price_per_m2_q10 - prediction.listed_price_per_m2
    median_gain = prediction.pred_price_per_m2_q50 - prediction.listed_price_per_m2
    details_url = (
        f"{public_url.rstrip('/')}/listing-details?url={quote(prediction.url, safe='')}"
        if public_url
        else prediction.url
    )
    verdict = (
        "выглядит ниже рынка по q10"
        if q10_gain > 0
        else "не выглядит ниже рынка по q10"
    )
    return (
        f"<b>{html.escape(short_title(prediction.title))}</b>\n"
        f"Вердикт: <b>{verdict}</b>\n\n"
        f"Цена объявления: {prediction.listed_price:,.0f} тг\n"
        f"Цена за м²: {prediction.listed_price_per_m2:,.0f} тг\n"
        f"Нижняя оценка q10: {prediction.pred_price_per_m2_q10:,.0f} тг/м²\n"
        f"Медианная оценка q50: {prediction.pred_price_per_m2_q50:,.0f} тг/м²\n"
        f"Выгода q10: {prediction.discount_vs_asking_pct_conservative:.1%}\n"
        f"Выгода медиана: {prediction.discount_vs_asking_pct_median:.1%}\n"
        f"Абсолютная выгода q10: {q10_gain:,.0f} тг/м²\n"
        f"Абсолютная выгода медиана: {median_gain:,.0f} тг/м²\n\n"
        f"<a href=\"{html.escape(details_url)}\">Подробнее на сайте</a>\n"
        f"<a href=\"{html.escape(prediction.url)}\">Открыть Krisha</a>"
    )


def format_digest(listings: list[dict], public_url: str) -> str:
    if not listings:
        return (
            "<b>Новые выгодные за 24 часа</b>\n\n"
            "За последние 24 часа новых объявлений ниже рынка не найдено."
        )

    lines = ["<b>Новые выгодные за 24 часа</b>", ""]
    for index, item in enumerate(listings, start=1):
        details_url = (
            f"{public_url.rstrip('/')}/listing-details?url={quote(item['url'], safe='')}"
            if public_url
            else item["url"]
        )
        lines.append(
            f"{index}. <b>{html.escape(item.get('short_title') or item.get('title') or 'Объявление')}</b>\n"
            f"   {item.get('listed_price') or 0:,.0f} тг · "
            f"{item.get('listed_price_per_m2') or 0:,.0f} тг/м² · "
            f"q10 выгода {item.get('discount_vs_asking_pct_conservative') or 0:.1%}\n"
            f"   <a href=\"{html.escape(details_url)}\">Подробнее</a> · "
            f"<a href=\"{html.escape(item['url'])}\">Krisha</a>"
        )
    lines.append("")
    lines.append("Чтобы выключить уведомления, отправьте /off.")
    return "\n".join(lines)


def short_title(title: str) -> str:
    cleaned = " ".join(str(title or "Объявление").split())
    return cleaned[:90] + "..." if len(cleaned) > 90 else cleaned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Telegram bot worker.")
    parser.add_argument("--once", action="store_true", help="Run startup checks and exit.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")

    bot = TelegramBot(
        token=token,
        root=ROOT,
        db_path=Path(os.getenv("DB_PATH", ROOT / "data" / "krisha.sqlite3")),
        public_url=os.getenv("APP_PUBLIC_URL", "https://kvartiry-ai.kz"),
        digest_hour=int(os.getenv("TELEGRAM_DIGEST_HOUR_ASTANA", "9")),
        prediction_cache_ttl_seconds=int(
            os.getenv("PREDICTION_CACHE_TTL_SECONDS", str(60 * 60 * 6))
        ),
    )
    if args.once:
        print("[OK] Telegram bot startup checks passed.", flush=True)
        return
    bot.run()


if __name__ == "__main__":
    main()
