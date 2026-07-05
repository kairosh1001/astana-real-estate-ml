from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import h3
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree


RAW_COLUMNS = {
    "area": "Площадь",
    "building_type": "Тип дома",
    "ceiling_height": "Высота потолков",
    "city": "Город",
    "condition": "Состояние квартиры",
    "construction_year": "Год постройки",
    "furnished": "Квартира меблирована",
    "residential_complex": "Жилой комплекс",
}

VALID_DISTRICTS = [
    "Алматы",
    "Есиль",
    "Есильский",
    "Сарыарка",
    "Байконур",
    "Нура",
    "Сарайшык",
]

PARK_COORDS = np.array(
    [
        [51.1577, 71.420],
        [51.137, 71.444],
        [51.126, 71.464],
        [51.134, 71.455],
        [51.132, 71.413],
        [51.119, 71.426],
        [51.113, 71.441],
        [51.102, 71.447],
        [51.152, 71.433],
        [51.107, 71.418],
    ]
)

MALL_COORDS = np.array(
    [
        [51.133, 71.404],
        [51.090, 71.408],
        [51.144, 71.478],
        [51.128, 71.425],
        [51.129, 71.414],
        [51.147, 71.421],
        [51.150, 71.480],
        [51.125, 71.444],
        [51.142, 71.423],
    ]
)

LRT_COORDS = np.array(
    [
        [51.027906, 71.458873],
        [51.041283, 71.442192],
        [51.048920, 71.429869],
        [51.055830, 71.418694],
        [51.075065, 71.397861],
        [51.081080, 71.400024],
        [51.088682, 71.402750],
        [51.099894, 71.406754],
        [51.106004, 71.408958],
        [51.114453, 71.411546],
        [51.121938, 71.413069],
        [51.123131, 71.429283],
        [51.122165, 71.437347],
        [51.117121, 71.467841],
        [51.114980, 71.481519],
        [51.112370, 71.497967],
        [51.109498, 71.516229],
        [51.112090, 71.528945],
    ]
)

LANDMARK_COORDS = {
    "baiterek": np.array([[51.128, 71.431]]),
    "botgarden": np.array([[51.106, 71.416]]),
    "mangilikel": np.array([[51.104, 71.430]]),
    "khanshatyr": np.array([[51.133, 71.404]]),
    "expo": np.array([[51.089, 71.416]]),
}

EARTH_RADIUS_KM = 6371.0


@dataclass
class FeatureConfig:
    building_type_fill: str = "монолитный"
    ceiling_height_fill: float = 2.8974719749983016
    current_floor_fill: int = 6
    total_floors_fill: int = 10
    furnished_fill: str = "без мебели"
    condition_fill: str = "Не указано"
    residential_complex_fill: str = "Панельный дом/Не указан"
    complex_to_district: dict[str, str] = field(default_factory=dict)
    manual_district_by_index: dict[int, str] = field(
        default_factory=lambda: {
            3237: "Есильский",
            8837: "Алматы",
            8980: "Алматы",
            16695: "Нура",
            19953: "Есильский",
        }
    )


def build_feature_config(raw_df: pd.DataFrame) -> FeatureConfig:
    if {"district", "residential_complex"}.issubset(raw_df.columns):
        mapping = (
            raw_df.dropna(subset=["district"])
            .drop_duplicates("residential_complex")
            .set_index("residential_complex")["district"]
            .to_dict()
        )
        return FeatureConfig(complex_to_district=mapping)

    frame = _prepare_base_frame(raw_df, FeatureConfig(), build_mapping=False)
    mapping = (
        frame.dropna(subset=["district"])
        .drop_duplicates("residential_complex")
        .set_index("residential_complex")["district"]
        .to_dict()
    )
    return FeatureConfig(complex_to_district=mapping)


