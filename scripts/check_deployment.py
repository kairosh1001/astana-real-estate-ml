from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.model_service import PriceModelService
from app.rental_prediction_service import RentalPredictionService


REQUIRED_PATHS = [
    "model_metadata.json",
    "df_check.csv",
    "scrape.py",
    "scripts/telegram_bot.py",
    "models/catboost_q10_price_per_m2_log.cbm",
    "models/catboost_q50_price_per_m2_log.cbm",
    "models/catboost_q90_price_per_m2_log.cbm",
    "app/main.py",
    "app/templates/base.html",
    "app/templates/feedback.html",
    "app/templates/feedback_admin.html",
    "app/templates/traffic.html",
    "app/templates/rentals.html",
    "app/rental_feature_pipeline.py",
    "app/rental_prediction_service.py",
    "scripts/scrape_rentals.py",
    "scripts/refresh_rentals.py",
    "scripts/retrain_rental_models.py",
    "models/rent_monthly/model_metadata.json",
    "models/rent_monthly/evaluation.json",
    "models/rent_monthly/catboost_q10_rent_total_log.cbm",
    "models/rent_monthly/catboost_q50_rent_total_log.cbm",
    "models/rent_monthly/catboost_q90_rent_total_log.cbm",
    "models/rent_daily/model_metadata.json",
    "models/rent_daily/evaluation.json",
    "models/rent_daily/catboost_q10_rent_total_log.cbm",
    "models/rent_daily/catboost_q50_rent_total_log.cbm",
    "models/rent_daily/catboost_q90_rent_total_log.cbm",
]

HOST_REQUIRED_PATHS = [
    "Dockerfile",
    "docker-compose.yml",
]


def check_required_paths() -> None:
    required_paths = list(REQUIRED_PATHS)
    if not Path("/.dockerenv").exists():
        required_paths.extend(HOST_REQUIRED_PATHS)

    missing = [path for path in required_paths if not (ROOT / path).exists()]
    if missing:
        missing_text = "\n".join(f"- {path}" for path in missing)
        raise SystemExit(f"Missing required deployment files:\n{missing_text}")
    print(f"[OK] Required files present: {len(required_paths)}", flush=True)


def check_model_prediction() -> None:
    model_service = PriceModelService(
        metadata_path=ROOT / "model_metadata.json",
        models_dir=ROOT / "models",
    )
    df = pd.read_csv(ROOT / "df_check.csv", nrows=1)
    predictions = model_service.predict(df).predictions

    required_keys = {
        "pred_price_per_m2_q10",
        "pred_price_per_m2_q50",
        "pred_price_per_m2_q90",
        "interval_width_pct",
    }
    missing = required_keys.difference(predictions)
    if missing:
        raise SystemExit(f"Prediction output is missing keys: {sorted(missing)}")

    q10 = float(predictions["pred_price_per_m2_q10"][0])
    q50 = float(predictions["pred_price_per_m2_q50"][0])
    q90 = float(predictions["pred_price_per_m2_q90"][0])
    if not (0 < q10 <= q90 and q50 > 0):
        raise SystemExit(
            "Prediction sanity check failed: "
            f"q10={q10:,.0f}, q50={q50:,.0f}, q90={q90:,.0f}"
        )

    print(
        "[OK] Sample prediction: "
        f"q10={q10:,.0f}, q50={q50:,.0f}, q90={q90:,.0f} KZT/m2",
        flush=True,
    )


def run_command(args: list[str]) -> None:
    print(f"[RUN] {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def check_rental_predictions() -> None:
    for period in ("monthly", "daily"):
        dataset = ROOT / "data" / f"rent_{period}_raw.csv"
        if not dataset.exists():
            raise SystemExit(f"Missing rental validation dataset: {dataset}")
        raw = pd.read_csv(dataset, nrows=1).iloc[0].to_dict()
        prediction = RentalPredictionService(ROOT, period).predict_raw_listing(raw)
        if not (0 < prediction.pred_rent_q10 <= prediction.pred_rent_q90):
            raise SystemExit(
                f"Rental prediction sanity check failed for {period}: {prediction}"
            )
        print(
            f"[OK] {period} rental sample: "
            f"q10={prediction.pred_rent_q10:,.0f}, "
            f"q50={prediction.pred_rent_q50:,.0f}, "
            f"q90={prediction.pred_rent_q90:,.0f} KZT",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deployment readiness checks.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run slower feature-pipeline and model validation scripts.",
    )
    parser.add_argument(
        "--model-rows",
        type=int,
        default=200000,
        help="Rows passed to validate_models.py when --full is used.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    check_required_paths()
    check_model_prediction()
    check_rental_predictions()

    if args.full:
        run_command([sys.executable, "scripts/validate_feature_pipeline.py"])
        run_command(
            [
                sys.executable,
                "scripts/validate_models.py",
                "--rows",
                str(args.model_rows),
            ]
        )

    print("[OK] Deployment checks passed.", flush=True)


if __name__ == "__main__":
    main()
