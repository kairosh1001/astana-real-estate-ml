from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import h3
import numpy as np
import pandas as pd
from sklearn.neighbors import BallTree


FEATURE_SCHEMA_VERSION = 2
EARTH_RADIUS_KM = 6371.0088
H3_RESOLUTIONS = (7, 8, 9)
POI_CATEGORIES = (
    "park",
    "school",
    "kindergarten",
    "grocery",
    "mall",
    "healthcare",
    "transit",
    "university",
)
POI_COUNT_RADII_KM = (0.5, 1.0, 2.0)

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "area": ("area_m2_structured", "Площадь", "area_m2", "area"),
    "building_type": ("Тип дома", "building_type"),
    "ceiling_height": ("Высота потолков", "ceiling_height"),
    "city_location": ("Город", "city_location"),
    "condition": ("Состояние квартиры", "apartment_condition", "condition"),
    "construction_year": ("Год постройки", "year_of_construction", "construction_year"),
    "district": ("Район", "district"),
    "floor": ("Этаж", "floor"),
    "furnished": ("Квартира меблирована", "furnished"),
    "new_build": ("Новостройка", "is_new_build", "new_build"),
    "residential_complex": ("Жилой комплекс", "residential_complex"),
    "rooms": ("rooms_structured", "rooms"),
    "scrape_city": ("scrape_city", "city"),
}

CITY_ALIASES = {
    "astana": {"astana", "астана", "нур-султан", "nur-sultan"},
    "almaty": {"almaty", "алматы", "алма-ата", "alma-ata"},
}

DISTRICT_ALIASES: dict[str, dict[str, str]] = {
    "astana": {
        "есильский": "есиль",
        "есиль": "есиль",
        "нура": "нура",
        "сарыарка": "сарыарка",
        "алматы": "алматы",
        "байконур": "байконур",
        "сарайшык": "сарайшык",
    },
    "almaty": {
        "алатауский": "алатауский",
        "алмалинский": "алмалинский",
        "ауэзовский": "ауэзовский",
        "бостандыкский": "бостандыкский",
        "жетысуский": "жетысуский",
        "медеуский": "медеуский",
        "наурызбайский": "наурызбайский",
        "турксибский": "турксибский",
    },
}


def _radius_label(radius_km: float) -> str:
    if radius_km < 1:
        return f"{round(radius_km * 1000):d}m"
    return f"{radius_km:g}km"


BASE_FEATURE_COLUMNS = [
    "city",
    "city_district",
    "city_residential_complex",
    "ceiling_height",
    "year_of_construction",
    "furnished",
    "apartment_condition",
    "building_type",
    "is_new_build",
    "rooms",
    "current_floor",
    "total_floors",
    "area_m2",
    "floor_ratio",
    "year_missing",
    "ceiling_height_missing",
    "district_missing",
    "residential_complex_missing",
    *[f"h3_res_{resolution}" for resolution in H3_RESOLUTIONS],
    "dist_to_city_center_km",
    "dist_to_city_center_normalized",
]

POI_FEATURE_COLUMNS = [
    feature
    for category in POI_CATEGORIES
    for feature in (
        f"dist_to_nearest_{category}_km",
        *(
            f"count_{category}_within_{_radius_label(radius)}"
            for radius in POI_COUNT_RADII_KM
        ),
    )
]

UNIVERSAL_FEATURE_COLUMNS = BASE_FEATURE_COLUMNS + POI_FEATURE_COLUMNS
UNIVERSAL_CATEGORICAL_FEATURES = [
    "city",
    "city_district",
    "city_residential_complex",
    "furnished",
    "apartment_condition",
    "building_type",
    *[f"h3_res_{resolution}" for resolution in H3_RESOLUTIONS],
]
TARGET_COLUMN = "price_per_m2_log"
DATASET_METADATA_COLUMNS = ["listing_url", "scraped_at", "scrape_partition"]


@dataclass
class UniversalFeatureConfig:
    schema_version: int = FEATURE_SCHEMA_VERSION
    complex_to_district: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UniversalFeatureConfig":
        return cls(
            schema_version=int(value.get("schema_version", FEATURE_SCHEMA_VERSION)),
            complex_to_district=dict(value.get("complex_to_district") or {}),
        )

    @classmethod
    def load(cls, path: Path | str) -> "UniversalFeatureConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@dataclass(frozen=True)
