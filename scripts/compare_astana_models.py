from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline import build_feature_config, build_model_features
from app.model_service import MODEL_FILENAMES, PriceModelService


DEFAULT_DATASET = ROOT / "data" / "universal_training_v2.csv"
DEFAULT_V2_METADATA = ROOT / "models" / "universal_v2" / "model_metadata.json"
DEFAULT_OUTPUT_JSON = ROOT / "reports" / "astana_model_comparison.json"
DEFAULT_OUTPUT_MD = ROOT / "reports" / "astana_model_comparison.md"
DEFAULT_CANDIDATE_DIR = ROOT / "models_candidate" / "astana_model_comparison"
DEFAULT_RAW_INPUTS = (
    ROOT / "krisha_data_raw_orig.csv",
    ROOT / "krisha_data_raw.csv",
    ROOT / "data" / "astana_sale_raw.csv",
)

MODEL_OBJECTIVES = {
    "q10": "Quantile:alpha=0.10",
    "q50": "RMSE",
    "q90": "Quantile:alpha=0.90",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Train fair Astana-only v1-style and v2 candidates, then compare "
            "them with universal_v2 on the same deterministic Astana test set."
        )
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--v2-metadata", type=Path, default=DEFAULT_V2_METADATA)
    parser.add_argument(
        "--raw-input",
        type=Path,
        action="append",
        dest="raw_inputs",
        help="Raw Astana CSV; repeat to override the default input list.",
    )
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--iterations", type=int, default=800)
    parser.add_argument("--depth", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=0.04)
    parser.add_argument("--l2-leaf-reg", type=float, default=7.0)
    parser.add_argument("--early-stopping-rounds", type=int, default=100)
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def normalized_token(series: pd.Series) -> pd.Series:
    return series.fillna("__missing__").astype(str).str.strip().str.lower()


def rounded_token(series: pd.Series, decimals: int = 1) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").round(decimals)
    return values.fillna(-999999).astype(str)


def property_groups(frame: pd.DataFrame) -> pd.Series:
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
    groups = parts[0]
    for part in parts[1:]:
        groups = groups.str.cat(part, sep="|")
    return groups


