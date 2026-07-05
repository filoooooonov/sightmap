# Sightmap PoC — photo-density map

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
   (presets in `CITIES`) and writes, per city and per interest category
   (all/sunset/party/food/nature/architecture/art, matched on Flickr
   usertags — see `CATEGORIES`):
   - `web/data/<city>.geojson` — H3 hexagons (res 7/8/9) scored by
     `log1p(distinct photographers)`, for the hex view;
   - `web/data/<city>_heat.geojson` — res-10 cells with neighbourhood-smoothed,
     locally contrast-normalized weights, for the gradient heatmap;
   - `web/data/<city>_regions.geojson` — discrete regions of interest:
     connected components at two thresholds (parent districts / child
     sights), each with photographer count, peak, and prominence vs. its
     own surroundings.
4. **`name_regions.py [city ...]`** — attaches OSM names to regions via one
   Overpass query per city (POIs + neighbourhood labels, fame-boosted by
   Wikipedia/Wikidata presence, anchored on each region's peak cell).
5. **`web/index.html`** — MapLibre GL map (free Carto basemaps): thermal
   gradient heatmap with a user threshold slider, named clickable region
   pills with viewport-based selection and parent→child reveal on zoom,
   region glows, hex view, hover cards, fly-to animations, light/dark theme.

## Run

```
pip install duckdb h3
sh download_parquet.sh              # once, ~1.7 GB
python extract_points.py            # once, ~a minute
python aggregate.py helsinki paris  # any preset cities
python name_regions.py helsinki paris   # needs network (Overpass)
python -m http.server 8000 -d web   # then open http://localhost:8000
```

## Design decisions

- **Distinct photographers, not photo count** — one enthusiast uploading 800
  shots shouldn't make a spot "interesting" (same choice original Sightsmap made).
- **log1p + per-city normalization** — photo counts follow a power law; without
  the log, only the single top landmark would be visible.
- **Local contrast scoring** — each cell is scored against the strongest spot
  in its ~3km neighbourhood (blended 15% toward the citywide max, with a
  ~4-photographer noise floor), so a real cluster in a quiet suburb stays
  visible while lone-photo specks don't.
- **Regions over field** — the heatmap is ambience; the product data model is
  the extracted region list (identity, stats, hierarchy, prominence). Names,
  categories, and tap-interactions all hang off regions, not pixels.
- **H3 hexagons kept** — stable cells for future per-category scoring
  (sunset/party/food) and drill-down.
