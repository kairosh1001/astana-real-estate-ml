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


def city_config(value: object) -> dict[str, Any]:
    return CITIES[normalize_city_slug(value)]


def district_options(city: object) -> list[dict[str, str]]:
    return [
        {"slug": item["slug"], "label": item["label"]}
        for item in city_config(city)["districts"]
    ]


def infer_listing_city(raw_listing: dict, *, default: str = "astana") -> str:
    candidates = [
        raw_listing.get("scrape_city"),
        raw_listing.get("city"),
        raw_listing.get("Город"),
    ]
    text = " ".join(str(value).casefold() for value in candidates if value)
    if "алмат" in text or "almaty" in text:
        return "almaty"
    if "астан" in text or "astana" in text or "нур-султан" in text:
        return "astana"
    return default