def assign_split(frame: pd.DataFrame) -> pd.Series:
    buckets = property_groups(frame).map(
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


def prepare_features(
    frame: pd.DataFrame,
    feature_columns: list[str],
    categorical_features: list[str],
) -> pd.DataFrame:
    result = frame.loc[:, feature_columns].copy()
    categorical = set(categorical_features)
    for column in result:
        if column in categorical:
            result[column] = result[column].fillna("__missing__").astype(str)
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def load_astana_frame(dataset_path: Path) -> pd.DataFrame:
    frame = pd.read_csv(dataset_path, low_memory=False)
    frame = frame.loc[frame["city"].eq("astana")].copy()
    price_per_m2 = np.exp(pd.to_numeric(frame["price_per_m2_log"], errors="coerce"))
    frame = frame.loc[price_per_m2.between(100_000, 5_000_000)].copy()
    frame["property_group"] = property_groups(frame)
    frame["split"] = assign_split(frame)
    if frame["listing_url"].duplicated().any():
        raise ValueError("The v2 dataset contains duplicate Astana listing URLs.")
    if frame.groupby("property_group")["split"].nunique().max() != 1:
        raise ValueError("A property-like group crossed split boundaries.")
    return frame.reset_index(drop=True)


def load_legacy_features(
    frame: pd.DataFrame,
    raw_inputs: list[Path],
) -> pd.DataFrame:
    missing_inputs = [str(path) for path in raw_inputs if not path.exists()]
    if missing_inputs:
        raise FileNotFoundError(f"Raw Astana inputs are missing: {missing_inputs}")

    raw_parts = []
    for path in raw_inputs:
        part = pd.read_csv(path, low_memory=False)
        part["_source_file"] = path.name
        raw_parts.append(part)
    raw = pd.concat(raw_parts, ignore_index=True, sort=False)
    if "scraped_at" in raw:
        raw["_scraped_order"] = pd.to_datetime(
            raw["scraped_at"], errors="coerce", utc=True
        )
        raw = raw.sort_values("_scraped_order", kind="stable")
    raw = raw.drop_duplicates(subset="url", keep="last")
    raw_by_url = raw.set_index("url", drop=False)

    urls = frame["listing_url"].astype(str)
    missing_urls = sorted(set(urls) - set(raw_by_url.index.astype(str)))
    if missing_urls:
        preview = ", ".join(missing_urls[:5])
        raise ValueError(
            f"Cannot reconstruct legacy features for {len(missing_urls)} URLs: {preview}"
        )
    selected_raw = raw_by_url.loc[urls].reset_index(drop=True)
    config = build_feature_config(raw.reset_index(drop=True))
    legacy = build_model_features(
        selected_raw,
        config,
        include_target=False,
        filter_training_rows=False,
    )
    if len(legacy) != len(frame):
        raise ValueError("Legacy feature reconstruction changed the comparison grain.")
    return legacy


def train_candidate(
    frame: pd.DataFrame,
    feature_columns: list[str],
    categorical_features: list[str],
    args: argparse.Namespace,
    output_dir: Path,
) -> tuple[dict[str, CatBoostRegressor], dict[str, float]]:
    features = prepare_features(frame, feature_columns, categorical_features)
    target = pd.to_numeric(frame["price_per_m2_log"], errors="raise")
    train_mask = frame["split"].eq("train")
    validation_mask = frame["split"].eq("validation")
    categorical_indices = [
        feature_columns.index(column) for column in categorical_features
    ]

    models: dict[str, CatBoostRegressor] = {}
    validation_predictions: dict[str, np.ndarray] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for label, objective in MODEL_OBJECTIVES.items():
        print(f"[INFO] Training {output_dir.name}/{label}")
        model = CatBoostRegressor(
            loss_function=objective,
            eval_metric=objective,
            iterations=args.iterations,
            depth=args.depth,
            learning_rate=args.learning_rate,
            random_seed=args.random_seed,
            l2_leaf_reg=args.l2_leaf_reg,
            random_strength=0.5,
            verbose=100,
            allow_writing_files=False,
            thread_count=-1,
        )
        model.fit(
            features.loc[train_mask],
            target.loc[train_mask],
            cat_features=categorical_indices,
            eval_set=(features.loc[validation_mask], target.loc[validation_mask]),
            early_stopping_rounds=args.early_stopping_rounds,
            use_best_model=True,
        )
        model.save_model(str(output_dir / MODEL_FILENAMES[label]))
        models[label] = model
        validation_predictions[label] = np.asarray(
            model.predict(features.loc[validation_mask])
        )

    actual_validation = target.loc[validation_mask].to_numpy()
    offsets = {
        "q10": float(
            np.quantile(actual_validation - validation_predictions["q10"], 0.10)
        ),
        "q90": float(
            np.quantile(actual_validation - validation_predictions["q90"], 0.90)
        ),
    }
    return models, offsets


def candidate_predictions(
    models: dict[str, CatBoostRegressor],
    offsets: dict[str, float],
    features: pd.DataFrame,
) -> dict[str, np.ndarray]:
    predictions = {
        label: np.asarray(model.predict(features)) + float(offsets.get(label, 0.0))
        for label, model in models.items()
    }
    predictions["q10"] = np.minimum(predictions["q10"], predictions["q50"])
    predictions["q90"] = np.maximum(predictions["q90"], predictions["q50"])
    return predictions


def regression_metrics(
    actual_log: np.ndarray,
    predictions: dict[str, np.ndarray],
) -> dict[str, float | int]:
    actual_log = np.asarray(actual_log, dtype=float)
    q10_log = np.asarray(predictions["q10"], dtype=float)
    q50_log = np.asarray(predictions["q50"], dtype=float)
    q90_log = np.asarray(predictions["q90"], dtype=float)
    actual = np.exp(actual_log)
    q50 = np.exp(q50_log)
    log_rmse = float(np.sqrt(mean_squared_error(actual_log, q50_log)))
    return {
        "rows": int(len(actual_log)),
        "log_rmse": log_rmse,
        "approx_multiplicative_error_pct": float(np.expm1(log_rmse) * 100),
        "log_mae": float(mean_absolute_error(actual_log, q50_log)),
        "log_r2": float(r2_score(actual_log, q50_log)),
        "mae_kzt_per_m2": float(mean_absolute_error(actual, q50)),
        "rmse_kzt_per_m2": float(np.sqrt(mean_squared_error(actual, q50))),
        "median_absolute_percentage_error_pct": float(
            np.median(np.abs(q50 - actual) / actual) * 100
        ),
        "q10_q90_coverage_pct": float(
            np.mean((actual_log >= q10_log) & (actual_log <= q90_log)) * 100
        ),
    }


def room_segment(value: Any) -> str:
    if pd.isna(value):
        return "missing"
    numeric = float(value)
    return "5+" if numeric >= 5 else str(int(numeric))


def segment_metrics(
    test_frame: pd.DataFrame,
    actual_log: np.ndarray,
    predictions: dict[str, np.ndarray],
) -> dict[str, dict[str, float | int]]:
    segments = test_frame["rooms"].map(room_segment).reset_index(drop=True)
    result = {}
    for segment in ["1", "2", "3", "4", "5+", "missing"]:
        mask = segments.eq(segment).to_numpy()
        if not mask.any():
            continue
        result[segment] = regression_metrics(
            actual_log[mask],
            {label: values[mask] for label, values in predictions.items()},
        )
    return result


def paired_cluster_bootstrap(
    test_frame: pd.DataFrame,
    actual_log: np.ndarray,
    predictions_by_model: dict[str, dict[str, np.ndarray]],
    *,
    replicates: int,
    random_seed: int,
) -> dict[str, dict[str, float]]:
    groups = test_frame["property_group"].reset_index(drop=True)
    unique_groups = groups.drop_duplicates().to_numpy()
    group_indices = {
        group: np.flatnonzero(groups.to_numpy() == group) for group in unique_groups
    }
    rng = np.random.default_rng(random_seed)
    winner = min(
        predictions_by_model,
        key=lambda name: regression_metrics(actual_log, predictions_by_model[name])[
            "log_rmse"
        ],
    )
    comparisons: dict[str, dict[str, float]] = {}
    for challenger, challenger_predictions in predictions_by_model.items():
        if challenger == winner:
            continue
        deltas = np.empty(replicates, dtype=float)
        for index in range(replicates):
            sampled_groups = rng.choice(unique_groups, size=len(unique_groups), replace=True)
            sampled_indices = np.concatenate(
                [group_indices[group] for group in sampled_groups]
            )
            winner_rmse = float(
                np.sqrt(
                    np.mean(
                        (
                            actual_log[sampled_indices]
                            - predictions_by_model[winner]["q50"][sampled_indices]
                        )
                        ** 2
                    )
                )
            )
            challenger_rmse = float(
                np.sqrt(
                    np.mean(
                        (
                            actual_log[sampled_indices]
                            - challenger_predictions["q50"][sampled_indices]
                        )
                        ** 2
                    )
                )
            )
            deltas[index] = challenger_rmse - winner_rmse
        comparisons[f"{challenger}_minus_{winner}"] = {
            "mean_log_rmse_delta": float(np.mean(deltas)),
            "ci95_low": float(np.quantile(deltas, 0.025)),
            "ci95_high": float(np.quantile(deltas, 0.975)),
            "probability_winner_is_better": float(np.mean(deltas > 0)),
        }
    return comparisons


def write_markdown(payload: dict, path: Path) -> None:
    overall = payload["overall_test_metrics"]
    ordered = sorted(overall, key=lambda name: overall[name]["log_rmse"])
    lines = [
        "# Astana Sale Model Comparison",
        "",
        f"Dataset: `{payload['dataset']['path']}`",
        (
            "Known scrape timestamps: "
            f"{payload['dataset']['scraped_at_known_rows']:,} of "
            f"{payload['dataset']['rows']:,} rows "
            f"({payload['dataset']['scraped_at_known_pct']:.2f}%); "
            f"known range {payload['dataset']['scraped_at_min']} to "
            f"{payload['dataset']['scraped_at_max']}."
        ),
        f"Common held-out test rows: {payload['dataset']['rows_by_split']['test']:,}",
        "",
        "All three models are evaluated on the same deterministic Astana test groups. "
        "The legacy comparison model is freshly retrained with the v1 feature contract "
        "to avoid evaluating a production artifact that may have seen these listings.",
        "",
        "| Model | Log RMSE | Median APE | MAE, KZT/m² | q10-q90 coverage |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ordered:
        metric = overall[name]
        lines.append(
            f"| {name} | {metric['log_rmse']:.6f} | "
            f"{metric['median_absolute_percentage_error_pct']:.2f}% | "
            f"{metric['mae_kzt_per_m2']:,.0f} | "
            f"{metric['q10_q90_coverage_pct']:.2f}% |"
        )
    lines.extend(
        [
            "",
            f"Winner by held-out log RMSE: **{payload['winner']}**.",
            "",
            "## Existing production v1 (reference only)",
            "",
            (
                "The existing production artifact scores "
                f"{payload['non_comparable_reference_metrics']['production_astana_v1']['log_rmse']:.6f} "
                "log RMSE on these rows, but it is excluded from model selection because its "
                "historical training membership is not recorded and may overlap this test set."
            ),
            "",
            "## Paired property-group bootstrap",
            "",
        ]
    )
    for comparison, values in payload["paired_cluster_bootstrap"].items():
        lines.append(
            f"- `{comparison}`: mean {values['mean_log_rmse_delta']:.6f}; "
            f"95% CI [{values['ci95_low']:.6f}, {values['ci95_high']:.6f}]; "
            f"P(winner better)={values['probability_winner_is_better']:.1%}."
        )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- This is a grouped holdout from the same accumulated listing period, not a future-period test.",
            "- Listing prices are public asking prices, not completed transaction prices.",
            "- Feature mappings and the frozen OSM catalog are target-free but were built before the split.",
            "- Most legacy source rows do not contain a scrape timestamp, so this run cannot establish full dataset freshness.",
            "- Small room segments, especially 5+, remain less stable than 1-3 room segments.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    raw_inputs = list(args.raw_inputs or DEFAULT_RAW_INPUTS)
    metadata = json.loads(args.v2_metadata.read_text(encoding="utf-8"))
    v2_features = list(metadata["feature_columns"])
    v2_categorical = list(metadata["categorical_features"])

    frame = load_astana_frame(args.dataset)
    legacy_features = load_legacy_features(frame, raw_inputs)
    legacy_metadata = json.loads((ROOT / "model_metadata.json").read_text(encoding="utf-8"))
    for column in legacy_metadata["feature_columns"]:
        frame[f"legacy__{column}"] = legacy_features[column].to_numpy()

    split_counts = frame["split"].value_counts().to_dict()
    print(f"[QA] Astana rows by split: {split_counts}")
    test_mask = frame["split"].eq("test")
    test_frame = frame.loc[test_mask].reset_index(drop=True)
    actual_test = pd.to_numeric(
        test_frame["price_per_m2_log"], errors="raise"
    ).to_numpy()

    v1_columns = [f"legacy__{column}" for column in legacy_metadata["feature_columns"]]
    v1_categorical = [
        f"legacy__{column}" for column in legacy_metadata["categorical_features"]
    ]
    v1_models, v1_offsets = train_candidate(
        frame,
        v1_columns,
        v1_categorical,
        args,
        args.candidate_dir / "astana_v1_retrained",
    )
    v1_test_features = prepare_features(test_frame, v1_columns, v1_categorical)
    v1_predictions = candidate_predictions(v1_models, v1_offsets, v1_test_features)

    production_v1_service = PriceModelService(
        models_dir=ROOT / "models",
        metadata_path=ROOT / "model_metadata.json",
    )
    production_v1_frame = production_v1_service.predict(
        legacy_features.loc[test_mask].reset_index(drop=True)
    )
    production_v1_predictions = {
        label: production_v1_frame.predictions[
            f"pred_price_per_m2_log_{label}"
        ].to_numpy()
        for label in MODEL_OBJECTIVES
    }

    v2_models, v2_offsets = train_candidate(
        frame,
        v2_features,
        v2_categorical,
        args,
        args.candidate_dir / "astana_v2",
    )
    v2_test_features = prepare_features(test_frame, v2_features, v2_categorical)
    v2_predictions = candidate_predictions(v2_models, v2_offsets, v2_test_features)

    universal_service = PriceModelService(
        models_dir=ROOT / "models" / "universal_v2",
        metadata_path=ROOT / "models" / "universal_v2" / "model_metadata.json",
    )
    universal_frame = universal_service.predict(test_frame)
    universal_predictions = {
        label: universal_frame.predictions[
            f"pred_price_per_m2_log_{label}"
        ].to_numpy()
        for label in MODEL_OBJECTIVES
    }

    predictions_by_model = {
        "astana_v1_retrained": v1_predictions,
        "astana_v2": v2_predictions,
        "universal_v2": universal_predictions,
    }
    overall = {
        name: regression_metrics(actual_test, predictions)
        for name, predictions in predictions_by_model.items()
    }
    segments = {
        name: segment_metrics(test_frame, actual_test, predictions)
        for name, predictions in predictions_by_model.items()
    }
    winner = min(overall, key=lambda name: overall[name]["log_rmse"])
    bootstrap = paired_cluster_bootstrap(
        test_frame,
        actual_test,
        predictions_by_model,
        replicates=args.bootstrap_replicates,
        random_seed=args.random_seed,
    )
    scraped_at = pd.to_datetime(frame.get("scraped_at"), errors="coerce", utc=True)
    known_scraped_at = scraped_at.dropna()
    payload = {
        "comparison_version": 1,
        "question": (
            "Which sale-price model is strongest for Astana on one common "
            "property-group holdout?"
        ),
        "dataset": {
            "path": str(args.dataset.relative_to(ROOT)),
            "rows": int(len(frame)),
            "unique_listing_urls": int(frame["listing_url"].nunique()),
            "unique_property_groups": int(frame["property_group"].nunique()),
            "scraped_at_known_rows": int(scraped_at.notna().sum()),
            "scraped_at_known_pct": float(scraped_at.notna().mean() * 100),
            "scraped_at_min": (
                known_scraped_at.min().isoformat() if not known_scraped_at.empty else None
            ),
            "scraped_at_max": (
                known_scraped_at.max().isoformat() if not known_scraped_at.empty else None
            ),
            "rows_by_split": {key: int(value) for key, value in split_counts.items()},
            "raw_inputs": [str(path.relative_to(ROOT)) for path in raw_inputs],
            "price_per_m2_filter": [100_000, 5_000_000],
        },
        "training_parameters": {
            "iterations": args.iterations,
            "depth": args.depth,
            "learning_rate": args.learning_rate,
            "l2_leaf_reg": args.l2_leaf_reg,
            "early_stopping_rounds": args.early_stopping_rounds,
            "random_seed": args.random_seed,
            "objectives": MODEL_OBJECTIVES,
        },
        "model_definitions": {
            "astana_v1_retrained": {
                "training_scope": "astana",
                "features": len(v1_columns),
                "feature_contract": "legacy_astana_v1",
                "quantile_offsets_log": v1_offsets,
            },
            "astana_v2": {
                "training_scope": "astana",
                "features": len(v2_features),
                "feature_contract": "optimized_compact_v2",
                "quantile_offsets_log": v2_offsets,
            },
            "universal_v2": {
                "training_scope": "astana+almaty",
                "features": len(v2_features),
                "feature_contract": "optimized_compact_v2",
                "model_version": universal_service.metadata.model_version,
            },
        },
        "overall_test_metrics": overall,
        "non_comparable_reference_metrics": {
            "production_astana_v1": {
                **regression_metrics(actual_test, production_v1_predictions),
                "eligible_for_selection": False,
                "reason": (
                    "Historical training membership is unavailable and may overlap "
                    "the common v2 test rows."
                ),
                "model_version": production_v1_service.metadata.model_version,
            }
        },
        "room_segment_test_metrics": segments,
        "paired_cluster_bootstrap": bootstrap,
        "bootstrap_replicates": args.bootstrap_replicates,
        "winner": winner,
        "confidence": "share_with_caveats",
        "caveats": [
            "Grouped holdout is not a future-period test.",
            "Targets are public asking prices rather than transaction prices.",
            "Target-free feature mappings and the OSM catalog predate the split.",
            "Most legacy source rows lack scrape timestamps, limiting freshness validation.",
            "Small 5+ room segments have higher uncertainty.",
        ],
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(payload, args.output_md)
    print(f"[OK] Winner: {winner}")
    for name, metric in sorted(overall.items(), key=lambda item: item[1]["log_rmse"]):
        print(
            f"[RESULT] {name}: log_rmse={metric['log_rmse']:.6f}, "
            f"median_ape={metric['median_absolute_percentage_error_pct']:.2f}%"
        )
    print(f"[OK] JSON: {args.output_json}")
    print(f"[OK] Markdown: {args.output_md}")


if __name__ == "__main__":
    main()