def build_model_features(
    raw_df: pd.DataFrame,
    config: FeatureConfig,
    *,
    include_target: bool = False,
    filter_training_rows: bool = False,
) -> pd.DataFrame:
    frame = _prepare_base_frame(raw_df, config, build_mapping=True)
    frame = _add_spatial_features(frame)
    frame["floor_ratio"] = frame["current_floor"] / frame["total_floors"]

    if filter_training_rows:
        frame = frame[
            (frame["dist_to_nearest_mall_km"] < 30)
            & (frame["dist_to_nearest_lrt_km"] < 30)
            & (frame["dist_to_nearest_park_km"] < 30)
            & (frame["dist_to_baiterek_km"] < 30)
        ].copy()

    if include_target:
        price = _clean_price(frame["price"])
        frame["price_per_m2_log"] = np.log(price / frame["area_m2"])

    columns = [
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
    if include_target:
        columns.append("price_per_m2_log")

    return frame.loc[:, columns].reset_index(drop=True)


def _prepare_base_frame(
    raw_df: pd.DataFrame,
    config: FeatureConfig,
    *,
    build_mapping: bool,
) -> pd.DataFrame:
    frame = raw_df.drop_duplicates(subset="url").copy()
    frame = _ensure_raw_columns(frame)
    frame["lat"] = pd.to_numeric(frame["lat"], errors="coerce")
    frame["lon"] = pd.to_numeric(frame["lon"], errors="coerce")

    frame["rooms"] = frame["title"].str.extract(r"(\d+)-комнатная").astype(float)
    floor_data = frame["title"].str.extract(r"(\d+)/(\d+)\s+этаж")
    frame["current_floor"] = pd.to_numeric(floor_data[0], errors="coerce")
    frame["total_floors"] = pd.to_numeric(floor_data[1], errors="coerce")
    frame["area_m2"] = _extract_first_number(frame[RAW_COLUMNS["area"]])

    frame[RAW_COLUMNS["building_type"]] = frame[
        RAW_COLUMNS["building_type"]
    ].fillna(config.building_type_fill)
    frame[RAW_COLUMNS["ceiling_height"]] = _clean_numeric(
        frame[RAW_COLUMNS["ceiling_height"]]
    ).fillna(config.ceiling_height_fill)
    frame[RAW_COLUMNS["furnished"]] = frame[RAW_COLUMNS["furnished"]].fillna(
        config.furnished_fill
    )
    frame[RAW_COLUMNS["condition"]] = frame[RAW_COLUMNS["condition"]].fillna(
        config.condition_fill
    )
    frame["current_floor"] = frame["current_floor"].fillna(
        config.current_floor_fill
    )
    frame["total_floors"] = frame["total_floors"].fillna(config.total_floors_fill)

    district = frame[RAW_COLUMNS["city"]].str.replace(
        r"^Астана,\s*", "", regex=True
    )
    district = district.str.replace(r"\s*р-н\s*", "", regex=True)
    district = district.where(district.isin(VALID_DISTRICTS), np.nan)

    frame = frame.rename(
        columns={
            RAW_COLUMNS["ceiling_height"]: "ceiling_height",
            RAW_COLUMNS["construction_year"]: "year_of_construction",
            RAW_COLUMNS["residential_complex"]: "residential_complex",
            RAW_COLUMNS["furnished"]: "furnished",
            RAW_COLUMNS["condition"]: "apartment_condition",
            RAW_COLUMNS["building_type"]: "building_type",
        }
    )
    frame["district"] = district
    frame["residential_complex"] = frame["residential_complex"].fillna(
        config.residential_complex_fill
    )

    for resolution in (7, 8, 9):
        frame[f"h3_res_{resolution}"] = frame.apply(
            lambda row: h3.latlng_to_cell(row["lat"], row["lon"], resolution)
            if pd.notnull(row["lat"]) and pd.notnull(row["lon"])
            else np.nan,
            axis=1,
        )

    if build_mapping:
        frame["district"] = frame["district"].fillna(
            frame["residential_complex"].map(config.complex_to_district)
        )
        for index, value in config.manual_district_by_index.items():
            if index in frame.index:
                frame.at[index, "district"] = value

    return frame


def _ensure_raw_columns(frame: pd.DataFrame) -> pd.DataFrame:
    required_columns = ["url", "title", "price", "lat", "lon", *RAW_COLUMNS.values()]
    for column in required_columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame


def _add_spatial_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    apt_coords_rad = np.radians(result[["lat", "lon"]].values)

    nearest_sets = {
        "dist_to_nearest_mall_km": MALL_COORDS,
        "dist_to_nearest_park_km": PARK_COORDS,
        "dist_to_nearest_lrt_km": LRT_COORDS,
    }
    for column, coords in nearest_sets.items():
        result[column] = _nearest_distance_km(apt_coords_rad, coords)

    for name, coords in LANDMARK_COORDS.items():
        result[f"dist_to_{name}_km"] = _nearest_distance_km(apt_coords_rad, coords)

    return result


def _nearest_distance_km(apt_coords_rad: np.ndarray, coords: np.ndarray) -> np.ndarray:
    tree = BallTree(np.radians(coords), metric="haversine")
    distances_rad, _ = tree.query(apt_coords_rad, k=1)
    return (distances_rad * EARTH_RADIUS_KM).flatten()


def _clean_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype("string").str.replace(r"[^\d.]", "", regex=True),
        errors="coerce",
    )


def _extract_first_number(series: pd.Series) -> pd.Series:
    extracted = series.astype("string").str.extract(r"(\d+\.?\d*)")[0]
    return pd.to_numeric(extracted, errors="coerce")


def _clean_price(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace("от", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")
