from __future__ import annotations

import math
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.prediction_service import PredictionService


def _sample(city: str) -> dict:
    is_almaty = city == "almaty"
    return {
        "url": f"https://krisha.kz/a/show/serving-smoke-{city}",
        "scrape_city": city,
        "title": "2-комнатная квартира, 60 м², 5/12 этаж",
        "price": 45_000_000 if is_almaty else 36_000_000,
        "lat": 43.238 if is_almaty else 51.128,
        "lon": 76.945 if is_almaty else 71.431,
        "Площадь": "60 м²",
        "Город": "Алматы, Бостандыкский р-н" if is_almaty else "Астана, Есильский р-н",
        "Тип дома": "монолитный",
        "Высота потолков": "2.8 м",
        "Состояние квартиры": "свежий ремонт",
        "Год постройки": "2022",
        "Квартира меблирована": "полностью",
        "Жилой комплекс": "Serving smoke ЖК",
        "Новостройка": "да",
    }


def _validate_prediction(name: str, prediction) -> None:
    values = [
        prediction.pred_price_per_m2_q10,
        prediction.pred_price_per_m2_q50,
        prediction.pred_price_per_m2_q90,
    ]
    if not all(math.isfinite(value) and 50_000 < value < 10_000_000 for value in values):
        raise AssertionError(f"{name}: implausible prediction values {values}")
    if values != sorted(values):
        raise AssertionError(f"{name}: quantiles are not ordered: {values}")
    print(
        f"[OK] {name}: q10={values[0]:,.0f}, "
        f"point={values[1]:,.0f}, q90={values[2]:,.0f} KZT/m²"
    )


def main() -> None:
    os.environ["PRICE_MODEL_ROUTING"] = "city_auto"
    service = PredictionService(ROOT)
    if service.available_model_bundles != [
        "astana_v1",
        "astana_v2",
        "almaty_v2",
        "universal_v2",
    ]:
        raise AssertionError(f"Unexpected bundles: {service.available_model_bundles}")

    astana = _sample("astana")
    almaty = _sample("almaty")
    if service._select_model_key(astana) != "astana_v2":
        raise AssertionError("Astana did not route to the Astana v2 model.")
    if service._select_model_key(almaty) != "almaty_v2":
        raise AssertionError("Almaty did not route to the Almaty v2 model.")

    _validate_prediction("Astana / astana_v2", service.predict_raw_listing(astana))
    if set(service._v2_bundles) != {"astana_v2"}:
        raise AssertionError("Only Astana v2 should be loaded after the Astana request.")
    _validate_prediction("Almaty / almaty_v2", service.predict_raw_listing(almaty))
    if set(service._v2_bundles) != {"astana_v2", "almaty_v2"}:
        raise AssertionError("City-specific v2 models were not loaded lazily.")
    print("[OK] City routing and lazy model loading validated.")


if __name__ == "__main__":
    main()
