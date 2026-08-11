from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import sleep
from urllib.parse import urlparse

import pandas as pd

from app.feature_pipeline import build_feature_config, build_model_features
from app.feature_pipeline_v2 import (
    PoiCatalog,
    UniversalFeatureConfig,
    build_model_features_v2,
)
from app.model_service import PriceModelService
from scrape import ApartmentScraper


MODEL_ROUTING_MODES = {"city_auto", "astana_v1", "almaty_v2", "universal_v2"}
V2_BUNDLE_NAMES = ("almaty_v2", "universal_v2")


@dataclass(frozen=True)
class ListingPrediction:
    url: str
    title: str
    listed_price: float
    area_m2: float
    listed_price_per_m2: float
    pred_price_per_m2_q10: float
    pred_price_per_m2_q50: float
    pred_price_per_m2_q90: float
    pred_total_q50: float
    discount_vs_asking_pct_conservative: float
    discount_vs_asking_pct_median: float
    interval_width_pct: float


class PredictionService:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.routing_mode = os.getenv("PRICE_MODEL_ROUTING", "city_auto").strip()
        if self.routing_mode not in MODEL_ROUTING_MODES:
            allowed = ", ".join(sorted(MODEL_ROUTING_MODES))
            raise ValueError(
                f"Unknown PRICE_MODEL_ROUTING={self.routing_mode!r}; expected {allowed}."
            )
        self.model_service = PriceModelService(
            models_dir=self.root / "models",
            metadata_path=self.root / "model_metadata.json",
        )
        self.feature_config = build_feature_config(self._load_training_raw())
        self._v2_bundles: dict[
            str, tuple[PriceModelService, UniversalFeatureConfig, PoiCatalog]
        ] = {}
        self._v2_load_lock = Lock()

    @property
    def available_model_bundles(self) -> list[str]:
        bundles = ["astana_v1"]
        bundles.extend(
            name for name in V2_BUNDLE_NAMES if self._v2_bundle_complete(name)
        )
        return bundles

    def cache_key(self, url: str) -> str:
        versions = [self.model_service.metadata.model_version]
        for bundle_name in V2_BUNDLE_NAMES:
            bundle_metadata = (
                self.root / "models" / bundle_name / "model_metadata.json"
            )
            if bundle_metadata.exists():
                try:
                    raw = json.loads(bundle_metadata.read_text(encoding="utf-8"))
                    versions.append(
                        str(raw.get("model_version") or bundle_name)
                    )
                except (OSError, json.JSONDecodeError):
                    versions.append(f"{bundle_name}_unknown")
        namespace = ":".join([self.routing_mode, *versions])
        return f"{url}#model={namespace}"

    def predict_by_url(self, url: str) -> ListingPrediction:
        validate_krisha_url(url)
        raw_listing = self._scrape_listing(url)
        return self.predict_raw_listing(raw_listing, url=url)

    def predict_raw_listing(
        self,
        raw_listing: dict,
        *,
        url: str | None = None,
    ) -> ListingPrediction:
        model_key = self._select_model_key(raw_listing)
        if model_key in V2_BUNDLE_NAMES:
            model_service, feature_config, poi_catalog = self._load_v2_bundle(
                model_key
            )
            features = build_model_features_v2(
                pd.DataFrame([raw_listing]),
                feature_config,
                poi_catalog,
                include_target=False,
                filter_training_rows=False,
            )
        else:
            model_service = self.model_service
            features = build_model_features(
                pd.DataFrame([raw_listing]),
                self.feature_config,
                include_target=False,
                filter_training_rows=False,
            )
        prediction = model_service.predict(features).predictions.iloc[0]

        listed_price = clean_price(raw_listing.get("price"))
        area_m2 = float(features.iloc[0]["area_m2"])
        listed_price_per_m2 = listed_price / area_m2

        pred_q10 = float(prediction["pred_price_per_m2_q10"])
        pred_q50 = float(prediction["pred_price_per_m2_q50"])
        pred_q90 = float(prediction["pred_price_per_m2_q90"])

        return ListingPrediction(
            url=url or str(raw_listing.get("url") or ""),
            title=str(raw_listing.get("title") or ""),
            listed_price=listed_price,
            area_m2=area_m2,
            listed_price_per_m2=listed_price_per_m2,
            pred_price_per_m2_q10=pred_q10,
            pred_price_per_m2_q50=pred_q50,
            pred_price_per_m2_q90=pred_q90,
            pred_total_q50=pred_q50 * area_m2,
            discount_vs_asking_pct_conservative=(
                pred_q10 - listed_price_per_m2
            )
            / listed_price_per_m2,
            discount_vs_asking_pct_median=(pred_q50 - listed_price_per_m2)
            / listed_price_per_m2,
            interval_width_pct=(pred_q90 - pred_q10) / pred_q50,
        )

    def _select_model_key(self, raw_listing: dict) -> str:
        if self.routing_mode != "city_auto":
            if (
                self.routing_mode in V2_BUNDLE_NAMES
                and not self._v2_bundle_complete(self.routing_mode)
            ):
                raise RuntimeError("Universal v2 model bundle is incomplete.")
            return self.routing_mode

        city_values = [
            raw_listing.get("scrape_city"),
            raw_listing.get("city"),
            raw_listing.get("Город"),
        ]
        city_text = " ".join(str(value).casefold() for value in city_values if value)
        if "алмат" in city_text or "almaty" in city_text:
            if self._v2_bundle_complete("almaty_v2"):
                return "almaty_v2"
            if self._v2_bundle_complete("universal_v2"):
                return "universal_v2"
        return "astana_v1"

    def _v2_bundle_complete(self, bundle_name: str) -> bool:
        bundle = self.root / "models" / bundle_name
        required = [
            bundle / "model_metadata.json",
            bundle / "feature_config.json",
            bundle / "catboost_q10_price_per_m2_log.cbm",
            bundle / "catboost_q50_price_per_m2_log.cbm",
            bundle / "catboost_q90_price_per_m2_log.cbm",
            self.root / "app" / "data" / "kazakhstan_pois.json",
        ]
        return all(path.exists() for path in required)

    def _load_v2_bundle(
        self,
        bundle_name: str,
    ) -> tuple[PriceModelService, UniversalFeatureConfig, PoiCatalog]:
        if bundle_name not in V2_BUNDLE_NAMES:
            raise ValueError(f"Unknown v2 bundle: {bundle_name}")
        if bundle_name not in self._v2_bundles:
            with self._v2_load_lock:
                if bundle_name not in self._v2_bundles:
                    if not self._v2_bundle_complete(bundle_name):
                        raise RuntimeError(
                            f"{bundle_name} model bundle is incomplete."
                        )
                    bundle = self.root / "models" / bundle_name
                    self._v2_bundles[bundle_name] = (
                        PriceModelService(
                            models_dir=bundle,
                            metadata_path=bundle / "model_metadata.json",
                        ),
                        UniversalFeatureConfig.load(bundle / "feature_config.json"),
                        PoiCatalog.load(
                            self.root / "app" / "data" / "kazakhstan_pois.json"
                        ),
                    )
        return self._v2_bundles[bundle_name]

    def _load_training_raw(self) -> pd.DataFrame:
        raw_paths = [
            self.root / "krisha_data_raw_orig.csv",
            self.root / "krisha_data_raw.csv",
        ]
        if all(path.exists() for path in raw_paths):
            return pd.concat(
                [pd.read_csv(path) for path in raw_paths],
                ignore_index=True,
            )
        return pd.read_csv(self.root / "df_check.csv")

    @staticmethod
    def _scrape_listing(url: str) -> dict:
        raw_listing = None
        for attempt in range(3):
            scraper = ApartmentScraper()
            try:
                raw_listing = scraper.parse_apartment_page(url)
            finally:
                scraper.session.close()

            if raw_listing:
                break

            if attempt < 2:
                sleep(0.5)

        if not raw_listing:
            raise RuntimeError("Не удалось загрузить объявление. Попробуйте позже.")

        return raw_listing


def validate_krisha_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Ссылка должна начинаться с http:// или https://")
    if parsed.netloc not in {"krisha.kz", "www.krisha.kz"}:
        raise ValueError("Ссылка должна вести на krisha.kz")


def clean_price(value: object) -> float:
    cleaned = str(value).replace("\u043e\u0442", "").strip()
    return float(pd.to_numeric(cleaned, errors="raise"))
