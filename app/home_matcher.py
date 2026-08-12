from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from sklearn.neighbors import BallTree

from app.feature_pipeline import EARTH_RADIUS_KM, LRT_COORDS, MALL_COORDS, PARK_COORDS


PRIORITY_OPTIONS = [
    {"value": 0, "label": "Неважно"},
    {"value": 1, "label": "Важно"},
    {"value": 2, "label": "Очень важно"},
]
DEFAULT_PRIORITIES = {
    "park": 1,
    "education": 0,
    "transit": 1,
    "grocery": 1,
    "value": 2,
    "ready": 1,
    "modern": 0,
}
HOME_PRESETS = [
    {
        "slug": "balanced",
        "label": "Сбалансированный",
        "description": "Цена важнее всего; также учитываются парк, остановки, магазины и готовность к заселению.",
        "priorities": dict(DEFAULT_PRIORITIES),
    },
    {
        "slug": "family",
        "label": "Для семьи",
        "description": "Максимальный вес получают школы, детсады, парки и магазины рядом.",
        "priorities": {
            "park": 2,
            "education": 2,
            "transit": 1,
            "grocery": 2,
            "value": 1,
            "ready": 1,
            "modern": 0,
        },
    },
    {
        "slug": "ready",
        "label": "Заехать сразу",
        "description": "Главное — готовое состояние и мебель; дополнительно учитывается современность дома.",
        "priorities": {
            "park": 0,
            "education": 0,
            "transit": 1,
            "grocery": 1,
            "value": 1,
            "ready": 2,
            "modern": 1,
        },
    },
    {
        "slug": "value",
        "label": "Максимум выгоды",
        "description": "Рейтинг строится только по сравнению цены объявления с оценкой ИИ.",
        "priorities": {
            "park": 0,
            "education": 0,
            "transit": 0,
            "grocery": 0,
            "value": 2,
            "ready": 0,
            "modern": 0,
        },
    },
]
PRIORITY_LABELS = {
    "park": "Парк рядом",
    "education": "Школа или детсад рядом",
    "transit": "Остановка рядом",
    "grocery": "Продукты рядом",
    "value": "Выгодная цена",
    "ready": "Можно заехать без ремонта",
    "modern": "Современный дом",
}


@dataclass(frozen=True)
class HomeSearchPreferences:
    districts: tuple[str, ...] = ()
    rooms: tuple[int, ...] = ()
    min_price: float | None = None
    max_price: float | None = None
    min_area: float | None = None
    max_area: float | None = None
    min_year: int | None = None
    housing_type: str = "any"
    conditions: tuple[str, ...] = ()
    furnished_only: bool = False
    priorities: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_PRIORITIES)
    )


def rank_home_candidates(
    candidates: list[dict],
    preferences: HomeSearchPreferences,
    *,
    catalog_path: Path,
    city: str = "astana",
    limit: int = 30,
) -> dict:
    filtered = [
        dict(item) for item in candidates if _passes_hard_filters(item, preferences)
    ]
    groups, catalog_meta = _load_poi_groups(
        str(catalog_path), _mtime(catalog_path), city
    )
    nearest_pois = _nearest_pois(filtered, groups)
    distances = {
        key: [poi["distance_km"] if poi else None for poi in values]
        for key, values in nearest_pois.items()
    }
    weights = {
        key: max(0, min(int(preferences.priorities.get(key, 0)), 2))
        for key in PRIORITY_LABELS
    }
    if not any(weights.values()):
        weights["value"] = 1

    ranked = []
    for index, item in enumerate(filtered):
        components = _build_components(item, index, distances, weights)
        total_weight = sum(component["weight"] for component in components)
        weighted_score = sum(
            component["score"] * component["weight"] for component in components
        )
        known_weight = sum(
            component["weight"] for component in components if component["known"]
        )
        item["match_score"] = round(weighted_score / total_weight) if total_weight else 0
        item["match_coverage"] = (
            round(known_weight / total_weight * 100) if total_weight else 0
        )
        item["match_components"] = components
        good = sorted(
            (component for component in components if component["score"] >= 68),
            key=lambda component: (component["weight"], component["score"]),
            reverse=True,
        )
        weak = sorted(
            (component for component in components if component["score"] < 55),
            key=lambda component: (component["weight"], -component["score"]),
            reverse=True,
        )
        item["match_reasons"] = good[:3]
        item["match_compromises"] = weak[:2]
        item["lifestyle_distances"] = {
            key: distances.get(key, [None] * len(filtered))[index]
            for key in ("park", "education", "transit", "grocery")
        }
        item["lifestyle_pois"] = {
            key: nearest_pois.get(key, [None] * len(filtered))[index]
            for key in ("park", "education", "transit", "grocery")
        }
        ranked.append(item)

    ranked.sort(
        key=lambda item: (
            item["match_score"],
            item["match_coverage"],
            item.get("discount_vs_asking_pct_conservative") or -99,
            item.get("last_seen_at") or "",
        ),
        reverse=True,
    )
    return {
        "items": ranked[:limit],
        "total": len(ranked),
        "catalog": catalog_meta,
        "priorities": weights,
    }


