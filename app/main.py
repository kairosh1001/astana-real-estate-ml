from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
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

from app.database import (
    DISTRICT_OPTIONS,
    connect,
    count_undervalued,
    create_feedback_message,
    delete_feedback_message,
    fetch_complex_stats,
    fetch_cached_prediction,
    fetch_feedback_messages,
    fetch_listing_by_url,
    fetch_listings_by_urls,
    fetch_market_dashboard,
    fetch_monitoring_snapshots,
    fetch_price_history,
    fetch_refresh_runs,
    fetch_running_refresh,
    fetch_status_summary,
    fetch_traffic_summary,
    fetch_undervalued,
    init_db,
    record_request_event,
    store_cached_prediction,
    valid_district_slugs,
)
from app.model_service import MODEL_FILENAMES
from app.prediction_service import (
    ListingPrediction,
    PredictionService,
    validate_krisha_url,
)
from app.refresh_service import run_refresh


ROOT = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1]))
ASTANA_TZ = timezone(timedelta(hours=5), name="Asia/Astana")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["astana_time"] = lambda value: format_astana_time(value)
ADMIN_SESSION_COOKIE = "krisha_admin_session"
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
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


@app.middleware("http")
async def traffic_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
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
    kind: str = "manual"
    start_page: int = 1
    pages: int = 1
    min_delay: float = 1.0
    max_delay: float = 2.0
    max_listings: int = 0


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "models_loaded": sorted(prediction_service.model_service.models.keys()),
        "feature_count": len(
            prediction_service.model_service.metadata.feature_columns
        ),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    with connect(DB_PATH) as db_connection:
        preview_items = fetch_undervalued(
            db_connection,
            limit=HOME_UNDERVALUED_LIMIT,
            include_stale=False,
        )
        fresh_items = fetch_undervalued(
            db_connection,
            limit=5,
            new_since=_new_since_threshold(24),
            include_stale=False,
        )
        total_undervalued = count_undervalued(db_connection, include_stale=False)
        status_summary = fetch_status_summary(db_connection)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "error": None,
            "url": "",
            "items": preview_items,
            "fresh_items": fresh_items,
            "total_undervalued": total_undervalued,
            "active_listings": status_summary.get("active_listings") or 0,
            "latest_refresh": status_summary.get("latest_refresh"),
            "district_options": DISTRICT_OPTIONS,
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


