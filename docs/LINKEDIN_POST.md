# LinkedIn Post Draft

I built and deployed an end-to-end ML web app for finding potentially undervalued apartment listings in Astana.

The app scrapes public listings from Krisha.kz, processes property and geospatial features, uses CatBoost quantile regression to estimate market price per square meter, and ranks listings where the asking price appears lower than the model's conservative q10 estimate.

What I implemented:

- scraping and preprocessing pipeline;
- feature engineering for property, district, residential complex, floor, and location signals;
- CatBoost q10/q50/q90 models;
- FastAPI web app with filters, map-based search, listing comparison, and model explanations;
- SQLite persistence, scheduled refresh jobs, admin monitoring, Docker deployment, and HTTPS on a VPS.

Live demo: https://kvartiry-ai.kz

GitHub: https://github.com/kairosh1001/astana-real-estate-ml

This project helped me practice the full ML product lifecycle: data collection, modeling, backend development, deployment, monitoring, and user-facing product design.