def _passes_hard_filters(item: dict, preferences: HomeSearchPreferences) -> bool:
    if preferences.districts and item.get("district_slug") not in preferences.districts:
        return False
    if preferences.rooms and item.get("rooms") not in preferences.rooms:
        return False
    if preferences.min_price is not None and (
        item.get("listed_price") is None
        or float(item["listed_price"]) < preferences.min_price
    ):
        return False
    if preferences.max_price is not None and (
        item.get("listed_price") is None
        or float(item["listed_price"]) > preferences.max_price
    ):
        return False
    if preferences.min_area is not None and (
        item.get("area_m2") is None or float(item["area_m2"]) < preferences.min_area
    ):
        return False
    if preferences.max_area is not None and (
        item.get("area_m2") is None or float(item["area_m2"]) > preferences.max_area
    ):
        return False
    if preferences.min_year is not None and (
        not item.get("construction_year")
        or int(item["construction_year"]) < preferences.min_year
    ):
        return False
    if preferences.housing_type == "new" and not item.get("is_new_build"):
        return False
    if preferences.housing_type == "secondary" and item.get("is_new_build"):
        return False
    if preferences.conditions and item.get("apartment_condition_slug") not in preferences.conditions:
        return False
    if preferences.furnished_only and item.get("is_furnished") is not True:
        return False
    return True


def _build_components(
    item: dict,
    index: int,
    distances: dict[str, list[float | None]],
    weights: dict[str, int],
) -> list[dict]:
    components = []
    distance_settings = {
        "park": (0.30, 2.50),
        "education": (0.45, 3.00),
        "transit": (0.25, 1.50),
        "grocery": (0.35, 2.00),
    }
    for key, (ideal, maximum) in distance_settings.items():
        weight = weights.get(key, 0)
        if not weight:
            continue
        distance = distances.get(key, [None] * (index + 1))[index]
        known = distance is not None and math.isfinite(distance)
        score = _distance_score(distance, ideal=ideal, maximum=maximum) if known else 40
        detail = (
            f"{format_distance(distance)} по прямой"
            if known
            else "не хватает точных координат"
        )
        components.append(_component(key, score, detail, weight, known))

    if weights.get("value"):
        discount = item.get("discount_vs_asking_pct_conservative")
        known = discount is not None
        score = _clamp(70 + float(discount) * 200, 0, 100) if known else 40
        if known and discount >= 0:
            detail = f"цена на {discount:.0%} ниже оценки q10"
        elif known:
            detail = f"цена на {abs(discount):.0%} выше оценки q10"
        else:
            detail = "оценка цены недоступна"
        components.append(_component("value", score, detail, weights["value"], known))

    if weights.get("ready"):
        score, detail, known = _ready_score(item)
        components.append(_component("ready", score, detail, weights["ready"], known))

    if weights.get("modern"):
        year = item.get("construction_year")
        known = bool(year)
        score = _modern_score(int(year)) if known else 40
        detail = f"дом {int(year)} года" if known else "год дома не указан"
        components.append(_component("modern", score, detail, weights["modern"], known))
    return components


def _component(key: str, score: float, detail: str, weight: int, known: bool) -> dict:
    return {
        "key": key,
        "label": PRIORITY_LABELS[key],
        "score": round(float(score)),
        "detail": detail,
        "weight": weight,
        "known": known,
    }


def _ready_score(item: dict) -> tuple[float, str, bool]:
    condition_slug = item.get("apartment_condition_slug")
    condition_scores = {
        "fresh_repair": 100,
        "tidy_repair": 78,
        "rough_finish": 15,
        "needs_repair": 8,
        "open_plan": 20,
    }
    condition_known = condition_slug in condition_scores
    condition_score = condition_scores.get(condition_slug, 42)
    furnished = item.get("is_furnished")
    furniture_known = furnished is not None
    furniture_score = 90 if furnished else 25 if furnished is False else 45
    score = condition_score * 0.72 + furniture_score * 0.28
    condition = item.get("apartment_condition") or "состояние не указано"
    furniture = item.get("furnished_label") or "мебель не указана"
    return score, f"{condition}; {furniture}", condition_known or furniture_known


