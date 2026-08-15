from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from app.feature_pipeline_v2 import PoiCatalog, build_universal_feature_config
from app.rental_feature_pipeline import (
    RENTAL_CATEGORICAL_FEATURES,
    RENTAL_FEATURE_COLUMNS,
    RENT_PER_M2_TARGET,
    RENT_TOTAL_TARGET,
    build_rental_features,
    clean_rent_price,
    rental_model_metadata,
)
from app.rental_model_service import RENTAL_MODEL_FILENAMES


RANDOM_SEED = 42


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train independent monthly-rent q10/q50/q90 models."
    )
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "rent_monthly_raw.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "models" / "rent_monthly_v2")
    parser.add_argument("--iterations", type=int, default=900)
    return parser.parse_args()


def prepare_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.loc[:, RENTAL_FEATURE_COLUMNS].copy()
    categorical = set(RENTAL_CATEGORICAL_FEATURES)
    for column in result:
        if column in categorical:
            result[column] = result[column].astype("string").fillna("missing")
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def split_name(row: pd.Series) -> str:
    signature = "|".join(
        str(row.get(column, ""))
        for column in (
            "city", "h3_res_9", "city_residential_complex", "rooms",
            "area_m2", "current_floor", "total_floors",
        )
    )
    bucket = int(hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if bucket < 70 else "validation" if bucket < 85 else "test"


def metrics(actual_log: np.ndarray, predicted_log: np.ndarray) -> dict:
    actual = np.exp(actual_log)
    predicted = np.exp(predicted_log)
    error = predicted - actual
    return {
        "rows": int(len(actual)),
        "log_rmse": float(np.sqrt(np.mean((predicted_log - actual_log) ** 2))),
        "rmse_kzt_month": float(np.sqrt(np.mean(error ** 2))),
        "mae_kzt_month": float(np.mean(np.abs(error))),
        "median_absolute_percentage_error": float(
            np.median(np.abs(error) / np.maximum(actual, 1))
        ),
    }


def segment_metrics(
    frame: pd.DataFrame,
    actual_log: np.ndarray,
    predicted_log: np.ndarray,
) -> dict:
    result: dict[str, dict] = {}
    for column in ("city", "rooms", "furnished"):
        groups: dict[str, dict] = {}
        values = frame[column].astype("string").fillna("missing")
        for value in sorted(values.unique()):
            mask = values.eq(value).to_numpy()
            if int(mask.sum()) >= 20:
                groups[str(value)] = metrics(actual_log[mask], predicted_log[mask])
        result[column] = groups
    return result


def train_one(
    train_x: pd.DataFrame,
    train_y: np.ndarray,
    validation_x: pd.DataFrame,
    validation_y: np.ndarray,
    *,
    loss: str,
    iterations: int,
) -> CatBoostRegressor:
    model = CatBoostRegressor(
        loss_function=loss,
        eval_metric=loss,
        iterations=iterations,
        depth=7,
        learning_rate=0.04,
        l2_leaf_reg=7,
        random_seed=RANDOM_SEED,
        allow_writing_files=False,
        verbose=False,
        thread_count=-1,
    )
    cat_indices = [
        RENTAL_FEATURE_COLUMNS.index(column)
        for column in RENTAL_CATEGORICAL_FEATURES
    ]
    model.fit(
        train_x,
        train_y,
        cat_features=cat_indices,
        eval_set=(validation_x, validation_y),
        early_stopping_rounds=100,
        use_best_model=True,
    )
    return model


def main() -> None:
    args = parse_args()
    raw = pd.read_csv(args.input, low_memory=False)
    raw["price"] = raw["price"].map(clean_rent_price)
    if "rental_period" in raw:
        raw = raw.loc[raw["rental_period"].fillna("monthly").eq("monthly")]
    raw = raw.drop_duplicates(subset="url", keep="last").reset_index(drop=True)

    catalog = PoiCatalog.load(ROOT / "app" / "data" / "kazakhstan_pois.json")
    config = build_universal_feature_config(raw)
    frame = build_rental_features(raw, config, catalog, include_target=True)
    valid = (
        frame["city"].isin(["astana", "almaty"])
        & frame["rooms"].between(1, 10)
        & frame["area_m2"].between(12, 500)
        & raw["price"].between(30_000, 5_000_000)
        & frame[RENT_TOTAL_TARGET].notna()
    )
    frame = frame.loc[valid].reset_index(drop=True)
    raw = raw.loc[valid].reset_index(drop=True)
    if len(frame) < 500:
        raise SystemExit(f"Only {len(frame)} valid rental rows; at least 500 are required.")
    frame["split"] = frame.apply(split_name, axis=1)
    parts = {name: frame.loc[frame["split"].eq(name)] for name in ("train", "validation", "test")}
    if any(len(part) < 50 for part in parts.values()):
        raise SystemExit({name: len(part) for name, part in parts.items()})

    matrices = {name: prepare_matrix(part) for name, part in parts.items()}
    candidates: list[dict] = []
    candidate_models: dict[str, CatBoostRegressor] = {}
    for mode, target in (("total", RENT_TOTAL_TARGET), ("per_m2", RENT_PER_M2_TARGET)):
        y_train = parts["train"][target].to_numpy(float)
        y_validation = parts["validation"][target].to_numpy(float)
        model = train_one(
            matrices["train"], y_train, matrices["validation"], y_validation,
            loss="RMSE", iterations=args.iterations,
        )
        predicted = model.predict(matrices["validation"])
        if mode == "per_m2":
            area_log = np.log(parts["validation"]["area_m2"].to_numpy(float))
            predicted = predicted + area_log
            actual = parts["validation"][RENT_TOTAL_TARGET].to_numpy(float)
        else:
            actual = y_validation
        result = {
            "target_mode": mode,
            "best_iteration": int(model.get_best_iteration()),
            "validation": metrics(actual, predicted),
        }
        candidates.append(result)
        candidate_models[mode] = model
        print(mode, json.dumps(result, ensure_ascii=False))

    winner = min(candidates, key=lambda item: item["validation"]["log_rmse"])
    mode = winner["target_mode"]
    target = RENT_TOTAL_TARGET if mode == "total" else RENT_PER_M2_TARGET
    y = {name: parts[name][target].to_numpy(float) for name in parts}
    models = {
        "q10": train_one(matrices["train"], y["train"], matrices["validation"], y["validation"], loss="Quantile:alpha=0.1", iterations=args.iterations),
        "q50": candidate_models[mode],
        "q90": train_one(matrices["train"], y["train"], matrices["validation"], y["validation"], loss="Quantile:alpha=0.9", iterations=args.iterations),
    }

    def to_total_log(values: np.ndarray, part: pd.DataFrame) -> np.ndarray:
        return values + np.log(part["area_m2"].to_numpy(float)) if mode == "per_m2" else values

    actual_validation = parts["validation"][RENT_TOTAL_TARGET].to_numpy(float)
    raw_validation = {
        quantile: to_total_log(model.predict(matrices["validation"]), parts["validation"])
        for quantile, model in models.items()
    }
    offsets = {
        "q10": float(np.quantile(actual_validation - raw_validation["q10"], 0.10)),
        "q50": float(np.median(actual_validation - raw_validation["q50"])),
        "q90": float(np.quantile(actual_validation - raw_validation["q90"], 0.90)),
    }
    actual_test = parts["test"][RENT_TOTAL_TARGET].to_numpy(float)
    test_predictions = {
        quantile: to_total_log(model.predict(matrices["test"]), parts["test"]) + offsets[quantile]
        for quantile, model in models.items()
    }
    coverage = float(np.mean((actual_test >= test_predictions["q10"]) & (actual_test <= test_predictions["q90"])))

    baseline_map = (
        parts["train"].assign(target_total=parts["train"][RENT_TOTAL_TARGET])
        .groupby(["city", "rooms"])["target_total"].median()
    )
    fallback = float(parts["train"][RENT_TOTAL_TARGET].median())
    baseline = np.asarray([
        baseline_map.get((row.city, row.rooms), fallback)
        for row in parts["test"].itertuples()
    ])
    city_counts = {str(key): int(value) for key, value in frame["city"].value_counts().items()}
    room_counts = {str(int(key)): int(value) for key, value in frame["rooms"].value_counts().sort_index().items()}
    city_room_counts = {
        str(city): {
            str(int(room)): int(count)
            for room, count in group["rooms"].value_counts().sort_index().items()
        }
        for city, group in frame.groupby("city")
    }
    evaluation = {
        "dataset_rows": int(len(frame)),
        "unique_urls": int(raw["url"].nunique()),
        "city_counts": city_counts,
        "room_counts": room_counts,
        "city_room_counts": city_room_counts,
        "split_counts": {name: int(len(part)) for name, part in parts.items()},
        "target_candidates": candidates,
        "selected_target_mode": mode,
        "test": metrics(actual_test, test_predictions["q50"]),
        "test_segments": segment_metrics(
            parts["test"], actual_test, test_predictions["q50"]
        ),
        "test_baseline_city_room_median": metrics(actual_test, baseline),
        "q10_q90_test_coverage": coverage,
    }
    evaluation["production_ready"] = bool(
        len(frame) >= 2_000
        and min(city_counts.get("astana", 0), city_counts.get("almaty", 0)) >= 500
        and all(
            city_room_counts.get(city, {}).get(room, 0) >= 75
            for city in ("astana", "almaty")
            for room in ("1", "2", "3")
        )
        and evaluation["test"]["log_rmse"] < evaluation["test_baseline_city_room_median"]["log_rmse"]
        and 0.70 <= coverage <= 0.95
    )

    args.output.mkdir(parents=True, exist_ok=True)
    for quantile, model in models.items():
        model.save_model(str(args.output / RENTAL_MODEL_FILENAMES[quantile]))
    config.save(args.output / "feature_config.json")
    metadata = rental_model_metadata(catalog, target_mode=mode)
    metadata.update({
        "model_version": "rent_monthly_v2_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quantile_calibration": {"offsets_log": offsets},
        "evaluation": evaluation,
    })
    (args.output / "model_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "evaluation.json").write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evaluation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
