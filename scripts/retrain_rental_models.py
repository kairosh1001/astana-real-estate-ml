from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Callable

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
    "q50": ("RMSE", "catboost_q50_rent_total_log.cbm"),
    "q90": ("Quantile:alpha=0.9", "catboost_q90_rent_total_log.cbm"),
}
PRICE_LIMITS = {
    "monthly": (20_000, 10_000_000),
    "daily": (1_000, 1_000_000),
}


def bootstrap_ci(
    values: np.ndarray,
    reducer: Callable[[np.ndarray], float],
    seed: int,
    samples: int = 2_000,
) -> list[float]:
    rng = np.random.default_rng(seed)
    estimates = np.fromiter(
        (reducer(values[rng.integers(0, len(values), len(values))]) for _ in range(samples)),
        dtype=float,
        count=samples,
    )
    return [float(bound) for bound in np.quantile(estimates, [0.025, 0.975])]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train monthly or daily rental models.")
    parser.add_argument("--period", choices=["monthly", "daily"], required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--candidate-iterations", type=int, default=450)
    parser.add_argument("--selection-splits", type=int, default=3)
    parser.add_argument("--depth", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument(
        "--target-mode",
        choices=["auto", "total", "per_m2"],
        default="auto",
        help="Auto compares total-rent and rent-per-m2 targets on grouped inner splits.",
    )
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


def listing_groups(raw: pd.DataFrame) -> pd.Series:
    groups = raw.get("listing_id", pd.Series(pd.NA, index=raw.index)).astype("string")
    fallback = raw["url"].astype("string")
    return groups.where(groups.notna() & groups.ne("<NA>"), fallback)


def deduplicate_training_snapshots(raw: pd.DataFrame) -> pd.DataFrame:
    if "scraped_at" not in raw:
        return raw.drop_duplicates("url", keep="last").reset_index(drop=True)
    result = raw.copy()
    scraped = pd.to_datetime(result["scraped_at"], errors="coerce", utc=True)
    result["_scrape_day"] = scraped.dt.strftime("%Y-%m-%d").fillna("unknown")
    result["_scraped_order"] = scraped
    result["_listing_group"] = listing_groups(result)
    return (
        result.sort_values("_scraped_order", kind="stable")
        .drop_duplicates(["_listing_group", "_scrape_day"], keep="last")
        .drop(columns=["_listing_group", "_scrape_day", "_scraped_order"])
        .reset_index(drop=True)
    )


def split_rows(raw: pd.DataFrame, test_size: float, seed: int) -> tuple[np.ndarray, np.ndarray, str]:
    scraped = pd.to_datetime(raw.get("scraped_at"), errors="coerce", utc=True)
    scrape_day = scraped.dt.floor("D")
    groups = listing_groups(raw)
    if scrape_day.notna().any() and scrape_day.nunique() > 1:
        latest_day = scrape_day.max()
        valid_mask = scrape_day == latest_day
        valid_groups = set(groups.loc[valid_mask])
        train_mask = (scrape_day < latest_day) & ~groups.isin(valid_groups)
        if train_mask.sum() >= 100 and valid_mask.sum() >= 100:
            return np.flatnonzero(train_mask), np.flatnonzero(valid_mask), "chronological_group_holdout"

    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_index, valid_index = next(splitter.split(raw, groups=groups))
    return train_index, valid_index, "group_shuffle_holdout"


def model_target(total_log: pd.Series, area_m2: pd.Series, mode: str) -> pd.Series:
    if mode == "total":
        return total_log
    if mode == "per_m2":
        return total_log - np.log(area_m2)
    raise ValueError(f"Unknown target mode: {mode}")


def prediction_to_total_log(prediction: np.ndarray, area_m2: pd.Series, mode: str) -> np.ndarray:
    result = np.asarray(prediction, dtype=float)
    if mode == "per_m2":
        result = result + np.log(area_m2.to_numpy(dtype=float))
    return result


def candidate_grid(args: argparse.Namespace) -> list[dict]:
    candidates = [
        {"depth": 6, "learning_rate": 0.05, "l2_leaf_reg": 5.0},
        {"depth": 7, "learning_rate": 0.04, "l2_leaf_reg": 8.0},
        {"depth": 8, "learning_rate": 0.035, "l2_leaf_reg": 10.0},
        {"depth": args.depth, "learning_rate": args.learning_rate, "l2_leaf_reg": 3.0},
    ]
    unique: list[dict] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def select_candidate(
    train_x: pd.DataFrame,
    train_total_log: pd.Series,
    train_groups: pd.Series,
    categorical_indices: list[int],
    args: argparse.Namespace,
) -> tuple[dict, list[dict]]:
    modes = [args.target_mode] if args.target_mode != "auto" else ["total", "per_m2"]
    splitter = GroupShuffleSplit(
        n_splits=args.selection_splits,
        test_size=args.test_size,
        random_state=args.random_seed + 101,
    )
    splits = list(splitter.split(train_x, groups=train_groups))
    results: list[dict] = []
    for mode in modes:
        target = model_target(train_total_log, train_x["area_m2"], mode)
        for hyperparameters in candidate_grid(args):
            scores: list[float] = []
            best_iterations: list[int] = []
            for split_number, (inner_train, inner_valid) in enumerate(splits):
                model = CatBoostRegressor(
                    loss_function="RMSE",
                    iterations=args.candidate_iterations,
                    random_seed=args.random_seed + split_number,
                    verbose=False,
                    allow_writing_files=False,
                    **hyperparameters,
                )
                model.fit(
                    train_x.iloc[inner_train],
                    target.iloc[inner_train],
                    cat_features=categorical_indices,
                    eval_set=(train_x.iloc[inner_valid], target.iloc[inner_valid]),
                    early_stopping_rounds=60,
                    use_best_model=True,
                )
                prediction_log = prediction_to_total_log(
                    model.predict(train_x.iloc[inner_valid]),
                    train_x.iloc[inner_valid]["area_m2"],
                    mode,
                )
                scores.append(
                    float(
                        np.sqrt(
                            np.mean(
                                np.square(
                                    prediction_log - train_total_log.iloc[inner_valid].to_numpy()
                                )
                            )
                        )
                    )
                )
                best_iteration = model.get_best_iteration()
                best_iterations.append(
                    int(best_iteration + 1 if best_iteration is not None and best_iteration >= 0 else args.candidate_iterations)
                )
            result = {
                "target_mode": mode,
                **hyperparameters,
                "mean_log_rmse": float(np.mean(scores)),
                "std_log_rmse": float(np.std(scores)),
                "split_log_rmse": scores,
                "mean_best_iteration": float(np.mean(best_iterations)),
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))

    selected = min(results, key=lambda item: (item["mean_log_rmse"], item["std_log_rmse"]))
    selected_iterations = int(
        np.clip(round(selected["mean_best_iteration"] * 1.15), 150, args.iterations)
    )
    selected = {**selected, "final_iterations": selected_iterations}
    return selected, results


def main() -> None:
    args = parse_args()
    dataset = args.dataset or ROOT / "data" / f"rent_{args.period}_raw.csv"
    output_dir = args.output_dir or ROOT / "models" / f"rent_{args.period}"
    raw_input = pd.read_csv(dataset)
    input_rows = len(raw_input)
    raw = raw_input[raw_input.get("rental_period", "") == args.period].copy()
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce")
    raw["area_m2_structured"] = pd.to_numeric(raw.get("area_m2_structured"), errors="coerce")
    raw["rooms_structured"] = pd.to_numeric(raw.get("rooms_structured"), errors="coerce")
    raw["lat"] = pd.to_numeric(raw.get("lat"), errors="coerce")
    raw["lon"] = pd.to_numeric(raw.get("lon"), errors="coerce")
    low_price, high_price = PRICE_LIMITS[args.period]
    quality_mask = (
        raw["price"].between(low_price, high_price)
        & raw["area_m2_structured"].between(10, 1_000)
        & raw["rooms_structured"].between(1, 20)
        & raw["lat"].between(49.5, 52.5)
        & raw["lon"].between(69.0, 73.5)
        & raw["url"].notna()
    )
    raw = deduplicate_training_snapshots(raw.loc[quality_mask].copy())
    if len(raw) < args.min_rows:
        raise ValueError(f"Need at least {args.min_rows} valid {args.period} listings, found {len(raw)}")

    config = build_rental_feature_config(raw)
    prepared = build_rental_features(raw, config, include_target=True)
    finite_columns = ["rent_total_log", "area_m2"]
    finite = prepared[finite_columns].replace([np.inf, -np.inf], np.nan).notna().all(axis=1)
    finite &= prepared["area_m2"].gt(0)
    raw = raw.loc[finite].reset_index(drop=True)
    prepared = prepared.loc[finite].reset_index(drop=True)
    metadata = metadata_for_period(args.period)
    features = prepare_features(prepared, metadata)
    total_log = prepared["rent_total_log"]
    train_idx, valid_idx, strategy = split_rows(raw, args.test_size, args.random_seed)

    train_x, valid_x = features.iloc[train_idx], features.iloc[valid_idx]
    train_total_log, valid_total_log = total_log.iloc[train_idx], total_log.iloc[valid_idx]
    cat_indices = [train_x.columns.get_loc(column) for column in metadata["categorical_features"]]
    selected, selection_results = select_candidate(
        train_x,
        train_total_log,
        listing_groups(raw).iloc[train_idx],
        cat_indices,
        args,
    )
    target_mode = selected["target_mode"]
    train_y = model_target(train_total_log, train_x["area_m2"], target_mode)

    predictions: dict[str, np.ndarray] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    final_hyperparameters = {
        "iterations": selected["final_iterations"],
        "depth": selected["depth"],
        "learning_rate": selected["learning_rate"],
        "l2_leaf_reg": selected["l2_leaf_reg"],
    }
    for quantile, (loss, filename) in MODEL_NAMES.items():
        print(f"[INFO] Training {args.period} {quantile} with {target_mode} target")
        model = CatBoostRegressor(
            loss_function=loss,
            random_seed=args.random_seed,
            verbose=100,
            allow_writing_files=False,
            **final_hyperparameters,
        )
        model.fit(train_x, train_y, cat_features=cat_indices)
        model.save_model(str(output_dir / filename))
        prediction_log = prediction_to_total_log(
            model.predict(valid_x),
            valid_x["area_m2"],
            target_mode,
        )
        predictions[quantile] = np.exp(prediction_log)

    actual = np.exp(valid_total_log.to_numpy())
    train_actual = np.exp(train_total_log.to_numpy())
    median_by_rooms = (
        raw.iloc[train_idx]
        .assign(target=train_actual)
        .groupby("rooms_structured")["target"]
        .median()
    )
    global_median = float(np.median(train_actual))
    baseline = raw.iloc[valid_idx]["rooms_structured"].map(median_by_rooms).fillna(global_median).to_numpy()
    q50 = predictions["q50"]
    actual_log = valid_total_log.to_numpy()
    q50_log = np.log(q50)
    baseline_log = np.log(baseline)
    rmse_log = float(np.sqrt(np.mean(np.square(q50_log - actual_log))))
    baseline_rmse_log = float(np.sqrt(np.mean(np.square(baseline_log - actual_log))))
    rmse_log_ci95 = bootstrap_ci(
        np.square(q50_log - actual_log),
        lambda sample: float(np.sqrt(np.mean(sample))),
        args.random_seed,
    )
    mae_kzt_ci95 = bootstrap_ci(
        np.abs(q50 - actual),
        lambda sample: float(np.mean(sample)),
        args.random_seed,
    )
    unique_listings = int(listing_groups(raw).nunique())
    production_ready = bool(
        unique_listings >= 500
        and len(valid_idx) >= 100
        and strategy == "chronological_group_holdout"
    )
    readiness_notes: list[str] = []
    if unique_listings < 500:
        readiness_notes.append(f"Collect at least 500 unique listings; this dataset has {unique_listings}.")
    if strategy != "chronological_group_holdout":
        readiness_notes.append("Collect a fresh snapshot on a later UTC date for an independent chronological holdout.")
    if len(valid_idx) < 100:
        readiness_notes.append(f"Use at least 100 validation rows; this evaluation has {len(valid_idx)}.")
    lower = np.minimum(predictions["q10"], predictions["q90"])
    upper = np.maximum(predictions["q10"], predictions["q90"])
    metrics = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": args.period,
        "input_rows": input_rows,
        "rows": len(raw),
        "rows_removed_by_quality_and_deduplication": input_rows - len(raw),
        "unique_listings": unique_listings,
        "train_rows": len(train_idx),
        "validation_rows": len(valid_idx),
        "split_strategy": strategy,
        "selected_target_mode": target_mode,
        "selected_hyperparameters": final_hyperparameters,
        "candidate_selection": selection_results,
        "rmse_log": rmse_log,
        "rmse_log_ci95": rmse_log_ci95,
        "multiplicative_rmse": float(np.expm1(rmse_log)),
        "rmse_kzt": float(np.sqrt(np.mean(np.square(q50 - actual)))),
        "mae_kzt": float(np.mean(np.abs(q50 - actual))),
        "mae_kzt_ci95": mae_kzt_ci95,
        "median_ape": float(np.median(np.abs(q50 - actual) / actual)),
        "baseline_rmse_log": baseline_rmse_log,
        "baseline_multiplicative_rmse": float(np.expm1(baseline_rmse_log)),
        "baseline_rmse_kzt": float(np.sqrt(np.mean(np.square(baseline - actual)))),
        "baseline_mae_kzt": float(np.mean(np.abs(baseline - actual))),
        "q10_q90_coverage": float(np.mean((actual >= lower) & (actual <= upper))),
        "production_ready": production_ready,
        "readiness_notes": readiness_notes,
    }
    metadata["target"] = "rent_total_log" if target_mode == "total" else "rent_per_m2_log"
    metadata["target_units"] = (
        "log(KZT/month)" if args.period == "monthly" else "log(KZT/day)"
    ) if target_mode == "total" else (
        "log(KZT/month/m2)" if args.period == "monthly" else "log(KZT/day/m2)"
    )
    metadata["served_prediction_units"] = "KZT/month" if args.period == "monthly" else "KZT/day"
    metadata["target_mode"] = target_mode
    metadata["point_model_loss"] = "RMSE"
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
    (output_dir / "model_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "evaluation.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