def _modern_score(year: int) -> float:
    if year >= 2022:
        return 100
    if year >= 2015:
        return 75 + (year - 2015) * 3
    if year >= 2000:
        return 45 + (year - 2000) * 2
    return _clamp(20 + (year - 1970) * 0.8, 0, 44)


def _distance_score(distance: float, *, ideal: float, maximum: float) -> float:
    if distance <= ideal:
        return 100
    if distance >= maximum:
        return 0
    return 100 * (maximum - distance) / (maximum - ideal)


def format_distance(distance: float | None) -> str:
    if distance is None:
        return "—"
    if distance < 1:
        return f"{round(distance * 1000 / 50) * 50:.0f} м"
    return f"{distance:.1f} км"


def _nearest_pois(
    items: list[dict],
    groups: dict[str, dict],
) -> dict[str, list[dict | None]]:
    result: dict[str, list[dict | None]] = {
        key: [None] * len(items) for key in groups
    }
    valid_indices = [
        index
        for index, item in enumerate(items)
        if _valid_coordinate(item.get("lat"), item.get("lon"))
    ]
    if not valid_indices:
        return result
    apartment_coords = np.radians(
        np.array(
            [[items[index]["lat"], items[index]["lon"]] for index in valid_indices],
            dtype=float,
        )
    )
    for key, group in groups.items():
        poi_coords = group["coords"]
        if not len(poi_coords):
            continue
        tree = BallTree(np.radians(poi_coords), metric="haversine")
        distance_radians, nearest_indices = tree.query(apartment_coords, k=1)
        for position, item_index in enumerate(valid_indices):
            poi = group["items"][int(nearest_indices[position][0])]
            osm_id = str(poi.get("id") or "")
            result[key][item_index] = {
                "distance_km": float(
                    distance_radians[position][0] * EARTH_RADIUS_KM
                ),
                "name": str(poi.get("name") or "").strip(),
                "url": (
                    f"https://www.openstreetmap.org/{osm_id}"
                    if "/" in osm_id
                    else ""
                ),
            }
    return result


@lru_cache(maxsize=4)
def _load_poi_groups(
    path_text: str,
    modified_at: float | None,
    city: str,
) -> tuple[dict[str, dict], dict]:
    del modified_at
    path = Path(path_text)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [
            item
            for item in (data.get("items") or [])
            if not item.get("city") or item.get("city") == city
        ]
        groups = {}
        category_map = {
            "park": {"park"},
            "education": {"education", "school", "kindergarten"},
            "transit": {"transit"},
            "grocery": {"grocery"},
            "healthcare": {"healthcare"},
            "mall": {"mall"},
        }
        for key, source_categories in category_map.items():
            category_items = [
                item for item in items if item.get("category") in source_categories
            ]
            groups[key] = {
                "coords": np.array(
                    [
                        [float(item["lat"]), float(item["lon"])]
                        for item in category_items
                    ],
                    dtype=float,
                ).reshape((-1, 2)),
                "items": category_items,
            }
        city_meta = (data.get("cities") or {}).get(city) or {}
        return groups, {
            "source": data.get("source") or "OpenStreetMap contributors",
            "generated_at": data.get("generated_at"),
            "counts": city_meta.get("counts") or data.get("counts") or {},
            "city": city,
            "fallback": False,
        }
    fallback_coords = {
        "park": np.asarray(PARK_COORDS, dtype=float),
        "education": np.empty((0, 2)),
        "transit": np.asarray(LRT_COORDS, dtype=float),
        "grocery": np.empty((0, 2)),
        "healthcare": np.empty((0, 2)),
        "mall": np.asarray(MALL_COORDS, dtype=float),
    }
    groups = {
        key: {
            "coords": coords,
            "items": [
                {"id": "", "name": "", "lat": lat, "lon": lon}
                for lat, lon in coords
            ],
        }
        for key, coords in fallback_coords.items()
    }
    return groups, {
        "source": "Встроенный ограниченный список объектов",
        "generated_at": None,
        "counts": {key: len(value["coords"]) for key, value in groups.items()},
        "fallback": True,
    }


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _valid_coordinate(lat: object, lon: object) -> bool:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError):
        return False
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))
