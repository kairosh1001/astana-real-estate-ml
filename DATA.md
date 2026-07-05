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