@app.get("/about-page", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "about.html",
        {"request": request},
    )


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
def market_page(request: Request) -> HTMLResponse:
    with connect(DB_PATH) as db_connection:
        dashboard = fetch_market_dashboard(db_connection)
        status_summary = fetch_status_summary(db_connection)

    return templates.TemplateResponse(
        request,
        "market.html",
        {
            "request": request,
            "dashboard": dashboard,
            "summary": status_summary,
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
    district: list[str] | None = Query(default=None),
    rooms: str | None = None,
    max_price: str | None = None,
    min_year: str | None = None,
    max_year: str | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    min_area: str | None = None,
    max_area: str | None = None,
    map_polygon: str | None = None,
    new_since_hours: str | None = None,
    min_discount_pct: str | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> dict:
    selected_districts = valid_district_slugs(district)
    selected_rooms = _parse_optional_int(rooms, allowed={1, 2, 3, 4, 5})
    selected_max_price = _parse_optional_positive_float(max_price)
    selected_min_year = _parse_optional_int(min_year)
    selected_max_year = _parse_optional_int(max_year)
    selected_complex = _parse_optional_text(residential_complex)
    selected_developer = _parse_optional_text(developer)
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
            limit=safe_limit,
            offset=offset,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            developer=selected_developer,
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
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            developer=selected_developer,
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
    district: list[str] | None = Query(default=None),
    rooms: str | None = None,
    max_price: str | None = None,
    min_year: str | None = None,
    max_year: str | None = None,
    residential_complex: str | None = None,
    min_area: str | None = None,
    max_area: str | None = None,
    map_polygon: str | None = None,
    new_since_hours: str | None = None,
    min_discount_pct: str | None = None,
    sort: str = "q10_discount",
    include_stale: bool = False,
) -> HTMLResponse:
    selected_districts = valid_district_slugs(district)
    selected_rooms = _parse_optional_int(rooms, allowed={1, 2, 3, 4, 5})
    selected_max_price = _parse_optional_positive_float(max_price)
    selected_min_year = _parse_optional_int(min_year)
    selected_max_year = _parse_optional_int(max_year)
    selected_complex = _parse_optional_text(residential_complex)
    selected_min_area = _parse_optional_positive_float(min_area)
    selected_max_area = _parse_optional_positive_float(max_area)
    selected_polygon = _parse_polygon(map_polygon)
    selected_new_since_hours = _parse_optional_int(new_since_hours, allowed={24, 48})
    selected_new_since = _new_since_threshold(selected_new_since_hours)
    selected_min_discount_pct = _parse_optional_percent(min_discount_pct)
    safe_page = max(page, 1)
    offset = (safe_page - 1) * UNDERVALUED_PAGE_SIZE
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            limit=UNDERVALUED_PAGE_SIZE,
            offset=offset,
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
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
            districts=selected_districts,
            rooms=selected_rooms,
            max_price=selected_max_price,
            min_year=selected_min_year,
            max_year=selected_max_year,
            residential_complex=selected_complex,
            min_area=selected_min_area,
            max_area=selected_max_area,
            polygon=selected_polygon,
            new_since=selected_new_since,
            min_discount_pct=selected_min_discount_pct,
            sort=sort,
            include_stale=include_stale,
        )
        status_summary = fetch_status_summary(db_connection)
    return templates.TemplateResponse(
        request,
        "undervalued.html",
        {
            "request": request,
            "items": items,
            "district_options": DISTRICT_OPTIONS,
            "selected_districts": selected_districts,
            "selected_rooms": selected_rooms,
            "selected_max_price": selected_max_price,
            "selected_min_year": selected_min_year,
            "selected_max_year": selected_max_year,
            "selected_complex": selected_complex,
            "selected_min_area": selected_min_area,
            "selected_max_area": selected_max_area,
            "selected_polygon": map_polygon or "",
            "selected_new_since_hours": selected_new_since_hours,
            "selected_min_discount_pct": selected_min_discount_pct,
            "selected_sort": sort,
            "filter_query": _build_filter_query(
                districts=selected_districts,
                rooms=selected_rooms,
                max_price=selected_max_price,
                min_year=selected_min_year,
                max_year=selected_max_year,
                residential_complex=selected_complex,
                min_area=selected_min_area,
                max_area=selected_max_area,
                map_polygon=map_polygon,
                new_since_hours=selected_new_since_hours,
                min_discount_pct=selected_min_discount_pct,
                sort=sort,
            ),
            "active_listings": status_summary.get("active_listings") or 0,
            "page": safe_page,
            "page_size": UNDERVALUED_PAGE_SIZE,
            "total": total,
            "has_previous": safe_page > 1,
            "has_next": offset + UNDERVALUED_PAGE_SIZE < total,
            "start_rank": offset + 1,
            "is_preview": False,
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
    return templates.TemplateResponse(
        request,
        "status.html",
        {"request": request, "summary": summary},
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

    form = {
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
        "start_page": payload.start_page,
        "pages": payload.pages,
        "max_listings": payload.max_listings,
    }


def _default_refresh_form() -> dict:
    return {
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


def _predict_for_request(request: Request, url: str) -> ListingPrediction:
    normalized_url = _normalize_prediction_url(url)
    validate_krisha_url(normalized_url)
    _enforce_predict_rate_limit(request)

    with connect(DB_PATH) as db_connection:
        cached = fetch_cached_prediction(
            db_connection,
            normalized_url,
            ttl_seconds=PREDICTION_CACHE_TTL_SECONDS,
        )
    if cached:
        return ListingPrediction(**cached)

    prediction = prediction_service.predict_by_url(normalized_url)
    with connect(DB_PATH) as db_connection:
        store_cached_prediction(
            db_connection,
            url=normalized_url,
            prediction=asdict(prediction),
        )
    return prediction


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
    with connect(DB_PATH) as db_connection:
        listing = fetch_listing_by_url(db_connection, prediction.url)
        price_history = fetch_price_history(db_connection, prediction.url)
        if listing:
            complex_stats = fetch_complex_stats(
                db_connection,
                listing.get("residential_complex"),
            )
    risk_flags = _build_risk_flags(prediction, listing, price_history, complex_stats)
    price_chart_points = _price_chart_points(price_history)

    return {
        "request": request,
        "prediction": prediction,
        "listing": listing,
        "price_history": price_history,
        "complex_stats": complex_stats,
        "risk_flags": risk_flags,
        "price_chart_points": price_chart_points,
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
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


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
        if not (50.0 <= lat <= 53.0 and 69.0 <= lon <= 73.0):
            return None
        points.append((lat, lon))
    return points if len(points) >= 3 else None


def _build_filter_query(
    *,
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    map_polygon: str | None = None,
    new_since_hours: int | None = None,
    min_discount_pct: float | None = None,
    sort: str | None = None,
    page: int | None = None,
) -> str:
    params = _filter_params(
        districts=districts,
        rooms=rooms,
        max_price=max_price,
        min_year=min_year,
        max_year=max_year,
        residential_complex=residential_complex,
        developer=developer,
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
    districts: list[str] | None = None,
    rooms: int | None = None,
    max_price: float | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    residential_complex: str | None = None,
    developer: str | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    map_polygon: str | None = None,
    new_since_hours: int | None = None,
    min_discount_pct: float | None = None,
    sort: str | None = None,
    page: int | None = None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
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
