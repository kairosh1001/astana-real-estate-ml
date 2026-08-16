from __future__ import annotations

import math
from statistics import median


def build_comparable_insight(
    target: dict,
    candidates: list[dict],
    *,
    limit: int = 5,
) -> dict | None:
    """Rank transparent, same-city asking-price comparables for one listing."""
    target_city = str(target.get("city") or "")
    target_area = _positive_float(target.get("area_m2"))
    target_price_per_m2 = _positive_float(target.get("listed_price_per_m2"))
    if not target_city or not target_area:
        return None

    ranked: list[dict] = []
    for candidate in candidates:
        if candidate.get("url") == target.get("url"):
            continue
        if str(candidate.get("city") or "") != target_city:
            continue
        candidate_area = _positive_float(candidate.get("area_m2"))
        candidate_price_per_m2 = _positive_float(candidate.get("listed_price_per_m2"))
        if not candidate_area or not candidate_price_per_m2:
            continue

        score, reasons = _similarity_score(target, candidate, target_area, candidate_area)
        if score < 0.25:
            continue
        ranked.append(
            {
                **candidate,
                "similarity_score": round(score * 100),
                "similarity_reasons": reasons[:3],
                "asking_difference_pct": (
                    (candidate_price_per_m2 - target_price_per_m2)
                    / target_price_per_m2
                    if target_price_per_m2
                    else None
                ),
            }
        )

    ranked.sort(
        key=lambda item: (
            -item["similarity_score"],
            abs((item.get("area_m2") or 0) - target_area),
            abs((item.get("listed_price_per_m2") or 0) - (target_price_per_m2 or 0)),
            str(item.get("url") or ""),
        )
    )
    selected = ranked[: max(1, min(limit, 8))]
    if not selected:
        return None

    prices = [float(item["listed_price_per_m2"]) for item in selected]
    median_price_per_m2 = median(prices)
    exact_complex_count = sum(
        1
        for item in selected
        if _normalized_text(item.get("residential_complex"))
        and _normalized_text(item.get("residential_complex"))
        == _normalized_text(target.get("residential_complex"))
    )
    return {
        "items": selected,
        "count": len(selected),
        "median_price_per_m2": median_price_per_m2,
        "median_total_for_target_area": median_price_per_m2 * target_area,
        "min_price_per_m2": min(prices),
        "max_price_per_m2": max(prices),
        "exact_complex_count": exact_complex_count,
        "scope_label": (
            f"тот же ЖК — {exact_complex_count}"
            if exact_complex_count
            else "тот же город, ближайшие параметры"
        ),
    }


def _similarity_score(
    target: dict,
    candidate: dict,
    target_area: float,
    candidate_area: float,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    target_complex = _normalized_text(target.get("residential_complex"))
    candidate_complex = _normalized_text(candidate.get("residential_complex"))
    if target_complex and target_complex == candidate_complex:
        score += 0.34
        reasons.append("тот же ЖК")

    target_district = target.get("district_slug")
    if target_district and target_district == candidate.get("district_slug"):
        score += 0.18
        reasons.append("тот же район")

    target_rooms = _positive_int(target.get("rooms"))
    candidate_rooms = _positive_int(candidate.get("rooms"))
    if target_rooms and candidate_rooms:
        room_gap = abs(target_rooms - candidate_rooms)
        if room_gap == 0:
            score += 0.22
            reasons.append("столько же комнат")
        elif room_gap == 1:
            score += 0.06

    area_gap = abs(candidate_area - target_area) / target_area
    if area_gap <= 0.10:
        score += 0.18
        reasons.append("площадь ±10%")
    elif area_gap <= 0.20:
        score += 0.11
        reasons.append("похожая площадь")
    elif area_gap <= 0.35:
        score += 0.04

    target_year = _positive_int(target.get("construction_year"))
    candidate_year = _positive_int(candidate.get("construction_year"))
    if target_year and candidate_year:
        year_gap = abs(target_year - candidate_year)
        if year_gap <= 3:
            score += 0.07
            reasons.append("близкий год дома")
        elif year_gap <= 8:
            score += 0.03

    distance = _distance_km(target, candidate)
    if distance is not None:
        if distance <= 1.5:
            score += 0.07
            reasons.append("рядом на карте")
        elif distance <= 4:
            score += 0.03

    return min(score, 1.0), reasons


def _distance_km(first: dict, second: dict) -> float | None:
    lat1 = _positive_float(first.get("lat"))
    lon1 = _positive_float(first.get("lon"))
    lat2 = _positive_float(second.get("lat"))
    lon2 = _positive_float(second.get("lon"))
    if None in {lat1, lon1, lat2, lon2}:
        return None
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 6371.0 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
