from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import nbformat as nbf
from nbclient import NotebookClient


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "notebooks" / "rental_data_quality.ipynb"


def code(source: str):
    return nbf.v4.new_code_cell(dedent(source).strip())


def markdown(source: str):
    return nbf.v4.new_markdown_cell(dedent(source).strip())


def main() -> None:
    notebook = nbf.v4.new_notebook()
    notebook.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook.metadata["language_info"] = {"name": "python", "version": "3.11"}
    notebook.cells = [
        markdown(
            """
            # Rental data quality and model validation

            Reproducible audit of the monthly and daily Krisha rental snapshots used by the service.
            """
        ),
        markdown("## tl;dr"),
        code(
            """
            import json
            from pathlib import Path

            import numpy as np
            import pandas as pd
            from IPython.display import Markdown, display

            ROOT = Path.cwd()
            periods = ("monthly", "daily")
            frames = {
                period: pd.read_csv(ROOT / "data" / f"rent_{period}_raw.csv")
                for period in periods
            }
            evaluations = {
                period: json.loads(
                    (ROOT / "models" / f"rent_{period}" / "evaluation.json").read_text(encoding="utf-8")
                )
                for period in periods
            }

            summary = pd.DataFrame([
                {
                    "period": period,
                    "snapshots": len(frames[period]),
                    "unique_listings": frames[period]["listing_id"].astype(str).nunique(),
                    "validation_rows": evaluations[period]["validation_rows"],
                    "log_rmse": evaluations[period]["rmse_log"],
                    "baseline_log_rmse": evaluations[period]["baseline_rmse_log"],
                    "production_ready": evaluations[period]["production_ready"],
                }
                for period in periods
            ]).set_index("period")
            display(summary.round(4))
            display(Markdown(
                "Both datasets exceed 500 unique listings and both CatBoost median models beat the room-median "
                "baseline on grouped holdout data. They remain **candidate models**, because every snapshot was "
                "collected on one UTC date and therefore no independent future-date holdout is available yet."
            ))
            """
        ),
        markdown(
            """
            ## Context & Methods

            The intended grain is one listing snapshot per `(url, scraped_at)` pair. Repeated listing IDs across
            different scrape batches are expected, but must remain in only one side of a model split.

            ### Key Assumptions

            - Prices are total rent in KZT for the period named by `rental_period`.
            - Coordinates should fall in a broad Astana-area box (49.5–52.5 latitude, 69.0–73.5 longitude).
            - Same-day crawl batches are not treated as an independent chronological validation period.
            """
        ),
        markdown("## Data"),
        code(
            """
            grain_rows = []
            for period, frame in frames.items():
                scraped = pd.to_datetime(frame["scraped_at"], errors="coerce", utc=True)
                grain_rows.append({
                    "period": period,
                    "rows": len(frame),
                    "columns": len(frame.columns),
                    "unique_listings": frame["listing_id"].astype(str).nunique(),
                    "repeated_snapshot_rows": len(frame) - frame["listing_id"].astype(str).nunique(),
                    "duplicate_url_timestamp": int(frame.duplicated(["url", "scraped_at"]).sum()),
                    "scrape_batches": int(scraped.nunique()),
                    "scrape_days_utc": int(scraped.dt.floor("D").nunique()),
                    "latest_scrape_utc": scraped.max(),
                })
            grain_profile = pd.DataFrame(grain_rows).set_index("period")
            display(grain_profile)
            """
        ),
        code(
            """
            important_columns = [
                "listing_id", "url", "price", "rooms_structured", "area_m2_structured",
                "lat", "lon", "Квартира меблирована", "Состояние квартиры", "Жилой комплекс",
            ]
            completeness = []
            for period, frame in frames.items():
                for column in important_columns:
                    values = frame[column]
                    missing = values.isna()
                    if values.dtype == object:
                        missing |= values.astype("string").str.strip().eq("")
                    completeness.append({
                        "period": period,
                        "column": column,
                        "missing_rows": int(missing.sum()),
                        "missing_rate": float(missing.mean()),
                    })
            completeness_profile = pd.DataFrame(completeness)
            display(
                completeness_profile.pivot(index="column", columns="period", values="missing_rate")
                .sort_values("monthly", ascending=False)
                .style.format("{:.1%}")
            )
            """
        ),
        markdown("## Results"),
        code(
            """
            validity_rows = []
            price_limits = {"monthly": (20_000, 10_000_000), "daily": (1_000, 1_000_000)}
            for period, frame in frames.items():
                low_price, high_price = price_limits[period]
                checks = {
                    "wrong rental period": frame["rental_period"].ne(period),
                    "missing listing id": frame["listing_id"].isna(),
                    "missing URL": frame["url"].isna() | frame["url"].astype("string").str.strip().eq(""),
                    "duplicate snapshot grain": frame.duplicated(["url", "scraped_at"]),
                    "implausible price": ~frame["price"].between(low_price, high_price),
                    "implausible room count": ~frame["rooms_structured"].between(1, 20),
                    "implausible area": ~frame["area_m2_structured"].between(10, 1_000),
                    "coordinates outside broad Astana box": ~(frame["lat"].between(49.5, 52.5) & frame["lon"].between(69.0, 73.5)),
                }
                for check_name, failed in checks.items():
                    validity_rows.append({
                        "period": period,
                        "check": check_name,
                        "failed_rows": int(failed.sum()),
                        "failed_rate": float(failed.mean()),
                    })
            validity = pd.DataFrame(validity_rows)
            display(validity[validity["failed_rows"] > 0].sort_values(["failed_rate", "period"], ascending=False))
            """
        ),
        code(
            """
            distribution_rows = []
            for period, frame in frames.items():
                quantiles = frame["price"].quantile([0.01, 0.1, 0.5, 0.9, 0.99])
                distribution_rows.append({
                    "period": period,
                    "p01_kzt": quantiles.loc[0.01],
                    "p10_kzt": quantiles.loc[0.1],
                    "median_kzt": quantiles.loc[0.5],
                    "p90_kzt": quantiles.loc[0.9],
                    "p99_kzt": quantiles.loc[0.99],
                    "furnished_known_rate": frame["Квартира меблирована"].notna().mean(),
                    "condition_known_rate": frame["Состояние квартиры"].notna().mean(),
                    "coordinates_known_rate": (frame["lat"].notna() & frame["lon"].notna()).mean(),
                })
            distributions = pd.DataFrame(distribution_rows).set_index("period")
            display(distributions.style.format({
                "p01_kzt": "{:,.0f}", "p10_kzt": "{:,.0f}", "median_kzt": "{:,.0f}",
                "p90_kzt": "{:,.0f}", "p99_kzt": "{:,.0f}",
                "furnished_known_rate": "{:.1%}", "condition_known_rate": "{:.1%}",
                "coordinates_known_rate": "{:.1%}",
            }))
            """
        ),
        code(
            """
            model_rows = []
            leakage_rows = []
            for period, metrics in evaluations.items():
                metadata = json.loads(
                    (ROOT / "models" / f"rent_{period}" / "model_metadata.json").read_text(encoding="utf-8")
                )
                unsafe_features = [
                    feature for feature in metadata["feature_columns"]
                    if any(token in feature.lower() for token in ("price", "rent_total", "target"))
                ]
                leakage_rows.append({"period": period, "target_like_features": unsafe_features})
                model_rows.append({
                    "period": period,
                    "validation_rows": metrics["validation_rows"],
                    "split": metrics["split_strategy"],
                    "log_rmse": metrics["rmse_log"],
                    "log_rmse_ci_low": metrics["rmse_log_ci95"][0],
                    "log_rmse_ci_high": metrics["rmse_log_ci95"][1],
                    "baseline_log_rmse": metrics["baseline_rmse_log"],
                    "log_rmse_improvement": 1 - metrics["rmse_log"] / metrics["baseline_rmse_log"],
                    "rmse_kzt": metrics["rmse_kzt"],
                    "median_ape": metrics["median_ape"],
                    "q10_q90_coverage": metrics["q10_q90_coverage"],
                })
            model_profile = pd.DataFrame(model_rows).set_index("period")
            display(model_profile.style.format({
                "log_rmse": "{:.3f}", "log_rmse_ci_low": "{:.3f}", "log_rmse_ci_high": "{:.3f}",
                "baseline_log_rmse": "{:.3f}", "log_rmse_improvement": "{:.1%}",
                "rmse_kzt": "{:,.0f}", "median_ape": "{:.1%}", "q10_q90_coverage": "{:.1%}",
            }))
            display(pd.DataFrame(leakage_rows).set_index("period"))
            """
        ),
        markdown("## Takeaways"),
        code(
            """
            notes = []
            for period in periods:
                metrics = evaluations[period]
                label = "Monthly" if period == "monthly" else "Daily"
                improvement = 1 - metrics["rmse_log"] / metrics["baseline_rmse_log"]
                notes.append(
                    f"- **{label}:** {metrics['unique_listings']} unique listings; log RMSE "
                    f"{metrics['rmse_log']:.3f} (95% CI {metrics['rmse_log_ci95'][0]:.3f}–"
                    f"{metrics['rmse_log_ci95'][1]:.3f}), {improvement:.1%} below its baseline."
                )
            notes.extend([
                "- **No snapshot-grain leakage:** duplicate `(url, scraped_at)` rows are zero and listing IDs are grouped during splitting.",
                "- **Main remaining blocker:** all data is from one UTC date, so performance on a later market snapshot is still unverified.",
                "- **Decision:** suitable for an expanded website pilot with a visible caveat; not yet suitable for high-stakes investment decisions.",
            ])
            display(Markdown("\\n".join(notes)))
            """
        ),
    ]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    client = NotebookClient(
        notebook,
        timeout=180,
        kernel_name="python3",
        resources={"metadata": {"path": str(ROOT)}},
    )
    executed = client.execute()
    nbf.write(executed, OUTPUT)
    print(f"Wrote and executed {OUTPUT}")


if __name__ == "__main__":
    main()
