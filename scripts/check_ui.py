from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def seed_listing(db_path: Path) -> None:
    from app.database import connect, create_monitoring_snapshot, init_db, utc_now

    with connect(db_path) as connection:
        init_db(connection)
        first_seen_at = utc_now()
        refresh_cursor = connection.execute(
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
        run_id = int(refresh_cursor.lastrowid)
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
                "3-комнатная квартира, 80 м², 7/12 этаж, рядом с парком",
                '{"Город": "Астана, Есиль р-н", "Адрес": "Кабанбай батыра 48", "Год постройки": "2020", "Жилой комплекс": "Test ЖК", "Застройщик": "Test Developer", "Состояние квартиры": "свежий ремонт", "Квартира меблирована": "полностью меблирована", "Новостройка": true, "lat": 51.13, "lon": 71.43}',
                first_seen_at,
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
        connection.execute(
            """
            INSERT INTO listing_price_history (
                url, observed_at, listed_price, listed_price_per_m2, status
            )
            VALUES (?, ?, ?, ?, ?), (?, ?, ?, ?, ?)
            """,
            (
                "https://krisha.kz/a/show/123",
                "2026-06-29T00:00:00+00:00",
                21000000,
                525000,
                "active",
                "https://krisha.kz/a/show/123",
                "2026-06-30T00:00:00+00:00",
                20000000,
                500000,
                "active",
            ),
        )
        connection.execute(
            """
            INSERT INTO listings (
                url, city, title, raw_json, first_seen_at, last_seen_at,
                last_checked_at, missed_refreshes, status, listed_price, area_m2,
                listed_price_per_m2, pred_price_per_m2_q10,
                pred_price_per_m2_q50, pred_price_per_m2_q90, pred_total_q50,
                discount_vs_asking_pct_conservative,
                discount_vs_asking_pct_median, interval_width_pct
            )
            SELECT ?, 'almaty', title, ?, first_seen_at, last_seen_at,
                   last_checked_at, missed_refreshes, status, listed_price, area_m2,
                   listed_price_per_m2, pred_price_per_m2_q10,
                   pred_price_per_m2_q50, pred_price_per_m2_q90, pred_total_q50,
                   discount_vs_asking_pct_conservative,
                   discount_vs_asking_pct_median, interval_width_pct
            FROM listings
            WHERE url = ?
            """,
            (
                "https://krisha.kz/a/show/456-almaty",
                '{"scrape_city": "almaty", "Город": "Алматы, Бостандыкский р-н", "Адрес": "Аль-Фараби 77", "Год постройки": "2021", "Жилой комплекс": "Test Almaty ЖК", "Состояние квартиры": "свежий ремонт", "Новостройка": true, "lat": 43.22, "lon": 76.93}',
                "https://krisha.kz/a/show/123",
            ),
        )
        connection.commit()
        create_monitoring_snapshot(connection, run_id=run_id)


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise SystemExit(f"Expected page to contain: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise SystemExit(f"Expected page not to contain: {needle}")


def form_value(html: str, name: str) -> str:
    from bs4 import BeautifulSoup

    field = BeautifulSoup(html, "html.parser").find("input", {"name": name})
    if not field or not field.get("value"):
        raise SystemExit(f"Expected form field {name} to have a value")
    return str(field["value"])


def meta_content(html: str, name: str) -> str:
    from bs4 import BeautifulSoup

    field = BeautifulSoup(html, "html.parser").find("meta", {"name": name})
    if not field or not field.get("content"):
        raise SystemExit(f"Expected meta field {name} to have content")
    return str(field["content"])


def check_complex_developer_parser() -> None:
    from bs4 import BeautifulSoup
    from scrape import ApartmentScraper

    scraper = ApartmentScraper()
    html = """
    <div class="complex__sidebar-info">
      <div>\u0417\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a</div>
      <div class="complex__sidebar-info-text">Sensata Group</div>
    </div>
    """
    developer = scraper.parse_complex_developer(BeautifulSoup(html, "html.parser"))
    if developer != "Sensata Group":
        raise SystemExit("Complex developer parser did not read the visible developer block")

    meta_html = """
    <html><head>
      <meta name="description" content="\u041a\u0443\u043f\u0438\u0442\u044c \u043a\u0432\u0430\u0440\u0442\u0438\u0440\u0443 \u043e\u0442 \u0437\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a\u0430 Sensata Group - \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u044b\u0435 \u0446\u0435\u043d\u044b">
    </head></html>
    """
    developer = scraper.parse_complex_developer(BeautifulSoup(meta_html, "html.parser"))
    if developer != "Sensata Group":
        raise SystemExit("Complex developer parser did not read the meta fallback")


def check_listing_field_parser() -> None:
    from scrape import ApartmentScraper

    scraper = ApartmentScraper()
    try:
        retry_policy = scraper.session.get_adapter("https://").max_retries
        if retry_policy.total != 4 or 429 not in retry_policy.status_forcelist:
            raise SystemExit("Krisha scraper retry policy is not configured")
        new_build_html = """
        <div class="offer__advert-title"><h1>2-комнатная квартира · 60 м²</h1></div>
        <div class="offer__price">30 000 000 ₸</div>
        <script id="jsdata">
          window.data = {"advert": {"map": {"lat": 51.1, "lon": 71.4},
          "userType": "builder", "ownerName": "Test Builder"}};
          window.afterData = true;
        </script>
        <a href="/prodazha/kvartiry/astana/?das%5Bnovostroiki%5D=1">Продажа квартир</a>
        <div class="offer__parameters">
          <dl><dt>Состояние квартиры</dt><dd>  черновая\nотделка  </dd></dl>
        </div>
        """
        parsed = scraper.parse_apartment_html(
            "https://krisha.kz/a/show/456",
            new_build_html,
        )
        if not parsed:
            raise SystemExit("Listing parser returned no data")
        if parsed.get("Состояние квартиры") != "черновая отделка":
            raise SystemExit("Listing parser did not normalize apartment condition")
        if parsed.get("Новостройка") is not True:
            raise SystemExit("Listing parser did not detect a new-build offer")
        if parsed.get("Застройщик") != "Test Builder":
            raise SystemExit("Listing parser did not retain builder identity")

        resale_html = """
        <div class="offer__advert-title"><h1>2-комнатная квартира · 60 м²</h1></div>
        <div class="offer__price">30 000 000 ₸</div>
        <script id="jsdata">window.data = {"advert": {"userType": "specialist"}};</script>
        <div class="offer__advert-info">
          <div class="offer__info-item">
            <span class="offer__info-title">Состояние квартиры</span>
            <span class="offer__advert-short-info">свежий ремонт</span>
          </div>
        </div>
        """
        parsed_resale = scraper.parse_apartment_html(
            "https://krisha.kz/a/show/789",
            resale_html,
        )
        if not parsed_resale or parsed_resale.get("Новостройка") is not False:
            raise SystemExit("Listing parser incorrectly marked a resale offer as new-build")
        if parsed_resale.get("Состояние квартиры") != "свежий ремонт":
            raise SystemExit("Listing parser did not read condition from summary fields")
    finally:
        scraper.session.close()


def check_refresh_storage_safety() -> None:
    from app.database import (
        connect,
        init_db,
        recover_abandoned_refreshes,
        upsert_listing_prediction,
        utc_now,
    )
    from app.prediction_service import ListingPrediction

    db_path = ROOT / "data" / "refresh_safety_check.sqlite3"
    for suffix in ["", "-wal", "-shm"]:
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()
    prediction = ListingPrediction(
        url="https://krisha.kz/a/show/refresh-safety",
        title="2-комнатная квартира, 60 м²",
        listed_price=30000000,
        area_m2=60,
        listed_price_per_m2=500000,
        pred_price_per_m2_q10=520000,
        pred_price_per_m2_q50=560000,
        pred_price_per_m2_q90=620000,
        pred_total_q50=33600000,
        discount_vs_asking_pct_conservative=0.04,
        discount_vs_asking_pct_median=0.12,
        interval_width_pct=0.18,
    )
    connection = connect(db_path)
    try:
        init_db(connection)
        raw_listing = {
            "url": prediction.url,
            "title": prediction.title,
            "price": "30000000",
            "Город": "Астана, Есиль р-н",
        }
        upsert_listing_prediction(
            connection,
            raw_listing=raw_listing,
            prediction=prediction,
        )
        upsert_listing_prediction(
            connection,
            raw_listing=raw_listing,
            prediction=prediction,
        )
        daily_rows = connection.execute(
            "SELECT COUNT(*) FROM listing_price_history WHERE url = ?",
            (prediction.url,),
        ).fetchone()[0]
        if daily_rows != 1:
            raise SystemExit("Refresh stored more than one price point per day")

        connection.execute(
            """
            INSERT INTO refresh_runs (started_at, kind, start_page, end_page, status)
            VALUES (?, 'daily', 1, 100, 'running'),
                   (?, 'daily', 1, 100, 'running')
            """,
            ("2020-01-01T00:00:00+00:00", utc_now()),
        )
        connection.commit()
        recovered = recover_abandoned_refreshes(connection)
        if recovered != 1:
            raise SystemExit("Interrupted refresh recovery count is incorrect")
        statuses = connection.execute(
            "SELECT status FROM refresh_runs ORDER BY id"
        ).fetchall()
        if [row["status"] for row in statuses] != ["failed", "running"]:
            raise SystemExit("Interrupted refresh recovery changed wrong runs")
    finally:
        connection.close()
        for suffix in ["", "-wal", "-shm"]:
            path = Path(str(db_path) + suffix)
            if path.exists():
                path.unlink()


def check_telegram_digest_format() -> None:
    from scripts.telegram_bot import format_digest

    text = format_digest(
        [
            {
                "url": "https://krisha.kz/a/show/123",
                "listing_summary": "2-комнатная квартира · 55 м², есиль",
                "listed_price": 30000000,
                "listed_price_per_m2": 545455,
                "discount_vs_asking_pct_conservative": 0.12,
            }
        ],
        "https://kvartiry-ai.kz",
    )
    assert_contains(text, "Новые выгодные квартиры за 24 часа")
    assert_contains(text, "2-комнатная квартира · 55 м², есиль")


def check_market_dashboard_calculations(db_path: Path) -> None:
    from app.database import connect, fetch_market_dashboard

    with connect(db_path) as connection:
        dashboard = fetch_market_dashboard(connection)
    if dashboard["city"]["count"] != 1:
        raise SystemExit("Market dashboard active listing count is incorrect")
    if dashboard["city"]["median_price_per_m2"] != 500000:
        raise SystemExit("Market dashboard city median is incorrect")
    if dashboard["districts"][0]["name"] != "Есиль":
        raise SystemExit("Market dashboard district normalization is incorrect")
    history = dashboard["historical"]
    if history["available"]:
        raise SystemExit("Market history should require at least eight daily points")
    if history["observation_count"] != 2 or history["day_count"] != 2:
        raise SystemExit("Market history observation coverage is incorrect")
    if history["eligible_price_change_count"] != 1 or history["price_cut_count"] != 1:
        raise SystemExit("Market price-cut calculation is incorrect")
    if not 0.047 < history["median_price_cut"] < 0.048:
        raise SystemExit("Market median price-cut percentage is incorrect")

    with connect(db_path) as connection:
        for day in range(1, 7):
            connection.execute(
                """
                INSERT INTO listing_price_history (
                    url, observed_at, listed_price, listed_price_per_m2, status
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "https://krisha.kz/a/show/123",
                    f"2026-07-{day:02d}T00:00:00+00:00",
                    20000000 - day * 100000,
                    500000 - day * 2500,
                    "active",
                ),
            )
        connection.commit()
        trend_dashboard = fetch_market_dashboard(connection)
        connection.execute(
            "DELETE FROM listing_price_history WHERE observed_at >= ?",
            ("2026-07-01T00:00:00+00:00",),
        )
        connection.commit()
    trend = trend_dashboard["historical"]
    if not trend["available"] or trend["day_count"] != 8:
        raise SystemExit("Market history did not enable the trend after eight days")
    if len(trend["chart_points"]) != 8 or not trend["polyline"]:
        raise SystemExit("Market history chart coordinates are incomplete")


def main() -> None:
    db_path = ROOT / "data" / "ui_check.sqlite3"
    db_path.parent.mkdir(exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()

    os.environ["DB_PATH"] = str(db_path)
    os.environ["ADMIN_TOKEN"] = "test-token"
    os.environ["TELEGRAM_BOT_USERNAME"] = "krisha_test_bot"
    seed_listing(db_path)
    check_market_dashboard_calculations(db_path)
    check_complex_developer_parser()
    check_listing_field_parser()
    check_refresh_storage_safety()
    check_telegram_digest_format()

    from fastapi.testclient import TestClient
    import app.main as main

    refresh_calls = []

    def fake_run_refresh(**kwargs) -> None:
        refresh_calls.append(kwargs)

    main.run_refresh = fake_run_refresh
    client = TestClient(main.app)

    home = client.get("/")
    if home.status_code != 200:
        raise SystemExit(f"Home page returned {home.status_code}")
    assert_contains(home.text, "Kvartiry-ai.kz")
    assert_contains(home.text, "Подбор квартир Krisha.kz с помощью ИИ")
    assert_contains(
        home.text,
        "Найдите нужную вам квартиру в Астане или Алматы по выгодной цене с помощью искусственного интеллекта. Быстро, бесплатно, эффективно.",
    )
    assert_not_contains(home.text, "Как работает ИИ модель")
    assert_not_contains(home.text, "Локальная оценка вместо усреднения")
    assert_not_contains(home.text, "Уже нашли квартиру?")
    assert_contains(home.text, "Светлая тема")
    assert_contains(home.text, 'savedTheme === "light" ? "light" : "dark"')
    assert_contains(home.text, "Какая квартира вам нужна?")
    assert_contains(home.text, "Поиск по базе сервиса")
    assert_contains(home.text, 'class="home-search-bar"')
    assert_contains(home.text, 'action="/find-home-page#finder-results"')
    assert_contains(home.text, 'name="city"')
    assert_contains(home.text, 'name="room"')
    assert_contains(home.text, 'name="min_price"')
    assert_contains(home.text, 'name="max_price"')
    assert_contains(home.text, 'name="housing_type" type="checkbox" value="new"')
    assert_contains(home.text, "Только новостройки")
    assert_contains(home.text, "Найти квартиру")
    assert_contains(home.text, "медиана за м²")
    assert_not_contains(home.text, "CatBoost")
    assert_contains(home.text, "Топ-10 квартир ниже рынка")
    assert_contains(home.text, "Новые выгодные квартиры за 24 часа")
    assert_contains(home.text, "объявлений в базе сервиса")
    assert_contains(home.text, 'data-home-city-switcher')
    assert_contains(home.text, 'data-home-city-button="astana"')
    assert_contains(home.text, 'data-home-city-button="almaty"')
    assert_contains(home.text, 'data-home-city-panel="almaty" hidden')
    assert_contains(home.text, "Открыть весь рейтинг")
    assert_not_contains(home.text, "модельной оценкой")
    assert_contains(home.text, "1 активных")
    assert_contains(home.text, "Медианная оценка")
    assert_contains(home.text, "3-комнатная квартира · 40 м²")
    assert_contains(home.text, "Есиль")
    assert_contains(home.text, "Жилой комплекс")
    assert_contains(home.text, "Test ЖК")
    assert_contains(home.text, "Сохранить")
    assert_contains(home.text, "Скрыть")
    assert_contains(home.text, "Сравнить")
    assert_contains(home.text, "Telegram")
    assert_contains(home.text, "Оценить по ссылке")
    assert_contains(home.text, "Оценить квартиру по ссылке")
    assert_contains(home.text, "Смотреть рейтинги квартир")
    assert_contains(home.text, "Подобрать квартиру")
    assert_contains(home.text, "Как это работает")
    assert_contains(home.text, "Умный список")
    assert_contains(home.text, "Регистрация")
    asset_version = main.STATIC_ASSET_VERSION
    assert len(asset_version) == 12
    assert_contains(home.text, f"/static/krisha-ai-mark.png?v={asset_version}")
    assert_contains(home.text, f"/static/site.css?v={asset_version}")
    assert_contains(home.text, 'width="42" height="42"')
    assert home.text.index("Какая квартира вам нужна?") < home.text.index("Новые выгодные квартиры за 24 часа")
    assert home.text.index("Топ-10 квартир ниже рынка") < home.text.index("Как это работает")
    site_css = client.get(f"/static/site.css?v={asset_version}")
    assert site_css.status_code == 200
    assert_contains(site_css.text, ".brand-mark img")
    assert_contains(site_css.text, "max-width: 42px")
    assert_contains(site_css.text, "@media (max-width: 1360px) and (min-width: 761px)")
    assert_contains(site_css.text, "@media (max-width: 900px)")
    assert_contains(site_css.text, "@media (max-width: 540px)")
    assert_not_contains(site_css.text, "width: 100vw")
    assert_contains(site_css.text, "body:has(main > .hero) main > :not(.hero)")
    assert_contains(site_css.text, "body:has(main > .hero) main > .hero")
    assert_contains(home.text, 'window.matchMedia("(min-width: 901px)")')
    assert_contains(
        home.text,
        'row.hasAttribute("data-watchlist-card")) window.location.reload()',
    )
    favicon = client.get("/favicon.ico", follow_redirects=False)
    assert favicon.status_code == 307
    assert favicon.headers["location"] == f"/static/krisha-ai-mark.png?v={asset_version}"
    assert_contains(home.text, "© 2026 Kvartiry-ai.kz. Все права защищены.")
    assert_contains(home.text, "Условия использования")
    assert_contains(home.text, "Конфиденциальность")
    assert_contains(home.text, "/model-page")
    assert_contains(home.text, "/market-page")
    assert_contains(home.text, "/find-home-page")
    assert_contains(home.text, "Анализ рынка")
    assert_not_contains(home.text, "/about-page")
    assert_not_contains(home.text, ">О проекте<")
    assert_contains(home.text, "https://t.me/krisha_test_bot")
    assert_contains(home.text, "Telegram бот")
    assert_not_contains(home.text, "Статус сервиса")
    assert_not_contains(home.text, "История обновлений")
    assert_not_contains(home.text, "Админ: обновить данные")
    assert_not_contains(home.text, 'href="/feedback-page">Предложения</a>')
    assert_not_contains(home.text, "Оценка объявлений Krisha с помощью ИИ")
    assert_not_contains(
        home.text,
        "Если список пуст, новых вариантов с запасом по низкой оценке за сутки не найдено",
    )
    assert_not_contains(
        home.text,
        "Рейтинг строится по низкой оценке q10: если q10 выше цены объявления, вариант попадает в базу",
    )
    assert_not_contains(home.text, 'class="city-switch"')
    nav_start = home.text.index('<nav class="site-nav"')
    nav_end = home.text.index("</nav>", nav_start)
    home_nav = home.text[nav_start:nav_end]
    nav_links = [
        'href="/find-home-page?city=astana">Подобрать квартиру</a>',
        'href="/predict-page">Оценить по ссылке</a>',
        'href="/undervalued-page?city=astana">Квартиры ниже рынка</a>',
        'href="/market-page?city=astana">Анализ рынка</a>',
    ]
    nav_positions = [home_nav.index(link) for link in nav_links]
    if nav_positions != sorted(nav_positions):
        raise SystemExit("Main navigation links are not in the expected order")

    neutral_home_with_legacy_query = client.get("/?city=almaty")
    if neutral_home_with_legacy_query.status_code != 200:
        raise SystemExit(
            f"Neutral home with legacy city query returned {neutral_home_with_legacy_query.status_code}"
        )
    for needle in [
        "Найдите нужную вам квартиру в Астане или Алматы",
        "Какая квартира вам нужна?",
        '<option value="almaty">Алматы</option>',
        'href="/undervalued-page?city=almaty"',
        "Бостандык",
        "Test Almaty ЖК",
    ]:
        assert_contains(neutral_home_with_legacy_query.text, needle)
    assert_contains(neutral_home_with_legacy_query.text, "Test ЖК")

    almaty_market = client.get("/market-page?city=almaty")
    if almaty_market.status_code != 200:
        raise SystemExit(f"Almaty market page returned {almaty_market.status_code}")
    assert_contains(almaty_market.text, "Рынок квартир в Алматы")
    assert_contains(almaty_market.text, "Бостандык")
    assert_not_contains(almaty_market.text, "Есиль")
    assert_contains(almaty_market.text, 'aria-label="Город анализа рынка"')

    almaty_rating = client.get("/undervalued-page?city=almaty")
    if almaty_rating.status_code != 200:
        raise SystemExit(f"Almaty rating returned {almaty_rating.status_code}")
    assert_contains(almaty_rating.text, "Квартиры ниже рынка в Алматы")
    assert_contains(almaty_rating.text, "Бостандык")
    assert_not_contains(almaty_rating.text, "Есиль")
    assert_contains(almaty_rating.text, "43.238")
    assert_contains(almaty_rating.text, "76.945")
    assert_contains(almaty_rating.text, 'aria-label="Город рейтинга"')
    assert_contains(
        almaty_rating.text,
        "/login?next=/undervalued-page%3Fcity%3Dalmaty",
    )

    legacy_combined_rating = client.get("/undervalued-page?city=both")
    if legacy_combined_rating.status_code != 200:
        raise SystemExit(
            f"Legacy combined rating returned {legacy_combined_rating.status_code}"
        )
    assert_contains(legacy_combined_rating.text, "Квартиры ниже рынка в Астане")
    assert_contains(legacy_combined_rating.text, "Test ЖК")
    assert_not_contains(legacy_combined_rating.text, "Test Almaty ЖК")
    assert_not_contains(legacy_combined_rating.text, "Рейтинг двух городов")

    home_finder = client.get("/find-home-page")
    if home_finder.status_code != 200:
        raise SystemExit(f"Home finder returned {home_finder.status_code}")
    for needle in [
        "Подобрать квартиру для жизни",
        "Личные приоритеты",
        "Лучшие совпадения в Астане",
        "Почему подходит",
        "Компромиссы",
        "Мебель: полностью меблирована",
        "OpenStreetMap",
        "Откуда берутся расстояния",
        "Как рассчитывается процент совпадения?",
        "Неважно» — 0",
        "Открыть ближайший объект на OpenStreetMap",
        "3-комнатная квартира · 40 м²",
        "Сбалансированный",
        "Текущая стратегия",
        "Цена важнее всего",
        "Выбран",
        'action="/find-home-page#finder-results"',
        'aria-label="Город подбора"',
        "Выберите город",
        "Настроить фильтры",
        'id="finder-settings"',
    ]:
        assert_contains(home_finder.text, needle)
    assert_not_contains(
        home_finder.text,
        "Задайте обязательные условия и расставьте приоритеты",
    )
    first_finder_card = home_finder.text.index('class="finder-card"')
    distance_method = home_finder.text.index("Откуда берутся расстояния?")
    match_method = home_finder.text.index("Как рассчитывается процент совпадения?")
    if not (first_finder_card < distance_method < match_method):
        raise SystemExit("Finder methodology must appear after listing results")

    family_home_finder = client.get(
        "/find-home-page?priority_park=2&priority_education=2"
        "&priority_transit=1&priority_grocery=2&priority_value=1"
        "&priority_ready=1&priority_modern=0"
    )
    if family_home_finder.status_code != 200:
        raise SystemExit(
            f"Family home finder returned {family_home_finder.status_code}"
        )
    assert_contains(family_home_finder.text, "Максимальный вес получают школы")
    assert_contains(family_home_finder.text, 'aria-current="true"')

    filtered_home_finder = client.get(
        "/find-home-page?district=yesil&room=3&min_price=19000000&max_price=21000000"
        "&housing_type=new&condition=fresh_repair&furnished_only=1"
        "&priority_park=2&priority_value=2"
    )
    if filtered_home_finder.status_code != 200:
        raise SystemExit(
            f"Filtered home finder returned {filtered_home_finder.status_code}"
        )
    assert_contains(filtered_home_finder.text, "3-комнатная квартира · 40 м²")
    assert_contains(filtered_home_finder.text, 'name="room" value="3" checked')
    assert_contains(filtered_home_finder.text, 'name="min_price" type="text" inputmode="numeric" data-price-input value="19 000 000"')
    assert_contains(filtered_home_finder.text, 'name="furnished_only" value="1" checked')
    assert_contains(
        filtered_home_finder.text,
        'data-url="/find-home-page?district=yesil&amp;room=3',
    )

    below_minimum_home_finder = client.get("/find-home-page?min_price=21000000")
    if below_minimum_home_finder.status_code != 200:
        raise SystemExit(
            f"Minimum-price home finder returned {below_minimum_home_finder.status_code}"
        )
    assert_contains(
        below_minimum_home_finder.text,
        "По выбранным обязательным условиям квартир не найдено",
    )

    empty_home_finder = client.get("/find-home-page?room=1")
    if empty_home_finder.status_code != 200:
        raise SystemExit(f"Empty home finder returned {empty_home_finder.status_code}")
    assert_contains(empty_home_finder.text, "По выбранным обязательным условиям квартир не найдено")

    for path, needle in [
        ("/how-it-works", "Как работает Kvartiry-ai.kz"),
        ("/features", "Инструменты для осознанного поиска квартиры"),
        ("/about", "Делаем рынок квартир понятнее"),
        ("/contact", "Расскажите, что стоит улучшить"),
        ("/terms", "Условия использования"),
        ("/privacy", "Политика конфиденциальности"),
    ]:
        page = client.get(path)
        if page.status_code != 200:
            raise SystemExit(f"Public page {path} returned {page.status_code}")
        assert_contains(page.text, needle)

    auth_client = TestClient(main.app)
    register_page = auth_client.get("/register?next=/saved-listings")
    if register_page.status_code != 200:
        raise SystemExit(f"Registration page returned {register_page.status_code}")
    assert_contains(register_page.text, "Сохраните свой поиск")
    assert_contains(register_page.text, "политику конфиденциальности")
    assert_contains(register_page.text, "/login?next=/saved-listings")
    assert_not_contains(register_page.text, "/login?next=/register")
    registration_csrf = form_value(register_page.text, "csrf_token")

    mismatched_registration = auth_client.post(
        "/register",
        data={
            "display_name": "Тестовый пользователь",
            "email": "buyer@example.com",
            "password": "secure-password-123",
            "password_confirm": "different-password-123",
            "csrf_token": registration_csrf,
            "accept_terms": "yes",
            "next": "/saved-listings",
        },
    )
    if mismatched_registration.status_code != 400:
        raise SystemExit(
            f"Mismatched registration returned {mismatched_registration.status_code}"
        )
    assert_contains(mismatched_registration.text, "Пароли не совпадают")

    registered = auth_client.post(
        "/register",
        data={
            "display_name": "Тестовый пользователь",
            "email": "buyer@example.com",
            "password": "secure-password-123",
            "password_confirm": "secure-password-123",
            "csrf_token": registration_csrf,
            "accept_terms": "yes",
            "remember_me": "yes",
            "next": "/saved-listings",
        },
        follow_redirects=False,
    )
    if registered.status_code != 303 or registered.headers.get("location") != "/saved-listings":
        raise SystemExit("Successful registration did not redirect to the watchlist")
    session_cookie = registered.headers.get("set-cookie", "").lower()
    if "httponly" not in session_cookie or "samesite=lax" not in session_cookie:
        raise SystemExit("User session cookie is missing security attributes")

    account = auth_client.get("/account")
    if account.status_code != 200:
        raise SystemExit(f"Account page returned {account.status_code}")
    assert_contains(account.text, "Тестовый пользователь")
    assert_contains(account.text, "Изменить пароль")
    user_csrf = meta_content(account.text, "csrf-token")

    missing_csrf_save = auth_client.put(
        "/api/saved-listings",
        json={"url": "https://krisha.kz/a/show/123", "saved": True},
    )
    if missing_csrf_save.status_code != 403:
        raise SystemExit("Saved-listing API accepted a request without CSRF")

    saved = auth_client.put(
        "/api/saved-listings",
        headers={"X-CSRF-Token": user_csrf},
        json={"url": "https://krisha.kz/a/show/123", "saved": True},
    )
    if saved.status_code != 200 or saved.json().get("count") != 1:
        raise SystemExit(f"Saving a listing failed: {saved.status_code} {saved.text}")

    imported = auth_client.post(
        "/api/saved-listings/import",
        headers={"X-CSRF-Token": user_csrf},
        json={
            "urls": [
                "https://krisha.kz/a/show/123",
                "https://krisha.kz/a/show/456-almaty",
            ]
        },
    )
    if imported.status_code != 200 or imported.json().get("count") != 2:
        raise SystemExit(f"Guest save import failed: {imported.status_code} {imported.text}")

    note = auth_client.patch(
        "/api/saved-listings/note",
        headers={"X-CSRF-Token": user_csrf},
        json={
            "url": "https://krisha.kz/a/show/123",
            "note": "Уточнить документы перед просмотром",
        },
    )
    if note.status_code != 200:
        raise SystemExit(f"Saved note failed: {note.status_code} {note.text}")

    watchlist = auth_client.get("/saved-listings")
    if watchlist.status_code != 200:
        raise SystemExit(f"Watchlist page returned {watchlist.status_code}")
    for needle in [
        "Сохранённые квартиры",
        "Текущая цена",
        "Уточнить документы перед просмотром",
        'data-header-saved-count>2</span>',
    ]:
        assert_contains(watchlist.text, needle)

    changed_password = auth_client.post(
        "/account/password",
        data={
            "current_password": "secure-password-123",
            "new_password": "new-secure-password-456",
            "new_password_confirm": "new-secure-password-456",
            "csrf_token": user_csrf,
        },
        follow_redirects=False,
    )
    if changed_password.status_code != 303:
        raise SystemExit(f"Password change returned {changed_password.status_code}")

    logged_out = auth_client.post(
        "/logout",
        data={"csrf_token": user_csrf},
        follow_redirects=False,
    )
    if logged_out.status_code != 303 or logged_out.headers.get("location") != "/":
        raise SystemExit("Logout failed")
    if auth_client.get("/account", follow_redirects=False).status_code != 303:
        raise SystemExit("Logged-out account request did not redirect")

    login_page = auth_client.get("/login?next=//example.com")
    assert_contains(login_page.text, "/register?next=/")
    assert_not_contains(login_page.text, "/register?next=/login")
    login_csrf = form_value(login_page.text, "csrf_token")
    old_password_login = auth_client.post(
        "/login",
        data={
            "email": "buyer@example.com",
            "password": "secure-password-123",
            "csrf_token": login_csrf,
            "next": "//example.com",
        },
        follow_redirects=False,
    )
    if old_password_login.status_code != 400:
        raise SystemExit("Old password still worked after password change")
    new_password_login = auth_client.post(
        "/login",
        data={
            "email": "buyer@example.com",
            "password": "new-secure-password-456",
            "csrf_token": login_csrf,
            "next": "//example.com",
        },
        follow_redirects=False,
    )
    if new_password_login.status_code != 303 or new_password_login.headers.get("location") != "/account":
        raise SystemExit("Login failed or accepted an unsafe next redirect")

    predict_entry = client.get("/predict-page")
    if predict_entry.status_code != 200:
        raise SystemExit(f"Predict entry page returned {predict_entry.status_code}")
    assert_contains(predict_entry.text, "Оценить ссылку Krisha")
    assert_contains(predict_entry.text, "Вернуться на главную")

    invalid_url = client.post("/predict", data={"url": "https://example.com/a/show/123"})
    if invalid_url.status_code != 400:
        raise SystemExit(f"Invalid URL check returned {invalid_url.status_code}")
    assert_contains(invalid_url.text, "Ссылка должна вести на krisha.kz")

    from app.prediction_service import ListingPrediction

    predict_calls = []

    def fake_predict_by_url(url: str) -> ListingPrediction:
        predict_calls.append(url)
        return ListingPrediction(
            url=url,
            title="3-комнатная квартира, 80 м², 7/12 этаж",
            listed_price=40000000,
            area_m2=80,
            listed_price_per_m2=500000,
            pred_price_per_m2_q10=550000,
            pred_price_per_m2_q50=620000,
            pred_price_per_m2_q90=700000,
            pred_total_q50=49600000,
            discount_vs_asking_pct_conservative=0.10,
            discount_vs_asking_pct_median=0.24,
            interval_width_pct=0.24,
            monthly_rent_q10=250000,
            monthly_rent_q50=280000,
            monthly_rent_q90=320000,
            gross_yield_q10=0.075,
            gross_yield_q50=0.084,
            gross_yield_q90=0.096,
            payback_years_q50=11.9,
            rental_model_version="rent-test",
        )

    main.prediction_service.predict_by_url = fake_predict_by_url
    result_page = client.post(
        "/predict",
        data={"url": "https://krisha.kz/a/show/123"},
    )
    if result_page.status_code != 200:
        raise SystemExit(f"Result page returned {result_page.status_code}")
    if predict_calls != ["https://krisha.kz/a/show/123"]:
        raise SystemExit(f"Expected first prediction to call the model once, got {predict_calls}")
    for needle in [
        "Результат оценки",
        "Объявление",
        "Оценка модели",
        "Нижняя оценка q10",
        "Медианная оценка q50",
        "Абсолютная выгода по q10",
        "Абсолютная выгода по медиане",
        "Выгода к цене по q10",
        "Ширина интервала",
        "CatBoost",
        "Ипотечный анализ Отбасы банка",
        "Жилищный заём",
        "Промежуточный заём",
        "Нужно накопить 50%",
        "8,5% годовых",
        "hcsbk.kz/ru/most-important/zhilzaim/",
        "hcsbk.kz/ru/to-get-a-loan/secondary-housing/",
        "Оценочная аренда и доходность",
        "280 000 тг/мес.",
        "8.4% в год",
    ]:
        assert_contains(result_page.text, needle)

    model_page = client.get("/model-page")
    if model_page.status_code != 200:
        raise SystemExit(f"Model page returned {model_page.status_code}")
    for needle in [
        "Как работает модель",
        "Сервис оценивает ориентировочную рыночную цену объявления за квадратный метр",
        "Модель обучалась на объявлениях, поэтому она видит рынок только через публичные цены",
        "Её главная задача — подсветить объявления",
        "q10",
        "q50",
        "q90",
        "квантили",
        "поделенная",
        "О сервисе",
        "Для кого",
        "Источник данных",
        "Данные получены из открытых объявлений на сайте krisha.kz",
        "Как пользоваться",
        "Что важно помнить",
        "Почему каждый город оценивается отдельно",
        "Частые вопросы",
        "Это официальная оценка Krisha.kz?",
        "Может ли оценка ошибаться?",
        "Что означает процент совпадения в подборе квартиры?",
    ]:
        assert_contains(model_page.text, needle)
    assert_not_contains(model_page.text, "Она не заменяет проверку документов")
    assert_not_contains(model_page.text, "закрытые цены реальных сделок")
    assert_not_contains(model_page.text, "Технический список признаков")
    assert_not_contains(model_page.text, "магический")

    details_page = client.get("/listing-details?url=https://krisha.kz/a/show/123")
    if details_page.status_code != 200:
        raise SystemExit(f"Listing details page returned {details_page.status_code}")
    if predict_calls != ["https://krisha.kz/a/show/123"]:
        raise SystemExit("Prediction cache did not reuse the first Krisha URL result")
    assert_contains(details_page.text, "Результат оценки")
    assert_contains(details_page.text, "История цены")
    assert_contains(details_page.text, "График истории цены")
    assert_contains(details_page.text, "На что обратить внимание")
    assert_contains(details_page.text, "Цена снижалась")
    assert_contains(details_page.text, "Жилой комплекс")
    assert_contains(details_page.text, "Район")
    assert_contains(details_page.text, "Есиль")
    assert_contains(details_page.text, "Улица")
    assert_contains(details_page.text, "Кабанбай батыра 48")
    assert_contains(details_page.text, "Test ЖК")
    assert_contains(details_page.text, "/district/yesil")
    assert_contains(details_page.text, "/complex-page?city=astana")
    assert_contains(details_page.text, "Активных объявлений в базе")
    assert_contains(details_page.text, "2026-06-30 05:00")
    assert_not_contains(details_page.text, "Оценочная аренда и доходность")

    stored_details_page = client.get(
        "/listing-details?url=https://krisha.kz/a/show/456-almaty"
    )
    if stored_details_page.status_code != 200:
        raise SystemExit(
            f"Stored listing details returned {stored_details_page.status_code}"
        )
    if predict_calls != ["https://krisha.kz/a/show/123"]:
        raise SystemExit("Stored listing details unexpectedly called the Krisha parser")
    assert_contains(stored_details_page.text, "Результат оценки")
    assert_contains(stored_details_page.text, "Алматы")
    assert_not_contains(stored_details_page.text, "Оценочная аренда и доходность")

    compare_page = client.get("/compare-page?url=https://krisha.kz/a/show/123")
    if compare_page.status_code != 200:
        raise SystemExit(f"Compare page returned {compare_page.status_code}")
    assert_contains(compare_page.text, "Сравнение квартир")
    assert_contains(compare_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(compare_page.text, "Выгода q10")

    market_page = client.get("/market-page")
    if market_page.status_code != 200:
        raise SystemExit(f"Market page returned {market_page.status_code}")
    for needle in [
        "Рынок квартир в Астане",
        "Районы: цена, разброс и возможности",
        "Медианная цена за м² в Астане",
        "Структура предложения",
        "Ценовые диапазоны в Астане",
        "Предложение по комнатности в Астане",
        "Цена по состоянию квартир в Астане",
        "Тип предложения в Астане",
        "История медианной цены за м² в Астане",
        "Истории пока недостаточно для честного графика",
        "Объектов со снижением цены",
        "Есиль",
        "/district/yesil",
    ]:
        assert_contains(market_page.text, needle)
    assert_not_contains(market_page.text, "экспозиц")
    assert_not_contains(market_page.text, "Экспозиц")
    assert_not_contains(market_page.text, "закрыт")
    assert_not_contains(market_page.text, "Жилые комплексы с возможностями")
    assert_not_contains(market_page.text, "Как читать аналитику")

    district_page = client.get("/district/yesil")
    if district_page.status_code != 200:
        raise SystemExit(f"District analytics page returned {district_page.status_code}")
    for needle in [
        "Район Есиль",
        "Аналитика района Астаны",
        "Медианный срок в базе",
        "Относительно Астаны",
        "История медианной цены за м² — Район Есиль, Астана",
        "ЖК района Есиль",
        "Test ЖК",
        "/complex-page?city=astana",
    ]:
        assert_contains(district_page.text, needle)

    complex_page = client.get("/complex-page?name=Test%20%D0%96%D0%9A")
    if complex_page.status_code != 200:
        raise SystemExit(f"Complex analytics page returned {complex_page.status_code}")
    for needle in [
        "ЖК Test ЖК",
        "Аналитика жилого комплекса в Астане",
        "Рейтинг и отзывы",
        "Найти Test ЖК в 2ГИС",
        "https://2gis.kz/astana/search/",
        "/district/yesil",
        "История медианной цены за м² — ЖК Test ЖК, Астана",
        "3-комнатная квартира · 40 м²",
    ]:
        assert_contains(complex_page.text, needle)

    missing_complex_page = client.get("/complex-page?name=Unknown%20Complex")
    if missing_complex_page.status_code != 404:
        raise SystemExit(
            f"Missing complex analytics returned {missing_complex_page.status_code}"
        )

    missing_district_page = client.get("/district/unknown")
    if missing_district_page.status_code != 404:
        raise SystemExit(
            f"Missing district analytics returned {missing_district_page.status_code}"
        )

    about_page = client.get("/about-page", follow_redirects=False)
    if about_page.status_code != 308:
        raise SystemExit(f"Legacy about page returned {about_page.status_code}")
    if about_page.headers.get("location") != "/model-page":
        raise SystemExit("Legacy about page does not redirect to /model-page")

    feedback_page = client.get("/feedback-page")
    if feedback_page.status_code != 200:
        raise SystemExit(f"Feedback page returned {feedback_page.status_code}")
    for needle in [
        "Предложения по улучшению",
        "Email для ответа",
        "Предложение или сообщение об ошибке",
        "Отправить",
    ]:
        assert_contains(feedback_page.text, needle)

    bad_feedback = client.post(
        "/feedback-page",
        data={"email": "not-email", "message": "коротко"},
    )
    if bad_feedback.status_code != 400:
        raise SystemExit(f"Bad feedback returned {bad_feedback.status_code}")
    assert_contains(bad_feedback.text, "хотя бы 10 символов")

    good_feedback = client.post(
        "/feedback-page",
        data={
            "email": "user@example.com",
            "message": "Добавьте, пожалуйста, фильтр по сроку сдачи жилого комплекса.",
        },
    )
    if good_feedback.status_code != 200:
        raise SystemExit(f"Good feedback returned {good_feedback.status_code}")
    assert_contains(good_feedback.text, "Предложение отправлено")

    for path in ["/refresh-runs", "/status-summary"]:
        response = client.get(path)
        if response.status_code != 401:
            raise SystemExit(f"{path} without login returned {response.status_code}")

    for path in [
        "/refresh-runs-page",
        "/status-page",
        "/admin-refresh-page",
        "/model-monitoring-page",
        "/model-version-page",
        "/traffic-page",
        "/feedback-admin-page",
    ]:
        response = client.get(path, follow_redirects=False)
        if response.status_code != 303:
            raise SystemExit(f"{path} without login returned {response.status_code}")
        if not response.headers["location"].startswith("/admin-login?next="):
            raise SystemExit(f"{path} redirected to {response.headers['location']}")

    login_page = client.get("/admin-login?next=/status-page")
    if login_page.status_code != 200:
        raise SystemExit(f"Admin login page returned {login_page.status_code}")
    assert_contains(login_page.text, "Вход для администратора")

    bad_login = client.post(
        "/admin-login",
        data={"password": "wrong-token", "next": "/status-page"},
    )
    if bad_login.status_code != 400:
        raise SystemExit(f"Bad admin login returned {bad_login.status_code}")
    assert_contains(bad_login.text, "Неверный пароль")

    good_login = client.post(
        "/admin-login",
        data={"password": "test-token", "next": "/status-page"},
        follow_redirects=False,
    )
    if good_login.status_code != 303:
        raise SystemExit(f"Good admin login returned {good_login.status_code}")
    if good_login.headers["location"] != "/status-page":
        raise SystemExit(f"Good admin login redirected to {good_login.headers['location']}")

    undervalued = client.get("/undervalued-page")
    if undervalued.status_code != 200:
        raise SystemExit(f"Undervalued page returned {undervalued.status_code}")

    for needle in [
        "Квартиры ниже рынка",
        "Фильтр по району",
        "№",
        "Krisha",
        "Жилой комплекс",
        "Нижняя оценка",
        "Медианная оценка",
        "Выгода q10",
        "Выгода медиана",
        "Количество комнат",
        "Максимальная цена",
        "Год постройки от",
        "Год постройки до",
        "Жилой комплекс",
        "Состояние квартиры",
        "Свежий ремонт",
        "Черновая отделка",
        "Новостройка",
        "Только новостройки",
        "Площадь от",
        "Площадь до",
        "Новые квартиры за 24 часа",
        "Новые квартиры за 48 часов",
        "Минимальная выгода q10",
        "Сортировка",
        "Сначала новые",
        "Только сохранённые",
        "Скопировать поиск",
        "Выбрано для сравнения",
        "Активных объявлений в базе: 1",
        "Зона на карте",
        "map_polygon",
        "leaflet",
        "Есиль",
        "Test ЖК",
        "3-комнатная квартира · 40 м²",
        "Подробнее",
        "/listing-details?url=",
        "/district/yesil",
        "/complex-page?city=astana",
        "2026-06-29 05:00",
        "Страница 1 из 1",
        "Страницы рейтинга",
        "Перейти на страницу",
        'aria-current="page">1</span>',
    ]:
        assert_contains(undervalued.text, needle)
    assert_not_contains(undervalued.text, "активно")

    if main._pagination_window(1, 20) != [1, 2, 3, None, 20]:
        raise SystemExit("Pagination window for the first page is incorrect")
    if main._pagination_window(10, 20) != [1, None, 8, 9, 10, 11, 12, None, 20]:
        raise SystemExit("Pagination window around the current page is incorrect")
    if main._pagination_window(4, 6) != [1, 2, 3, 4, 5, 6]:
        raise SystemExit("Pagination window for a short result set is incorrect")

    oversized_page = client.get("/undervalued-page?page=999")
    if oversized_page.status_code != 200:
        raise SystemExit(f"Oversized page returned {oversized_page.status_code}")
    assert_contains(oversized_page.text, "Страница 1 из 1")

    yesil_page = client.get("/undervalued-page?district=yesil")
    if yesil_page.status_code != 200:
        raise SystemExit(f"Yesil filter returned {yesil_page.status_code}")
    assert_contains(yesil_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(yesil_page.text, "Показано 1 из 1")

    multi_district_page = client.get("/undervalued-page?district=yesil&district=nura")
    if multi_district_page.status_code != 200:
        raise SystemExit(f"Multi district filter returned {multi_district_page.status_code}")
    assert_contains(multi_district_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(multi_district_page.text, "value=\"yesil\"")
    assert_contains(multi_district_page.text, "value=\"nura\"")
    assert_contains(multi_district_page.text, 'type="hidden" name="district" value="yesil"')
    assert_contains(multi_district_page.text, 'type="hidden" name="district" value="nura"')

    room_price_page = client.get("/undervalued-page?rooms=3&max_price=21000000")
    if room_price_page.status_code != 200:
        raise SystemExit(f"Room/price filter returned {room_price_page.status_code}")
    assert_contains(room_price_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(room_price_page.text, "Показано 1 из 1")

    sorted_page = client.get("/undervalued-page?sort=price_per_m2_desc")
    if sorted_page.status_code != 200:
        raise SystemExit(f"Sorted filter returned {sorted_page.status_code}")
    assert_contains(sorted_page.text, "Цена за м²: сначала дороже")
    assert_contains(sorted_page.text, "Площадь: сначала меньше")

    api_sorted = client.get("/undervalued?sort=listed_price_desc")
    if api_sorted.status_code != 200:
        raise SystemExit(f"Sorted API returned {api_sorted.status_code}")
    if api_sorted.json()["sort"] != "listed_price_desc":
        raise SystemExit("Sorted API did not echo selected sort")

    blank_price_page = client.get("/undervalued-page?rooms=3&max_price=")
    if blank_price_page.status_code != 200:
        raise SystemExit(f"Blank max price filter returned {blank_price_page.status_code}")
    assert_contains(blank_price_page.text, "3-комнатная квартира · 40 м²")

    api_blank_price = client.get("/undervalued?rooms=3&max_price=")
    if api_blank_price.status_code != 200:
        raise SystemExit(f"Blank max price API returned {api_blank_price.status_code}")

    blank_filters_page = client.get("/undervalued-page?rooms=&max_price=")
    if blank_filters_page.status_code != 200:
        raise SystemExit(f"Blank room/price filter returned {blank_filters_page.status_code}")
    assert_contains(blank_filters_page.text, "3-комнатная квартира · 40 м²")

    api_blank_filters = client.get("/undervalued?rooms=&max_price=")
    if api_blank_filters.status_code != 200:
        raise SystemExit(f"Blank room/price API returned {api_blank_filters.status_code}")

    advanced_filter_page = client.get(
        "/undervalued-page?min_year=2019&max_year=2021&residential_complex=Test&min_area=39&max_area=41"
    )
    if advanced_filter_page.status_code != 200:
        raise SystemExit(f"Advanced filter returned {advanced_filter_page.status_code}")
    assert_contains(advanced_filter_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(advanced_filter_page.text, "Test")

    listing_fields_page = client.get(
        "/undervalued-page?condition=fresh_repair&new_build=1"
    )
    if listing_fields_page.status_code != 200:
        raise SystemExit(f"Listing field filters returned {listing_fields_page.status_code}")
    assert_contains(listing_fields_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(listing_fields_page.text, 'value="fresh_repair" selected')
    assert_contains(listing_fields_page.text, 'value="1" selected')

    wrong_condition_page = client.get("/undervalued-page?condition=rough_finish")
    if wrong_condition_page.status_code != 200:
        raise SystemExit(f"Condition filter returned {wrong_condition_page.status_code}")
    assert_contains(wrong_condition_page.text, "Показано 0 из 0")

    api_listing_fields = client.get("/undervalued?condition=fresh_repair&new_build=1")
    if api_listing_fields.status_code != 200:
        raise SystemExit(f"Listing field API returned {api_listing_fields.status_code}")
    api_listing_fields_payload = api_listing_fields.json()
    if api_listing_fields_payload["total"] != 1:
        raise SystemExit("Listing field API did not return the seeded listing")
    if api_listing_fields_payload["items"][0]["apartment_condition_slug"] != "fresh_repair":
        raise SystemExit("Listing field API did not expose normalized condition")
    if api_listing_fields_payload["items"][0]["is_new_build"] is not True:
        raise SystemExit("Listing field API did not expose new-build status")

    fresh_strong_page = client.get("/undervalued-page?new_since_hours=24&min_discount_pct=10")
    if fresh_strong_page.status_code != 200:
        raise SystemExit(f"Fresh/strong filter returned {fresh_strong_page.status_code}")
    assert_contains(fresh_strong_page.text, "3-комнатная квартира · 40 м²")

    too_strong_page = client.get("/undervalued-page?min_discount_pct=15")
    if too_strong_page.status_code != 200:
        raise SystemExit(f"Too strong filter returned {too_strong_page.status_code}")
    assert_contains(too_strong_page.text, "Показано 0 из 0")

    api_fresh_strong = client.get("/undervalued?new_since_hours=24&min_discount_pct=10")
    if api_fresh_strong.status_code != 200:
        raise SystemExit(f"Fresh/strong API returned {api_fresh_strong.status_code}")
    if api_fresh_strong.json()["total"] != 1:
        raise SystemExit("Fresh/strong API did not return the seeded listing")

    polygon_page = client.get(
        "/undervalued-page?map_polygon=51.0,71.3;51.0,71.6;51.3,71.6;51.3,71.3"
    )
    if polygon_page.status_code != 200:
        raise SystemExit(f"Polygon filter returned {polygon_page.status_code}")
    assert_contains(polygon_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(polygon_page.text, "Фильтр по зоне включён")

    outside_polygon_page = client.get(
        "/undervalued-page?map_polygon=51.5,71.7;51.5,71.9;51.7,71.9;51.7,71.7"
    )
    if outside_polygon_page.status_code != 200:
        raise SystemExit(f"Outside polygon filter returned {outside_polygon_page.status_code}")
    assert_contains(outside_polygon_page.text, "Показано 0 из 0")

    nura_page = client.get("/undervalued-page?district=nura")
    if nura_page.status_code != 200:
        raise SystemExit(f"Nura filter returned {nura_page.status_code}")
    assert_contains(nura_page.text, "Показано 0 из 0")

    refresh_runs_api = client.get("/refresh-runs")
    if refresh_runs_api.status_code != 200:
        raise SystemExit(f"Refresh runs API returned {refresh_runs_api.status_code}")

    status_api = client.get("/status-summary")
    if status_api.status_code != 200:
        raise SystemExit(f"Status API returned {status_api.status_code}")

    refresh_runs = client.get("/refresh-runs-page")
    if refresh_runs.status_code != 200:
        raise SystemExit(f"Refresh runs page returned {refresh_runs.status_code}")
    for needle in [
        "История обновлений",
        "ежедневное",
        "завершено",
        "Найдено URL",
        "Обработано",
        "Начато (UTC+5)",
        "2026-06-29 05:05",
    ]:
        assert_contains(refresh_runs.text, needle)

    status_page = client.get("/status-page")
    if status_page.status_code != 200:
        raise SystemExit(f"Status page returned {status_page.status_code}")
    for needle in [
        "Статус сервиса",
        "Всего объявлений в базе",
        "Квартир ниже рынка",
        "Последнее обновление",
        "Мониторинг модели",
        "Версия модели",
        "Трафик сайта",
        "Предложения пользователей",
        "2026-06-29 05:05",
    ]:
        assert_contains(status_page.text, needle)

    feedback_admin = client.get("/feedback-admin-page")
    if feedback_admin.status_code != 200:
        raise SystemExit(f"Feedback admin page returned {feedback_admin.status_code}")
    assert_contains(feedback_admin.text, "Предложения пользователей")
    assert_contains(feedback_admin.text, "user@example.com")
    assert_contains(feedback_admin.text, "фильтр по сроку сдачи")
    assert_contains(feedback_admin.text, "Удалить")

    delete_feedback = client.post(
        "/feedback-admin-delete",
        data={"feedback_id": 1},
    )
    if delete_feedback.status_code != 200:
        raise SystemExit(f"Delete feedback returned {delete_feedback.status_code}")
    assert_contains(delete_feedback.text, "Предложение удалено")
    assert_not_contains(delete_feedback.text, "user@example.com")

    traffic_page = client.get("/traffic-page")
    if traffic_page.status_code != 200:
        raise SystemExit(f"Traffic page returned {traffic_page.status_code}")
    for needle in [
        "Трафик сайта",
        "Запросов за 24 часа",
        "Посетителей за 24 часа",
        "Оценок ссылок за 24 часа",
        "Rate limit",
        "Ошибок 5xx за 24 часа",
        "Кэш прогноза",
        "Популярные страницы",
        "Последние события",
    ]:
        assert_contains(traffic_page.text, needle)

    monitoring_page = client.get("/model-monitoring-page")
    if monitoring_page.status_code != 200:
        raise SystemExit(f"Model monitoring page returned {monitoring_page.status_code}")
    for needle in [
        "Мониторинг модели",
        "История snapshots",
        "Последние предупреждения",
        "Доля ниже рынка",
        "Медиана q50/м²",
    ]:
        assert_contains(monitoring_page.text, needle)

    model_version_page = client.get("/model-version-page")
    if model_version_page.status_code != 200:
        raise SystemExit(f"Model version page returned {model_version_page.status_code}")
    for needle in [
        "Версия модели",
        "Целевая переменная",
        "catboost_q10_price_per_m2_log.cbm",
        "catboost_q50_price_per_m2_log.cbm",
        "catboost_q90_price_per_m2_log.cbm",
    ]:
        assert_contains(model_version_page.text, needle)

    admin_page = client.get("/admin-refresh-page")
    if admin_page.status_code != 200:
        raise SystemExit(f"Admin refresh page returned {admin_page.status_code}")
    assert_contains(admin_page.text, "Админ: обновить данные")
    assert_contains(admin_page.text, "Запустить обновление")
    assert_not_contains(admin_page.text, "Админ-токен")

    bad_admin = client.post(
        "/admin-refresh",
        data={
            "kind": "manual",
            "start_page": 1,
            "pages": 0,
            "min_delay": 0,
            "max_delay": 0,
            "max_listings": 0,
        },
    )
    if bad_admin.status_code != 400:
        raise SystemExit(f"Bad admin refresh returned {bad_admin.status_code}")
    assert_contains(
        bad_admin.text,
        "Количество страниц и стартовая страница должны быть положительными",
    )

    good_admin = client.post(
        "/admin-refresh",
        data={
            "kind": "manual",
            "start_page": 1,
            "pages": 1,
            "min_delay": 0,
            "max_delay": 0,
            "max_listings": 1,
        },
    )
    if good_admin.status_code != 200:
        raise SystemExit(f"Good admin refresh returned {good_admin.status_code}")
    assert_contains(good_admin.text, "Обновление запущено")
    if len(refresh_calls) != 1:
        raise SystemExit(f"Expected 1 fake refresh call, got {len(refresh_calls)}")

    print("[OK] UI checks passed.")


if __name__ == "__main__":
    main()
