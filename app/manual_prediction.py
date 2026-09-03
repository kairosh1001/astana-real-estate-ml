from __future__ import annotations

from dataclasses import dataclass

from app.cities import CITIES, city_config


FURNISHED_VALUES = {
    "missing": "",
    "full": "полностью",
    "partial": "частично",
    "none": "без мебели",
}
BUILDING_TYPE_VALUES = {
    "missing": "",
    "monolith": "монолитный",
    "brick": "кирпичный",
    "panel": "панельный",
    "other": "иной",
}
CONDITION_VALUES = {
    "missing": "",
    "fresh_repair": "свежий ремонт",
    "tidy_repair": "не новый, но аккуратный ремонт",
    "rough_finish": "черновая отделка",
    "needs_repair": "требует ремонта",
    "open_plan": "свободная планировка",
}


@dataclass(frozen=True)
class ManualApartment:
    city: str
    listed_price: float
    area_m2: float
    rooms: int
    current_floor: int
    total_floors: int
    construction_year: int
    district: str = ""
    residential_complex: str = ""
    ceiling_height: float | None = None
    furnished: str = "missing"
    apartment_condition: str = "missing"
    building_type: str = "missing"
    is_new_build: bool = False
    middle_floor_only: bool = False
    lat: float | None = None
    lon: float | None = None


def build_manual_raw_listing(apartment: ManualApartment) -> dict:
    _validate(apartment)
    selected_city = city_config(apartment.city)
    district_label = next(
        (
            item["label"]
            for item in selected_city["districts"]
            if item["slug"] == apartment.district
        ),
        "",
    )
    location = selected_city["name"]
    if district_label:
        location = f"{location}, {district_label} р-н"
    title = (
        f"{apartment.rooms}-комнатная квартира, {apartment.area_m2:g} м², "
        f"{apartment.current_floor}/{apartment.total_floors} этаж"
    )
    raw = {
        "url": "",
        "title": title,
        "price": apartment.listed_price,
        "scrape_city": apartment.city,
        "Город": location,
        "Площадь": f"{apartment.area_m2:g} м²",
        "rooms_structured": apartment.rooms,
        "Этаж": f"{apartment.current_floor} из {apartment.total_floors}",
        "Год постройки": apartment.construction_year,
        "Новостройка": apartment.is_new_build,
    }
    optional_values = {
        "Жилой комплекс": apartment.residential_complex.strip(),
        "Высота потолков": apartment.ceiling_height,
        "Квартира меблирована": FURNISHED_VALUES[apartment.furnished],
        "Состояние квартиры": CONDITION_VALUES[apartment.apartment_condition],
        "Тип дома": BUILDING_TYPE_VALUES[apartment.building_type],
        "lat": apartment.lat,
        "lon": apartment.lon,
    }
    raw.update(
        {
            key: value
            for key, value in optional_values.items()
            if value not in (None, "")
        }
    )
    return raw


def _validate(apartment: ManualApartment) -> None:
    if apartment.city not in CITIES:
        raise ValueError("Выберите поддерживаемый город.")
    if not 1_000_000 <= apartment.listed_price <= 5_000_000_000:
        raise ValueError("Цена должна быть от 1 млн до 5 млрд тенге.")
    if not 10 <= apartment.area_m2 <= 1_000:
        raise ValueError("Площадь должна быть от 10 до 1 000 м².")
    if not 1 <= apartment.rooms <= 20:
        raise ValueError("Количество комнат должно быть от 1 до 20.")
    if not 1 <= apartment.current_floor <= 200:
        raise ValueError("Укажите корректный этаж квартиры.")
    if not 1 <= apartment.total_floors <= 200:
        raise ValueError("Укажите корректное количество этажей в доме.")
    if apartment.current_floor > apartment.total_floors:
        raise ValueError("Этаж квартиры не может быть выше этажности дома.")
    if apartment.middle_floor_only and (
        apartment.current_floor == 1
        or apartment.current_floor == apartment.total_floors
    ):
        raise ValueError(
            "Для галочки «не первый и не последний этаж» укажите промежуточный этаж."
        )
    if not 1800 <= apartment.construction_year <= 2035:
        raise ValueError("Год постройки должен быть от 1800 до 2035.")
    if apartment.ceiling_height is not None and not 2 <= apartment.ceiling_height <= 10:
        raise ValueError("Высота потолков должна быть от 2 до 10 метров.")
    district_slugs = {item["slug"] for item in CITIES[apartment.city]["districts"]}
    if apartment.district and apartment.district not in district_slugs:
        raise ValueError("Выберите район указанного города.")
    if len(apartment.residential_complex.strip()) > 160:
        raise ValueError("Название жилого комплекса слишком длинное.")
    if apartment.furnished not in FURNISHED_VALUES:
        raise ValueError("Выберите допустимый вариант меблировки.")
    if apartment.apartment_condition not in CONDITION_VALUES:
        raise ValueError("Выберите допустимое состояние квартиры.")
    if apartment.building_type not in BUILDING_TYPE_VALUES:
        raise ValueError("Выберите допустимый тип дома.")
    if (apartment.lat is None) != (apartment.lon is None):
        raise ValueError("Укажите и широту, и долготу либо оставьте оба поля пустыми.")
    if apartment.lat is not None and apartment.lon is not None:
        min_lat, min_lon, max_lat, max_lon = CITIES[apartment.city]["map_bounds"]
        if not (
            min_lat <= apartment.lat <= max_lat
            and min_lon <= apartment.lon <= max_lon
        ):
            raise ValueError("Координаты находятся за пределами выбранного города.")