class PoiCatalog:
    schema_version: int
    generated_at: str | None
    source: str
    sha256: str
    cities: dict[str, dict[str, Any]]
    groups: dict[tuple[str, str], np.ndarray]

    @classmethod
    def load(cls, path: Path | str) -> "PoiCatalog":
        catalog_path = Path(path)
        catalog_bytes = catalog_path.read_bytes()
        raw = json.loads(catalog_bytes.decode("utf-8"))
        schema_version = int(raw.get("schema_version") or 0)
        if schema_version < FEATURE_SCHEMA_VERSION:
            raise ValueError(
                f"POI catalog {catalog_path} uses schema {schema_version}; "
                f"feature pipeline v2 requires schema {FEATURE_SCHEMA_VERSION}."
            )
        cities = raw.get("cities") or {}
        if not cities:
            raise ValueError(f"POI catalog has no city metadata: {catalog_path}")
        grouped_items: dict[tuple[str, str], list[list[float]]] = {}
        for item in raw.get("items") or []:
            city = str(item.get("city") or "")
            category = str(item.get("category") or "")
            if city not in cities or category not in POI_CATEGORIES:
                continue
            try:
                coordinate = [float(item["lat"]), float(item["lon"])]
            except (KeyError, TypeError, ValueError):
                continue
            grouped_items.setdefault((city, category), []).append(coordinate)
        groups = {
            key: np.asarray(coordinates, dtype=float).reshape((-1, 2))
            for key, coordinates in grouped_items.items()
        }
        return cls(
            schema_version=schema_version,
            generated_at=raw.get("generated_at"),
            source=str(raw.get("source") or "OpenStreetMap contributors"),
            sha256=hashlib.sha256(catalog_bytes).hexdigest(),
            cities=dict(cities),
            groups=groups,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "source": self.source,
            "sha256": self.sha256,
            "cities": {
                city: {
                    "counts": metadata.get("counts") or {},
                    "missing_categories": metadata.get("missing_categories") or [],
                }
                for city, metadata in self.cities.items()
            },
        }


def build_universal_feature_config(raw_df: pd.DataFrame) -> UniversalFeatureConfig:
    prepared = _prepare_base_frame(raw_df, config=None)
    mapping_frame = prepared[
        prepared["city"].ne("missing")
        & prepared["city_district"].ne("missing")
        & prepared["city_residential_complex"].ne("missing")
    ]
    if mapping_frame.empty:
        return UniversalFeatureConfig()
    mapping = (
        mapping_frame.groupby("city_residential_complex")["city_district"]
        .agg(lambda values: values.mode().iloc[0])
        .to_dict()
    )
    return UniversalFeatureConfig(complex_to_district=mapping)


def build_model_features_v2(
    raw_df: pd.DataFrame,
    config: UniversalFeatureConfig,
    catalog: PoiCatalog,
    *,
    include_target: bool = False,
    filter_training_rows: bool = False,
    deduplicate_listings: bool = True,
    include_metadata: bool = False,
) -> pd.DataFrame:
    source = _deduplicate_raw(raw_df) if deduplicate_listings else raw_df.copy()
    frame = _prepare_base_frame(source, config=config)
    frame = _add_h3_features(frame)
    frame = _add_city_center_features(frame, catalog)
    frame = _add_poi_features(frame, catalog)
    frame["floor_ratio"] = frame["current_floor"] / frame["total_floors"]
    frame["floor_ratio"] = frame["floor_ratio"].where(frame["total_floors"] > 0)

    price = _clean_numeric(_series(raw_df=frame, canonical="price"))
    valid_target = ((price > 0) & (frame["area_m2"] > 0)).fillna(False)
    if include_target:
        frame[TARGET_COLUMN] = np.where(
            valid_target,
            np.log(price / frame["area_m2"]),
            np.nan,
        )

    if filter_training_rows:
        valid = (
            frame["city"].isin(catalog.cities)
            & frame["lat"].between(-90, 90)
            & frame["lon"].between(-180, 180)
            & frame["area_m2"].between(10, 1000)
            & frame["rooms"].between(1, 20)
        ).fillna(False)
        if include_target:
            valid &= valid_target & np.isfinite(frame[TARGET_COLUMN])
        frame = frame.loc[valid].copy()

    columns = list(UNIVERSAL_FEATURE_COLUMNS)
    if include_target:
        columns.append(TARGET_COLUMN)
    if include_metadata:
        frame["listing_url"] = frame["url"].astype("string")
        if "scraped_at" not in frame.columns:
            frame["scraped_at"] = pd.NA
        if "scrape_partition" not in frame.columns:
            frame["scrape_partition"] = pd.NA
        columns = list(DATASET_METADATA_COLUMNS) + columns
    return frame.loc[:, columns].reset_index(drop=True)


