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
                "3-комнатная квартира, 80 м², 7/12 этаж, рядом с парком",
                '{"Город": "Астана, Есиль р-н", "Год постройки": "2020", "Жилой комплекс": "Test ЖК", "lat": 51.13, "lon": 71.43}',
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


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise SystemExit(f"Expected page not to contain: {needle}")


def main() -> None:
    db_path = ROOT / "data" / "ui_check.sqlite3"
    db_path.parent.mkdir(exist_ok=True)
    for suffix in ["", "-wal", "-shm"]:
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()

    os.environ["DB_PATH"] = str(db_path)
    os.environ["ADMIN_TOKEN"] = "test-token"
    seed_listing(db_path)

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
    assert_contains(home.text, "Квартиры ниже рынка")
    assert_contains(home.text, "CatBoost оценивает")
    assert_contains(home.text, "Квартиры ниже рынка в Астане")
    assert_contains(home.text, "Топ-10 квартир ниже рынка")
    assert_contains(home.text, "Медианная оценка")
    assert_contains(home.text, "3-комнатная квартира · 40 м²")
    assert_contains(home.text, "Есиль")
    assert_contains(home.text, "Разработчик - Кайрат Жаркынбай")
    assert_contains(home.text, "/model-page")
    assert_not_contains(home.text, "Статус сервиса")
    assert_not_contains(home.text, "История обновлений")
    assert_not_contains(home.text, "Админ: обновить данные")

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

    def fake_predict_by_url(url: str) -> ListingPrediction:
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
        )

    main.prediction_service.predict_by_url = fake_predict_by_url
    result_page = client.post(
        "/predict",
        data={"url": "https://krisha.kz/a/show/123"},
    )
    if result_page.status_code != 200:
        raise SystemExit(f"Result page returned {result_page.status_code}")
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
    ]:
        assert_contains(result_page.text, needle)

    model_page = client.get("/model-page")
    if model_page.status_code != 200:
        raise SystemExit(f"Model page returned {model_page.status_code}")
    for needle in [
        "Как работает модель",
        "q10",
        "q50",
        "q90",
        "квантили",
        "поделенная",
    ]:
        assert_contains(model_page.text, needle)
    assert_not_contains(model_page.text, "Технический список признаков")
    assert_not_contains(model_page.text, "магический")

    details_page = client.get("/listing-details?url=https://krisha.kz/a/show/123")
    if details_page.status_code != 200:
        raise SystemExit(f"Listing details page returned {details_page.status_code}")
    assert_contains(details_page.text, "Результат оценки")

    for path in ["/refresh-runs", "/status-summary"]:
        response = client.get(path)
        if response.status_code != 401:
            raise SystemExit(f"{path} without login returned {response.status_code}")

    for path in ["/refresh-runs-page", "/status-page", "/admin-refresh-page"]:
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
        "Нижняя оценка",
        "Медианная оценка",
        "Выгода q10",
        "Выгода медиана",
        "Количество комнат",
        "Максимальная цена",
        "Год постройки от",
        "Год постройки до",
        "Жилой комплекс",
        "Площадь от",
        "Площадь до",
        "Активных объявлений в базе: 1",
        "Зона на карте",
        "map_polygon",
        "leaflet",
        "Есиль",
        "3-комнатная квартира · 40 м²",
        "Подробнее",
        "/listing-details?url=",
        "2026-06-29 05:00",
    ]:
        assert_contains(undervalued.text, needle)
    assert_not_contains(undervalued.text, "активно")

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

    room_price_page = client.get("/undervalued-page?rooms=3&max_price=21000000")
    if room_price_page.status_code != 200:
        raise SystemExit(f"Room/price filter returned {room_price_page.status_code}")
    assert_contains(room_price_page.text, "3-комнатная квартира · 40 м²")
    assert_contains(room_price_page.text, "Показано 1 из 1")

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
        "Начато (Астана)",
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
        "2026-06-29 05:05",
    ]:
        assert_contains(status_page.text, needle)

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
