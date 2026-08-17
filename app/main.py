from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlencode, urlparse, urlunparse

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.auth import (
    AuthValidationError,
    hash_password,
    new_csrf_token,
    new_session_token,
    normalize_display_name,
    normalize_email,
    session_token_hash,
    validate_password,
    verify_password,
)

from app.cities import (
    CITY_OPTIONS,
    city_config,
    district_options,
    normalize_city_slug,
)
from app.database import (
    APARTMENT_CONDITION_OPTIONS,
    connect,
    count_undervalued,
    create_feedback_message,
    create_user,
    create_user_session,
    delete_feedback_message,
    delete_user_session,
    fetch_complex_stats,
    fetch_complex_analytics,
    fetch_comparable_candidates,
    fetch_cached_prediction,
    fetch_feedback_messages,
    fetch_home_match_candidates,
    fetch_listing_by_url,
    fetch_listings_by_urls,
    fetch_market_brief,
    fetch_market_dashboard,
    fetch_district_analytics,
    fetch_monitoring_snapshots,
    fetch_price_history,
    fetch_refresh_runs,
    fetch_running_refresh,
    fetch_status_summary,
    fetch_saved_listing_urls,
    fetch_saved_listings,
    fetch_traffic_summary,
    fetch_undervalued,
    fetch_user_by_email,
    fetch_user_session,
    init_db,
    record_request_event,
    save_user_listing,
    store_cached_prediction,
    unsave_user_listing,
    update_saved_listing_note,
    update_user_last_login,
    update_user_password,
    valid_apartment_condition_slug,
    valid_district_slug,
    valid_district_slugs,
)
from app.home_matcher import (
    DEFAULT_PRIORITIES,
    HOME_PRESETS,
    PRIORITY_OPTIONS,
    HomeSearchPreferences,
    format_distance,
    rank_home_candidates,
)
from app.listing_insights import build_comparable_insight
from app.model_service import MODEL_FILENAMES
from app.mortgage import analyze_otbasy_mortgage
from app.prediction_service import (
    ListingPrediction,
    PredictionService,
    validate_krisha_url,
)
from app.refresh_service import run_refresh


logger = logging.getLogger(__name__)

ROOT = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1]))
POI_CATALOG_PATH = ROOT / "app" / "data" / "kazakhstan_pois.json"
ASTANA_TZ = timezone(timedelta(hours=5), name="Asia/Astana")
STATIC_ASSET_VERSION = hashlib.sha256(
    b"".join(
        (ROOT / relative_path).read_bytes()
        for relative_path in (
            "app/static/site.css",
            "app/static/krisha-ai-mark.png",
        )
    )
).hexdigest()[:12]


def _city_template_context(request: Request) -> dict:
    neutral_home = request.url.path == "/"
    current_path_with_query = request.url.path
    if request.url.query:
        current_path_with_query = f"{current_path_with_query}?{request.url.query}"
    auth_return_to = current_path_with_query
    if request.url.path in {"/login", "/register"}:
        auth_return_to = request.query_params.get("next") or "/"
        if not auth_return_to.startswith("/") or auth_return_to.startswith("//"):
            auth_return_to = "/"
    selected_city = city_config(request.query_params.get("city"))
    city_choices = []
    for option in CITY_OPTIONS:
        if neutral_home:
            city_choices.append(
                {
                    **option,
                    "url": f"/find-home-page?city={option['slug']}",
                }
            )
            continue
        params = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in {"city", "page"}
        ]
        params.append(("city", option["slug"]))
        city_choices.append({**option, "url": f"{request.url.path}?{urlencode(params)}"})
    return {
        "city": selected_city,
        "city_options": city_choices,
        "neutral_home": neutral_home,
        "current_path_with_query": current_path_with_query,
        "auth_return_to": auth_return_to,
    }


def _auth_template_context(request: Request) -> dict:
    session = getattr(request.state, "user_session", None)
    saved_urls: list[str] = []
    if session:
        with connect(DB_PATH) as db_connection:
            saved_urls = fetch_saved_listing_urls(db_connection, int(session["id"]))
    return {
        "current_user": session,
        "csrf_token": session.get("csrf_token") if session else "",
        "saved_listing_urls": saved_urls,
        "saved_count": len(saved_urls),
    }


templates = Jinja2Templates(
    directory=str(ROOT / "app" / "templates"),
    context_processors=[_city_template_context, _auth_template_context],
)
templates.env.filters["astana_time"] = lambda value: format_astana_time(value)
templates.env.filters["distance"] = format_distance
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
templates.env.globals["telegram_bot_url"] = (
    f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else ""
)
templates.env.globals["static_asset_version"] = STATIC_ASSET_VERSION
ADMIN_SESSION_COOKIE = "krisha_admin_session"
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
USER_SESSION_COOKIE = "krisha_user_session"
AUTH_CSRF_COOKIE = "krisha_auth_csrf"
USER_SESSION_TTL_SECONDS = 60 * 60 * 24
REMEMBERED_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
TERMS_VERSION = "2026-08-16"
AUTH_RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}
DUMMY_PASSWORD_HASH = hash_password("krisha-ai-dummy-password")
HOME_UNDERVALUED_LIMIT = 10
UNDERVALUED_PAGE_SIZE = 10
PREDICTION_CACHE_TTL_SECONDS = int(
    os.getenv("PREDICTION_CACHE_TTL_SECONDS", str(60 * 60 * 6))
)
PREDICT_RATE_LIMIT_PER_MINUTE = int(os.getenv("PREDICT_RATE_LIMIT_PER_MINUTE", "12"))
PREDICT_RATE_LIMIT_PER_HOUR = int(os.getenv("PREDICT_RATE_LIMIT_PER_HOUR", "80"))
RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}