def universal_model_metadata(catalog: PoiCatalog | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "feature_columns": list(UNIVERSAL_FEATURE_COLUMNS),
        "categorical_features": list(UNIVERSAL_CATEGORICAL_FEATURES),
        "target": TARGET_COLUMN,
        "target_definition": "ln(listing_price_kzt / area_m2)",
        "poi_distance_metric": "haversine_km_to_osm_representative_point",
        "poi_count_radii_km": list(POI_COUNT_RADII_KM),
    }
    if catalog is not None:
        metadata["poi_catalog"] = catalog.metadata()
    return metadata


def _deduplicate_raw(raw_df: pd.DataFrame) -> pd.DataFrame:
    frame = raw_df.copy()
    if "url" not in frame.columns:
        return frame
    if "scraped_at" in frame.columns:
        frame["_scraped_order"] = pd.to_datetime(
            frame["scraped_at"], errors="coerce", utc=True
        )
        frame = frame.sort_values("_scraped_order", kind="stable")
    frame = frame.drop_duplicates(subset="url", keep="last")
    return frame.drop(columns=["_scraped_order"], errors="ignore")


def _prepare_base_frame(
    raw_df: pd.DataFrame,
    config: UniversalFeatureConfig | None,
) -> pd.DataFrame:
    frame = raw_df.copy()
    for required in ("url", "title", "price", "lat", "lon"):
        if required not in frame.columns:
            frame[required] = pd.NA
    frame["lat"] = pd.to_numeric(frame["lat"], errors="coerce")
    frame["lon"] = pd.to_numeric(frame["lon"], errors="coerce")
    frame["city"] = _extract_city(frame)

    raw_district = _coalesce(frame, COLUMN_ALIASES["district"])
    location = _coalesce(frame, COLUMN_ALIASES["city_location"])
    district = pd.Series(index=frame.index, dtype="string")
    for index in frame.index:
        city = str(frame.at[index, "city"])
        explicit = raw_district.at[index]
        value = explicit if _known(explicit) else _district_from_location(location.at[index])
        district.at[index] = _normalize_district(city, value)
    frame["district_missing"] = district.isna().astype("int8")
    frame["city_district"] = [
        f"{city}__{value}" if city != "missing" and _known(value) else "missing"
        for city, value in zip(frame["city"], district)
    ]

    complex_name = _coalesce(frame, COLUMN_ALIASES["residential_complex"])
    normalized_complex = complex_name.map(_normalize_category_value)
    frame["residential_complex_missing"] = normalized_complex.isna().astype("int8")
    frame["city_residential_complex"] = [
        f"{city}__{value}" if city != "missing" and _known(value) else "missing"
        for city, value in zip(frame["city"], normalized_complex)
    ]
    if config is not None:
        missing_district = frame["city_district"].eq("missing")
        mapped = frame["city_residential_complex"].map(config.complex_to_district)
        frame.loc[missing_district & mapped.notna(), "city_district"] = mapped
        frame["district_missing"] = frame["city_district"].eq("missing").astype("int8")

    title = frame["title"].astype("string")
    rooms_structured = _clean_numeric(_coalesce(frame, COLUMN_ALIASES["rooms"]))
    rooms_title = pd.to_numeric(
        title.str.extract(r"(\d+)\s*-\s*комнат", flags=re.IGNORECASE)[0],
        errors="coerce",
    )
    frame["rooms"] = rooms_structured.fillna(rooms_title)

    floor_title = title.str.extract(r"(\d+)\s*/\s*(\d+)\s*этаж", flags=re.IGNORECASE)
    floor_raw = _coalesce(frame, COLUMN_ALIASES["floor"]).astype("string")
    floor_fallback = floor_raw.str.extract(r"(\d+)\D+(\d+)")
    frame["current_floor"] = pd.to_numeric(
        floor_title[0].fillna(floor_fallback[0]), errors="coerce"
    )
    frame["total_floors"] = pd.to_numeric(
        floor_title[1].fillna(floor_fallback[1]), errors="coerce"
    )
    area_raw = _clean_numeric(_coalesce(frame, COLUMN_ALIASES["area"]))
    area_title = pd.to_numeric(
        title.str.extract(r"(\d+(?:[\.,]\d+)?)\s*м²", flags=re.IGNORECASE)[0]
        .str.replace(",", ".", regex=False),
        errors="coerce",
    )
    frame["area_m2"] = area_raw.fillna(area_title)

    ceiling = _clean_numeric(_coalesce(frame, COLUMN_ALIASES["ceiling_height"]))
    frame["ceiling_height"] = ceiling.where(ceiling.between(2.0, 10.0))
    frame["ceiling_height_missing"] = frame["ceiling_height"].isna().astype("int8")
    year = _clean_numeric(_coalesce(frame, COLUMN_ALIASES["construction_year"]))
    frame["year_of_construction"] = year.where(year.between(1800, 2100))
    frame["year_missing"] = frame["year_of_construction"].isna().astype("int8")

    frame["furnished"] = _categorical(_coalesce(frame, COLUMN_ALIASES["furnished"]))
    frame["apartment_condition"] = _categorical(
        _coalesce(frame, COLUMN_ALIASES["condition"])
    )
    frame["building_type"] = _categorical(
        _coalesce(frame, COLUMN_ALIASES["building_type"])
    )
    frame["is_new_build"] = _coalesce(frame, COLUMN_ALIASES["new_build"]).map(
        _parse_bool
    )
    return frame


