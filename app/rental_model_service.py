from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from app.feature_pipeline_v2 import PoiCatalog, UniversalFeatureConfig
from app.rental_feature_pipeline import build_rental_features


RENTAL_MODEL_FILENAMES = {
    "q10": "catboost_q10_rent_log.cbm",
    "q50": "catboost_q50_rent_log.cbm",
    "q90": "catboost_q90_rent_log.cbm",
}


@dataclass(frozen=True)
class RentalEstimate:
    monthly_rent_q10: float
    monthly_rent_q50: float
    monthly_rent_q90: float
    gross_yield_q10: float
    gross_yield_q50: float
    gross_yield_q90: float
    payback_years_q50: float
    model_version: str

    def to_dict(self) -> dict:
        return asdict(self)


class RentalModelService:
    def __init__(self, root: Path | str = ".") -> None:
        root_path = Path(root)
        self.model_dir = root_path / "models" / "rent_monthly_v2"
        self.metadata = json.loads(
            (self.model_dir / "model_metadata.json").read_text(encoding="utf-8")
        )
        self.feature_config = UniversalFeatureConfig.load(
            self.model_dir / "feature_config.json"
        )
        self.poi_catalog = PoiCatalog.load(
            root_path / "app" / "data" / "kazakhstan_pois.json"
        )
        self.models: dict[str, CatBoostRegressor] = {}
        for quantile, filename in RENTAL_MODEL_FILENAMES.items():
            model = CatBoostRegressor()
            model.load_model(str(self.model_dir / filename))
            self.models[quantile] = model

    @property
    def model_version(self) -> str:
        return str(self.metadata.get("model_version") or "rent_monthly_v2")

    def estimate(
        self,
        raw_listing: dict,
        *,
        purchase_price: float,
    ) -> RentalEstimate:
        if not np.isfinite(purchase_price) or purchase_price <= 0:
            raise ValueError("A positive purchase price is required")
        frame = build_rental_features(
            pd.DataFrame([raw_listing]),
            self.feature_config,
            self.poi_catalog,
        )
        features = frame.loc[:, self.metadata["feature_columns"]].copy()
        categorical = set(self.metadata["categorical_features"])
        for column in features:
            if column in categorical:
                features[column] = features[column].astype("string").fillna("missing")
            else:
                features[column] = pd.to_numeric(features[column], errors="coerce")

        target_mode = str(self.metadata.get("target_mode") or "total")
        area_m2 = float(features.iloc[0]["area_m2"])
        if target_mode == "per_m2" and (
            not np.isfinite(area_m2) or area_m2 <= 0
        ):
            raise ValueError("A positive apartment area is required")
        offsets = (
            self.metadata.get("quantile_calibration", {}).get("offsets_log") or {}
        )
        predictions = {}
        for quantile, model in self.models.items():
            prediction_log = float(model.predict(features)[0]) + float(
                offsets.get(quantile, 0.0)
            )
            if target_mode == "per_m2":
                prediction_log += float(np.log(area_m2))
            predictions[quantile] = float(np.exp(prediction_log))

        q50 = predictions["q50"]
        q10 = min(predictions["q10"], q50)
        q90 = max(predictions["q90"], q50)
        yield_q10 = q10 * 12 / purchase_price
        yield_q50 = q50 * 12 / purchase_price
        yield_q90 = q90 * 12 / purchase_price
        return RentalEstimate(
            monthly_rent_q10=q10,
            monthly_rent_q50=q50,
            monthly_rent_q90=q90,
            gross_yield_q10=yield_q10,
            gross_yield_q50=yield_q50,
            gross_yield_q90=yield_q90,
            payback_years_q50=1 / yield_q50,
            model_version=self.model_version,
        )


def rental_bundle_complete(root: Path | str = ".") -> bool:
    root_path = Path(root)
    model_dir = root_path / "models" / "rent_monthly_v2"
    required = [
        model_dir / "model_metadata.json",
        model_dir / "feature_config.json",
        *(model_dir / filename for filename in RENTAL_MODEL_FILENAMES.values()),
        root_path / "app" / "data" / "kazakhstan_pois.json",
    ]
    return all(path.exists() for path in required)