app = FastAPI(title="Оценка объявлений Krisha")
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
prediction_service = PredictionService(ROOT)
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "krisha.sqlite3"))
with connect(DB_PATH) as db_connection:
    init_db(db_connection)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> RedirectResponse:
    return RedirectResponse(
        url=f"/static/krisha-ai-mark.png?v={STATIC_ASSET_VERSION}",
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@app.middleware("http")
async def traffic_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    request.state.user_session = _resolve_user_session(request)
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        if _should_track_request(request):
            try:
                with connect(DB_PATH) as db_connection:
                    record_request_event(
                        db_connection,
                        method=request.method,
                        path=request.url.path,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        client_hash=_client_hash(request),
                        user_agent=request.headers.get("user-agent"),
                        referer=request.headers.get("referer"),
                    )
            except Exception:
                pass


class PredictByLinkRequest(BaseModel):
    url: str


class RefreshRequest(BaseModel):
    city: str = "astana"
    kind: str = "manual"
    start_page: int = 1
    pages: int = 1
    min_delay: float = 1.0
    max_delay: float = 2.0
    max_listings: int = 0


class SavedListingRequest(BaseModel):
    url: str
    saved: bool = True
    title: str | None = None
    price: float | None = None
    city: str | None = None


class SavedListingImportRequest(BaseModel):
    urls: list[str]


class SavedListingNoteRequest(BaseModel):
    url: str
    note: str = ""


PUBLIC_PAGES = {
    "how-it-works": {
        "eyebrow": "Прозрачный процесс",
        "title": "Как работает Kvartiry-ai.kz",
        "intro": "Сервис превращает поток объявлений в понятный короткий список, но оставляет окончательное решение за вами.",
        "sections": [
            {
                "title": "1. Собираем и приводим данные к единому виду",
                "paragraphs": [
                    "Мы анализируем открытые объявления Krisha.kz по Астане и Алматы: цену, площадь, комнаты, район, этаж, год постройки, жилой комплекс и географию."
                ],
            },
            {
                "title": "2. Сравниваем квартиру с рынком",
                "paragraphs": [
                    "Квантильные модели оценивают нижний, медианный и верхний ориентиры цены за м². Рейтинг ниже рынка использует консервативную оценку q10, чтобы не опираться только на оптимистичный сценарий."
                ],
            },
            {
                "title": "3. Вы проверяете, сравниваете и сохраняете",
                "paragraphs": [
                    "Фильтры, персональный подбор, история цены, риски, ипотечный и арендный ориентиры помогают выбрать объявления для ручной проверки. С аккаунтом можно следить за изменениями цены и вести заметки."
                ],
            },
        ],
        "callout": "Оценка модели — аналитический ориентир по ценам предложения, а не отчёт оценщика, юридическая проверка или гарантия сделки.",
        "actions": [
            {"label": "Подобрать квартиру", "url": "/find-home-page?city=astana"},
            {"label": "О модели", "url": "/model-page", "secondary": True},
        ],
    },
    "features": {
        "eyebrow": "Возможности",
        "title": "Инструменты для осознанного поиска квартиры",
        "intro": "От первой идеи до короткого списка — без попытки скрыть неопределённость за одной красивой цифрой.",
        "sections": [
            {"title": "Квартиры ниже рынка", "paragraphs": ["Городской рейтинг с фильтрами, картой, сортировкой по выгоде и признаками качества данных."]},
            {"title": "Персональный подбор", "paragraphs": ["Жёсткие условия и приоритеты по району, бюджету, площади, возрасту дома и близости к важным местам."]},
            {"title": "Проверка по ссылке", "paragraphs": ["q10/q50/q90, история цены, технические предупреждения, аренда, валовая доходность и предварительный расчёт Отбасы банка."]},
            {"title": "Рынок, районы и ЖК", "paragraphs": ["Медианы, диапазоны и динамика по городам, районам и жилым комплексам на основе активных объявлений."]},
            {"title": "Сравнение и умный список", "paragraphs": ["Сравнивайте до пяти вариантов, сохраняйте их в аккаунте, отмечайте свои наблюдения и замечайте снижение цены."]},
            {"title": "Два города, единая логика", "paragraphs": ["Отдельный городской контекст для Астаны и Алматы без смешивания похожих, но разных рынков."]},
        ],
        "actions": [{"label": "Открыть рейтинг", "url": "/undervalued-page?city=astana"}],
    },
    "about": {
        "eyebrow": "О проекте",
        "title": "Делаем рынок квартир понятнее",
        "intro": "Kvartiry-ai.kz — независимый аналитический проект Кайрата Жаркынбая для покупателей, инвесторов и всех, кто следит за недвижимостью Астаны и Алматы.",
        "sections": [
            {"title": "Зачем существует сервис", "paragraphs": ["Поиск квартиры часто означает десятки вкладок и несопоставимые обещания. Мы собираем рыночные сигналы в одном месте и объясняем, почему вариант заслуживает внимания."]},
            {"title": "Наш принцип", "paragraphs": ["Показывать диапазон и ограничения важнее, чем выдавать прогноз за истину. Поэтому рядом с оценкой есть q10/q50/q90, история цены, предупреждения и ссылка на исходное объявление."]},
            {"title": "Независимый статус", "paragraphs": ["Сервис не является частью Krisha.kz, банка, агентства недвижимости или застройщика. Упоминания сторонних организаций нужны только для источника данных и контекста анализа."]},
        ],
        "actions": [{"label": "Написать автору", "url": "/contact"}],
    },
    "contact": {
        "eyebrow": "Контакты",
        "title": "Расскажите, что стоит улучшить",
        "intro": "Сообщения об ошибках, идеи новых функций и предложения о сотрудничестве помогают проекту становиться полезнее.",
        "sections": [
            {"title": "Обратная связь", "paragraphs": ["Используйте форму — так сообщение сохранится и не потеряется. Для прямого контакта: kairosh1001@gmail.com."]},
            {"title": "Что указать", "paragraphs": ["Если проблема связана с объявлением, приложите ссылку и город. Не отправляйте ИИН, банковские данные, пароли и документы на квартиру."]},
        ],
        "actions": [
            {"label": "Открыть форму", "url": "/feedback-page"},
            {"label": "Написать email", "url": "mailto:kairosh1001@gmail.com", "secondary": True},
        ],
    },
    "terms": {
        "eyebrow": "Правила сервиса",
        "title": "Условия использования",
        "intro": "Действуют с 16 августа 2026 года. Используя сервис или создавая аккаунт, вы соглашаетесь с этими условиями.",
        "sections": [
            {"title": "Назначение", "paragraphs": ["Сервис предоставляет информационные инструменты для поиска и анализа открытых объявлений о недвижимости. Он не оказывает риелторские, оценочные, банковские, инвестиционные или юридические услуги."]},
            {"title": "Точность и решения", "paragraphs": ["Прогнозы основаны на ценах предложения и могут быть неполными или ошибочными. Проверяйте объект, документы, продавца, ограничения и условия финансирования самостоятельно с профильными специалистами."]},
            {"title": "Аккаунт", "paragraphs": ["Вы отвечаете за конфиденциальность пароля и действия в своём аккаунте. Запрещены автоматизированные атаки, попытки получить чужие данные и использование сервиса в нарушение закона."]},
            {"title": "Сторонние источники", "paragraphs": ["Права на объявления, товарные знаки и внешние сайты принадлежат их владельцам. Kvartiry-ai.kz является независимым сервисом и не гарантирует доступность внешних ссылок."]},
            {"title": "Доступность и изменения", "paragraphs": ["Функции могут изменяться или временно быть недоступны. Существенные изменения условий будут опубликованы на этой странице с новой датой."]},
            {"title": "Ограничение ответственности", "paragraphs": ["В пределах, допускаемых законом, проект не отвечает за сделки, упущенную выгоду или потери, возникшие из решений на основе данных сервиса."]},
        ],
    },
    "privacy": {
        "eyebrow": "Ваши данные",
        "title": "Политика конфиденциальности",
        "intro": "Действует с 16 августа 2026 года. Здесь описано, какие данные нужны аккаунту и работе сервиса.",
        "sections": [
            {"title": "Что мы храним", "paragraphs": ["При регистрации: имя, email, защищённый хеш пароля, факт принятия условий, сеансы входа, сохранённые ссылки и ваши заметки. Пароль в открытом виде не сохраняется."]},
            {"title": "Технические данные", "paragraphs": ["Для безопасности и статистики фиксируются путь запроса, время ответа, код ответа, сокращённый необратимый идентификатор клиента, тип браузера и источник перехода. Мы не используем это для продажи рекламных профилей."]},
            {"title": "Зачем используем", "paragraphs": ["Чтобы авторизовать вас, синхронизировать список квартир, защитить сервис, исправлять ошибки и понимать, какие функции полезны."]},
            {"title": "Передача и срок", "paragraphs": ["Мы не продаём личные данные. Инфраструктурные подрядчики могут обрабатывать их только для работы сервиса. Данные аккаунта хранятся, пока аккаунт активен или пока этого требует безопасность и закон."]},
            {"title": "Ваш выбор", "paragraphs": ["Вы можете запросить исправление, выгрузку или удаление данных через страницу контактов. Перед выполнением запроса потребуется подтвердить владение email."]},
            {"title": "Безопасность", "paragraphs": ["Используются Argon2id для паролей, серверные отзываемые сеансы, CSRF-защита и защищённые cookie в HTTPS. Ни одна система не исключает риск полностью, поэтому используйте уникальный пароль."]},
        ],
        "actions": [{"label": "Связаться по данным", "url": "/contact"}],
    },
}


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "models_loaded": sorted(prediction_service.model_service.models.keys()),
        "feature_count": len(
            prediction_service.model_service.metadata.feature_columns
        ),
        "model_routing": prediction_service.routing_mode,
        "available_model_bundles": prediction_service.available_model_bundles,
    }


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "how-it-works")


@app.get("/features", response_class=HTMLResponse)
def features_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "features")


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "about")


@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "contact")


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "terms")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    return _public_page_response(request, "privacy")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str = "/account") -> Response:
    if request.state.user_session:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    csrf_token = new_csrf_token()
    response = templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "form": {"display_name": "", "email": "", "next": next},
            "auth_csrf_token": csrf_token,
        },
    )
    _set_auth_csrf_cookie(response, request, csrf_token)
    return response


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(...),
    accept_terms: str | None = Form(None),
    remember_me: str | None = Form(None),
    next: str = Form("/account"),
) -> Response:
    _verify_pre_auth_csrf(request, csrf_token)
    _enforce_auth_rate_limit(request, "register")
    form = {"display_name": display_name, "email": email, "next": next}
    try:
        normalized_name = normalize_display_name(display_name)
        normalized_email = normalize_email(email)
        validate_password(password)
        if password != password_confirm:
            raise AuthValidationError("Пароли не совпадают.")
        if accept_terms != "yes":
            raise AuthValidationError(
                "Подтвердите согласие с условиями использования и политикой конфиденциальности."
            )
        with connect(DB_PATH) as db_connection:
            user = create_user(
                db_connection,
                email=normalized_email,
                email_normalized=normalized_email,
                display_name=normalized_name,
                password_hash=hash_password(password),
                accepted_terms_version=TERMS_VERSION,
            )
            raw_token, max_age = _create_user_session(
                db_connection,
                request=request,
                user_id=int(user["id"]),
                remembered=remember_me == "yes",
            )
    except AuthValidationError as exc:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "form": form,
                "error": str(exc),
                "auth_csrf_token": csrf_token,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "form": form,
                "error": "Аккаунт с таким email уже существует. Войдите в него.",
                "auth_csrf_token": csrf_token,
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    response = RedirectResponse(
        _safe_user_next_url(next),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_user_session_cookie(response, request, raw_token, max_age=max_age)
    response.delete_cookie(AUTH_CSRF_COOKIE, path="/")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/account") -> Response:
    if request.state.user_session:
        return RedirectResponse("/account", status_code=status.HTTP_303_SEE_OTHER)
    csrf_token = new_csrf_token()
    response = templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "form": {"email": "", "next": next},
            "auth_csrf_token": csrf_token,
        },
    )
    _set_auth_csrf_cookie(response, request, csrf_token)
    return response


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    remember_me: str | None = Form(None),
    next: str = Form("/account"),
) -> Response:
    _verify_pre_auth_csrf(request, csrf_token)
    _enforce_auth_rate_limit(request, "login")
    form = {"email": email, "next": next}
    try:
        normalized_email = normalize_email(email)
    except AuthValidationError:
        normalized_email = "invalid@example.invalid"

    with connect(DB_PATH) as db_connection:
        user = fetch_user_by_email(db_connection, normalized_email)
        password_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
        password_valid = verify_password(password, password_hash)
        if not user or user.get("disabled_at") or not password_valid:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "form": form,
                    "error": "Неверный email или пароль.",
                    "auth_csrf_token": csrf_token,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        update_user_last_login(db_connection, int(user["id"]))
        raw_token, max_age = _create_user_session(
            db_connection,
            request=request,
            user_id=int(user["id"]),
            remembered=remember_me == "yes",
        )

    response = RedirectResponse(
        _safe_user_next_url(next),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_user_session_cookie(response, request, raw_token, max_age=max_age)
    response.delete_cookie(AUTH_CSRF_COOKIE, path="/")
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    session = require_user_session(request)
    _require_user_csrf(session, csrf_token)
    raw_token = request.cookies.get(USER_SESSION_COOKIE, "")
    if raw_token:
        with connect(DB_PATH) as db_connection:
            delete_user_session(db_connection, session_token_hash(raw_token))
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request) -> Response:
    session = _user_session_or_redirect(request)
    if isinstance(session, RedirectResponse):
        return session
    with connect(DB_PATH) as db_connection:
        items = fetch_saved_listings(db_connection, int(session["id"]))
    return templates.TemplateResponse(
        "account.html",
        _watchlist_context(request, session, items),
    )


