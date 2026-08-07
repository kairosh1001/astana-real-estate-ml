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


REQUIRED_PATHS = [
    "model_metadata.json",
    "df_check.csv",
    "scrape.py",
    "scripts/telegram_bot.py",
    "models/catboost_q10_price_per_m2_log.cbm",
    "models/catboost_q50_price_per_m2_log.cbm",
    "models/catboost_q90_price_per_m2_log.cbm",
    "app/main.py",
    "app/home_matcher.py",
    "app/data/astana_pois.json",
    "app/templates/base.html",
    "app/templates/home_finder.html",
    "app/templates/feedback.html",
    "app/templates/feedback_admin.html",
    "app/templates/traffic.html",
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


def check_refresh_schedule() -> None:
    crontab = (ROOT / "deploy" / "crontab.example").read_text(encoding="utf-8")
    required = [
        "--kind daily --pages 100",
        "--kind weekly --pages 200",
        "flock -w 21600 /tmp/krisha-refresh.lock",
    ]
    missing = [value for value in required if value not in crontab]
    if missing:
        raise SystemExit(f"Refresh schedule is missing safeguards: {missing}")
    print("[OK] Refresh schedule uses 100/200 pages and a shared lock.", flush=True)


def run_command(args: list[str]) -> None:
    print(f"[RUN] {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


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
    check_refresh_schedule()

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
