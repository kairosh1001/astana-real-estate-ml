from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from app.feature_pipeline_v2 import (
    OPTIMIZED_MODEL_CATEGORICAL_FEATURES,
    OPTIMIZED_MODEL_FEATURE_COLUMNS,
    PoiCatalog,
    UniversalFeatureConfig,
    build_model_features_v2,
)


RENTAL_FEATURE_COLUMNS = list(OPTIMIZED_MODEL_FEATURE_COLUMNS)
RENTAL_CATEGORICAL_FEATURES = list(OPTIMIZED_MODEL_CATEGORICAL_FEATURES)
RENT_TOTAL_TARGET = "rent_total_log"
RENT_PER_M2_TARGET = "rent_per_m2_log"


def build_rental_features(
    raw_df: pd.DataFrame,
    config: UniversalFeatureConfig,
    catalog: PoiCatalog,
    *,
    include_target: bool = False,
) -> pd.DataFrame:
    """Build features shared by rental training rows and sale inference rows."""
    raw = raw_df.reset_index(drop=True).copy()
    result = build_model_features_v2(
        raw,
        config,
        catalog,
        include_target=False,
        filter_training_rows=False,
        deduplicate_listings=False,
    ).loc[:, RENTAL_FEATURE_COLUMNS]
    if include_target:
        prices = raw.get("price", pd.Series(np.nan, index=raw.index)).map(
            clean_rent_price
        )
        area = pd.to_numeric(result["area_m2"], errors="coerce")
        valid = prices.gt(0) & area.gt(0)
        result[RENT_TOTAL_TARGET] = np.where(valid, np.log(prices), np.nan)
        result[RENT_PER_M2_TARGET] = np.where(
            valid,
            np.log(prices / area),
            np.nan,
        )
    return result.reset_index(drop=True)


def rental_model_metadata(
    catalog: PoiCatalog,
    *,
    target_mode: str,
) -> dict[str, Any]:
    if target_mode not in {"total", "per_m2"}:
        raise ValueError("target_mode must be 'total' or 'per_m2'")
    return {
        "market": "rent",
        "period": "monthly",
        "feature_profile": "optimized_compact_v2_shared_sale_features",
        "feature_columns": RENTAL_FEATURE_COLUMNS,
        "categorical_features": RENTAL_CATEGORICAL_FEATURES,
        "target_mode": target_mode,
        "target": RENT_TOTAL_TARGET if target_mode == "total" else RENT_PER_M2_TARGET,
        "served_prediction_units": "KZT/month",
        "poi_catalog": catalog.metadata(),
    }


def clean_rent_price(value: object) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    digits = re.sub(r"\D", "", str(value))
    try:
        return float(digits)
    except ValueError:
        return float("nan")
