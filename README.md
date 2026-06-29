# Krisha Listing Valuation

CatBoost-based valuation service for Astana apartment listings from krisha.kz.

## Local Setup

Create a Python 3.11 virtual environment and install dependencies:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

On Windows PowerShell, run scripts with:

```powershell
.venv\Scripts\python.exe scripts\check_deployment.py
.venv\Scripts\python.exe scripts\check_ui.py
.venv\Scripts\python.exe scripts\validate_feature_pipeline.py
.venv\Scripts\python.exe scripts\validate_models.py --rows 200000
```

For the slower full validation:

```powershell
.venv\Scripts\python.exe scripts\check_deployment.py --full
```

## Run The App

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

Useful pages:

```text
http://127.0.0.1:8000/undervalued-page
http://127.0.0.1:8000/refresh-runs-page
```

## Refresh Listings

Small smoke test:

```powershell
.venv\Scripts\python.exe scripts\refresh_listings.py --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

Planned daily refresh:

```powershell
.venv\Scripts\python.exe scripts\refresh_listings.py --kind daily --pages 50
```

Planned weekly refresh:

```powershell
.venv\Scripts\python.exe scripts\refresh_listings.py --kind weekly --pages 200
```

Admin-only web refresh, after starting the app and setting `ADMIN_TOKEN`:

```powershell
$env:ADMIN_TOKEN="change-me"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another shell:

```powershell
curl.exe -X POST http://127.0.0.1:8000/refresh-listings `
  -H "Content-Type: application/json" `
  -H "X-Admin-Token: change-me" `
  -d "{\"kind\":\"manual\",\"pages\":1,\"max_listings\":3,\"min_delay\":0,\"max_delay\":0}"
```

## Docker

Docker Desktop must be running before using Docker commands.

```bash
docker compose build
docker compose up
```

For admin refresh calls in Docker, copy `.env.example` to `.env` and set a real `ADMIN_TOKEN`.

Manual refresh through Compose:

```bash
docker compose --profile tools run --rm refresh
```

See `deploy/README.md` for VPS, cron, and HTTPS notes.

## Notes

- The final feature contract is in `model_metadata.json`.
- Trained CatBoost models live in `models/`.
- Runtime SQLite files live in `data/` and are ignored by Git.
- See `DEPLOYMENT_PLAN.md` for architecture and deployment decisions.