def _extract_city(frame: pd.DataFrame) -> pd.Series:
    scrape_city = _coalesce(frame, COLUMN_ALIASES["scrape_city"])
    location = _coalesce(frame, COLUMN_ALIASES["city_location"])
    result = pd.Series("missing", index=frame.index, dtype="string")
    for index in frame.index:
        result.at[index] = _normalize_city(scrape_city.at[index], location.at[index])
    return result


def _normalize_city(explicit: object, location: object) -> str:
    for value in (explicit, location):
        if not _known(value):
            continue
        cleaned = re.sub(r"\s+", " ", str(value)).strip().casefold()
        prefix = cleaned.split(",", 1)[0].strip()
        for city, aliases in CITY_ALIASES.items():
            if cleaned in aliases or prefix in aliases:
                return city
    return "missing"


def _district_from_location(value: object) -> str | None:
    if not _known(value):
        return None
    text = str(value).split(",", 1)
    return text[1].strip() if len(text) == 2 else None


def _normalize_district(city: str, value: object) -> str | None:
    if not _known(value) or city == "missing":
        return None
    cleaned = str(value).casefold().replace("ё", "е")
    cleaned = re.sub(r"\bр\s*-?\s*н\b", "", cleaned)
    cleaned = re.sub(r"\bрайон\b", "", cleaned)
    cleaned = re.sub(r"[^\w\-]+", " ", cleaned, flags=re.UNICODE).strip()
    if not cleaned:
        return None
    return DISTRICT_ALIASES.get(city, {}).get(cleaned, cleaned)


def _normalize_category_value(value: object) -> str | None:
    if not _known(value):
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip().casefold()
    return cleaned or None


def _categorical(series: pd.Series) -> pd.Series:
    normalized = series.map(_normalize_category_value)
    return normalized.astype("string").fillna("missing")