@app.get("/saved-listings", response_class=HTMLResponse)
def saved_listings_page(request: Request) -> Response:
    session = _user_session_or_redirect(request)
    if isinstance(session, RedirectResponse):
        return session
    with connect(DB_PATH) as db_connection:
        items = fetch_saved_listings(db_connection, int(session["id"]))
    return templates.TemplateResponse(
        "saved_listings.html",
        _watchlist_context(request, session, items),
    )


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    session = require_user_session(request)
    _require_user_csrf(session, csrf_token)
    with connect(DB_PATH) as db_connection:
        user = fetch_user_by_email(db_connection, str(session["email_normalized"]))
        try:
            if not user or not verify_password(current_password, user["password_hash"]):
                raise AuthValidationError("Текущий пароль введён неверно.")
            validate_password(new_password)
            if new_password != new_password_confirm:
                raise AuthValidationError("Новые пароли не совпадают.")
            raw_token = request.cookies.get(USER_SESSION_COOKIE, "")
            update_user_password(
                db_connection,
                user_id=int(session["id"]),
                password_hash=hash_password(new_password),
                keep_session_hash=session_token_hash(raw_token),
            )
        except AuthValidationError as exc:
            items = fetch_saved_listings(db_connection, int(session["id"]))
            context = _watchlist_context(request, session, items)
            context["password_error"] = str(exc)
            return templates.TemplateResponse(
                "account.html",
                context,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    return RedirectResponse(
        "/account?password_changed=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/api/saved-listings")
def saved_listings_api(request: Request) -> dict:
    session = require_user_session(request)
    with connect(DB_PATH) as db_connection:
        urls = fetch_saved_listing_urls(db_connection, int(session["id"]))
    return {"urls": urls, "count": len(urls)}


@app.put("/api/saved-listings")
def update_saved_listing_api(
    request: Request,
    payload: SavedListingRequest,
    x_csrf_token: str | None = Header(None),
) -> dict:
    session = require_user_session(request)
    _require_user_csrf(session, x_csrf_token)
    listing_url = _canonical_listing_url(payload.url)
    with connect(DB_PATH) as db_connection:
        listing = fetch_listing_by_url(db_connection, listing_url)
        if payload.saved:
            snapshot_price = payload.price if payload.price and 0 < payload.price < 1_000_000_000_000 else None
            snapshot_city = payload.city if payload.city in {"astana", "almaty"} else None
            save_user_listing(
                db_connection,
                user_id=int(session["id"]),
                listing_url=listing_url,
                saved_price=(listing or {}).get("listed_price") or snapshot_price,
                saved_title=(listing or {}).get("title") or payload.title,
                saved_city=(listing or {}).get("city") or snapshot_city,
            )
        else:
            unsave_user_listing(
                db_connection,
                user_id=int(session["id"]),
                listing_url=listing_url,
            )
        urls = fetch_saved_listing_urls(db_connection, int(session["id"]))
    return {"url": listing_url, "saved": payload.saved, "count": len(urls)}


@app.post("/api/saved-listings/import")
def import_saved_listings_api(
    request: Request,
    payload: SavedListingImportRequest,
    x_csrf_token: str | None = Header(None),
) -> dict:
    session = require_user_session(request)
    _require_user_csrf(session, x_csrf_token)
    if len(payload.urls) > 100:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="За один раз можно импортировать до 100 объявлений.",
        )
    imported = 0
    with connect(DB_PATH) as db_connection:
        for raw_url in dict.fromkeys(payload.urls):
            try:
                listing_url = _canonical_listing_url(raw_url)
            except (ValueError, HTTPException):
                continue
            listing = fetch_listing_by_url(db_connection, listing_url)
            imported += int(
                save_user_listing(
                    db_connection,
                    user_id=int(session["id"]),
                    listing_url=listing_url,
                    saved_price=(listing or {}).get("listed_price"),
                    saved_title=(listing or {}).get("title"),
                    saved_city=(listing or {}).get("city"),
                )
            )
        urls = fetch_saved_listing_urls(db_connection, int(session["id"]))
    return {"imported": imported, "urls": urls, "count": len(urls)}


@app.patch("/api/saved-listings/note")
def update_saved_listing_note_api(
    request: Request,
    payload: SavedListingNoteRequest,
    x_csrf_token: str | None = Header(None),
) -> dict:
    session = require_user_session(request)
    _require_user_csrf(session, x_csrf_token)
    listing_url = _canonical_listing_url(payload.url)
    if len(payload.note) > 500:
        raise HTTPException(status_code=400, detail="Заметка длиннее 500 символов.")
    with connect(DB_PATH) as db_connection:
        updated = update_saved_listing_note(
            db_connection,
            user_id=int(session["id"]),
            listing_url=listing_url,
            note=payload.note.strip(),
        )
    if not updated:
        raise HTTPException(status_code=404, detail="Сохранённое объявление не найдено.")
    return {"url": listing_url, "note": payload.note.strip()}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    city_cards = []
    total_undervalued = 0
    active_listings = 0
    latest_refreshes = []
    with connect(DB_PATH) as db_connection:
        for option in CITY_OPTIONS:
            city_slug = option["slug"]
            selected_city = city_config(city_slug)
            city_preview = fetch_undervalued(
                db_connection,
                city=city_slug,
                limit=HOME_UNDERVALUED_LIMIT,
                include_stale=False,
            )
            city_fresh = fetch_undervalued(
                db_connection,
                city=city_slug,
                limit=5,
                new_since=_new_since_threshold(24),
                include_stale=False,
            )
            city_total = count_undervalued(
                db_connection, city=city_slug, include_stale=False
            )
            status_summary = fetch_status_summary(db_connection, city=city_slug)
            market_brief = fetch_market_brief(db_connection, city=city_slug)
            city_active = status_summary.get("active_listings") or 0
            latest_refresh = status_summary.get("latest_refresh")
            total_undervalued += city_total
            active_listings += city_active
            if latest_refresh:
                latest_refreshes.append(latest_refresh)
            city_cards.append(
                {
                    **selected_city,
                    "active_listings": city_active,
                    "undervalued": city_total,
                    "market": market_brief,
                    "latest_refresh": latest_refresh,
                    "preview_items": city_preview,
                    "fresh_items": city_fresh,
                }
            )

    latest_refresh = max(
        latest_refreshes,
        key=lambda item: item.get("finished_at") or item.get("started_at") or "",
        default=None,
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "error": None,
            "url": "",
            "total_undervalued": total_undervalued,
            "active_listings": active_listings,
            "latest_refresh": latest_refresh,
            "city_cards": city_cards,
            "start_rank": 1,
            "is_preview": True,
        },
    )


@app.get("/predict-page", response_class=HTMLResponse)
def predict_entry_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "predict_form.html",
        {"request": request, "error": None, "url": ""},
    )


@app.get("/model-page", response_class=HTMLResponse)
def model_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "model.html",
        {"request": request},
    )


@app.get("/about-page", include_in_schema=False)
def legacy_about_page() -> RedirectResponse:
    return RedirectResponse(url="/model-page", status_code=308)


@app.get("/feedback-page", response_class=HTMLResponse)
def feedback_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "request": request,
            "error": None,
            "message": "",
            "email": "",
            "success": False,
        },
    )


@app.post("/feedback-page", response_class=HTMLResponse)
def submit_feedback(
    request: Request,
    message: str = Form(...),
    email: str = Form(""),
) -> HTMLResponse:
    cleaned_email = _parse_optional_email(email)
    cleaned_message = message.strip()
    error = _feedback_error(cleaned_message, cleaned_email, email)
    if error:
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {
                "request": request,
                "error": error,
                "message": cleaned_message,
                "email": email.strip(),
                "success": False,
            },
            status_code=400,
        )

    with connect(DB_PATH) as db_connection:
        create_feedback_message(
            db_connection,
            email=cleaned_email,
            message=cleaned_message,
            client_hash=_client_hash(request),
        )

    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "request": request,
            "error": None,
            "message": "",
            "email": "",
            "success": True,
        },
    )


@app.get("/predict", response_class=HTMLResponse)
@app.get("/listing-details", response_class=HTMLResponse)
def predict_page(request: Request, url: str = "") -> HTMLResponse:
    if not url:
        return templates.TemplateResponse(
            request,
            "predict_form.html",
            {"request": request, "error": None, "url": ""},
        )

    try:
        prediction = _predict_for_request(
            request,
            url,
            prefer_stored=request.url.path == "/listing-details",
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "predict_form.html",
            {"request": request, "error": str(exc.detail), "url": url},
            status_code=exc.status_code,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "predict_form.html",
            {"request": request, "error": str(exc), "url": url},
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "result.html",
        _prediction_context(request, prediction),
    )


@app.post("/predict", response_class=HTMLResponse)
def predict_form(request: Request, url: str = Form(...)) -> HTMLResponse:
    try:
        prediction = _predict_for_request(request, url)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "predict_form.html",
            {"request": request, "error": str(exc.detail), "url": url},
            status_code=exc.status_code,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "predict_form.html",
            {"request": request, "error": str(exc), "url": url},
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "result.html",
        _prediction_context(request, prediction),
    )


@app.get("/compare-page", response_class=HTMLResponse)
def compare_page(
    request: Request,
    url: list[str] | None = Query(default=None),
) -> HTMLResponse:
    selected_urls = []
    for value in url or []:
        if value not in selected_urls:
            selected_urls.append(value)

    with connect(DB_PATH) as db_connection:
        items = fetch_listings_by_urls(db_connection, selected_urls)

    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "request": request,
            "items": items,
            "selected_count": len(selected_urls),
        },
    )


