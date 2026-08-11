from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.feature_pipeline_v2 import (
    PoiCatalog,
    build_model_features_v2,
    build_universal_feature_config,
    universal_model_metadata,
)


DEFAULT_CATALOG = ROOT / "app" / "data" / "kazakhstan_pois.json"
DEFAULT_OUTPUT = ROOT / "data" / "universal_training_v2.csv"
DEFAULT_CONFIG = ROOT / "models_candidate" / "universal_v2_feature_config.json"
DEFAULT_METADATA = ROOT / "models_candidate" / "universal_v2_model_metadata.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build one city-aware Astana + Almaty model dataset."
    )
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="Raw Krisha CSV; repeat to combine cities.",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config-output", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--metadata-output", type=Path, default=DEFAULT_METADATA)
    parser.add_argument(
        "--keep-duplicate-urls",
        action="store_true",
        help="Keep repeated URL snapshots instead of retaining the latest one.",
    )
    return parser.parse_args()


def atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def atomic_write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    args = parse_args()
    missing = [str(path) for path in args.input if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Raw input files do not exist: {missing}")
    raw_parts = []
    for path in args.input:
        frame = pd.read_csv(path, low_memory=False)
        frame["_source_file"] = path.name
        raw_parts.append(frame)
        print(f"[INFO] {path}: {len(frame):,} raw rows")
    raw = pd.concat(raw_parts, ignore_index=True, sort=False)
    catalog = PoiCatalog.load(args.catalog)
    config = build_universal_feature_config(raw)
    features = build_model_features_v2(
        raw,
        config,
        catalog,
        include_target=True,
        filter_training_rows=True,
        deduplicate_listings=not args.keep_duplicate_urls,
        include_metadata=True,
    )
    atomic_write_csv(features, args.output)
    atomic_write_json(config.to_dict(), args.config_output)
    atomic_write_json(universal_model_metadata(catalog), args.metadata_output)

    city_counts = features["city"].value_counts().sort_index().to_dict()
    null_share = features.isna().mean().sort_values(ascending=False).head(10)
    print(f"[OK] Saved {len(features):,} model rows to {args.output}")
    print(f"[QA] Rows by city: {city_counts}")
    print("[QA] Highest numeric missing shares:")
    for column, share in null_share.items():
        print(f"  {column}: {share:.1%}")
    print(f"[OK] Feature config: {args.config_output}")
    print(f"[OK] Model metadata: {args.metadata_output}")


if __name__ == "__main__":
    main()
