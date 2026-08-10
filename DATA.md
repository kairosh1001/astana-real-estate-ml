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

It targets 20,000 unique apartment-sale listings by default and writes them to
`data/almaty_sale_raw.csv`. Krisha limits a single result set to 1,000 pages, so
the collector uses non-overlapping room-count partitions and deduplicates the
combined output by canonical listing URL. Progress is checkpointed in
`data/almaty_sale_raw.state.json`. After an interruption, rerun the same command
to continue.

The default delay is deliberately conservative. A complete detail-page crawl
can take several hours. Keep the terminal and computer awake, do not open the
output CSV in Excel while it is running, and press `Ctrl+C` once if you need a
clean checkpointed stop.
