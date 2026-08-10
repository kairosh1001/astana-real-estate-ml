from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline_v2 import (
    POI_CATEGORIES,
    POI_COUNT_RADII_KM,
    UNIVERSAL_FEATURE_COLUMNS,
    PoiCatalog,
    _radius_label,
    build_model_features_v2,
    build_universal_feature_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate universal feature pipeline v2.")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "app" / "data" / "kazakhstan_pois.json",
    )
    parser.add_argument("--astana-raw", type=Path, default=ROOT / "krisha_data_raw_orig.csv")
    return parser.parse_args()


def synthetic_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "url": "https://krisha.kz/a/show/v2-astana",
                "title": "2-комнатная квартира · 60 м² · 5/12 этаж",
                "price": "36000000",
                "lat": 51.1282,
                "lon": 71.4304,
                "Город": "Астана, Есильский р-н",
                "Жилой комплекс": "Test Residence",
                "Квартира меблирована": "полностью",
                "Состояние квартиры": "свежий ремонт",
                "Тип дома": "монолитный",
                "Год постройки": "2022",
                "Высота потолков": "2.8 м",
                "Новостройка": False,
            },
            {
                "url": "https://krisha.kz/a/show/v2-almaty",
                "title": "3-комнатная квартира · 85 м² · 7/10 этаж",
                "price": "68000000",
                "lat": 43.2389,
                "lon": 76.8897,
                "scrape_city": "almaty",
                "Город": "Алматы, Бостандыкский р-н",
                "Жилой комплекс": "Test Residence",
                "Квартира меблирована": "частично",
                "Состояние квартиры": "не новый, но аккуратный ремонт",
                "Тип дома": "кирпичный",
                "Год постройки": "2018",
                "Высота потолков": "3 м",
                "Новостройка": True,
            },
        ]
    )


def validate_frame(features: pd.DataFrame) -> None:
    expected = list(UNIVERSAL_FEATURE_COLUMNS) + ["price_per_m2_log"]
    if list(features.columns) != expected:
        raise AssertionError("Feature columns or order do not match v2 metadata")
    if features["city"].tolist() != ["astana", "almaty"]:
        raise AssertionError(f"City normalization failed: {features['city'].tolist()}")
    if features["city_district"].tolist() != [
        "astana__есиль",
        "almaty__бостандыкский",
    ]:
        raise AssertionError(
            f"City-aware district normalization failed: {features['city_district'].tolist()}"
        )
    if features["city_residential_complex"].nunique() != 2:
        raise AssertionError("The same ЖК name in two cities must remain two categories")
    if not np.allclose(features["dist_to_city_center_km"], 0, atol=0.05):
        raise AssertionError("City-center distance calculation is inconsistent")
    forbidden = ("baiterek", "expo", "khanshatyr", "lrt", "mangilikel")
    if any(any(token in column for token in forbidden) for column in features.columns):
        raise AssertionError("Astana-only landmark leaked into v2 features")
    for category in POI_CATEGORIES:
        distance = f"dist_to_nearest_{category}_km"
        if features[distance].isna().any():
            raise AssertionError(f"Missing {distance}; catalog coverage is incomplete")
        previous = None
        for radius in POI_COUNT_RADII_KM:
            column = f"count_{category}_within_{_radius_label(radius)}"
            if previous is not None and (features[column] < features[previous]).any():
                raise AssertionError(f"POI counts are not monotonic: {previous} -> {column}")
            previous = column


def main() -> None:
    args = parse_args()
    catalog = PoiCatalog.load(args.catalog)
    rows = synthetic_rows()
    config = build_universal_feature_config(rows)
    features = build_model_features_v2(
        rows,
        config,
        catalog,
        include_target=True,
        filter_training_rows=True,
    )
    validate_frame(features)
    print("[OK] Synthetic Astana + Almaty feature checks passed")

    if args.astana_raw.exists():
        raw = pd.read_csv(args.astana_raw, nrows=1000, low_memory=False)
        astana_config = build_universal_feature_config(raw)
        astana_features = build_model_features_v2(
            raw,
            astana_config,
            catalog,
            include_target=True,
            filter_training_rows=True,
        )
        if astana_features.empty:
            raise AssertionError("Existing Astana raw data produced no v2 rows")
        if set(astana_features["city"]) != {"astana"}:
            raise AssertionError("Existing Astana rows were assigned to the wrong city")
        print(
            f"[OK] Existing Astana smoke test: {len(astana_features):,} rows, "
            f"{len(UNIVERSAL_FEATURE_COLUMNS)} features"
        )
    print(f"[QA] Catalog generated at: {catalog.generated_at}")
    print(f"[QA] Catalog cities: {sorted(catalog.cities)}")


if __name__ == "__main__":
    main()