@app.get("/market-page", response_class=HTMLResponse)
def market_page(request: Request, city: str = "astana") -> HTMLResponse:
    city_slug = normalize_city_slug(city)
    with connect(DB_PATH) as db_connection:
        dashboard = fetch_market_dashboard(db_connection, city=city_slug)
        status_summary = fetch_status_summary(db_connection, city=city_slug)

    return templates.TemplateResponse(
        request,
        "market.html",
        {
            "request": request,
            "dashboard": dashboard,
            "summary": status_summary,
        },
    )


@app.get("/district/{district_slug}", response_class=HTMLResponse)
def district_analytics_page(
    request: Request,
    district_slug: str,
    city: str = "astana",
) -> HTMLResponse:
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    valid_slug = valid_district_slug(district_slug, city=city_slug)
    if not valid_slug:
        raise HTTPException(
            status_code=404,
            detail=f"Район города {selected_city['name']} не найден.",
        )
    with connect(DB_PATH) as db_connection:
        analytics = fetch_district_analytics(
            db_connection, valid_slug, city=city_slug
        )
    if not analytics:
        raise HTTPException(
            status_code=404,
            detail=f"Район города {selected_city['name']} не найден.",
        )
    return templates.TemplateResponse(
        request,
        "market_entity.html",
        {
            "request": request,
            "analytics": analytics,
            "entity_kind": "district",
            "entity_name": analytics["entity"]["name"],
            "entity_title": f"Район {analytics['entity']['name']}",
            "listing_filter_url": (
                f"/undervalued-page?city={city_slug}&district={valid_slug}"
            ),
            "two_gis_url": None,
            "selected_city": selected_city,
        },
    )


@app.get("/complex-page", response_class=HTMLResponse)
def complex_analytics_page(
    request: Request,
    name: str = "",
    city: str = "astana",
) -> HTMLResponse:
    city_slug = normalize_city_slug(city)
    selected_city = city_config(city_slug)
    complex_name = name.strip()
    if not complex_name or len(complex_name) > 160:
        raise HTTPException(status_code=404, detail="Жилой комплекс не найден.")
    with connect(DB_PATH) as db_connection:
        analytics = fetch_complex_analytics(
            db_connection, complex_name, city=city_slug
        )
    if not analytics:
        raise HTTPException(status_code=404, detail="Жилой комплекс не найден.")
    encoded_name = quote(complex_name, safe="")
    two_gis_query = quote(f"ЖК {complex_name}", safe="")
    return templates.TemplateResponse(
        request,
        "market_entity.html",
        {
            "request": request,
            "analytics": analytics,
            "entity_kind": "complex",
            "entity_name": analytics["entity"]["name"],
            "entity_title": f"ЖК {analytics['entity']['name']}",
            "listing_filter_url": (
                f"/undervalued-page?city={city_slug}&residential_complex={encoded_name}"
            ),
            "two_gis_url": (
                f"https://2gis.kz/{selected_city['two_gis_slug']}/search/"
                f"{two_gis_query}"
            ),
            "selected_city": selected_city,
        },
    )


