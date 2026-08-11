# Data Notes

This repository keeps `df_check.csv` as a model-ready dataset snapshot so the model-serving and validation scripts are reproducible.

Raw scrape snapshots are intentionally excluded from the polished public repository state:

- they are intermediate artifacts, not required for serving the app;
- they can be regenerated with `scrape.py`;
- keeping only the model-ready snapshot makes the repository easier to review.

## Reproducing The Dataset

The original workflow was:

1. Scrape apartment listings from Krisha.kz with `scrape.py`.
2. Clean, deduplicate, preprocess, and engineer features in `dataset.ipynb`.
3. Save the final model-ready frame as `df_check.csv`.
4. Train or validate CatBoost models using `df_check.csv` and `model_metadata.json`.

For a fresh retraining cycle, regenerate `df_check.csv` first, then run:

```powershell
.\.venv\Scripts\python.exe scripts\retrain_models.py
```

The deployed application does not train models online. It loads the committed CatBoost files from `models/` and updates listing predictions during refresh jobs.

## Collecting Almaty Listings

Run the resumable full collector from PowerShell at the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\scrape_almaty.py
```

It resumes the existing `data/almaty_sale_raw.csv` and works toward balanced
room-count quotas: 10,000 one-room, 12,000 two-room, 10,000 three-room, 6,000
four-room, and 3,000 five-room-or-larger listings. Existing listings count
toward the quotas, so the current 20,000-row file is not downloaded again.

Krisha limits a single result set to 1,000 pages, so the collector uses
non-overlapping room-count partitions and deduplicates the combined output by
canonical listing URL. Progress is checkpointed in
`data/almaty_sale_raw.state.json`. After an interruption, rerun the same command
to continue. A partition is accepted when its quota is reached or its available
inventory is exhausted.

To scale the same room mix to a different aggregate target, use for example:

```powershell
.\.venv\Scripts\python.exe scripts\scrape_almaty.py --target 50000
```

Individual quotas can also be overridden, for example
`--rooms-3-target 12000 --rooms-4-target 8000`. Increasing a quota later resumes
from the saved search page; it does not discard previously collected rows.

The default delay is deliberately conservative. A complete detail-page crawl
can take several hours. Keep the terminal and computer awake, do not open the
output CSV in Excel while it is running, and press `Ctrl+C` once if you need a
clean checkpointed stop.

## Universal Astana + Almaty Features (v2)

The v2 pipeline is isolated from the current Astana production model. Refresh
the shared OpenStreetMap catalog with:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_poi_catalog.py
```

After the Almaty scrape has completed, build one deduplicated model-ready
dataset with:

```powershell
.\.venv\Scripts\python.exe scripts\build_universal_dataset.py `
  --input krisha_data_raw_orig.csv `
  --input krisha_data_raw.csv `
  --input data\almaty_sale_raw.csv
```

The generated `data/universal_training_v2.csv` contains city-aware location
categories, H3 cells, distance to each city center, and reusable OSM proximity
and density features for parks, schools, kindergartens, groceries, malls,
healthcare, transit, and universities. It intentionally contains no
Astana-only landmark features. Model metadata and the ЖК-to-district mapping
are written under the ignored `models_candidate/` directory.
The dataset also retains `listing_url`, `scraped_at`, and `scrape_partition` as
non-model audit columns. The training notebooks assign a deterministic split
from property-like attributes so likely duplicates/reposts cannot cross from
training into validation or test, without leaking the audit fields into CatBoost.

OSM proximity values currently use great-circle distance to the representative
point returned by Overpass. They are deterministic and inexpensive enough for
model training and online prediction, but they are not walking-route distances;
large park polygons can therefore be approximated by their representative
point. The catalog timestamp and SHA-256 fingerprint are recorded in model
metadata so training and serving can use the same frozen POI snapshot.

## Training Notebooks (v2)

Open either notebook from the repository root after starting `jupyter notebook`:

- `notebooks/universal_astana_almaty_model.ipynb` trains one city-aware model
  on both available cities;
- `notebooks/almaty_model.ipynb` trains an Almaty-only model on the identical
  deterministic Almaty split for a fair comparison.

Both notebooks train q10, q50, and q90 CatBoost candidates, show held-out
metrics and diagnostics, and save only under the ignored `models_candidate/`
directory. They do not overwrite the current production model. Finish every
Almaty room-count partition and rebuild the shared dataset before considering
either candidate for production.
