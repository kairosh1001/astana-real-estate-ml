from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.model_selection import GroupShuffleSplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rental_feature_pipeline import (
    build_rental_feature_config,
    build_rental_features,
    metadata_for_period,
)

MODEL_NAMES = {
    "q10": ("Quantile:alpha=0.1", "catboost_q10_rent_total_log.cbm"),
    "q50": ("Quantile:alpha=0.5", "catboost_q50_rent_total_log.cbm"),
    "q90": ("Quantile:alpha=0.9", "catboost_q90_rent_total_log.cbm"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train monthly or daily rental models.")
    parser.add_argument("--period", choices=["monthly", "daily"], required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=600)
    parser.add_argument("--depth", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument(
        "--min-rows",
        type=int,
        default=100,
        help="Safety gate; lower only for an explicitly labelled pilot model.",
    )
    return parser.parse_args()


def prepare_features(frame: pd.DataFrame, metadata: dict) -> pd.DataFrame:
    features = frame.loc[:, metadata["feature_columns"]].copy()
    categorical = set(metadata["categorical_features"])
    for column in features:
        if column in categorical:
            features[column] = features[column].astype("string").fillna("missing")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")
    return features


def split_rows(raw: pd.DataFrame, test_size: float, seed: int) -> tuple[np.ndarray, np.ndarray, str]:
    scraped = pd.to_datetime(raw.get("scraped_at"), errors="coerce", utc=True)
    if scraped.notna().any() and scraped.nunique() > 1:
        cutoff = scraped.quantile(1 - test_size)
        valid_mask = scraped >= cutoff
        valid_groups = set(raw.loc[valid_mask, "listing_id"].astype(str))
        train_mask = ~raw["listing_id"].astype(str).isin(valid_groups)
        if train_mask.sum() >= 50 and valid_mask.sum() >= 20:
            return np.flatnonzero(train_mask), np.flatnonzero(valid_mask), "chronological_group_holdout"

    groups = raw.get("listing_id", raw["url"]).astype(str)
    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_index, valid_index = next(splitter.split(raw, groups=groups))
    return train_index, valid_index, "group_shuffle_holdout"


def main() -> None:
    args = parse_args()
    dataset = args.dataset or ROOT / "data" / f"rent_{args.period}_raw.csv"
    output_dir = args.output_dir or ROOT / "models" / f"rent_{args.period}"
    raw = pd.read_csv(dataset)
    raw = raw[raw.get("rental_period", "") == args.period].copy()
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    snapshot_key = ["url", "scraped_at"] if "scraped_at" in raw.columns else ["url"]
    raw = raw[(raw["price"] > 0) & raw["url"].notna()].drop_duplicates(snapshot_key).reset_index(drop=True)
    if len(raw) < args.min_rows:
        raise ValueError(
            f"Need at least {args.min_rows} valid {args.period} listings, found {len(raw)}"
        )

    config = build_rental_feature_config(raw)
    prepared = build_rental_features(raw, config, include_target=True)
    valid_target = prepared["rent_total_log"].replace([np.inf, -np.inf], np.nan).notna()
    raw = raw.loc[valid_target].reset_index(drop=True)
    prepared = prepared.loc[valid_target].reset_index(drop=True)
    metadata = metadata_for_period(args.period)
    features = prepare_features(prepared, metadata)
    target = prepared[metadata["target"]]
    train_idx, valid_idx, strategy = split_rows(raw, args.test_size, args.random_seed)

    train_x, valid_x = features.iloc[train_idx], features.iloc[valid_idx]
    train_y, valid_y = target.iloc[train_idx], target.iloc[valid_idx]
    cat_indices = [train_x.columns.get_loc(c) for c in metadata["categorical_features"]]
    predictions: dict[str, np.ndarray] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for quantile, (loss, filename) in MODEL_NAMES.items():
        print(f"[INFO] Training {args.period} {quantile}")
        model = CatBoostRegressor(
            loss_function=loss,
            iterations=args.iterations,
            depth=args.depth,
            learning_rate=args.learning_rate,
            random_seed=args.random_seed,
            verbose=100,
            allow_writing_files=False,
        )
        model.fit(train_x, train_y, cat_features=cat_indices, eval_set=(valid_x, valid_y), use_best_model=True)
        model.save_model(str(output_dir / filename))
        predictions[quantile] = np.exp(model.predict(valid_x))

    actual = np.exp(valid_y.to_numpy())
    median_by_rooms = raw.iloc[train_idx].assign(target=np.exp(train_y.to_numpy())).groupby("rooms_structured")["target"].median()
    global_median = float(np.median(np.exp(train_y.to_numpy())))
    baseline = raw.iloc[valid_idx]["rooms_structured"].map(median_by_rooms).fillna(global_median).to_numpy()
    q50 = predictions["q50"]
    unique_listings = int(raw["listing_id"].astype(str).nunique())
    production_ready = bool(
        unique_listings >= 500 and strategy == "chronological_group_holdout"
    )
    metrics = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": args.period,
        "rows": len(raw),
        "unique_listings": unique_listings,
        "train_rows": len(train_idx),
        "validation_rows": len(valid_idx),
        "split_strategy": strategy,
        "mae_kzt": float(np.mean(np.abs(q50 - actual))),
        "median_ape": float(np.median(np.abs(q50 - actual) / actual)),
        "baseline_mae_kzt": float(np.mean(np.abs(baseline - actual))),
        "q10_q90_coverage": float(np.mean((actual >= predictions["q10"]) & (actual <= predictions["q90"]))),
        "production_ready": production_ready,
        "readiness_notes": (
            []
            if production_ready
            else [
                "Pilot only: fewer than 500 unique listings or no chronological holdout.",
                "Collect repeated snapshots before using the model for investment decisions.",
            ]
        ),
    }
    metadata["feature_config"] = {
        "building_type_fill": config.building_type_fill,
        "ceiling_height_fill": config.ceiling_height_fill,
        "current_floor_fill": config.current_floor_fill,
        "total_floors_fill": config.total_floors_fill,
        "furnished_fill": config.furnished_fill,
        "condition_fill": config.condition_fill,
        "residential_complex_fill": config.residential_complex_fill,
        "complex_to_district": config.complex_to_district,
    }
    (output_dir / "model_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "evaluation.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
