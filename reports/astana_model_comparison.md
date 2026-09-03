# Astana Sale Model Comparison

Dataset: `data\universal_training_v2.csv`
Known scrape timestamps: 151 of 16,226 rows (0.93%); known range 2026-08-11T16:52:12+00:00 to 2026-08-11T17:01:40+00:00.
Common held-out test rows: 2,390

All three models are evaluated on the same deterministic Astana test groups. The legacy comparison model is freshly retrained with the v1 feature contract to avoid evaluating a production artifact that may have seen these listings.

| Model | Log RMSE | Median APE | MAE, KZT/m² | q10-q90 coverage |
|---|---:|---:|---:|---:|
| astana_v2 | 0.128471 | 7.35% | 62,954 | 79.58% |
| astana_v1_retrained | 0.130245 | 7.61% | 64,040 | 79.37% |
| universal_v2 | 0.135244 | 7.83% | 66,918 | 79.25% |

Winner by held-out log RMSE: **astana_v2**.

## Existing production v1 (reference only)

The existing production artifact scores 0.110219 log RMSE on these rows, but it is excluded from model selection because its historical training membership is not recorded and may overlap this test set.

## Paired property-group bootstrap

- `astana_v1_retrained_minus_astana_v2`: mean 0.001806; 95% CI [-0.000075, 0.003731]; P(winner better)=96.9%.
- `universal_v2_minus_astana_v2`: mean 0.006749; 95% CI [0.004254, 0.009140]; P(winner better)=100.0%.

## Caveats

- This is a grouped holdout from the same accumulated listing period, not a future-period test.
- Listing prices are public asking prices, not completed transaction prices.
- Feature mappings and the frozen OSM catalog are target-free but were built before the split.
- Most legacy source rows do not contain a scrape timestamp, so this run cannot establish full dataset freshness.
- Small room segments, especially 5+, remain less stable than 1-3 room segments.
