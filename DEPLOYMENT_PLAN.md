# Deployment Plan

## Goal

Build a low-cost web service for Astana apartment valuation using the trained CatBoost models.

The service should:

- Check Astana apartment listings from krisha.kz.
- Return the most undervalued recently active listings.
- Predict a model price for an individual Krisha listing URL.
- Provide an easily accessible website.
- Support basic drift/performance monitoring.
- Allow periodic model retraining.
- Stay simple and cheap to operate.

## Current Project State

The repository currently contains:

- `scrape.py`: working scraper used to collect Krisha listing data.
- Raw scraped datasets: local intermediate artifacts used to build `df_check.csv`; excluded from the polished public repository state.
- `dataset.ipynb`: notebook containing cleaning, feature engineering, training, evaluation, and model export.
- `df_check.csv`: final model-ready dataset used for CatBoost work.
- `model_metadata.json`: final model feature contract.
- `models/`: trained CatBoost model files.

Current model files:

- `models/catboost_q10_price_per_m2_log.cbm`
- `models/catboost_q50_price_per_m2_log.cbm`
- `models/catboost_q90_price_per_m2_log.cbm`

## Final Model Contract

`model_metadata.json` is the source of truth for inference.

Required features:

```text
ceiling_height
year_of_construction
district
residential_complex
furnished
apartment_condition
building_type
rooms
current_floor
total_floors
area_m2
h3_res_7
h3_res_8
h3_res_9
dist_to_nearest_mall_km
dist_to_nearest_park_km
dist_to_nearest_lrt_km
dist_to_baiterek_km
dist_to_botgarden_km
dist_to_mangilikel_km
dist_to_khanshatyr_km
dist_to_expo_km
floor_ratio
```

Categorical features:

```text
district
residential_complex
furnished
apartment_condition
building_type
h3_res_7
h3_res_8
h3_res_9
```

Inference rules:

- Ensure all required columns exist.
- Reorder columns to match `feature_columns`.
- Cast categorical columns to string.
- Fill missing categorical values with `"missing"`.
- Keep numeric features numeric.
- The model target is `price_per_m2_log`.
- Convert predictions back with `exp(pred_log)`.

Important caution:

- Some training-derived values were used only for intermediate notebook columns that were later dropped before `df_check`.
- Do not carry unnecessary intermediate features into production inference.
- Security-related features are the main example of dropped intermediate logic.

## Architecture

Use a Dockerized FastAPI app. Do not use Kubernetes for v1.

Components:

- FastAPI backend.
- FastAPI Jinja2 templates for the first website version.
- CatBoost models loaded once at startup.
- SQLite database for raw listings, cached predictions, listing status, and undervalued rankings.
- Scheduled scraping jobs.
- Docker Compose for local and VPS deployment.
- Simple admin token for API/cron refresh operations.
- Admin login form for internal browser pages, using `ADMIN_TOKEN` as the password.

## Deployment Direction

Do not buy hosting until the app works locally with Docker.

Preferred v1 deployment:

- Hetzner CX23 VPS if final checkout price is acceptable.
- Docker Compose.
- No Coolify for v1 unless Docker Compose becomes painful.
- Caddy or another simple reverse proxy for HTTPS when a domain is attached.

Budget:

- Target budget is about `$5/month`.
- Exact VPS cost may vary by IPv4 pricing, VAT, region, and provider.

## Scraping Strategy

Bulk scraping should not run inside normal user web requests.

Daily refresh:

- Run at a low-traffic time, such as 03:00 server time.
- Scrape pages 1-50 of Astana apartment sale listings.
- Upsert all scraped listings into SQLite.
- Recompute predictions and undervaluation metrics for seen listings.

Weekly deeper refresh:

- Enabled for v1.
- Start with 200 pages.
- Tune after observing runtime, rate limits, and data usefulness.

Listing status:

- New listing URL: insert into database.
- Existing listing URL: update fields, prediction outputs, and `last_seen_at`.
- Not seen in current refresh: keep in database but increment missed-refresh logic.
- Mark stale after 3 missed daily refreshes.
- Hide stale listings by default or show them with a clear stale warning.

Coverage note:

- The daily undervalued list initially covers the latest daily scrape window plus recently active cached listings.
- It is not guaranteed to cover every active Astana listing unless the scrape reaches all result pages.
- Re-scraping pages 1-50 daily is useful because it detects new top-page listings, price changes, and removals.

## Single-Link Prediction

Flow:

1. User submits a Krisha listing URL.
2. Validate the URL belongs to krisha.kz.
3. Scrape that one listing page.
4. Extract raw fields.
5. Apply the same final feature pipeline used to create `df_check.csv`.
6. Build a one-row dataframe matching `model_metadata.json`.
7. Run q10, q50, and q90 models.
8. Return predicted price per square meter and undervaluation metrics.

Target response time:

- Up to 10 seconds for v1.

Expected always-available raw fields from project context:

- URL.
- Title.
- Price.
- Latitude.
- Longitude.
- Construction year.
- City/district.
- Area.

Other raw fields may need fallback handling.

## Undervaluation Metrics

Suggested stored metrics:

- `listed_price_per_m2`
- `pred_price_per_m2_q10`
- `pred_price_per_m2_q50`
- `pred_price_per_m2_q90`
- `discount_vs_asking_pct_conservative = (pred_q10 - listed_price_per_m2) / listed_price_per_m2`
- `discount_vs_asking_pct_median = (pred_q50 - listed_price_per_m2) / listed_price_per_m2`
- `interval_width_pct = (pred_q90 - pred_q10) / pred_q50`

Initial ranking rule:

