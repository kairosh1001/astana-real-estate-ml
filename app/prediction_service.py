from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import sleep
from urllib.parse import urlparse

import pandas as pd

from app.feature_pipeline import build_feature_config, build_model_features
from app.model_service import PriceModelService
from scrape import ApartmentScraper


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
        self.model_service = PriceModelService(
            models_dir=self.root / "models",
            metadata_path=self.root / "model_metadata.json",
        )
        self.feature_config = build_feature_config(self._load_training_raw())

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
        features = build_model_features(
            pd.DataFrame([raw_listing]),
            self.feature_config,
            include_target=False,
            filter_training_rows=False,
        )
        prediction = self.model_service.predict(features).predictions.iloc[0]

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
