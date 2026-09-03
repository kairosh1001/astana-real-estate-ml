from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline_v2 import FEATURE_REFERENCE_YEAR

DATA_PATH = ROOT / "data" / "universal_training_v2.csv"
METADATA_PATH = ROOT / "models_candidate" / "universal_v2_model_metadata.json"
OUTPUT_ROOT = ROOT / "models_candidate" / "optimization_v2"
PRICE_PER_M2_MIN = 100_000
PRICE_PER_M2_MAX = 5_000_000
RANDOM_SEED = 42


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare CatBoost objectives and v2 feature subsets on validation data."
    )
    parser.add_argument(
        "--scope",
        choices=("universal", "astana", "almaty"),
        required=True,
    )
    parser.add_argument("--iterations", type=int, default=800)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run the baseline and the three strongest general-purpose candidates.",
    )
    return parser.parse_args()


def normalized_token(series: pd.Series) -> pd.Series:
    return series.fillna("__missing__").astype(str).str.strip().str.lower()


def rounded_token(series: pd.Series, decimals: int = 1) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").round(decimals)
    return values.fillna(-999999).astype(str)


def assign_split(frame: pd.DataFrame) -> pd.Series:
    parts = [
        normalized_token(frame["city"]),
        normalized_token(frame["h3_res_9"]),
        normalized_token(frame["city_residential_complex"]),
        rounded_token(frame["rooms"], 0),
        rounded_token(frame["area_m2"], 1),
        rounded_token(frame["current_floor"], 0),
        rounded_token(frame["total_floors"], 0),
        rounded_token(frame["year_of_construction"], 0),
    ]
    group = parts[0]
    for part in parts[1:]:
        group = group.str.cat(part, sep="|")
    buckets = group.map(
        lambda value: int(
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16
        )
        % 100
    )
    return pd.Series(
        np.select(
            [buckets.lt(70), buckets.lt(85)],
            ["train", "validation"],
            default="test",
        ),
        index=frame.index,
    )


def room_segment(value: Any) -> str:
    if pd.isna(value):
        return "missing"
    numeric = float(value)
    return "5+" if numeric >= 5 else str(int(numeric))


def add_derived_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    rooms = pd.to_numeric(result["rooms"], errors="coerce")
    area = pd.to_numeric(result["area_m2"], errors="coerce")
    floor = pd.to_numeric(result["current_floor"], errors="coerce")
    floors = pd.to_numeric(result["total_floors"], errors="coerce")
    year = pd.to_numeric(result["year_of_construction"], errors="coerce")
    result["rooms_segment"] = rooms.map(room_segment)
    result["area_per_room"] = area / rooms.where(rooms.gt(0))
    result["log_area_m2"] = np.log1p(area.clip(lower=0))
    result["building_age"] = (FEATURE_REFERENCE_YEAR - year).clip(
        lower=0, upper=200
    )
    result["is_first_floor"] = floor.eq(1).fillna(False).astype(int)
    result["is_top_floor"] = floor.eq(floors).fillna(False).astype(int)
    result["floor_from_top"] = floors - floor
    return result


def metrics(actual_log: pd.Series, predicted_log: np.ndarray) -> dict[str, float]:
    actual_log_array = np.asarray(actual_log)
    predicted_log_array = np.asarray(predicted_log)
    actual = np.exp(actual_log_array)
    predicted = np.exp(predicted_log_array)
    log_rmse = float(np.sqrt(mean_squared_error(actual_log_array, predicted_log_array)))
    return {
        "rows": int(len(actual_log_array)),
        "log_rmse": log_rmse,
        "log_mae": float(mean_absolute_error(actual_log_array, predicted_log_array)),
        "log_r2": float(r2_score(actual_log_array, predicted_log_array)),
        "median_absolute_percentage_error_pct": float(
            np.median(np.abs(predicted - actual) / actual) * 100
        ),
        "approx_multiplicative_error_pct": float(np.expm1(log_rmse) * 100),
    }


def segment_metrics(
    source: pd.DataFrame, actual_log: pd.Series, predicted_log: np.ndarray
) -> dict[str, dict[str, float]]:
    result = pd.DataFrame(
        {
            "rooms_segment": source["rooms"].map(room_segment).to_numpy(),
            "actual": np.asarray(actual_log),
            "predicted": np.asarray(predicted_log),
        }
    )
    return {
        str(segment): metrics(part["actual"], part["predicted"])
        for segment, part in result.groupby("rooms_segment")
        if len(part) >= 30
    }


