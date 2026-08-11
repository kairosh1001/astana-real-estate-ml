from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from catboost import CatBoostRegressor


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.model_service import MODEL_FILENAMES


MODEL_SOURCES = {
    "q10": "model_q10.cbm",
    "q50": "model_q50.cbm",
    "q90": "model_q90.cbm",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a validated v2 notebook bundle into models/."
    )
    parser.add_argument(
        "--scope",
        choices=["universal", "almaty"],
        default="universal",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _atomic_json(payload: dict, destination: Path) -> None:
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    args = parse_args()
    bundle_name = f"{args.scope}_v2"
    candidate_dir = ROOT / "models_candidate" / bundle_name / "notebook_baseline"
    bundle_dir = ROOT / "models" / bundle_name
    feature_config_path = ROOT / "models_candidate" / "universal_v2_feature_config.json"
    base_metadata_path = ROOT / "models_candidate" / "universal_v2_model_metadata.json"
    evaluation_path = candidate_dir / "evaluation.json"
    required = [feature_config_path, base_metadata_path, evaluation_path]
    required.extend(candidate_dir / name for name in MODEL_SOURCES.values())
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Candidate artifacts are missing: {missing}")

    base_metadata = _read_json(base_metadata_path)
    evaluation = _read_json(evaluation_path)
    expected_features = list(base_metadata["feature_columns"])
    if expected_features != list(evaluation["feature_columns"]):
        raise ValueError("Feature contract differs between metadata and evaluation.")

    bundle_dir.mkdir(parents=True, exist_ok=True)
    model_artifacts = {}
    for quantile, source_name in MODEL_SOURCES.items():
        source = candidate_dir / source_name
        model = CatBoostRegressor()
        model.load_model(str(source))
        if list(model.feature_names_) != expected_features:
            raise ValueError(f"{quantile} model feature names do not match metadata.")
        destination = bundle_dir / MODEL_FILENAMES[quantile]
        _atomic_copy(source, destination)
        model_artifacts[quantile] = {
            "filename": destination.name,
            "sha256": _sha256(destination),
            "size_bytes": destination.stat().st_size,
        }

    _atomic_copy(feature_config_path, bundle_dir / "feature_config.json")
    _atomic_copy(evaluation_path, bundle_dir / "evaluation.json")

    metadata = {
        **base_metadata,
        "model_version": f"{bundle_name}_2026-08-11",
        "training_scope": args.scope,
        "serving_policy": {
            "default_routing": "city_auto",
            "astana": "legacy_astana_v1",
            "almaty": "almaty_v2",
            "reason": (
                "Retain the established Astana model and use the stronger "
                "Almaty-specific v2 model for Almaty inference."
            ),
        },
        "model_objectives": evaluation["parameters"]["objectives"],
        "quantile_calibration": evaluation["quantile_calibration"],
        "held_out_metrics": {
            "overall": evaluation["overall_test_metrics"],
            "segments": evaluation["segment_test_metrics"],
        },
        "model_artifacts": model_artifacts,
    }
    _atomic_json(metadata, bundle_dir / "model_metadata.json")
    print(f"[OK] Promoted {bundle_name} bundle to {bundle_dir}")
    print(f"[OK] Features: {len(expected_features)}")
    print(
        "[OK] Held-out log RMSE: "
        f"{evaluation['overall_test_metrics']['log_rmse']:.6f}"
    )


if __name__ == "__main__":
    main()