@app.post("/predict-by-link")
def predict_by_link(request: Request, payload: PredictByLinkRequest) -> dict:
    try:
        prediction = _predict_for_request(request, payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return asdict(prediction)


@app.get("/undervalued")
def undervalued(
    limit: int = 50,
    page: int = 1,
    city: str = "astana",
    district: list[str] | None = Query(default=None),
    rooms: str | None = None,
    max_price: str | None = None,
    min_year: str | None = None,
    max_year: str | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    condition: str | None = None,
    new_build: str | None = None,
    min_area: str | None = None,
    max_area: str | None = None,
    map_polygon: str | None = None,
    new_since_hours: str | None = None,
    min_discount_pct: str | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> dict:
    city_slug = normalize_city_slug(city)
    selected_districts = valid_district_slugs(district, city=city_slug)
    selected_rooms = _parse_optional_int(rooms, allowed={1, 2, 3, 4, 5})
    selected_max_price = _parse_optional_positive_float(max_price)
    selected_min_year = _parse_optional_int(min_year)
    selected_max_year = _parse_optional_int(max_year)
    selected_complex = _parse_optional_text(residential_complex)
    selected_developer = _parse_optional_text(developer)
    selected_condition = valid_apartment_condition_slug(condition)
    selected_new_build = _parse_checkbox_bool(new_build)
    selected_min_area = _parse_optional_positive_float(min_area)
    selected_max_area = _parse_optional_positive_float(max_area)
    selected_polygon = _parse_polygon(map_polygon)
    selected_new_since_hours = _parse_optional_int(new_since_hours, allowed={24, 48})
    selected_new_since = _new_since_threshold(selected_new_since_hours)
    selected_min_discount_pct = _parse_optional_percent(min_discount_pct)
    safe_limit = min(max(limit, 1), 100)
    safe_page = max(page, 1)
    offset = (safe_page - 1) * safe_limit
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            city=city_slug,
            limit=safe_limit,
            offset=offset,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            developer=selected_developer,
            apartment_condition=selected_condition,
            new_build=selected_new_build,
            min_area=selected_min_area,
            max_area=selected_max_area,
            polygon=selected_polygon,
            new_since=selected_new_since,
            min_discount_pct=selected_min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
        total = count_undervalued(
            db_connection,
            city=city_slug,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            developer=selected_developer,
            apartment_condition=selected_condition,
            new_build=selected_new_build,
            min_area=selected_min_area,
            max_area=selected_max_area,
            polygon=selected_polygon,
            new_since=selected_new_since,
            min_discount_pct=selected_min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
    return {
        "items": items,
        "city": city_slug,
        "total": total,
        "page": safe_page,
        "limit": safe_limit,
        "districts": selected_districts,
        "rooms": selected_rooms,
        "max_price": selected_max_price,
        "min_year": selected_min_year,
        "max_year": selected_max_year,
        "residential_complex": selected_complex,
        "developer": selected_developer,
        "condition": selected_condition,
        "new_build": selected_new_build,
        "min_area": selected_min_area,
        "max_area": selected_max_area,
        "map_polygon": selected_polygon,
        "new_since_hours": selected_new_since_hours,
        "min_discount_pct": selected_min_discount_pct,
        "sort": sort,
    }


@app.get("/undervalued-page", response_class=HTMLResponse)
def undervalued_page(
    request: Request,
    page: int = 1,
    city: str = "astana",
    district: list[str] | None = Query(default=None),
    rooms: str | None = None,
    max_price: str | None = None,
    min_year: str | None = None,
    max_year: str | None = None,
    residential_complex: str | None = None,
    condition: str | None = None,
    new_build: str | None = None,
    min_area: str | None = None,
    max_area: str | None = None,
    map_polygon: str | None = None,
    new_since_hours: str | None = None,
    min_discount_pct: str | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> HTMLResponse:
    city_slug = normalize_city_slug(city)
    selected_districts = valid_district_slugs(district, city=city_slug)
    selected_rooms = _parse_optional_int(rooms, allowed={1, 2, 3, 4, 5})
    selected_max_price = _parse_optional_positive_float(max_price)
    selected_min_year = _parse_optional_int(min_year)
    selected_max_year = _parse_optional_int(max_year)
    selected_complex = _parse_optional_text(residential_complex)
    selected_condition = valid_apartment_condition_slug(condition)
    selected_new_build = _parse_checkbox_bool(new_build)
    selected_min_area = _parse_optional_positive_float(min_area)
    selected_max_area = _parse_optional_positive_float(max_area)
    selected_polygon = _parse_polygon(map_polygon)
    selected_new_since_hours = _parse_optional_int(new_since_hours, allowed={24, 48})
    selected_new_since = _new_since_threshold(selected_new_since_hours)
    selected_min_discount_pct = _parse_optional_percent(min_discount_pct)
    with connect(DB_PATH) as db_connection:
        total = count_undervalued(
            db_connection,
            city=city_slug,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            apartment_condition=selected_condition,
            new_build=selected_new_build,
            min_area=selected_min_area,
            max_area=selected_max_area,
            polygon=selected_polygon,
            new_since=selected_new_since,
            min_discount_pct=selected_min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
        total_pages = max(
            (total + UNDERVALUED_PAGE_SIZE - 1) // UNDERVALUED_PAGE_SIZE,
            1,
        )
        safe_page = min(max(page, 1), total_pages)
        offset = (safe_page - 1) * UNDERVALUED_PAGE_SIZE
        items = fetch_undervalued(
            db_connection,
            city=city_slug,
            limit=UNDERVALUED_PAGE_SIZE,
            offset=offset,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            apartment_condition=selected_condition,
            new_build=selected_new_build,
            min_area=selected_min_area,
            max_area=selected_max_area,
            polygon=selected_polygon,
            new_since=selected_new_since,
            min_discount_pct=selected_min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
        active_listings = (
            fetch_status_summary(db_connection, city=city_slug).get("active_listings")
            or 0
        )
    return templates.TemplateResponse(
        request,
        "undervalued.html",
        {
            "request": request,
            "items": items,
            "district_options": district_options(city_slug),
            "selected_districts": selected_districts,
            "selected_rooms": selected_rooms,
            "selected_max_price": selected_max_price,
            "selected_min_year": selected_min_year,
            "selected_max_year": selected_max_year,
            "selected_complex": selected_complex,
            "condition_options": APARTMENT_CONDITION_OPTIONS,
            "selected_condition": selected_condition,
            "selected_new_build": selected_new_build,
            "selected_min_area": selected_min_area,
            "selected_max_area": selected_max_area,
            "selected_polygon": map_polygon or "",
            "selected_new_since_hours": selected_new_since_hours,
            "selected_min_discount_pct": selected_min_discount_pct,
            "selected_sort": sort,
            "filter_query": _build_filter_query(
                city=city_slug,
                districts=selected_districts,
                rooms=selected_rooms,
                max_price=selected_max_price,
                min_year=selected_min_year,
                max_year=selected_max_year,
                residential_complex=selected_complex,
                apartment_condition=selected_condition,
                new_build=selected_new_build,
                min_area=selected_min_area,
                max_area=selected_max_area,
                map_polygon=map_polygon,
                new_since_hours=selected_new_since_hours,
                min_discount_pct=selected_min_discount_pct,
                sort=sort,
            ),
            "active_listings": active_listings,
            "page": safe_page,
            "page_size": UNDERVALUED_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "pagination_pages": _pagination_window(safe_page, total_pages),
            "has_previous": safe_page > 1,
            "has_next": offset + UNDERVALUED_PAGE_SIZE < total,
            "start_rank": offset + 1,
            "is_preview": False,
        },
    )


@app.get("/find-home-page", response_class=HTMLResponse)
def find_home_page(
    request: Request,
    city: str = "astana",
    district: list[str] | None = Query(default=None),
    room: list[str] | None = Query(default=None),
    min_price: str | None = None,
    max_price: str | None = None,
    min_area: str | None = None,
    max_area: str | None = None,
    min_year: str | None = None,
    housing_type: str = "any",
    condition: list[str] | None = Query(default=None),
    furnished_only: str | None = None,
    priority_park: str | None = None,
    priority_education: str | None = None,
    priority_transit: str | None = None,
    priority_grocery: str | None = None,
    priority_value: str | None = None,
    priority_ready: str | None = None,
    priority_modern: str | None = None,
) -> HTMLResponse:
    city_slug = normalize_city_slug(city)
    selected_districts = tuple(
        valid_district_slugs(district, city=city_slug)
    )
    selected_rooms = tuple(
        value
        for value in (
            _parse_optional_int(item, allowed={1, 2, 3, 4, 5})
            for item in (room or [])
        )
        if value is not None
    )
    selected_conditions = tuple(
        slug
        for slug in (valid_apartment_condition_slug(item) for item in (condition or []))
        if slug is not None
    )
    selected_housing_type = (
        housing_type if housing_type in {"any", "new", "secondary"} else "any"
    )
    selected_priorities = {
        "park": _parse_priority(priority_park, "park"),
        "education": _parse_priority(priority_education, "education"),
        "transit": _parse_priority(priority_transit, "transit"),
        "grocery": _parse_priority(priority_grocery, "grocery"),
        "value": _parse_priority(priority_value, "value"),
        "ready": _parse_priority(priority_ready, "ready"),
        "modern": _parse_priority(priority_modern, "modern"),
    }
    preferences = HomeSearchPreferences(
        districts=selected_districts,
        rooms=selected_rooms,
        min_price=_parse_optional_positive_float(min_price),
        max_price=_parse_optional_positive_float(max_price),
        min_area=_parse_optional_positive_float(min_area),
        max_area=_parse_optional_positive_float(max_area),
        min_year=_parse_optional_int(min_year),
        housing_type=selected_housing_type,
        conditions=selected_conditions,
        furnished_only=_parse_checkbox_bool(furnished_only),
        priorities=selected_priorities,
    )
    with connect(DB_PATH) as db_connection:
        candidates = fetch_home_match_candidates(db_connection, city=city_slug)
    result = rank_home_candidates(
        candidates,
        preferences,
        catalog_path=POI_CATALOG_PATH,
        city=city_slug,
    )
    preserved_filters: dict[str, object] = {"city": city_slug}
    if preferences.districts:
        preserved_filters["district"] = preferences.districts
    if preferences.rooms:
        preserved_filters["room"] = preferences.rooms
    for key, value in (
        ("min_price", preferences.min_price),
        ("max_price", preferences.max_price),
        ("min_area", preferences.min_area),
        ("max_area", preferences.max_area),
        ("min_year", preferences.min_year),
    ):
        if value is not None:
            preserved_filters[key] = value
    if preferences.housing_type != "any":
        preserved_filters["housing_type"] = preferences.housing_type
    if preferences.conditions:
        preserved_filters["condition"] = preferences.conditions
    if preferences.furnished_only:
        preserved_filters["furnished_only"] = 1
    home_presets = []
    for preset in HOME_PRESETS:
        preset_copy = dict(preset)
        preset_params = dict(preserved_filters)
        if preset["slug"] != "balanced":
            preset_params.update(
                {
                    f"priority_{key}": value
                    for key, value in preset["priorities"].items()
                }
            )
        preset_query = urlencode(preset_params, doseq=True)
        preset_copy["url"] = (
            f"/find-home-page?{preset_query}"
            if preset_query
            else "/find-home-page"
        )
        home_presets.append(preset_copy)
    selected_preset = next(
        (
            preset
            for preset in home_presets
            if preset["priorities"] == result["priorities"]
        ),
        None,
    )
    return templates.TemplateResponse(
        request,
        "home_finder.html",
        {
            "request": request,
            "items": result["items"],
            "total": result["total"],
            "candidate_count": len(candidates),
            "catalog": result["catalog"],
            "district_options": district_options(city_slug),
            "condition_options": APARTMENT_CONDITION_OPTIONS,
            "priority_options": PRIORITY_OPTIONS,
            "home_presets": home_presets,
            "selected_preset": selected_preset,
            "selected_districts": selected_districts,
            "selected_rooms": selected_rooms,
            "selected_min_price": preferences.min_price,
            "selected_max_price": preferences.max_price,
            "selected_min_area": preferences.min_area,
            "selected_max_area": preferences.max_area,
            "selected_min_year": preferences.min_year,
            "selected_housing_type": selected_housing_type,
            "selected_conditions": selected_conditions,
            "selected_furnished_only": preferences.furnished_only,
            "selected_priorities": result["priorities"],
        },
    )


@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_page(
    request: Request,
    next: str = "/status-page",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {
            "request": request,
            "error": None,
            "next_url": _safe_next_url(next),
        },
    )


@app.post("/admin-login")
def admin_login(
    request: Request,
    password: str = Form(...),
    next: str = Form("/status-page"),
) -> Response:
    try:
        _require_admin_token(password)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "admin_login.html",
            {
                "request": request,
                "error": "Неверный пароль.",
                "next_url": _safe_next_url(next),
            },
            status_code=400,
        )

    response = RedirectResponse(_safe_next_url(next), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        _create_admin_session_cookie(),
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/admin-logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


def require_admin_api_session(request: Request) -> bool:
    if _is_valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE)):
        return True
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Нужна авторизация.")


@app.get("/refresh-runs")
def refresh_runs(
    limit: int = 20,
    _: bool = Depends(require_admin_api_session),
) -> dict:
    with connect(DB_PATH) as db_connection:
        runs = fetch_refresh_runs(db_connection, limit=limit)
    return {
        "items": runs,
    }


@app.get("/status-summary")
def status_summary(_: bool = Depends(require_admin_api_session)) -> dict:
    with connect(DB_PATH) as db_connection:
        summary = fetch_status_summary(db_connection)
        summary["cities"] = {
            option["slug"]: fetch_status_summary(
                db_connection,
                city=option["slug"],
            )
            for option in CITY_OPTIONS
        }
    return summary


@app.get("/status-page", response_class=HTMLResponse)
def status_page(
    request: Request,
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    with connect(DB_PATH) as db_connection:
        summary = fetch_status_summary(db_connection)
        city_summaries = [
            {
                "city": city_config(option["slug"]),
                "summary": fetch_status_summary(
                    db_connection,
                    city=option["slug"],
                ),
            }
            for option in CITY_OPTIONS
        ]
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "request": request,
            "summary": summary,
            "city_summaries": city_summaries,
        },
    )


@app.get("/model-monitoring-page", response_class=HTMLResponse)
def model_monitoring_page(
    request: Request,
    limit: int = 30,
) -> HTMLResponse:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    safe_limit = min(max(limit, 1), 100)
    with connect(DB_PATH) as db_connection:
        snapshots = fetch_monitoring_snapshots(db_connection, limit=safe_limit)
        summary = fetch_status_summary(db_connection)

    return templates.TemplateResponse(
        request,
        "model_monitoring.html",
        {
            "request": request,
            "snapshots": snapshots,
            "summary": summary,
        },
    )


@app.get("/model-version-page", response_class=HTMLResponse)
def model_version_page(request: Request) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    return templates.TemplateResponse(
        request,
        "model_version.html",
        {
            "request": request,
            "model_info": _model_version_info(),
        },
    )


@app.get("/traffic-page", response_class=HTMLResponse)
def traffic_page(
    request: Request,
    limit: int = 30,
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    safe_limit = min(max(limit, 1), 100)
    with connect(DB_PATH) as db_connection:
        traffic = fetch_traffic_summary(db_connection, limit=safe_limit)

    return templates.TemplateResponse(
        request,
        "traffic.html",
        {
            "request": request,
            "traffic": traffic,
            "rate_limit_per_minute": PREDICT_RATE_LIMIT_PER_MINUTE,
            "rate_limit_per_hour": PREDICT_RATE_LIMIT_PER_HOUR,
            "cache_ttl_hours": PREDICTION_CACHE_TTL_SECONDS / 3600,
        },
    )


@app.get("/feedback-admin-page", response_class=HTMLResponse)
def feedback_admin_page(
    request: Request,
    limit: int = 100,
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    safe_limit = min(max(limit, 1), 200)
    with connect(DB_PATH) as db_connection:
        items = fetch_feedback_messages(db_connection, limit=safe_limit)

    return templates.TemplateResponse(
        request,
        "feedback_admin.html",
        {
            "request": request,
            "items": items,
            "message": None,
        },
    )


@app.post("/feedback-admin-delete", response_class=HTMLResponse)
def delete_feedback_admin(
    request: Request,
    feedback_id: int = Form(...),
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    with connect(DB_PATH) as db_connection:
        deleted = delete_feedback_message(db_connection, feedback_id)
        items = fetch_feedback_messages(db_connection, limit=100)

    return templates.TemplateResponse(
        request,
        "feedback_admin.html",
        {
            "request": request,
            "items": items,
            "message": (
                "\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 "
                "\u0443\u0434\u0430\u043b\u0435\u043d\u043e."
                if deleted
                else "\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 "
                "\u0443\u0436\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u043e."
            ),
        },
    )


@app.get("/refresh-runs-page", response_class=HTMLResponse)
def refresh_runs_page(
    request: Request,
    limit: int = 20,
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    with connect(DB_PATH) as db_connection:
        runs = fetch_refresh_runs(db_connection, limit=limit)
    return templates.TemplateResponse(
        request,
        "refresh_runs.html",
        {"request": request, "items": runs},
    )


@app.get("/admin-refresh-page", response_class=HTMLResponse)
def admin_refresh_page(
    request: Request,
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    with connect(DB_PATH) as db_connection:
        running_refresh = fetch_running_refresh(db_connection)
    return templates.TemplateResponse(
        request,
        "admin_refresh.html",
        {
            "request": request,
            "error": None,
            "message": None,
            "form": _default_refresh_form(),
            "running_refresh": running_refresh,
        },
    )


@app.post("/admin-refresh", response_class=HTMLResponse)
def admin_refresh_form(
    request: Request,
    background_tasks: BackgroundTasks,
    city: str = Form("astana"),
    kind: str = Form("manual"),
    start_page: int = Form(1),
    pages: int = Form(1),
    min_delay: float = Form(1.0),
    max_delay: float = Form(2.0),
    max_listings: int = Form(0),
) -> Response:
    redirect = _admin_page_redirect_if_needed(request)
    if redirect:
        return redirect

    city_slug = normalize_city_slug(city)
    form = {
        "city": city_slug,
        "kind": kind,
        "start_page": start_page,
        "pages": pages,
        "min_delay": min_delay,
        "max_delay": max_delay,
        "max_listings": max_listings,
    }
    try:
        _validate_refresh_options(
            kind=kind,
            start_page=start_page,
            pages=pages,
            min_delay=min_delay,
            max_delay=max_delay,
            max_listings=max_listings,
        )
    except ValueError as exc:
        with connect(DB_PATH) as db_connection:
            running_refresh = fetch_running_refresh(db_connection)
        return templates.TemplateResponse(
            request,
            "admin_refresh.html",
            {
                "request": request,
                "error": str(exc),
                "message": None,
                "form": form,
                "running_refresh": running_refresh,
            },
            status_code=400,
        )

    conflict_response = _refresh_conflict_response(request, form)
    if conflict_response:
        return conflict_response

    background_tasks.add_task(
        run_refresh,
        root=ROOT,
        db_path=DB_PATH,
        city=city_slug,
        kind=kind,
        start_page=start_page,
        pages=pages,
        min_delay=min_delay,
        max_delay=max_delay,
        max_listings=max_listings,
    )
    return templates.TemplateResponse(
        request,
        "admin_refresh.html",
        {
            "request": request,
            "error": None,
            "message": "Обновление запущено. Проверьте историю обновлений через несколько минут.",
            "form": form,
            "running_refresh": None,
        },
    )


def _refresh_conflict_response(request: Request, form: dict) -> HTMLResponse | None:
    with connect(DB_PATH) as db_connection:
        running_refresh = fetch_running_refresh(db_connection)
    if not running_refresh:
        return None
    return templates.TemplateResponse(
        request,
        "admin_refresh.html",
        {
            "request": request,
            "error": f"Обновление уже выполняется: run #{running_refresh['id']}. Дождитесь завершения.",
            "message": None,
            "form": form,
            "running_refresh": running_refresh,
        },
        status_code=409,
    )


@app.post("/refresh-listings")
def refresh_listings(
    payload: RefreshRequest,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    try:
        _require_admin_token(x_admin_token)
        _validate_refresh_options(
            kind=payload.kind,
            start_page=payload.start_page,
            pages=payload.pages,
            min_delay=payload.min_delay,
            max_delay=payload.max_delay,
            max_listings=payload.max_listings,
        )
    except ValueError as exc:
        status_code = 503 if "ADMIN_TOKEN" in str(exc) else 400
        if "админ-токен" in str(exc):
            status_code = 403
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    with connect(DB_PATH) as db_connection:
        running_refresh = fetch_running_refresh(db_connection)
    if running_refresh:
        raise HTTPException(
            status_code=409,
            detail=f"Обновление уже выполняется: run #{running_refresh['id']}.",
        )

    background_tasks.add_task(
        run_refresh,
        root=ROOT,
        db_path=DB_PATH,
        city=normalize_city_slug(payload.city),
        kind=payload.kind,
        start_page=payload.start_page,
        pages=payload.pages,
        min_delay=payload.min_delay,
        max_delay=payload.max_delay,
        max_listings=payload.max_listings,
    )
    return {
        "status": "started",
        "message": "Обновление запущено.",
        "kind": payload.kind,
        "city": normalize_city_slug(payload.city),
        "start_page": payload.start_page,
        "pages": payload.pages,
        "max_listings": payload.max_listings,
    }


def _default_refresh_form() -> dict:
    return {
        "city": "astana",
        "kind": "manual",
        "start_page": 1,
        "pages": 1,
        "min_delay": 1.0,
        "max_delay": 2.0,
        "max_listings": 0,
    }


def _model_version_info() -> dict:
    metadata_path = ROOT / "model_metadata.json"
    metadata = {
        "feature_columns": prediction_service.model_service.metadata.feature_columns,
        "categorical_features": prediction_service.model_service.metadata.categorical_features,
        "target": prediction_service.model_service.metadata.target,
    }
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    model_files = []
    for quantile, filename in MODEL_FILENAMES.items():
        path = ROOT / "models" / filename
        if path.exists():
            stat = path.stat()
            model_files.append(
                {
                    "quantile": quantile,
                    "filename": filename,
                    "size_mb": stat.st_size / (1024 * 1024),
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        timezone.utc,
                    ).isoformat(timespec="seconds"),
                }
            )
        else:
            model_files.append(
                {
                    "quantile": quantile,
                    "filename": filename,
                    "size_mb": None,
                    "modified_at": None,
                }
            )

    return {
        "target": metadata.get("target"),
        "feature_count": len(metadata.get("feature_columns") or []),
        "categorical_count": len(metadata.get("categorical_features") or []),
        "metadata_modified_at": (
            datetime.fromtimestamp(
                metadata_path.stat().st_mtime,
                timezone.utc,
            ).isoformat(timespec="seconds")
            if metadata_path.exists()
            else None
        ),
        "model_files": model_files,
    }


def _predict_for_request(
    request: Request,
    url: str,
    *,
    prefer_stored: bool = False,
) -> ListingPrediction:
    normalized_url = _normalize_prediction_url(url)
    validate_krisha_url(normalized_url)

    # Listing detail links originate from our inventory. Reusing the prediction
    # already stored during refresh makes this route fast and independent of
    # Krisha availability. Unknown URLs still use the normal cached scraper flow.
    if prefer_stored:
        with connect(DB_PATH) as db_connection:
            stored_listing = fetch_listing_by_url(db_connection, normalized_url)
        stored_prediction = _prediction_from_stored_listing(stored_listing)
        if stored_prediction:
            return stored_prediction

    _enforce_predict_rate_limit(request)
    cache_key = prediction_service.cache_key(normalized_url)

    with connect(DB_PATH) as db_connection:
        cached = fetch_cached_prediction(
            db_connection,
            cache_key,
            ttl_seconds=PREDICTION_CACHE_TTL_SECONDS,
        )
    if cached:
        return ListingPrediction(**cached)

    prediction = prediction_service.predict_by_url(normalized_url)
    with connect(DB_PATH) as db_connection:
        store_cached_prediction(
            db_connection,
            url=cache_key,
            prediction=asdict(prediction),
        )
    return prediction


def _prediction_from_stored_listing(
    listing: dict | None,
) -> ListingPrediction | None:
    if not listing:
        return None

    required_fields = (
        "listed_price",
        "area_m2",
        "listed_price_per_m2",
        "pred_price_per_m2_q10",
        "pred_price_per_m2_q50",
        "pred_price_per_m2_q90",
    )
    if any(listing.get(field) is None for field in required_fields):
        return None

    listed_price = float(listing["listed_price"])
    area_m2 = float(listing["area_m2"])
    listed_price_per_m2 = float(listing["listed_price_per_m2"])
    pred_q10 = float(listing["pred_price_per_m2_q10"])
    pred_q50 = float(listing["pred_price_per_m2_q50"])
    pred_q90 = float(listing["pred_price_per_m2_q90"])
    if min(listed_price, area_m2, listed_price_per_m2, pred_q50) <= 0:
        return None

    def optional_float(name: str) -> float | None:
        value = listing.get(name)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    conservative_discount = optional_float(
        "discount_vs_asking_pct_conservative"
    )
    median_discount = optional_float("discount_vs_asking_pct_median")
    interval_width = optional_float("interval_width_pct")

    return ListingPrediction(
        url=str(listing["url"]),
        title=str(listing.get("title") or "Объявление Krisha.kz"),
        listed_price=listed_price,
        area_m2=area_m2,
        listed_price_per_m2=listed_price_per_m2,
        pred_price_per_m2_q10=pred_q10,
        pred_price_per_m2_q50=pred_q50,
        pred_price_per_m2_q90=pred_q90,
        pred_total_q50=(
            optional_float("pred_total_q50") or pred_q50 * area_m2
        ),
        discount_vs_asking_pct_conservative=(
            conservative_discount
            if conservative_discount is not None
            else (pred_q10 - listed_price_per_m2) / listed_price_per_m2
        ),
        discount_vs_asking_pct_median=(
            median_discount
            if median_discount is not None
            else (pred_q50 - listed_price_per_m2) / listed_price_per_m2
        ),
        interval_width_pct=(
            interval_width
            if interval_width is not None
            else (pred_q90 - pred_q10) / pred_q50
        ),
        city=str(listing.get("city") or "astana"),
        monthly_rent_q10=optional_float("monthly_rent_q10"),
        monthly_rent_q50=optional_float("monthly_rent_q50"),
        monthly_rent_q90=optional_float("monthly_rent_q90"),
        gross_yield_q10=optional_float("gross_yield_q10"),
        gross_yield_q50=optional_float("gross_yield_q50"),
        gross_yield_q90=optional_float("gross_yield_q90"),
        payback_years_q50=optional_float("payback_years_q50"),
        rental_model_version=(
            str(listing["rental_model_version"])
            if listing.get("rental_model_version")
            else None
        ),
    )


def _normalize_prediction_url(url: str) -> str:
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    if netloc == "www.krisha.kz":
        netloc = "krisha.kz"
    path = parsed.path.rstrip("/")
    return urlunparse(("https", netloc, path, "", "", ""))


def _enforce_predict_rate_limit(request: Request) -> None:
    key = _client_hash(request)
    now = time.time()
    bucket = [item for item in RATE_LIMIT_BUCKETS.get(key, []) if now - item < 3600]
    requests_last_minute = sum(1 for item in bucket if now - item < 60)
    if (
        requests_last_minute >= PREDICT_RATE_LIMIT_PER_MINUTE
        or len(bucket) >= PREDICT_RATE_LIMIT_PER_HOUR
    ):
        RATE_LIMIT_BUCKETS[key] = bucket
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "\u0421\u043b\u0438\u0448\u043a\u043e\u043c \u043c\u043d\u043e\u0433\u043e "
                "\u0437\u0430\u043f\u0440\u043e\u0441\u043e\u0432 \u043a "
                "\u043e\u0446\u0435\u043d\u043a\u0435. \u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435 "
                "\u043c\u0438\u043d\u0443\u0442\u0443 \u0438 \u043f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435."
            ),
        )
    bucket.append(now)
    RATE_LIMIT_BUCKETS[key] = bucket


def _should_track_request(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/static/"):
        return False
    return path not in {"/health", "/favicon.ico"}


def _client_hash(request: Request) -> str:
    client_ip = _client_ip(request)
    salt = os.getenv("ANALYTICS_SALT") or os.getenv("ADMIN_TOKEN") or "local-dev"
    return hmac.new(
        salt.encode("utf-8"),
        client_ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def _prediction_context(request: Request, prediction: object) -> dict:
    listing = None
    price_history = []
    complex_stats = None
    comparable_insight = None
    try:
        with connect(DB_PATH) as db_connection:
            listing = fetch_listing_by_url(db_connection, prediction.url)
            price_history = fetch_price_history(db_connection, prediction.url)
            if listing:
                try:
                    complex_stats = fetch_complex_stats(
                        db_connection,
                        listing.get("residential_complex"),
                        city=listing.get("city") or prediction.city,
                    )
                except Exception:
                    logger.exception(
                        "Could not build complex stats for %s", prediction.url
                    )
                try:
                    comparable_insight = build_comparable_insight(
                        listing,
                        fetch_comparable_candidates(
                            db_connection,
                            city=listing.get("city") or prediction.city,
                            exclude_url=prediction.url,
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Could not build comparables for %s", prediction.url
                    )
    except Exception:
        # Core prediction values are enough to render a useful result. Optional
        # database enrichment must never turn a listing page into a 500 response.
        logger.exception("Could not enrich prediction page for %s", prediction.url)
    selected_city = city_config(
        (listing or {}).get("city") or getattr(prediction, "city", "astana")
    )
    risk_flags = _build_risk_flags(prediction, listing, price_history, complex_stats)
    price_chart_points = _price_chart_points(price_history)
    mortgage = analyze_otbasy_mortgage(prediction.listed_price)

    return {
        "request": request,
        "prediction": prediction,
        "listing": listing,
        "price_history": price_history,
        "complex_stats": complex_stats,
        "risk_flags": risk_flags,
        "price_chart_points": price_chart_points,
        "mortgage": mortgage,
        "comparable_insight": comparable_insight,
        "rental_analysis_available": all(
            getattr(prediction, field, None) is not None
            for field in (
                "monthly_rent_q10",
                "monthly_rent_q50",
                "monthly_rent_q90",
                "gross_yield_q10",
                "gross_yield_q50",
                "gross_yield_q90",
                "payback_years_q50",
            )
        ),
        "city": selected_city,
    }


def _build_risk_flags(
    prediction: object,
    listing: dict | None,
    price_history: list[dict],
    complex_stats: dict | None,
) -> list[dict]:
    flags = []
    if getattr(prediction, "interval_width_pct", 0) >= 0.35:
        flags.append(
            {
                "level": "warning",
                "title": "Широкий интервал оценки",
                "text": "Модель менее уверена в оценке для этого объявления.",
            }
        )
    if listing and not listing.get("residential_complex"):
        flags.append(
            {
                "level": "neutral",
                "title": "ЖК не указан",
                "text": "Сравнение по жилому комплексу для этого объявления ограничено.",
            }
        )
    if listing and (listing.get("lat") is None or listing.get("lon") is None):
        flags.append(
            {
                "level": "warning",
                "title": "Нет координат",
                "text": "Карта и географические признаки могут быть менее точными.",
            }
        )
    if complex_stats and complex_stats.get("count", 0) < 3:
        flags.append(
            {
                "level": "neutral",
                "title": "Мало объявлений по ЖК",
                "text": "Статистика по жилому комплексу основана на небольшом числе объектов.",
            }
        )
    if len(price_history) >= 2:
        first_price = price_history[0].get("listed_price") or 0
        last_price = price_history[-1].get("listed_price") or 0
        if first_price and last_price < first_price:
            flags.append(
                {
                    "level": "positive",
                    "title": "Цена снижалась",
                    "text": f"С момента первого наблюдения цена ниже на {first_price - last_price:,.0f} тг.",
                }
            )
        elif first_price and last_price > first_price:
            flags.append(
                {
                    "level": "warning",
                    "title": "Цена повышалась",
                    "text": f"С момента первого наблюдения цена выше на {last_price - first_price:,.0f} тг.",
                }
            )
    if not flags:
        flags.append(
            {
                "level": "positive",
                "title": "Критичных предупреждений нет",
                "text": "По доступным данным явных технических ограничений для оценки не найдено.",
            }
        )
    return flags


def _price_chart_points(price_history: list[dict]) -> list[dict]:
    prices = [point.get("listed_price") for point in price_history if point.get("listed_price")]
    if not prices:
        return []
    min_price = min(prices)
    max_price = max(prices)
    span = max(max_price - min_price, 1)
    points = []
    for point in price_history:
        price = point.get("listed_price")
        if not price:
            continue
        height = 22 + ((price - min_price) / span) * 78
        points.append(
            {
                "observed_at": point.get("observed_at"),
                "listed_price": price,
                "height": height,
            }
        )
    return points


def _parse_optional_positive_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = float(re.sub(r"[\s\u00a0]+", "", value))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _pagination_window(current_page: int, total_pages: int) -> list[int | None]:
    """Return compact page numbers, using None as an ellipsis marker."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    visible_pages = {1, total_pages}
    visible_pages.update(
        range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1)
    )
    result: list[int | None] = []
    previous_page: int | None = None
    for page_number in sorted(visible_pages):
        if previous_page is not None and page_number - previous_page > 1:
            result.append(None)
        result.append(page_number)
        previous_page = page_number
    return result


def _parse_optional_percent(value: str | None) -> float | None:
    parsed = _parse_optional_positive_float(value)
    if parsed is None:
        return None
    if parsed > 100:
        return None
    return parsed / 100 if parsed > 1 else parsed


def _parse_optional_int(
    value: str | None,
    *,
    allowed: set[int] | None = None,
) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if allowed and parsed not in allowed:
        return None
    return parsed


def _parse_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_checkbox_bool(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "да"}


def _parse_priority(value: str | None, key: str) -> int:
    parsed = _parse_optional_int(value, allowed={0, 1, 2})
    return DEFAULT_PRIORITIES[key] if parsed is None else parsed


def _parse_optional_email(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _feedback_error(
    message: str,
    cleaned_email: str | None,
    raw_email: str,
) -> str | None:
    if len(message) < 10:
        return "\u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435, \u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u0445\u043e\u0442\u044f \u0431\u044b 10 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432."
    if len(message) > 3000:
        return "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u0434\u043b\u0438\u043d\u043d\u043e\u0435. \u041b\u0438\u043c\u0438\u0442 - 3000 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432."
    if raw_email.strip() and not cleaned_email:
        return "\u0423\u043a\u0430\u0436\u0438\u0442\u0435 email \u0438\u043b\u0438 \u043e\u0441\u0442\u0430\u0432\u044c\u0442\u0435 \u043f\u043e\u043b\u0435 \u043f\u0443\u0441\u0442\u044b\u043c."
    if cleaned_email:
        if len(cleaned_email) > 254 or not re.match(
            r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
            cleaned_email,
        ):
            return "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 email: \u043e\u043d \u043f\u043e\u0445\u043e\u0436 \u043d\u0430 \u043e\u0448\u0438\u0431\u043a\u0443."
    return None


def _new_since_threshold(hours: int | None) -> str | None:
    if not hours:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


def _parse_polygon(value: str | None) -> list[tuple[float, float]] | None:
    if not value:
        return None
    points: list[tuple[float, float]] = []
    for raw_point in value.split(";"):
        try:
            lat_text, lon_text = raw_point.split(",", 1)
            lat = float(lat_text)
            lon = float(lon_text)
        except ValueError:
            return None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        points.append((lat, lon))
    return points if len(points) >= 3 else None


def _build_filter_query(
    *,
    city: str = "astana",
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    apartment_condition: str | None = None,
    new_build: bool = False,
    min_area: float | None = None,
    max_area: float | None = None,
    map_polygon: str | None = None,
    new_since_hours: int | None = None,
    min_discount_pct: float | None = None,
    sort: str | None = None,
    page: int | None = None,
) -> str:
    params = _filter_params(
        city=city,
        districts=districts,
        rooms=rooms,
        max_price=max_price,
        min_year=min_year,
        max_year=max_year,
        residential_complex=residential_complex,
        developer=developer,
        apartment_condition=apartment_condition,
        new_build=new_build,
        min_area=min_area,
        max_area=max_area,
        map_polygon=map_polygon,
        new_since_hours=new_since_hours,
        min_discount_pct=min_discount_pct,
        sort=sort,
        page=page,
    )
    return urlencode(params)


def _filter_params(
    *,
    city: str = "astana",
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    apartment_condition: str | None = None,
    new_build: bool = False,
    min_area: float | None = None,
    max_area: float | None = None,
    map_polygon: str | None = None,
    new_since_hours: int | None = None,
    min_discount_pct: float | None = None,
    sort: str | None = None,
    page: int | None = None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("city", normalize_city_slug(city))]
    if page and page > 1:
        params.append(("page", str(page)))
    for district in districts or []:
        params.append(("district", district))
    if rooms:
        params.append(("rooms", str(rooms)))
    if max_price:
        params.append(("max_price", _format_filter_number(max_price)))
    if min_year:
        params.append(("min_year", str(min_year)))
    if max_year:
        params.append(("max_year", str(max_year)))
    if residential_complex:
        params.append(("residential_complex", residential_complex))
    if developer:
        params.append(("developer", developer))
    if apartment_condition:
        params.append(("condition", apartment_condition))
    if new_build:
        params.append(("new_build", "1"))
    if min_area:
        params.append(("min_area", _format_filter_number(min_area)))
    if max_area:
        params.append(("max_area", _format_filter_number(max_area)))
    if map_polygon:
        params.append(("map_polygon", map_polygon))
    if new_since_hours:
        params.append(("new_since_hours", str(new_since_hours)))
    if min_discount_pct:
        params.append(("min_discount_pct", _format_filter_number(min_discount_pct * 100)))
    if sort and sort != "q10_discount":
        params.append(("sort", sort))
    return params


def _format_filter_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _public_page_response(request: Request, page_slug: str) -> HTMLResponse:
    return templates.TemplateResponse(
        "info_page.html",
        {"request": request, "page": PUBLIC_PAGES[page_slug]},
    )


def _resolve_user_session(request: Request) -> dict | None:
    raw_token = request.cookies.get(USER_SESSION_COOKIE)
    if not raw_token or len(raw_token) > 160:
        return None
    try:
        with connect(DB_PATH) as db_connection:
            return fetch_user_session(db_connection, session_token_hash(raw_token))
    except sqlite3.Error:
        return None


def require_user_session(request: Request) -> dict:
    session = getattr(request.state, "user_session", None)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Войдите в аккаунт, чтобы продолжить.",
        )
    return session


def _user_session_or_redirect(request: Request) -> dict | RedirectResponse:
    session = getattr(request.state, "user_session", None)
    if session:
        return session
    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(
        f"/login?next={quote(next_url, safe='')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _create_user_session(
    connection,
    *,
    request: Request,
    user_id: int,
    remembered: bool,
) -> tuple[str, int | None]:
    ttl_seconds = (
        REMEMBERED_SESSION_TTL_SECONDS if remembered else USER_SESSION_TTL_SECONDS
    )
    raw_token = new_session_token()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat(timespec="seconds")
    create_user_session(
        connection,
        token_hash=session_token_hash(raw_token),
        user_id=user_id,
        csrf_token=new_csrf_token(),
        expires_at=expires_at,
        user_agent=request.headers.get("user-agent"),
    )
    return raw_token, ttl_seconds if remembered else None


def _set_user_session_cookie(
    response: Response,
    request: Request,
    raw_token: str,
    *,
    max_age: int | None,
) -> None:
    response.set_cookie(
        USER_SESSION_COOKIE,
        raw_token,
        max_age=max_age,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/",
    )


def _set_auth_csrf_cookie(
    response: Response,
    request: Request,
    token: str,
) -> None:
    response.set_cookie(
        AUTH_CSRF_COOKIE,
        token,
        max_age=60 * 60,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/",
    )


def _request_is_secure(request: Request) -> bool:
    configured = os.getenv("COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
    return request.url.scheme == "https" or forwarded_proto.strip().lower() == "https"


def _verify_pre_auth_csrf(request: Request, submitted_token: str | None) -> None:
    cookie_token = request.cookies.get(AUTH_CSRF_COOKIE)
    if (
        not cookie_token
        or not submitted_token
        or not secrets.compare_digest(cookie_token, submitted_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Форма устарела. Обновите страницу и попробуйте снова.",
        )


def _require_user_csrf(session: dict, submitted_token: str | None) -> None:
    expected = str(session.get("csrf_token") or "")
    if (
        not expected
        or not submitted_token
        or not secrets.compare_digest(expected, submitted_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Не удалось подтвердить безопасность запроса. Обновите страницу.",
        )


def _enforce_auth_rate_limit(request: Request, action: str) -> None:
    now = time.time()
    window_seconds = 60 * 60 if action == "register" else 15 * 60
    limit = 5 if action == "register" else 12
    key = f"{action}:{_client_hash(request)}"
    bucket = [
        timestamp
        for timestamp in AUTH_RATE_LIMIT_BUCKETS.get(key, [])
        if now - timestamp < window_seconds
    ]
    if len(bucket) >= limit:
        AUTH_RATE_LIMIT_BUCKETS[key] = bucket
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток. Подождите и попробуйте снова.",
        )
    bucket.append(now)
    AUTH_RATE_LIMIT_BUCKETS[key] = bucket


def _safe_user_next_url(value: str, default: str = "/account") -> str:
    parsed = urlparse(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in value
    ):
        return default
    return value


def _canonical_listing_url(value: str) -> str:
    normalized = _normalize_prediction_url(value)
    try:
        validate_krisha_url(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if len(normalized) > 500:
        raise HTTPException(status_code=400, detail="Ссылка слишком длинная.")
    return normalized


def _watchlist_context(
    request: Request,
    session: dict,
    items: list[dict],
) -> dict:
    price_drops = sum(1 for item in items if (item.get("price_change") or 0) < 0)
    unavailable = sum(1 for item in items if not item.get("is_available"))
    return {
        "request": request,
        "account_user": session,
        "items": items,
        "recent_items": items[:3],
        "watchlist_summary": {
            "total": len(items),
            "price_drops": price_drops,
            "unavailable": unavailable,
            "active": max(len(items) - unavailable, 0),
        },
    }


def _admin_page_redirect_if_needed(request: Request) -> RedirectResponse | None:
    if _is_valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE)):
        return None
    next_url = str(request.url.path)
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"
    return RedirectResponse(
        f"/admin-login?next={quote(next_url, safe='')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _create_admin_session_cookie() -> str:
    issued_at = str(int(time.time()))
    return f"{issued_at}.{_sign_admin_session(issued_at)}"


def _is_valid_admin_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        issued_at, signature = value.split(".", 1)
        issued_at_int = int(issued_at)
    except ValueError:
        return False

    if time.time() - issued_at_int > ADMIN_SESSION_TTL_SECONDS:
        return False

    expected_signature = _sign_admin_session(issued_at)
    if not expected_signature:
        return False
    return secrets.compare_digest(signature, expected_signature)


def _sign_admin_session(issued_at: str) -> str:
    admin_token = os.getenv("ADMIN_TOKEN")
    if not admin_token:
        return ""
    return hmac.new(
        admin_token.encode("utf-8"),
        issued_at.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _safe_next_url(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/status-page"
    return value


def _require_admin_token(value: str | None) -> None:
    admin_token = os.getenv("ADMIN_TOKEN")
    if not admin_token:
        raise ValueError("ADMIN_TOKEN не настроен.")
    if value != admin_token:
        raise ValueError("Неверный админ-токен.")


def _validate_refresh_options(
    *,
    kind: str,
    start_page: int,
    pages: int,
    min_delay: float,
    max_delay: float,
    max_listings: int,
) -> None:
    if kind not in {"manual", "daily", "weekly"}:
        raise ValueError("Неверный тип обновления.")
    if pages < 1 or start_page < 1:
        raise ValueError(
            "Количество страниц и стартовая страница должны быть положительными."
        )
    if max_listings < 0:
        raise ValueError("max_listings не может быть отрицательным.")
    if min_delay < 0 or max_delay < 0:
        raise ValueError("Паузы между запросами не могут быть отрицательными.")
    if max_delay < min_delay:
        raise ValueError("Максимальная пауза не может быть меньше минимальной.")


def format_astana_time(value: object) -> str:
    if not value:
        return "-"

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(ASTANA_TZ).strftime("%Y-%m-%d %H:%M")