def feature_sets(base_features: list[str]) -> dict[str, list[str]]:
    derived = [
        "rooms_segment",
        "area_per_room",
        "log_area_m2",
        "building_age",
        "is_first_floor",
        "is_top_floor",
        "floor_from_top",
    ]
    legacy_features = [
        feature for feature in base_features if feature not in derived
    ]
    missing_flags = {
        "year_missing",
        "ceiling_height_missing",
        "district_missing",
        "residential_complex_missing",
    }
    exact_duplicate = {"dist_to_city_center_normalized"}
    inner_radius_counts = {
        feature
        for feature in legacy_features
        if feature.startswith("count_")
        and (feature.endswith("within_500m") or feature.endswith("within_1km"))
    }
    compact = [
        feature
        for feature in legacy_features
        if feature not in exact_duplicate | missing_flags | inner_radius_counts
    ]
    no_duplicate = [
        feature for feature in legacy_features if feature not in exact_duplicate
    ]
    return {
        "full": legacy_features,
        "no_duplicate": no_duplicate,
        "compact": compact,
        "derived": no_duplicate + derived,
        "derived_compact": compact + derived,
    }


def candidate_definitions(iterations: int) -> list[dict[str, Any]]:
    common = {
        "iterations": iterations,
        "learning_rate": 0.04,
        "l2_leaf_reg": 7,
        "random_strength": 0.5,
    }
    return [
        {
            "name": "baseline_quantile_full",
            "feature_set": "full",
            "loss_function": "Quantile:alpha=0.5",
            "depth": 7,
            "iterations": 500,
            "learning_rate": 0.05,
            "l2_leaf_reg": 5,
            "random_strength": 0.5,
        },
        {
            "name": "rmse_full_d7",
            "feature_set": "full",
            "loss_function": "RMSE",
            "depth": 7,
            **common,
        },
        {
            "name": "rmse_no_duplicate_d7",
            "feature_set": "no_duplicate",
            "loss_function": "RMSE",
            "depth": 7,
            **common,
        },
        {
            "name": "rmse_compact_d7",
            "feature_set": "compact",
            "loss_function": "RMSE",
            "depth": 7,
            **common,
        },
        {
            "name": "rmse_derived_d7",
            "feature_set": "derived",
            "loss_function": "RMSE",
            "depth": 7,
            **common,
        },
        {
            "name": "rmse_derived_compact_d7",
            "feature_set": "derived_compact",
            "loss_function": "RMSE",
            "depth": 7,
            **common,
        },
        {
            "name": "rmse_derived_d6",
            "feature_set": "derived",
            "loss_function": "RMSE",
            "depth": 6,
            **common,
        },
        {
            "name": "rmse_derived_compact_d6",
            "feature_set": "derived_compact",
            "loss_function": "RMSE",
            "depth": 6,
            **common,
        },
        {
            "name": "rmse_derived_compact_d8",
            "feature_set": "derived_compact",
            "loss_function": "RMSE",
            "depth": 8,
            **common,
        },
        {
            "name": "rmse_derived_compact_slow",
            "feature_set": "derived_compact",
            "loss_function": "RMSE",
            "depth": 7,
            "iterations": max(iterations, 1200),
            "learning_rate": 0.03,
            "l2_leaf_reg": 9,
            "random_strength": 0.35,
        },
        {
            "name": "rmse_derived_compact_regularized",
            "feature_set": "derived_compact",
            "loss_function": "RMSE",
            "depth": 7,
            "iterations": iterations,
            "learning_rate": 0.04,
            "l2_leaf_reg": 12,
            "random_strength": 0.35,
        },
        {
            "name": "rmse_derived_weighted",
            "feature_set": "derived",
            "loss_function": "RMSE",
            "depth": 7,
            "weighted": True,
            **common,
        },
    ]


def prepare_matrix(
    frame: pd.DataFrame, features: list[str], categorical: list[str]
) -> pd.DataFrame:
    matrix = frame[features].copy()
    for column in categorical:
        matrix[column] = matrix[column].fillna("__missing__").astype(str)
    for column in set(features) - set(categorical):
        matrix[column] = pd.to_numeric(matrix[column], errors="coerce")
    return matrix


def training_weights(frame: pd.DataFrame) -> np.ndarray:
    rooms = pd.to_numeric(frame["rooms"], errors="coerce")
    return np.select([rooms.ge(5), rooms.eq(4)], [2.5, 1.5], default=1.0)


