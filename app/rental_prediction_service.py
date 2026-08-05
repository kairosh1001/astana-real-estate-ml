from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from app.feature_pipeline import FeatureConfig
from app.rental_feature_pipeline import build_rental_features


MODEL_FILES = {
    "q10": "catboost_q10_rent_total_log.cbm",
    "q50": "catboost_q50_rent_total_log.cbm",
    "q90": "catboost_q90_rent_total_log.cbm",
}


@dataclass(frozen=True)
class RentalPrediction:
    url: str
    title: str
    period: str
    listed_rent: float
    pred_rent_q10: float
    pred_rent_q50: float
    pred_rent_q90: float
    discount_vs_asking_pct_conservative: float
    discount_vs_asking_pct_median: float
    interval_width_pct: float
    production_ready: bool


class RentalPredictionService:
    def __init__(self, root: Path | str, period: str) -> None:
        if period not in {"monthly", "daily"}:
            raise ValueError("period must be 'monthly' or 'daily'")
        self.period = period
        self.model_dir = Path(root) / "models" / f"rent_{period}"
        self.metadata = json.loads((self.model_dir / "model_metadata.json").read_text(encoding="utf-8"))
        self.evaluation = json.loads((self.model_dir / "evaluation.json").read_text(encoding="utf-8"))
        self.production_ready = bool(self.evaluation.get("production_ready"))
        config_values = dict(self.metadata.get("feature_config") or {})
        self.feature_config = FeatureConfig(**config_values)
        self.models: dict[str, CatBoostRegressor] = {}
        for quantile, filename in MODEL_FILES.items():
            model = CatBoostRegressor()
            model.load_model(str(self.model_dir / filename))
            self.models[quantile] = model

    def predict_raw_listing(self, raw_listing: dict) -> RentalPrediction:
        actual_period = str(raw_listing.get("rental_period") or "")
        if actual_period != self.period:
            raise ValueError(f"Expected {self.period} listing, got {actual_period or 'unknown'}")
        frame = build_rental_features(pd.DataFrame([raw_listing]), self.feature_config)
        features = frame.loc[:, self.metadata["feature_columns"]].copy()
        for column in self.metadata["categorical_features"]:
            features[column] = features[column].astype("string").fillna("missing")
        categorical = set(self.metadata["categorical_features"])
        for column in self.metadata["feature_columns"]:
            if column not in categorical:
                features[column] = pd.to_numeric(features[column], errors="coerce").astype(float)
        raw_predictions = {
            quantile: float(np.exp(model.predict(features)[0]))
            for quantile, model in self.models.items()
        }
        # Independently trained quantiles can cross on small or unusual samples.
        # Sort them before serving so the displayed interval is always coherent.
        ordered = sorted(raw_predictions.values())
        predictions = {"q10": ordered[0], "q50": ordered[1], "q90": ordered[2]}
        listed = float(pd.to_numeric(raw_listing.get("price"), errors="raise"))
        return RentalPrediction(
            url=str(raw_listing.get("url") or ""),
            title=str(raw_listing.get("title") or ""),
            period=self.period,
            listed_rent=listed,
            pred_rent_q10=predictions["q10"],
            pred_rent_q50=predictions["q50"],
            pred_rent_q90=predictions["q90"],
            discount_vs_asking_pct_conservative=(predictions["q10"] - listed) / listed,
            discount_vs_asking_pct_median=(predictions["q50"] - listed) / listed,
            interval_width_pct=(predictions["q90"] - predictions["q10"]) / predictions["q50"],
            production_ready=self.production_ready,
        )