- Show listings where conservative discount versus asking is positive.
- Prefer `discount_vs_asking_pct_conservative > 0.05`.
- Optionally filter uncertain estimates with `interval_width_pct < 0.40`.

## Training-Derived Values To Audit

The feature pipeline must preserve only values needed to produce final model features.

Known final-feature-relevant values:

- Raw building-type missing values were filled from a dataset mode.
- Ceiling height was cleaned to numeric and missing values were filled with a dataset mean.
- `current_floor` and `total_floors` missing values were filled with dataset medians.
- District values were restricted to an allowed list.
- Missing district values were filled from a residential-complex-to-district mapping learned from the dataset.
- Fixed landmark coordinates were used to create distance features.
- Missing values for `furnished`, `apartment_condition`, and `residential_complex` were filled with fixed labels.

Initial constants calculated from the joined and deduplicated raw CSVs:

- Deduplicated raw rows: `16075`.
- Raw building-type mode: monolithic.
- Mean ceiling height: approximately `2.89747`.
- Median `current_floor`: `6`.
- Median `total_floors`: `10`.

Intermediate values that are probably not final inference requirements:

- Bathroom mode, because bathroom fields are not in `model_metadata.json`.
- Security mode and derived security features, because security fields were dropped before final `df_check`.
- Parking defaults, because parking is not in `model_metadata.json`.

These assumptions must be rechecked while extracting `dataset.ipynb` into production code.

## API

Initial endpoints:

- `GET /health`
- `POST /predict-by-link`
- `GET /undervalued`

Later endpoints:

- `POST /refresh-listings`
  - Admin-only.
- `GET /metrics-summary`

Implemented admin refresh endpoint:

- `POST /refresh-listings`
- Requires `X-Admin-Token` header matching `ADMIN_TOKEN`.
- Starts the refresh job in the background.
- Intended for manual/admin use; scheduled production refreshes should still use cron.

## Website

Initial pages:

- Home page with one listing URL input.
- Prediction result page.
- Undervalued listings page.
- Service status page.
- Refresh history page.
- Internal service status, refresh history, and admin refresh pages protected by the admin login session.

The undervalued page should include:

- Original listing link.
- Price.
- Area.
- District.
- Residential complex.
- Prediction interval.
- Undervaluation metrics.
- Last seen timestamp.
- Stale status if shown.

The refresh history page should include:

- Refresh type.
- Refresh status.
- Page range.
- URLs found.
- Listings processed.
- Listing failures.
- Start and finish timestamps.
- Error message when present.

The admin refresh page should:

- Require admin login with `ADMIN_TOKEN` as the password.
- Allow a small manual refresh from the browser.
- Let the admin choose refresh type, page range, request delay, and listing cap.
- Link back to refresh history.

## Docker Plan

Local development:

```bash
docker compose up --build
```

Expected local URL:

```text
http://localhost:8000
```

Manual refresh smoke test:

```bash
docker compose --profile tools run --rm refresh
```

Local Docker validation status:

- `docker compose build` passed on Windows 11 with Docker Desktop.
- `docker compose up -d app` passed.
- Containerized `/health` and `/predict-by-link` passed.
- Compose refresh smoke test processed 3 live listings successfully.
- Rebuilt after adding admin refresh endpoint.
- Containerized `/health` returned all three loaded models and 23 feature columns.
- Containerized admin refresh auth check passed:
  - Wrong `X-Admin-Token` returned 403.
  - Correct `X-Admin-Token` reached request validation.

Local Python equivalents:

```bash
.venv\Scripts\python.exe scripts\check_deployment.py
.venv\Scripts\python.exe scripts\validate_feature_pipeline.py
.venv\Scripts\python.exe scripts\validate_models.py --rows 200000
.venv\Scripts\python.exe scripts\refresh_listings.py --pages 1 --max-listings 3 --min-delay 0 --max-delay 0
```

Full pre-deploy validation:

```bash
.venv\Scripts\python.exe scripts\check_deployment.py --full
```

VPS deployment:

- Run app with Docker Compose.
- Persist SQLite and raw scrape outputs in mounted volumes.
- Schedule daily and weekly refresh jobs with cron or a separate Compose service.
- Add Caddy/HTTPS after the app works by IP.

## Implementation Milestones

### Milestone 1: Local Model Inference

- Load all three CatBoost models.
- Load `model_metadata.json`.
- Predict on rows from `df_check.csv`.
- Verify predictions are sane.

### Milestone 2: Feature Pipeline Extraction

- Move final preprocessing and feature engineering from `dataset.ipynb` into Python modules.
- Save required constants into code or `feature_config.json`.
- Confirm the pipeline can recreate model-ready features.

### Milestone 3: Single Listing Prediction

- Adapt scraper for one URL.
- Convert one scraped listing into model-ready features.
- Return q10/q50/q90 predictions.

### Milestone 4: FastAPI App

- Add API endpoints.
- Add minimal website pages.
- Load models at startup.

### Milestone 5: SQLite and Refresh Jobs

- Store raw scraped rows and predictions.
- Track `first_seen_at`, `last_seen_at`, `last_checked_at`, and status.
- Build daily and weekly refresh scripts.

### Milestone 6: Docker

- Add `Dockerfile`.
- Add `docker-compose.yml`.
- Test locally with Docker Desktop.

### Milestone 7: Deployment

- Buy VPS only after local Docker works.
- Deploy app with Docker Compose.
- Configure scheduled refresh.

### Milestone 8: Monitoring and Retraining

- Add logging and drift summaries.
- Add retraining script.
- Decide later whether retraining is manual or scheduled.

## Open Questions

- Which exact VPS provider should be used for v1?
- What maximum runtime should be allowed for daily and weekly scraping jobs?
- Should stale listings be hidden by default or shown with a stale warning?
