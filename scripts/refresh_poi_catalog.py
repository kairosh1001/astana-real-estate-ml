from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CATALOG_SCHEMA_VERSION = 2
EARTH_RADIUS_KM = 6371.0088
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
DEFAULT_OUTPUT = Path("app") / "data" / "kazakhstan_pois.json"

# Bounding boxes deliberately extend a little beyond the dense urban area so
# edge listings can still find nearby POIs outside an administrative boundary.
CITY_CONFIGS: dict[str, dict[str, Any]] = {
    "astana": {
        "name": "Астана",
        "bbox": (50.95, 71.20, 51.30, 71.75),
        "center": {"lat": 51.1282, "lon": 71.4304},
        "normalization_radius_km": 18.0,
    },
    "almaty": {
        "name": "Алматы",
        "bbox": (43.10, 76.70, 43.40, 77.15),
        "center": {"lat": 43.2389, "lon": 76.8897},
        "normalization_radius_km": 18.0,
    },
}

ESSENTIAL_CATEGORIES = (
    "park",
    "school",
    "kindergarten",
    "grocery",
    "mall",
    "healthcare",
    "transit",
    "university",
)


def build_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    bbox_text = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:240];
(
  nwr["leisure"~"^(park|garden)$"]({bbox_text});
  nwr["shop"~"^(mall|supermarket|convenience)$"]({bbox_text});
  nwr["amenity"~"^(school|kindergarten|clinic|hospital|doctors|university|college)$"]({bbox_text});
  nwr["highway"="bus_stop"]({bbox_text});
  nwr["public_transport"~"^(platform|station)$"]({bbox_text});
);
out center tags qt;
""".strip()


def classify(tags: dict[str, Any]) -> str | None:
    leisure = tags.get("leisure")
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    if leisure in {"park", "garden"}:
        return "park"
    if amenity == "school":
        return "school"
    if amenity == "kindergarten":
        return "kindergarten"
    if amenity in {"university", "college"}:
        return "university"
    if amenity in {"clinic", "hospital", "doctors"}:
        return "healthcare"
    if shop in {"supermarket", "convenience"}:
        return "grocery"
    if shop == "mall":
        return "mall"
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") in {
        "platform",
        "station",
    }:
        return "transit"
    return None


def normalize_element(element: dict[str, Any], city: str) -> dict[str, Any] | None:
    tags = element.get("tags") or {}
    category = classify(tags)
    center = element.get("center") or {}
    lat = element.get("lat", center.get("lat"))
    lon = element.get("lon", center.get("lon"))
    if not category or lat is None or lon is None:
        return None
    osm_type = str(element.get("type") or "object")
    osm_numeric_id = element.get("id")
    name = tags.get("name:ru") or tags.get("name") or tags.get("brand") or ""
    useful_tags = {
        key: tags[key]
        for key in (
            "amenity",
            "brand",
            "highway",
            "leisure",
            "name",
            "name:ru",
            "operator",
            "public_transport",
            "shop",
        )
        if tags.get(key) not in (None, "")
    }
    return {
        "id": f"{osm_type}/{osm_numeric_id}",
        "city": city,
        "category": category,
        "name": str(name).strip(),
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
        "tags": useful_tags,
    }


def create_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "Kvartiry-ai.kz POI catalog/2.0"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_elements(
    session: requests.Session,
    city: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    errors: list[str] = []
    query = build_query(tuple(config["bbox"]))
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            response = session.post(endpoint, data={"data": query}, timeout=300)
            response.raise_for_status()
            elements = response.json().get("elements") or []
            if not elements:
                raise RuntimeError("Overpass returned no elements")
            print(f"[OK] {city}: {len(elements):,} raw OSM elements from {endpoint}")
            return elements
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError(f"Could not download {city}: {'; '.join(errors)}")


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, float, float], dict[str, Any]] = {}
    for item in items:
        key = (
            item["city"],
            item["category"],
            round(float(item["lat"]), 5),
            round(float(item["lon"]), 5),
        )
        existing = unique.get(key)
        if existing is None or (not existing["name"] and item["name"]):
            unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: (item["city"], item["category"], item["name"], item["id"]),
    )


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    south, west, north, east = bbox
    height = math.radians(north - south) * EARTH_RADIUS_KM
    mean_latitude = math.radians((north + south) / 2)
    width = math.radians(east - west) * EARTH_RADIUS_KM * math.cos(mean_latitude)
    return abs(height * width)


def city_metadata(
    city: str,
    config: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = Counter(item["category"] for item in items)
    area = bbox_area_km2(tuple(config["bbox"]))
    named_share = sum(bool(item["name"]) for item in items) / len(items) if items else 0
    return {
        "name": config["name"],
        "bbox": list(config["bbox"]),
        "center": config["center"],
        "normalization_radius_km": config["normalization_radius_km"],
        "area_km2": round(area, 2),
        "counts": {category: int(counts.get(category, 0)) for category in ESSENTIAL_CATEGORIES},
        "density_per_100_km2": {
            category: round(counts.get(category, 0) / area * 100, 2)
            for category in ESSENTIAL_CATEGORIES
        },
        "named_share": round(named_share, 4),
        "missing_categories": [
            category for category in ESSENTIAL_CATEGORIES if counts.get(category, 0) == 0
        ],
    }


def build_catalog(
    elements_by_city: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for city, elements in elements_by_city.items():
        normalized.extend(
            item
            for element in elements
            if (item := normalize_element(element, city)) is not None
        )
    items = deduplicate_items(normalized)
    cities = {
        city: city_metadata(
            city,
            CITY_CONFIGS[city],
            [item for item in items if item["city"] == city],
        )
        for city in elements_by_city
    }
    global_counts = Counter(item["category"] for item in items)
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source": "OpenStreetMap contributors",
        "source_url": "https://www.openstreetmap.org/copyright",
        "license": "ODbL",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": dict(sorted(global_counts.items())),
        "cities": cities,
        "items": items,
    }


def validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("Unexpected catalog schema version")
    if not catalog.get("items"):
        raise ValueError("Catalog contains no POIs")
    for city, metadata in catalog.get("cities", {}).items():
        missing = metadata.get("missing_categories") or []
        if missing:
            raise ValueError(f"{city} is missing essential OSM categories: {missing}")


def atomic_write_json(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download one universal Astana + Almaty POI catalog from OpenStreetMap."
    )
    parser.add_argument(
        "--city",
        action="append",
        choices=sorted(CITY_CONFIGS),
        dest="cities",
        help="City to refresh; repeat for multiple cities. Defaults to all configured cities.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cities = args.cities or list(CITY_CONFIGS)
    session = create_session()
    try:
        elements_by_city = {
            city: fetch_elements(session, city, CITY_CONFIGS[city]) for city in cities
        }
    finally:
        session.close()
    catalog = build_catalog(elements_by_city)
    validate_catalog(catalog)
    atomic_write_json(catalog, args.output)
    print(f"[OK] Saved {len(catalog['items']):,} POIs to {args.output}")
    for city, metadata in catalog["cities"].items():
        print(
            f"[QA] {city}: {sum(metadata['counts'].values()):,} POIs, "
            f"named={metadata['named_share']:.1%}, counts={metadata['counts']}"
        )


if __name__ == "__main__":
    main()
