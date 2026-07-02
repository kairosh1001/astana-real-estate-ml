from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.database import (
    DISTRICT_OPTIONS,
    connect,
    count_undervalued,
    fetch_refresh_runs,
    fetch_status_summary,
    fetch_undervalued,
    init_db,
    valid_district_slug,
)
from app.prediction_service import PredictionService
from app.refresh_service import run_refresh


ROOT = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1]))
ASTANA_TZ = timezone(timedelta(hours=5), name="Asia/Astana")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
templates.env.filters["astana_time"] = lambda value: format_astana_time(value)
ADMIN_SESSION_COOKIE = "krisha_admin_session"
ADMIN_SESSION_TTL_SECONDS = 60 * 60 * 12
HOME_UNDERVALUED_LIMIT = 10
UNDERVALUED_PAGE_SIZE = 10

app = FastAPI(title="Оценка объявлений Krisha")
prediction_service = PredictionService(ROOT)
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "krisha.sqlite3"))
with connect(DB_PATH) as db_connection:
    init_db(db_connection)


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
        total_undervalued = count_undervalued(db_connection, include_stale=False)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "error": None,
            "url": "",
            "items": preview_items,
            "total_undervalued": total_undervalued,
            "district_options": DISTRICT_OPTIONS,
            "selected_district": None,
            "start_rank": 1,
            "is_preview": True,
        },
    )


@app.get("/predict", response_class=HTMLResponse)
def predict_page(request: Request, url: str) -> HTMLResponse:
    try:
        prediction = prediction_service.predict_by_url(url)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "error": str(exc),
                "url": url,
                "items": [],
                "total_undervalued": 0,
                "district_options": DISTRICT_OPTIONS,
                "selected_district": None,
                "start_rank": 1,
                "is_preview": True,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "result.html",
        {"request": request, "prediction": prediction},
    )


@app.post("/predict", response_class=HTMLResponse)
def predict_form(request: Request, url: str = Form(...)) -> HTMLResponse:
    try:
        prediction = prediction_service.predict_by_url(url)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "error": str(exc),
                "url": url,
                "items": [],
                "total_undervalued": 0,
                "district_options": DISTRICT_OPTIONS,
                "selected_district": None,
                "start_rank": 1,
                "is_preview": True,
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "result.html",
        {"request": request, "prediction": prediction},
    )


@app.post("/predict-by-link")
def predict_by_link(payload: PredictByLinkRequest) -> dict:
    try:
        prediction = prediction_service.predict_by_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return asdict(prediction)


@app.get("/undervalued")
def undervalued(
    limit: int = 50,
    page: int = 1,
    district: str | None = None,
    include_stale: bool = False,
) -> dict:
    selected_district = valid_district_slug(district)
    safe_limit = min(max(limit, 1), 100)
    safe_page = max(page, 1)
    offset = (safe_page - 1) * safe_limit
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            limit=safe_limit,
            offset=offset,
            district=selected_district,
            include_stale=include_stale,
        )
        total = count_undervalued(
            db_connection,
            district=selected_district,
            include_stale=include_stale,
        )
    return {
        "items": items,
        "total": total,
        "page": safe_page,
        "limit": safe_limit,
        "district": selected_district,
    }


@app.get("/undervalued-page", response_class=HTMLResponse)
def undervalued_page(
    request: Request,
    page: int = 1,
    district: str | None = None,
    include_stale: bool = False,
) -> HTMLResponse:
    selected_district = valid_district_slug(district)
    safe_page = max(page, 1)
    offset = (safe_page - 1) * UNDERVALUED_PAGE_SIZE
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            limit=UNDERVALUED_PAGE_SIZE,
            offset=offset,
            district=selected_district,
            include_stale=include_stale,
        )
        total = count_undervalued(
            db_connection,
            district=selected_district,
            include_stale=include_stale,
        )
    return templates.TemplateResponse(
        request,
        "undervalued.html",
        {
            "request": request,
            "items": items,
            "district_options": DISTRICT_OPTIONS,
            "selected_district": selected_district,
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

    return templates.TemplateResponse(
        request,
        "admin_refresh.html",
        {
            "request": request,
            "error": None,
            "message": None,
            "form": _default_refresh_form(),
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
        return templates.TemplateResponse(
            request,
            "admin_refresh.html",
            {"request": request, "error": str(exc), "message": None, "form": form},
            status_code=400,
        )

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
        },
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
