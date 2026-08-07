from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ASTANA_BBOX = (50.95, 71.20, 51.30, 71.75)
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
DEFAULT_OUTPUT = Path("app") / "data" / "astana_pois.json"


def build_query() -> str:
    south, west, north, east = ASTANA_BBOX
    bbox = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:180];
(
  nwr["leisure"~"^(park|garden)$"]({bbox});
  nwr["shop"~"^(mall|supermarket|convenience)$"]({bbox});
  nwr["amenity"~"^(school|kindergarten|clinic|hospital|doctors)$"]({bbox});
  nwr["highway"="bus_stop"]({bbox});
  nwr["public_transport"~"^(platform|station)$"]({bbox});
);
out center tags qt;
""".strip()


def classify(tags: dict) -> str | None:
    leisure = tags.get("leisure")
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    if leisure in {"park", "garden"}:
        return "park"
    if amenity in {"school", "kindergarten"}:
        return "education"
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


def normalize_element(element: dict) -> dict | None:
    tags = element.get("tags") or {}
    category = classify(tags)
    center = element.get("center") or {}
    lat = element.get("lat", center.get("lat"))
    lon = element.get("lon", center.get("lon"))
    if not category or lat is None or lon is None:
        return None
    return {
        "id": f"{element.get('type', 'object')}/{element.get('id')}",
        "category": category,
        "name": tags.get("name:ru") or tags.get("name") or "",
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
    }


def fetch_elements() -> list[dict]:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "Kvartiry-ai.kz POI catalog/1.0"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    errors = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            response = session.post(
                endpoint,
                data={"data": build_query()},
                timeout=120,
            )
            response.raise_for_status()
            return response.json().get("elements") or []
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{endpoint}: {exc}")
    raise RuntimeError("; ".join(errors))


def build_catalog(elements: list[dict]) -> dict:
    unique = {}
    for element in elements:
        item = normalize_element(element)
        if not item:
            continue
        key = (item["category"], round(item["lat"], 5), round(item["lon"], 5))
        existing = unique.get(key)
        if existing is None or (not existing["name"] and item["name"]):
            unique[key] = item
    items = sorted(
        unique.values(),
        key=lambda item: (item["category"], item["name"], item["id"]),
    )
    return {
        "source": "OpenStreetMap contributors",
        "license": "ODbL",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bbox": list(ASTANA_BBOX),
        "counts": dict(sorted(Counter(item["category"] for item in items).items())),
        "items": items,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a compact Astana POI catalog from OpenStreetMap."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog = build_catalog(fetch_elements())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Saved {len(catalog['items'])} POIs to {args.output}")
    print(catalog["counts"])


if __name__ == "__main__":
    main()