def main() -> None:
    args = parse_args()
    metadata = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    base_features = list(
        metadata.get("dataset_feature_columns") or metadata["feature_columns"]
    )
    base_categorical = [
        "city",
        "city_district",
        "city_residential_complex",
        "furnished",
        "apartment_condition",
        "building_type",
        "h3_res_7",
        "h3_res_8",
        "h3_res_9",
    ]
    target = metadata["target"]

    frame = pd.read_csv(DATA_PATH, low_memory=False)
    price_per_m2 = np.exp(pd.to_numeric(frame[target], errors="coerce"))
    frame = frame.loc[
        price_per_m2.between(PRICE_PER_M2_MIN, PRICE_PER_M2_MAX)
    ].copy()
    if args.scope in {"astana", "almaty"}:
        frame = frame.loc[frame["city"].eq(args.scope)].copy()
    frame = add_derived_features(frame)
    frame["split"] = assign_split(frame)

    subsets = {
        split: frame.loc[frame["split"].eq(split)].copy()
        for split in ("train", "validation", "test")
    }
    sets = feature_sets(base_features)
    derived_categorical = base_categorical + ["rooms_segment"]
    candidates = candidate_definitions(args.iterations)
    if args.quick:
        quick_names = {
            "baseline_quantile_full",
            "rmse_full_d7",
            "rmse_compact_d7",
            "rmse_derived_compact_d7",
        }
        candidates = [
            candidate for candidate in candidates if candidate["name"] in quick_names
        ]
    validation_results: list[dict[str, Any]] = []
    trained_models: dict[str, CatBoostRegressor] = {}

    for candidate in candidates:
        name = candidate["name"]
        features = sets[candidate["feature_set"]]
        categorical = [
            column for column in derived_categorical if column in features
        ]
        matrices = {
            split: prepare_matrix(part, features, categorical)
            for split, part in subsets.items()
        }
        y = {
            split: pd.to_numeric(part[target], errors="raise")
            for split, part in subsets.items()
        }
        cat_indices = [features.index(column) for column in categorical]
        parameters = {
            key: value
            for key, value in candidate.items()
            if key
            not in {"name", "feature_set", "weighted"}
        }
        model = CatBoostRegressor(
            **parameters,
            eval_metric="RMSE",
            random_seed=RANDOM_SEED,
            allow_writing_files=False,
            verbose=False,
            thread_count=-1,
        )
        started = time.perf_counter()
        fit_kwargs: dict[str, Any] = {}
        if candidate.get("weighted"):
            fit_kwargs["sample_weight"] = training_weights(subsets["train"])
        model.fit(
            matrices["train"],
            y["train"],
            cat_features=cat_indices,
            eval_set=(matrices["validation"], y["validation"]),
            early_stopping_rounds=100,
            use_best_model=True,
            **fit_kwargs,
        )
        predicted = model.predict(matrices["validation"])
        overall = metrics(y["validation"], predicted)
        segments = segment_metrics(subsets["validation"], y["validation"], predicted)
        result = {
            "name": name,
            "feature_set": candidate["feature_set"],
            "feature_count": len(features),
            "categorical_count": len(categorical),
            "best_iteration": int(model.get_best_iteration()),
            "fit_seconds": round(time.perf_counter() - started, 2),
            "validation": overall,
            "validation_segments": segments,
        }
        validation_results.append(result)
        trained_models[name] = model
        print(
            f"{name}: val_log_rmse={overall['log_rmse']:.6f}, "
            f"features={len(features)}, best_iter={result['best_iteration']}, "
            f"seconds={result['fit_seconds']}"
        )

    winner = min(
        validation_results,
        key=lambda result: result["validation"]["log_rmse"],
    )
    baseline = next(
        result
        for result in validation_results
        if result["name"] == "baseline_quantile_full"
    )
    test_results: dict[str, Any] = {}
    for result in (baseline, winner):
        name = result["name"]
        features = sets[result["feature_set"]]
        categorical = [
            column for column in derived_categorical if column in features
        ]
        test_matrix = prepare_matrix(subsets["test"], features, categorical)
        actual = pd.to_numeric(subsets["test"][target], errors="raise")
        predicted = trained_models[name].predict(test_matrix)
        test_results[name] = {
            "overall": metrics(actual, predicted),
            "segments": segment_metrics(subsets["test"], actual, predicted),
        }

    output = {
        "scope": args.scope,
        "selection_rule": "lowest validation log RMSE; test evaluated only for baseline and winner",
        "rows_by_split": {
            split: int(len(part)) for split, part in subsets.items()
        },
        "feature_sets": {name: features for name, features in sets.items()},
        "validation_results": validation_results,
        "winner": winner["name"],
        "test_results": test_results,
    }
    output_dir = OUTPUT_ROOT / args.scope
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    trained_models[winner["name"]].save_model(output_dir / "winner_q50.cbm")
    print(f"WINNER: {winner['name']}")
    print(json.dumps(test_results, ensure_ascii=False, indent=2))
    print(f"Saved: {output_dir}")


if __name__ == "__main__":
    main()
