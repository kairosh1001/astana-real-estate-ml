from __future__ import annotations

from dataclasses import replace
import re

import numpy as np
import pandas as pd

from app.feature_pipeline import (
    FeatureConfig,
    build_feature_config,
    build_model_features,
)


SHARED_FEATURE_COLUMNS = [
    "ceiling_height",
    "year_of_construction",
    "district",
    "residential_complex",
    "furnished",
    "apartment_condition",
    "building_type",
    "rooms",
    "current_floor",
    "total_floors",
    "area_m2",
    "h3_res_7",
    "h3_res_8",
    "h3_res_9",
    "dist_to_nearest_mall_km",
    "dist_to_nearest_park_km",
    "dist_to_nearest_lrt_km",
    "dist_to_baiterek_km",
    "dist_to_botgarden_km",
    "dist_to_mangilikel_km",
    "dist_to_khanshatyr_km",
    "dist_to_expo_km",
    "floor_ratio",
]

RENTAL_FEATURE_COLUMNS = SHARED_FEATURE_COLUMNS + [
    "furnished_missing",
    "photo_count",
    "is_estate_verified",
    "is_booking_enabled",
    "guest_capacity",
    "bed_count",
    "sofa_count",
    "has_internet",
    "has_air_conditioning",
    "has_washing_machine",
    "has_parking",
    "has_elevator",
]

CATEGORICAL_FEATURES = [
    "district",
    "residential_complex",
    "furnished",
    "apartment_condition",
    "building_type",
    "h3_res_7",
    "h3_res_8",
    "h3_res_9",
]


def build_rental_feature_config(raw_df: pd.DataFrame) -> FeatureConfig:
    """Reuse the sale location contract without treating missing furniture as none."""
    return replace(build_feature_config(raw_df), furnished_fill="не указано")


def build_rental_features(
    raw_df: pd.DataFrame,
    config: FeatureConfig,
    *,
    include_target: bool = False,
) -> pd.DataFrame:
    snapshot_key = ["url", "scraped_at"] if "scraped_at" in raw_df.columns else ["url"]
    raw = raw_df.drop_duplicates(subset=snapshot_key).reset_index(drop=True).copy()
    furnished_source = _series(raw, "Квартира меблирована")
    base = build_model_features(
        raw,
        config,
        include_target=False,
        deduplicate=False,
    )

    result = base.copy()
    result["furnished_missing"] = furnished_source.map(_is_missing_text).astype(int)
    result["photo_count"] = pd.to_numeric(_series(raw, "photo_count"), errors="coerce").fillna(0)
    result["is_estate_verified"] = _series(raw, "is_estate_verified").map(_as_bool).astype(int)
    result["is_booking_enabled"] = _series(raw, "is_booking_enabled").map(_as_bool).astype(int)

    placement = _series(raw, "Возможности размещения").fillna("").astype(str)
    amenities = _series(raw, "Удобства").fillna("").astype(str).str.casefold()
    security = _series(raw, "Безопасность").fillna("").astype(str).str.casefold()

    result["guest_capacity"] = placement.map(
        lambda value: _number_before(value, ("гост", "человек"))
    )
    result["bed_count"] = placement.map(lambda value: _number_before(value, ("кроват",)))
    result["sofa_count"] = placement.map(lambda value: _number_before(value, ("диван",)))
    result["has_internet"] = amenities.str.contains("интернет", regex=False).astype(int)
    result["has_air_conditioning"] = amenities.str.contains("кондиционер", regex=False).astype(int)
    result["has_washing_machine"] = amenities.str.contains("стиральная машина", regex=False).astype(int)
    result["has_parking"] = security.str.contains("паркинг|парков", regex=True).astype(int)
    result["has_elevator"] = amenities.str.contains("лифт", regex=False).astype(int)

    if include_target:
        price = pd.to_numeric(_series(raw, "price"), errors="coerce")
        result["rent_total_log"] = np.log(price.where(price > 0))

    columns = list(RENTAL_FEATURE_COLUMNS)
    if include_target:
        columns.append("rent_total_log")
    return result.loc[:, columns].reset_index(drop=True)


def metadata_for_period(period: str) -> dict:
    if period not in {"monthly", "daily"}:
        raise ValueError("period must be 'monthly' or 'daily'")
    return {
        "market": "rent",
        "period": period,
        "target": "rent_total_log",
        "target_units": "KZT/month" if period == "monthly" else "KZT/day",
        "feature_columns": RENTAL_FEATURE_COLUMNS,
        "categorical_features": CATEGORICAL_FEATURES,
        "shared_sale_features": SHARED_FEATURE_COLUMNS,
    }


def _series(frame: pd.DataFrame, name: str) -> pd.Series:
    if name in frame.columns:
        return frame[name].reset_index(drop=True)
    return pd.Series(pd.NA, index=range(len(frame)), dtype="object")


def _is_missing_text(value: object) -> bool:
    if pd.isna(value):
        return True
    return str(value).strip().casefold() in {"", "n/a", "nan", "none", "не указано"}


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "да"}


def _number_before(value: object, stems: tuple[str, ...]) -> float:
    text = str(value or "").casefold()
    for stem in stems:
        match = re.search(rf"(\d+)\s+[^,;]*{re.escape(stem)}", text)
        if match:
            return float(match.group(1))
    return np.nan
