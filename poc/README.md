# Sightmap PoC — photo-density heatmap

Recreates the core of sightsmap.com: places where more people take photos are
"more interesting". Data source: the YFCC15M subset of YFCC100M (geotagged
Creative Commons Flickr photos, 2004–2014), streamed from Hugging Face.

## Pipeline

1. **`download_parquet.sh`** — one-time. Downloads the 10 parquet shards
   (~1.7 GB) from Hugging Face sequentially with retries (HF rate-limits
   parallel anonymous range requests, so remote DuckDB scans get 429s).
2. **`extract_points.py`** — one-time. Extracts photoid, photographer uid,
   lat, lon, datetaken for every geotagged photo into
   `data/yfcc_points.parquet`.
3. **`aggregate.py [city ...]`** — filters points to a city bounding box
   (presets in `CITIES`), bins them into H3 hexagons at resolutions 7/8/9,
   scores each cell by `log1p(distinct photographers)` normalized to 0..1,
   and writes `web/data/<city>.geojson`.
4. **`web/index.html`** — MapLibre GL map (free Carto basemaps) rendering the
   hexagons with a sequential blue ramp, hover tooltips, city switcher,
   hex-size control, light/dark theme.

## Run

```
pip install duckdb h3
sh download_parquet.sh              # once, ~1.7 GB
python extract_points.py            # once, ~a minute
python aggregate.py helsinki paris  # any preset cities
python -m http.server 8000 -d web   # then open http://localhost:8000
```

## Design decisions

- **Distinct photographers, not photo count** — one enthusiast uploading 800
  shots shouldn't make a spot "interesting" (same choice original Sightsmap made).
- **log1p + per-city normalization** — photo counts follow a power law; without
  the log, only the single top landmark would be visible.
- **H3 hexagons over raw heatmap** — stable cells we can later attach labels,
  categories (sunset/party/food), and place names to.