def _parse_bool(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if not _known(value):
        return -1
    cleaned = str(value).strip().casefold()
    if cleaned in {"1", "true", "да", "yes", "новостройка"}:
        return 1
    if cleaned in {"0", "false", "нет", "no", "вторичное жилье"}:
        return 0
    return -1


def _add_h3_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    valid = result["lat"].between(-90, 90) & result["lon"].between(-180, 180)
    for resolution in H3_RESOLUTIONS:
        column = pd.Series("missing", index=result.index, dtype="string")
        for index in result.index[valid]:
            column.at[index] = h3.latlng_to_cell(
                float(result.at[index, "lat"]),
                float(result.at[index, "lon"]),
                resolution,
            )
        result[f"h3_res_{resolution}"] = column
    return result


def _add_city_center_features(frame: pd.DataFrame, catalog: PoiCatalog) -> pd.DataFrame:
    result = frame.copy()
    distance = pd.Series(np.nan, index=result.index, dtype=float)
    normalized = pd.Series(np.nan, index=result.index, dtype=float)
    for city, city_metadata in catalog.cities.items():
        mask = result["city"].eq(city) & result["lat"].notna() & result["lon"].notna()
        if not mask.any():
            continue
        center = city_metadata.get("center") or {}
        center_lat = float(center["lat"])
        center_lon = float(center["lon"])
        values = _haversine_vector_km(
            result.loc[mask, "lat"].to_numpy(dtype=float),
            result.loc[mask, "lon"].to_numpy(dtype=float),
            center_lat,
            center_lon,
        )
        distance.loc[mask] = values
        radius = float(city_metadata.get("normalization_radius_km") or 1.0)
        normalized.loc[mask] = values / radius
    result["dist_to_city_center_km"] = distance
    result["dist_to_city_center_normalized"] = normalized
    return result


def _add_poi_features(frame: pd.DataFrame, catalog: PoiCatalog) -> pd.DataFrame:
    result = frame.copy()
    for column in POI_FEATURE_COLUMNS:
        result[column] = np.nan
    for city in catalog.cities:
        valid = (
            result["city"].eq(city)
            & result["lat"].between(-90, 90)
            & result["lon"].between(-180, 180)
        )
        if not valid.any():
            continue
        apartment_coords = np.radians(
            result.loc[valid, ["lat", "lon"]].to_numpy(dtype=float)
        )
        for category in POI_CATEGORIES:
            poi_coords = catalog.groups.get((city, category))
            if poi_coords is None or not len(poi_coords):
                continue
            tree = BallTree(np.radians(poi_coords), metric="haversine")
            nearest_radians, _ = tree.query(apartment_coords, k=1)
            result.loc[valid, f"dist_to_nearest_{category}_km"] = (
                nearest_radians[:, 0] * EARTH_RADIUS_KM
            )
            for radius in POI_COUNT_RADII_KM:
                counts = tree.query_radius(
                    apartment_coords,
                    r=radius / EARTH_RADIUS_KM,
                    count_only=True,
                )
                result.loc[
                    valid, f"count_{category}_within_{_radius_label(radius)}"
                ] = counts.astype(float)
    return result


def _haversine_vector_km(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    target_latitude: float,
    target_longitude: float,
) -> np.ndarray:
    lat1 = np.radians(latitudes)
    lon1 = np.radians(longitudes)
    lat2 = math.radians(target_latitude)
    lon2 = math.radians(target_longitude)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        np.sin(delta_lat / 2) ** 2
        + np.cos(lat1) * math.cos(lat2) * np.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(value))


def _coalesce(frame: pd.DataFrame, aliases: Iterable[str]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for column in aliases:
        if column in frame.columns:
            values = frame[column]
            result = result.where(result.notna(), values)
    return result


def _series(raw_df: pd.DataFrame, canonical: str) -> pd.Series:
    if canonical in raw_df.columns:
        return raw_df[canonical]
    return pd.Series(pd.NA, index=raw_df.index, dtype="object")


def _clean_numeric(series: pd.Series) -> pd.Series:
    extracted = (
        series.astype("string")
        .str.replace("\u00a0", "", regex=False)
        .str.replace(",", ".", regex=False)
        .str.extract(r"(-?\d+(?:\.\d+)?)")[0]
    )
    return pd.to_numeric(extracted, errors="coerce")


def _known(value: object) -> bool:
    if value is None or value is pd.NA:
        return False
    try:
        if bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        pass
    return str(value).strip().casefold() not in {"", "n/a", "nan", "none", "missing"}
