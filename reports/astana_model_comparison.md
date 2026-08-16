# Astana Sale Model Comparison

Dataset: `data\universal_training_v2.csv`
Known scrape timestamps: 151 of 16,226 rows (0.93%); known range 2026-08-11T16:52:12+00:00 to 2026-08-11T17:01:40+00:00.
Common held-out test rows: 2,390

All three models are evaluated on the same deterministic Astana test groups. The legacy comparison model is freshly retrained with the v1 feature contract to avoid evaluating a production artifact that may have seen these listings.

| Model | Log RMSE | Median APE | MAE, KZT/m² | q10-q90 coverage |
|---|---:|---:|---:|---:|
| astana_v2 | 0.129988 | 7.31% | 63,605 | 80.46% |
| astana_v1_retrained | 0.131885 | 7.72% | 64,831 | 79.96% |
| universal_v2 | 0.135244 | 7.83% | 66,918 | 79.25% |

Winner by held-out log RMSE: **astana_v2**.

## Existing production v1 (reference only)

The existing production artifact scores 0.110219 log RMSE on these rows, but it is excluded from model selection because its historical training membership is not recorded and may overlap this test set.

## Paired property-group bootstrap

- `astana_v1_retrained_minus_astana_v2`: mean 0.001928; 95% CI [0.000080, 0.003778]; P(winner better)=98.0%.
- `universal_v2_minus_astana_v2`: mean 0.005236; 95% CI [0.002851, 0.007565]; P(winner better)=100.0%.

## Caveats

- This is a grouped holdout from the same accumulated listing period, not a future-period test.
- Listing prices are public asking prices, not completed transaction prices.
- Feature mappings and the frozen OSM catalog are target-free but were built before the split.
- Most legacy source rows do not contain a scrape timestamp, so this run cannot establish full dataset freshness.
- Small room segments, especially 5+, remain less stable than 1-3 room segments.
