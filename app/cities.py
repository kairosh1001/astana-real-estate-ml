from __future__ import annotations

from typing import Any


CITIES: dict[str, dict[str, Any]] = {
    "astana": {
        "slug": "astana",
        "name": "Астана",
        "in_name": "Астане",
        "genitive": "Астаны",
        "krisha_slug": "astana",
        "two_gis_slug": "astana",
        "map_center": [51.128, 71.431],
        "map_zoom": 11,
        "map_bounds": [50.80, 70.80, 51.50, 72.00],
        "districts": [
            {"slug": "yesil", "label": "Есиль", "aliases": {"есиль", "есильский"}},
            {"slug": "nura", "label": "Нура", "aliases": {"нура"}},
            {"slug": "saryarka", "label": "Сарыарка", "aliases": {"сарыарка"}},
            {"slug": "almaty", "label": "Алматы", "aliases": {"алматы", "алматинский"}},
            {"slug": "baikonyr", "label": "Байконур", "aliases": {"байконур"}},
            {"slug": "saraishyk", "label": "Сарайшык", "aliases": {"сарайшык"}},
        ],
    },
    "almaty": {
        "slug": "almaty",
        "name": "Алматы",
        "in_name": "Алматы",
        "genitive": "Алматы",
        "krisha_slug": "almaty",
        "two_gis_slug": "almaty",
        "map_center": [43.238, 76.945],
        "map_zoom": 11,
        "map_bounds": [42.90, 76.50, 43.55, 77.45],
        "districts": [
            {"slug": "almaly", "label": "Алмалинский", "aliases": {"алмалинский", "алмалы"}},
            {"slug": "auezov", "label": "Ауэзовский", "aliases": {"ауэзовский", "ауэзов"}},
            {"slug": "bostandyk", "label": "Бостандыкский", "aliases": {"бостандыкский", "бостандык"}},
            {"slug": "medeu", "label": "Медеуский", "aliases": {"медеуский", "медеу"}},
            {"slug": "nauryzbay", "label": "Наурызбайский", "aliases": {"наурызбайский", "наурызбай"}},
            {"slug": "turksib", "label": "Турксибский", "aliases": {"турксибский", "турксиб"}},
            {"slug": "zhetysu", "label": "Жетысуский", "aliases": {"жетысуский", "жетысу"}},
            {"slug": "alatau", "label": "Алатауский", "aliases": {"алатауский", "алатау"}},
        ],
    },
}

BOTH_CITIES = {
    "slug": "both",
    "name": "Астана и Алматы",
    "in_name": "Астане и Алматы",
    "genitive": "Астаны и Алматы",
    "map_center": [47.30, 74.20],
    "map_zoom": 5,
    "districts": [],
}

CITY_OPTIONS = [
    {"slug": city["slug"], "label": city["name"]} for city in CITIES.values()
]


def normalize_city_slug(value: object, *, default: str = "astana") -> str:
    cleaned = str(value or "").strip().casefold()
    aliases = {
        "astana": "astana",
        "астана": "astana",
        "nur-sultan": "astana",
        "нур-султан": "astana",
        "almaty": "almaty",
        "алматы": "almaty",
    }
    return aliases.get(cleaned, default)


def normalize_city_scope(value: object, *, default: str = "astana") -> str:
    cleaned = str(value or "").strip().casefold()
    if cleaned in {"both", "all", "оба", "оба города", "астана и алматы"}:
        return "both"
    return normalize_city_slug(value, default=default)


def city_config(value: object) -> dict[str, Any]:
    return CITIES[normalize_city_slug(value)]


def city_scope_config(value: object) -> dict[str, Any]:
    scope = normalize_city_scope(value)
    return BOTH_CITIES if scope == "both" else city_config(scope)


def district_options(city: object) -> list[dict[str, str]]:
    return [
        {"slug": item["slug"], "label": item["label"]}
        for item in city_config(city)["districts"]
    ]


def infer_listing_city(raw_listing: dict, *, default: str = "astana") -> str:
    # The location parsed from the listing page is authoritative. Search result
    # pages can occasionally contain promoted cards from another city, so the
    # requested scrape city must only be used as a fallback.
    for candidate in (
        raw_listing.get("Город"),
        raw_listing.get("city_location"),
        raw_listing.get("city"),
    ):
        detected = _city_from_text(candidate)
        if detected:
            return detected

    coordinate_city = city_from_coordinates(
        raw_listing.get("lat"),
        raw_listing.get("lon"),
    )
    if coordinate_city:
        return coordinate_city

    detected = _city_from_text(raw_listing.get("scrape_city"))
    if detected:
        return detected
    return default


def city_from_coordinates(lat: object, lon: object) -> str | None:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError):
        return None
    for slug, config in CITIES.items():
        min_lat, min_lon, max_lat, max_lon = config["map_bounds"]
        if min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon:
            return slug
    return None


def coordinates_match_city(lat: object, lon: object, city: object) -> bool:
    detected = city_from_coordinates(lat, lon)
    return detected is None or detected == normalize_city_slug(city)


def _city_from_text(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    # Astana has an "Алматы" district, so detect the city name before looking
    # for the broader Almaty substring.
    if "астан" in text or "astana" in text or "нур-султан" in text:
        return "astana"
    if "алмат" in text or "almaty" in text:
        return "almaty"
    return None
