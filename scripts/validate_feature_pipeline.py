from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline import build_feature_config, build_model_features


def main() -> None:
    raw_paths = [
        ROOT / "krisha_data_raw_orig.csv",
        ROOT / "krisha_data_raw.csv",
    ]
    missing_raw = [path.name for path in raw_paths if not path.exists()]
    if missing_raw:
        print(
            "Feature pipeline validation skipped because raw scrape snapshots "
            f"are not present: {missing_raw}."
        )
        print("The public repository keeps df_check.csv as the model-ready snapshot.")
        return

    raw = pd.concat(
        [pd.read_csv(path) for path in raw_paths],
        ignore_index=True,
    )
    expected = pd.read_csv(ROOT / "df_check.csv")

    config = build_feature_config(raw)
    actual = build_model_features(
        raw,
        config,
        include_target=True,
        filter_training_rows=True,
    )

    print("Feature pipeline validation")
    print(f"Expected shape: {expected.shape}")
    print(f"Actual shape:   {actual.shape}")

    if list(actual.columns) != list(expected.columns):
        missing = [column for column in expected.columns if column not in actual.columns]
        extra = [column for column in actual.columns if column not in expected.columns]
        print(f"Missing columns: {missing}")
        print(f"Extra columns:   {extra}")
        raise SystemExit(1)

    row_count = min(len(expected), len(actual))
    if len(expected) != len(actual):
        print("Row count mismatch; comparing overlapping rows only.")

    numeric_columns = [
        column for column in expected.columns if pd.api.types.is_numeric_dtype(expected[column])
    ]
    categorical_columns = [
        column for column in expected.columns if column not in numeric_columns
    ]

    numeric_diffs = {}
    for column in numeric_columns:
        expected_values = expected[column].iloc[:row_count].to_numpy(dtype=float)
        actual_values = actual[column].iloc[:row_count].to_numpy(dtype=float)
        max_abs_diff = float(np.nanmax(np.abs(expected_values - actual_values)))
        numeric_diffs[column] = max_abs_diff

    categorical_mismatches = {}
    for column in categorical_columns:
        expected_values = expected[column].iloc[:row_count].astype("string").fillna("<NA>")
        actual_values = actual[column].iloc[:row_count].astype("string").fillna("<NA>")
        mismatch_count = int((expected_values.reset_index(drop=True) != actual_values.reset_index(drop=True)).sum())
        categorical_mismatches[column] = mismatch_count

    print("\nMax absolute numeric diffs:")
    for column, value in numeric_diffs.items():
        print(f"  {column}: {value:.12g}")

    print("\nCategorical mismatches:")
    for column, value in categorical_mismatches.items():
        print(f"  {column}: {value}")

    failed_numeric = {
        column: value
        for column, value in numeric_diffs.items()
        if value > 1e-5
    }
    failed_categorical = {
        column: value for column, value in categorical_mismatches.items() if value
    }

    if failed_numeric or failed_categorical or len(expected) != len(actual):
        print("\nPipeline does not exactly match saved df_check.csv yet.")
        raise SystemExit(1)

    print("\nPipeline matches saved df_check.csv.")


if __name__ == "__main__":
    main()
