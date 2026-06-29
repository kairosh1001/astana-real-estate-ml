from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.database import connect, fetch_refresh_runs, fetch_undervalued, init_db
from app.prediction_service import PredictionService
from app.refresh_service import run_refresh


ROOT = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1]))
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

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
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "error": None, "url": ""},
    )


@app.get("/predict", response_class=HTMLResponse)
def predict_page(request: Request, url: str) -> HTMLResponse:
    try:
        prediction = prediction_service.predict_by_url(url)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"request": request, "error": str(exc), "url": url},
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
            {"request": request, "error": str(exc), "url": url},
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
def undervalued(limit: int = 50, include_stale: bool = False) -> dict:
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            limit=limit,
            include_stale=include_stale,
        )
    return {
        "items": items,
    }


@app.get("/undervalued-page", response_class=HTMLResponse)
def undervalued_page(
    request: Request,
    limit: int = 50,
    include_stale: bool = False,
) -> HTMLResponse:
    with connect(DB_PATH) as db_connection:
        items = fetch_undervalued(
            db_connection,
            limit=limit,
            include_stale=include_stale,
        )
    return templates.TemplateResponse(
        request,
        "undervalued.html",
        {"request": request, "items": items},
    )


@app.get("/refresh-runs")
def refresh_runs(limit: int = 20) -> dict:
    with connect(DB_PATH) as db_connection:
        runs = fetch_refresh_runs(db_connection, limit=limit)
    return {
        "items": runs,
    }


@app.get("/refresh-runs-page", response_class=HTMLResponse)
def refresh_runs_page(request: Request, limit: int = 20) -> HTMLResponse:
    with connect(DB_PATH) as db_connection:
        runs = fetch_refresh_runs(db_connection, limit=limit)
    return templates.TemplateResponse(
        request,
        "refresh_runs.html",
        {"request": request, "items": runs},
    )


@app.post("/refresh-listings")
def refresh_listings(
    payload: RefreshRequest,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    admin_token = os.getenv("ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN не настроен.",
        )
    if x_admin_token != admin_token:
        raise HTTPException(status_code=403, detail="Неверный админ-токен.")

    if payload.kind not in {"manual", "daily", "weekly"}:
        raise HTTPException(status_code=400, detail="Неверный тип обновления.")
    if payload.pages < 1 or payload.start_page < 1:
        raise HTTPException(
            status_code=400,
            detail="Количество страниц и стартовая страница должны быть положительными.",
        )
    if payload.max_listings < 0:
        raise HTTPException(
            status_code=400,
            detail="max_listings не может быть отрицательным.",
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
