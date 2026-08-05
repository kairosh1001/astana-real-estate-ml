from __future__ import annotations

import json
from pathlib import Path
import sys

from catboost import CatBoostRegressor
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline import FeatureConfig
from app.rental_feature_pipeline import build_rental_features
from app.rental_prediction_service import RentalPredictionService
from scripts.retrain_rental_models import (
    PRICE_LIMITS,
    deduplicate_training_snapshots,
    prediction_to_total_log,
    prepare_features,
    split_rows,
)


def validate_period(period: str) -> None:
    model_dir = ROOT / "models" / f"rent_{period}"
    metadata = json.loads((model_dir / "model_metadata.json").read_text(encoding="utf-8"))
    evaluation = json.loads((model_dir / "evaluation.json").read_text(encoding="utf-8"))
    raw_input = pd.read_csv(ROOT / "data" / f"rent_{period}_raw.csv")
    raw = raw_input[raw_input["rental_period"].eq(period)].copy()
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    raw["area_m2_structured"] = pd.to_numeric(raw["area_m2_structured"], errors="coerce")
    raw["rooms_structured"] = pd.to_numeric(raw["rooms_structured"], errors="coerce")
    raw["lat"] = pd.to_numeric(raw["lat"], errors="coerce")
    raw["lon"] = pd.to_numeric(raw["lon"], errors="coerce")
    low_price, high_price = PRICE_LIMITS[period]
    quality = (
        raw["price"].between(low_price, high_price)
        & raw["area_m2_structured"].between(10, 1_000)
        & raw["rooms_structured"].between(1, 20)
        & raw["lat"].between(49.5, 52.5)
        & raw["lon"].between(69.0, 73.5)
        & raw["url"].notna()
    )
    raw = deduplicate_training_snapshots(raw.loc[quality].copy())
    config = FeatureConfig(**metadata["feature_config"])
    prepared = build_rental_features(raw, config, include_target=True)
    finite = prepared[["rent_total_log", "area_m2"]].replace([np.inf, -np.inf], np.nan).notna().all(axis=1)
    finite &= prepared["area_m2"].gt(0)
    raw = raw.loc[finite].reset_index(drop=True)
    prepared = prepared.loc[finite].reset_index(drop=True)
    features = prepare_features(prepared, metadata)
    _, valid_idx, strategy = split_rows(raw, 0.2, 42)
    if strategy != evaluation["split_strategy"]:
        raise AssertionError(f"{period}: split strategy changed")

    model = CatBoostRegressor()
    model.load_model(str(model_dir / "catboost_q50_rent_total_log.cbm"))
    prediction_log = prediction_to_total_log(
        model.predict(features.iloc[valid_idx]),
        features.iloc[valid_idx]["area_m2"],
        metadata.get("target_mode", "total"),
    )
    actual_log = prepared.iloc[valid_idx]["rent_total_log"].to_numpy()
    recomputed_rmse = float(np.sqrt(np.mean(np.square(prediction_log - actual_log))))
    if not np.isclose(recomputed_rmse, evaluation["rmse_log"], rtol=1e-7, atol=1e-7):
        raise AssertionError(
            f"{period}: saved RMSE {evaluation['rmse_log']} != recomputed {recomputed_rmse}"
        )

    unsafe = [
        feature
        for feature in metadata["feature_columns"]
        if any(token in feature.casefold() for token in ("price", "rent_total", "target"))
    ]
    if unsafe:
        raise AssertionError(f"{period}: target-like features found: {unsafe}")

    service = RentalPredictionService(ROOT, period)
    for row in raw.head(10).to_dict(orient="records"):
        prediction = service.predict_raw_listing(row)
        values = [prediction.pred_rent_q10, prediction.pred_rent_q50, prediction.pred_rent_q90]
        if not all(np.isfinite(values)) or not (0 < values[0] <= values[1] <= values[2]):
            raise AssertionError(f"{period}: invalid served prediction interval for {prediction.url}")
    print(
        f"[OK] {period}: rows={len(raw)} valid={len(valid_idx)} "
        f"target={metadata.get('target_mode', 'total')} log_rmse={recomputed_rmse:.4f}"
    )


def main() -> None:
    for period in ("monthly", "daily"):
        validate_period(period)


if __name__ == "__main__":
    main()
