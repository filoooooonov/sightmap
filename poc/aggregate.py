"""Aggregate geotagged photo points into H3 hexagon cells for one city.

Reads the local yfcc_points.parquet produced by extract_points.py, filters to
the city bounding box, bins points into H3 cells at several resolutions, and
writes a GeoJSON FeatureCollection the web map can load directly.

Interestingness score per cell = log1p(distinct photographers), normalized to
0..1 within the city per resolution. Distinct photographers, not photo count,
so one prolific uploader can't dominate a cell.

Usage: python aggregate.py [city ...]   (default: helsinki)
"""

import json
import math
import pathlib
import sys
from collections import defaultdict

import duckdb
import h3

DATA = pathlib.Path(__file__).parent / "data"
WEB_DATA = pathlib.Path(__file__).parent / "web" / "data"
POINTS = DATA / "yfcc_points.parquet"

RESOLUTIONS = [7, 8, 9]

# lon_min, lat_min, lon_max, lat_max
CITIES = {
    "helsinki": (24.70, 60.05, 25.30, 60.35),
    "paris": (2.20, 48.79, 2.50, 48.94),
    "london": (-0.30, 51.42, 0.10, 51.60),
    "barcelona": (2.05, 41.32, 2.28, 41.47),
    "newyork": (-74.05, 40.66, -73.85, 40.85),
}


def build_city(con: duckdb.DuckDBPyConnection, city: str) -> None:
    lon_min, lat_min, lon_max, lat_max = CITIES[city]
    rows = con.execute(
        f"""
        SELECT uid, lat, lon FROM '{POINTS.as_posix()}'
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
        """,
        [lon_min, lon_max, lat_min, lat_max],
    ).fetchall()
    print(f"{city}: {len(rows):,} points, ", end="")

    features = []
    for res in RESOLUTIONS:
        cells: dict[str, set] = defaultdict(set)
        photos: dict[str, int] = defaultdict(int)
        for uid, lat, lon in rows:
            cell = h3.latlng_to_cell(lat, lon, res)
            cells[cell].add(uid)
            photos[cell] += 1

        max_score = max(
            (math.log1p(len(u)) for u in cells.values()), default=1.0
        )
        for cell, uids in cells.items():
            boundary = h3.cell_to_boundary(cell)  # [(lat, lng), ...]
            ring = [[lng, lat] for lat, lng in boundary]
            ring.append(ring[0])
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        "res": res,
                        "users": len(uids),
                        "photos": photos[cell],
                        "score": round(math.log1p(len(uids)) / max_score, 4),
                    },
                }
            )

    # Heatmap points: res-10 cells (~66m) with log-scaled photographer score.
    # Raw photo points don't work — KDE sums them, so the center clamps long
    # before sparse areas register. Log compression must happen before
    # rendering. Positions are each cell's photo centroid, not the hex
    # center, so no lattice shows through the blur.
    heat_users: dict[str, set] = defaultdict(set)
    heat_pos: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for uid, lat, lon in rows:
        cell = h3.latlng_to_cell(lat, lon, 10)
        heat_users[cell].add(uid)
        p = heat_pos[cell]
        p[0] += lon
        p[1] += lat
        p[2] += 1
    # Smooth scores over the H3 neighborhood: each cell spills half its value
    # into its 6 neighbors (including empty ones). Nearby spots merge into
    # contiguous regions of interest and lone specks get averaged down.
    raw = {cell: math.log1p(len(u)) for cell, u in heat_users.items()}
    smoothed: dict[str, float] = defaultdict(float)
    for cell, val in raw.items():
        smoothed[cell] += val
        for n in h3.grid_ring(cell, 1):
            smoothed[n] += val * 0.5
    # Local contrast: score each cell against the strongest spot in its own
    # ~3km neighbourhood (res-7 parent + ring), blended toward the citywide
    # max. A real cluster in a quiet suburb (Otaniemi) keeps a high score,
    # while a lone photographer is never his own reference (floor) and
    # stays dim everywhere.
    LOCAL_BLEND = 0.15  # 0 = fully local contrast, 1 = citywide only
    LOCAL_FLOOR = math.log1p(4)
    parent_max: dict[str, float] = defaultdict(float)
    for cell, val in smoothed.items():
        p = h3.cell_to_parent(cell, 7)
        parent_max[p] = max(parent_max[p], val)
    hmax = max(smoothed.values(), default=1.0)
    heat_feats = []
    for cell, val in smoothed.items():
        p = h3.cell_to_parent(cell, 7)
        local_max = max(parent_max.get(n, 0.0) for n in h3.grid_disk(p, 1))
        ref = max(local_max, LOCAL_FLOOR)
        ref = ref + LOCAL_BLEND * (hmax - ref)
        w = val / ref
        if w < 0.02:
            continue
        if cell in heat_pos:
            lon_sum, lat_sum, n = heat_pos[cell]
            coords = [round(lon_sum / n, 5), round(lat_sum / n, 5)]
        else:  # spill-over cell with no photos of its own
            lat_c, lon_c = h3.cell_to_latlng(cell)
            coords = [round(lon_c, 5), round(lat_c, 5)]
        heat_feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": coords},
                "properties": {"w": round(w, 3)},
            }
        )
    heat_out = {"type": "FeatureCollection", "features": heat_feats}
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    heat_path = WEB_DATA / f"{city}_heat.geojson"
    heat_path.write_text(json.dumps(heat_out, separators=(",", ":")), encoding="utf-8")

    # center on where the photos actually are, not the bbox middle
    if rows:
        center = [
            round(sum(r[2] for r in rows) / len(rows), 5),
            round(sum(r[1] for r in rows) / len(rows), 5),
        ]
    else:
        center = [(lon_min + lon_max) / 2, (lat_min + lat_max) / 2]
    out = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {"city": city, "center": center},
    }
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    path = WEB_DATA / f"{city}.geojson"
    path.write_text(json.dumps(out), encoding="utf-8")
    n9 = sum(1 for f in features if f["properties"]["res"] == 9)
    print(f"{n9:,} res-9 cells -> {path.name}, {len(heat_feats):,} heat points -> {heat_path.name}")


def main() -> None:
    targets = sys.argv[1:] or ["helsinki"]
    unknown = [c for c in targets if c not in CITIES]
    if unknown:
        sys.exit(f"unknown cities: {unknown}; known: {list(CITIES)}")
    con = duckdb.connect()
    for city in targets:
        build_city(con, city)


if __name__ == "__main__":
    main()
