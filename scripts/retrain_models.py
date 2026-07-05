from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from sklearn.model_selection import train_test_split


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "df_check.csv"
DEFAULT_METADATA = ROOT / "model_metadata.json"
DEFAULT_OUTPUT_DIR = ROOT / "models_candidate"
MODEL_NAMES = {
    "q10": ("Quantile:alpha=0.1", "catboost_q10_price_per_m2_log.cbm"),
    "q50": ("Quantile:alpha=0.5", "catboost_q50_price_per_m2_log.cbm"),
    "q90": ("Quantile:alpha=0.9", "catboost_q90_price_per_m2_log.cbm"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train candidate CatBoost quantile models and write an evaluation report.",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--iterations", type=int, default=800)
    parser.add_argument("--depth", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def load_metadata(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        metadata = json.load(file)
    for key in ["feature_columns", "categorical_features", "target"]:
        if key not in metadata:
            raise ValueError(f"Missing metadata key: {key}")
    return metadata


def validate_dataset(frame: pd.DataFrame, metadata: dict) -> None:
    required = list(metadata["feature_columns"]) + [metadata["target"]]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"Dataset is missing required columns: {missing}")


def prepare_features(frame: pd.DataFrame, metadata: dict) -> pd.DataFrame:
    features = frame.loc[:, metadata["feature_columns"]].copy()
    categorical = set(metadata["categorical_features"])
    for column in features.columns:
        if column in categorical:
            features[column] = features[column].astype("string").fillna("missing")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")
    return features


def train_one_model(
    quantile: str,
    loss_function: str,
    train_x: pd.DataFrame,
    train_y: pd.Series,
    valid_x: pd.DataFrame,
    valid_y: pd.Series,
    categorical_features: list[str],
    args: argparse.Namespace,
) -> CatBoostRegressor:
    model = CatBoostRegressor(
        loss_function=loss_function,
        iterations=args.iterations,
        depth=args.depth,
        learning_rate=args.learning_rate,
        random_seed=args.random_seed,
        verbose=100,
        allow_writing_files=False,
    )
    cat_indices = [
        train_x.columns.get_loc(column)
        for column in categorical_features
        if column in train_x.columns
    ]
    model.fit(
        train_x,
        train_y,
        cat_features=cat_indices,
        eval_set=(valid_x, valid_y),
        use_best_model=True,
    )
    return model


def evaluate(valid_y_log: pd.Series, predictions: dict[str, np.ndarray]) -> dict:
    actual = np.exp(valid_y_log.to_numpy())
    q10 = np.exp(predictions["q10"])
    q50 = np.exp(predictions["q50"])
    q90 = np.exp(predictions["q90"])
    residual = q50 - actual
    return {
        "rows": int(len(actual)),
        "mae_per_m2": float(np.mean(np.abs(residual))),
        "rmse_per_m2": float(np.sqrt(np.mean(np.square(residual)))),
        "median_abs_error_per_m2": float(np.median(np.abs(residual))),
        "interval_coverage_q10_q90": float(np.mean((actual >= q10) & (actual <= q90))),
        "median_interval_width_pct": float(np.median((q90 - q10) / q50)),
    }


def write_report(output_dir: Path, metadata: dict, metrics: dict, args: argparse.Namespace) -> None:
    lines = [
        "# Candidate Model Evaluation",
        "",
        f"Created at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Dataset: `{args.dataset}`",
        f"Target: `{metadata['target']}`",
        f"Features: {len(metadata['feature_columns'])}",
        f"Categorical features: {len(metadata['categorical_features'])}",
        f"Validation rows: {metrics['rows']}",
        "",
        "## Validation Metrics",
        "",
        f"- MAE, KZT/m2: {metrics['mae_per_m2']:,.0f}",
        f"- RMSE, KZT/m2: {metrics['rmse_per_m2']:,.0f}",
        f"- Median absolute error, KZT/m2: {metrics['median_abs_error_per_m2']:,.0f}",
        f"- q10-q90 interval coverage: {metrics['interval_coverage_q10_q90']:.1%}",
        f"- Median interval width: {metrics['median_interval_width_pct']:.1%}",
        "",
        "## Promotion Checklist",
        "",
        "- Compare these metrics with the current production model.",
        "- Open a few predicted listings manually and check whether ranking looks reasonable.",
        "- Replace production files in `models/` only after the candidate passes review.",
        "- Restart the app and confirm `/model-version-page` shows the new file timestamps.",
        "",
    ]
    (output_dir / "evaluation_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    metadata = load_metadata(args.metadata)
    frame = pd.read_csv(args.dataset)
    validate_dataset(frame, metadata)
    frame = frame.dropna(subset=[metadata["target"]]).copy()

    features = prepare_features(frame, metadata)
    target = pd.to_numeric(frame[metadata["target"]], errors="coerce")
    mask = target.notna()
    features = features.loc[mask].reset_index(drop=True)
    target = target.loc[mask].reset_index(drop=True)

    train_x, valid_x, train_y, valid_y = train_test_split(
        features,
        target,
        test_size=args.test_size,
        random_state=args.random_seed,
    )

    run_dir = args.output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    predictions = {}
    for quantile, (loss_function, filename) in MODEL_NAMES.items():
        print(f"[INFO] Training {quantile} with {loss_function}")
        model = train_one_model(
            quantile,
            loss_function,
            train_x,
            train_y,
            valid_x,
            valid_y,
            list(metadata["categorical_features"]),
            args,
        )
        model.save_model(str(run_dir / filename))
        predictions[quantile] = model.predict(valid_x)

    metrics = evaluate(valid_y, predictions)
    (run_dir / "model_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_report(run_dir, metadata, metrics, args)
    print(f"[OK] Candidate models written to {run_dir}")
    print(f"[OK] Evaluation report: {run_dir / 'evaluation_report.md'}")


if __name__ == "__main__":
    main()
