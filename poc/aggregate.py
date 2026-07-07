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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


def all_cities() -> dict:
    """Preset cities plus the phase-1 queue (cities_phase1.json)."""
    cities = dict(CITIES)
    extra = pathlib.Path(__file__).parent / "cities_phase1.json"
    if extra.exists():
        cities.update(
            {k: tuple(v) for k, v in json.loads(extra.read_text(encoding="utf-8")).items()}
        )
    return cities


def city_tz(city: str) -> ZoneInfo:
    """Timezone for golden-hour math: presets are hardcoded, everything
    else resolved from the bbox center via timezonefinder."""
    if city in CITY_TZ:
        return ZoneInfo(CITY_TZ[city])
    from timezonefinder import TimezoneFinder  # lazy: ~50MB dataset

    lon_min, lat_min, lon_max, lat_max = all_cities()[city]
    tzname = TimezoneFinder().timezone_at(
        lng=(lon_min + lon_max) / 2, lat=(lat_min + lat_max) / 2
    )
    return ZoneInfo(tzname or "UTC")


CELL_AREA_M2 = 15_047  # average H3 res-10 cell area


def user_weight(uid: str) -> float:
    """Flickr photographers (NSIDs like 12345@N00) are interest signal;
    Commons uploaders document everything systematically (every metro
    station gets photographed), so they count half."""
    return 1.0 if "@N" in uid else 0.5


def wcount(uids) -> float:
    return sum(user_weight(u) for u in uids)


def cell_coords(cell: str, heat_pos: dict) -> list[float]:
    """Photo centroid of a cell if it has photos, else the cell center."""
    if cell in heat_pos:
        lon_sum, lat_sum, n = heat_pos[cell]
        return [round(lon_sum / n, 5), round(lat_sum / n, 5)]
    lat_c, lon_c = h3.cell_to_latlng(cell)
    return [round(lon_c, 5), round(lat_c, 5)]


def connected_components(cells: set) -> list[list[str]]:
    comps: list[list[str]] = []
    seen: set = set()
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        stack, comp = [start], []
        while stack:
            c = stack.pop()
            comp.append(c)
            for n in h3.grid_ring(c, 1):
                if n in cells and n not in seen:
                    seen.add(n)
                    stack.append(n)
        comps.append(comp)
    return comps


def region_props(comp, wmap, heat_users, heat_pos):
    """Stats for one connected blob of cells.

    prominence = peak minus the highest value just outside the blob: an
    isolated island (zoo) keeps ~its full peak, a fragment glued to a big
    hotspot scores ~0 and can be dropped or absorbed.
    """
    comp_set = set(comp)
    peak = max(wmap[c] for c in comp)
    # background = strongest cell 2-3 rings out. The ring immediately
    # outside is the region's own smoothing halo — measuring there made
    # every isolated region look non-prominent.
    d1: set = set()
    for c in comp:
        for n in h3.grid_ring(c, 1):
            if n not in comp_set:
                d1.add(n)
    ring_prev, skip = d1, comp_set | d1
    bg = 0.0
    for _ in range(2):
        ring_next: set = set()
        for c in ring_prev:
            for n in h3.grid_ring(c, 1):
                if n not in skip:
                    ring_next.add(n)
        bg = max([bg, *(wmap.get(n, 0.0) for n in ring_next)])
        skip |= ring_next
        ring_prev = ring_next
    users: set = set()
    for c in comp:
        users |= heat_users.get(c, set())
    lon = lat = wsum = 0.0
    for c in comp:
        w = wmap[c]
        clon, clat = cell_coords(c, heat_pos)
        lon += clon * w
        lat += clat * w
        wsum += w
    peak_cell = max(comp, key=lambda c: wmap[c])
    return {
        "peak": round(peak, 3),
        "prom": round(max(0.0, peak - bg), 3),
        # weighted evidence (commons counts half) so pill ranking matches
        # the heat scoring; close enough to "photographers" for display
        "users": round(wcount(users)),
        "center": [round(lon / wsum, 5), round(lat / wsum, 5)],
        "peak_at": cell_coords(peak_cell, heat_pos),
        "radius_m": round(max(120.0, math.sqrt(len(comp) * CELL_AREA_M2 / math.pi))),
    }


def extract_regions(wmap, heat_users, heat_pos):
    """Discrete regions of interest from the scored cell field.

    Two thresholds give a 2-level hierarchy: loose components are parent
    regions (districts), strict components nested inside them are child
    regions (individual sights). Parents are kept on evidence (enough
    photographers) or prominence (locally outstanding, e.g. an island zoo).
    """
    # Density-adaptive threshold: with dense data a fixed cutoff percolates —
    # the whole center fuses into one mega-region that swallows every
    # landmark. Raise the parent threshold until the largest component is
    # district-sized (~180 res-10 cells ≈ 2.7 km²).
    MAX_COMP_CELLS = 180
    parent_t = 0.25
    while parent_t < 0.55:
        parents = connected_components({c for c, w in wmap.items() if w >= parent_t})
        if max((len(c) for c in parents), default=0) <= MAX_COMP_CELLS:
            break
        parent_t += 0.05
    children = connected_components(
        {c for c, w in wmap.items() if w >= parent_t + 0.25}
    )

    kept = []
    for comp in parents:
        p = region_props(comp, wmap, heat_users, heat_pos)
        if p["users"] < 5 and p["prom"] < 0.35:
            continue
        kept.append((comp, p))
    kept.sort(key=lambda t: t[1]["peak"] * 0.6 + t[1]["prom"] * 0.4, reverse=True)
    kept = kept[:40]

    cell_to_parent: dict[str, int] = {}
    feats = []
    for i, (comp, p) in enumerate(kept):
        for c in comp:
            cell_to_parent[c] = i
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": p["center"]},
                "properties": {
                    "level": "parent", "id": i, "rank": i,
                    "users": p["users"], "peak": p["peak"],
                    "prom": p["prom"], "radius_m": p["radius_m"],
                    "peak_at": p["peak_at"],
                },
            }
        )

    # children grouped per parent; a lone child that mirrors its whole
    # parent adds nothing, so it is dropped
    by_parent: dict[int, list] = defaultdict(list)
    for comp in children:
        pid = cell_to_parent.get(comp[0])
        if pid is None:
            continue
        p = region_props(comp, wmap, heat_users, heat_pos)
        if p["users"] < 3:
            continue
        by_parent[pid].append(p)
    n_children = 0
    for pid, kids in by_parent.items():
        parent_users = feats[pid]["properties"]["users"]
        if len(kids) == 1 and kids[0]["users"] >= 0.75 * parent_users:
            continue
        for p in sorted(kids, key=lambda k: k["peak"], reverse=True)[:6]:
            feats.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": p["center"]},
                    "properties": {
                        "level": "child", "parent": pid,
                        "users": p["users"], "peak": p["peak"],
                        "prom": p["prom"], "radius_m": p["radius_m"],
                    "peak_at": p["peak_at"],
                    },
                }
            )
            n_children += 1
    return {"type": "FeatureCollection", "features": feats}, len(kept), n_children


CITY_TZ = {
    "helsinki": "Europe/Helsinki",
    "paris": "Europe/Paris",
    "london": "Europe/London",
    "barcelona": "Europe/Madrid",
    "newyork": "America/New_York",
}

# golden hour = sun elevation in this band (deg above horizon)
GOLDEN_LO, GOLDEN_HI = -4.0, 6.0


def sun_elevation_deg(dt_utc: datetime, lat: float, lon: float) -> float:
    """Sun elevation via the NOAA low-precision formulas (~0.01 deg)."""
    d = (dt_utc - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    e = math.radians(23.439 - 0.00000036 * d)
    ra_h = math.degrees(math.atan2(math.cos(e) * math.sin(lam), math.cos(lam))) / 15
    decl = math.asin(math.sin(e) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians(((gmst + lon / 15 - ra_h) % 24) * 15)
    lat_r = math.radians(lat)
    return math.degrees(
        math.asin(math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(ha))
    )


def golden_flags(tagged, city: str) -> list:
    """(uid, lat, lon, is_golden) for YFCC rows with a trustworthy capture
    time. Commons rows are excluded (their datetaken is the upload time),
    as are unset camera clocks (exact midnight) and junk years."""
    tz = city_tz(city)
    out = []
    for uid, lat, lon, _tags, dt in tagged:
        if "@N" not in uid or not dt:
            continue
        try:
            local = datetime.fromisoformat(str(dt)[:19])
        except ValueError:
            continue
        if local.hour == 0 and local.minute == 0 and local.second == 0:
            continue
        if not 2000 <= local.year <= 2026:
            continue
        try:
            utc = local.replace(tzinfo=tz).astimezone(timezone.utc)
        except Exception:
            continue
        elev = sun_elevation_deg(utc, lat, lon)
        out.append((uid, lat, lon, GOLDEN_LO <= elev <= GOLDEN_HI))
    return out


def golden_layers(flags):
    """Score cells by golden-hour *share*, not count — the count map is just
    the popularity map again. Shrinkage toward the citywide share keeps
    low-evidence cells dark; full heat at ~2.5x the city average."""
    g_users: dict[str, set] = defaultdict(set)
    t_users: dict[str, set] = defaultdict(set)
    g_pos: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for uid, lat, lon, gold in flags:
        cell = h3.latlng_to_cell(lat, lon, 10)
        t_users[cell].add(uid)
        if gold:
            g_users[cell].add(uid)
            p = g_pos[cell]
            p[0] += lon
            p[1] += lat
            p[2] += 1

    def smooth(users_by_cell):
        s: dict[str, float] = defaultdict(float)
        for cell, users in users_by_cell.items():
            v = wcount(users)
            s[cell] += v
            for n in h3.grid_ring(cell, 1):
                s[n] += v * 0.5
        return s

    gs, ts = smooth(g_users), smooth(t_users)
    total_g = sum(wcount(u) for u in g_users.values())
    total_t = sum(wcount(u) for u in t_users.values())
    p0 = total_g / total_t if total_t else 0.0
    ALPHA = 8.0
    EVIDENCE = 8.0  # golden mass needed for full score: 2-of-2 flukes stay dim
    wmap: dict[str, float] = {}
    for cell, t in ts.items():
        if p0 <= 0:
            break
        g = gs.get(cell, 0.0)
        share = (g + ALPHA * p0) / (t + ALPHA)
        lift = max(0.0, min(1.0, (share - p0) / (1.5 * p0)))
        wmap[cell] = lift * min(1.0, g / EVIDENCE)
    # normalize to the layer's own max like every other layer, so the best
    # golden spot renders at full heat instead of drowning under the slider
    wmax = max(wmap.values(), default=0.0)
    if wmax > 0:
        wmap = {c: w / wmax for c, w in wmap.items()}
    heat_feats = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": cell_coords(cell, g_pos)},
            "properties": {"w": round(w, 3)},
        }
        for cell, w in wmap.items()
        if w >= 0.02
    ]
    heat_out = {"type": "FeatureCollection", "features": heat_feats}
    regions_out, n_parents, n_children = extract_regions(wmap, g_users, g_pos)
    return heat_out, regions_out, p0, n_parents


# Interest categories: photo qualifies if any of its usertags matches.
# Tags are user-entered, lowercase, comma-separated (URL-encoded).
CATEGORIES = {
    "sunset": {
        "sunset", "sunsets", "sunrise", "dusk", "dawn", "twilight",
        "goldenhour", "golden+hour", "atardecer", "sonnenuntergang",
        "auringonlasku", "solnedgang",
    },
    "party": {
        "party", "nightlife", "club", "nightclub", "clubbing", "concert",
        "gig", "festival", "rave", "dj", "bar", "pub", "karaoke", "disco",
    },
    "food": {
        "food", "restaurant", "restaurants", "cafe", "caf%c3%a9", "coffee",
        "breakfast", "brunch", "lunch", "dinner", "foodporn", "market",
        "streetfood", "pizza", "sushi", "dessert", "bakery", "wine", "beer",
    },
    "nature": {
        "nature", "park", "garden", "gardens", "forest", "beach", "lake",
        "river", "flowers", "trees", "botanical", "wildlife", "birds",
        "hiking", "sea", "island", "autumn", "spring",
    },
    "architecture": {
        "architecture", "building", "buildings", "church", "cathedral",
        "castle", "palace", "bridge", "tower", "skyscraper", "facade",
        "dome", "monument", "ruins", "modernism",
    },
    "art": {
        "art", "streetart", "street+art", "graffiti", "mural", "murals",
        "museum", "gallery", "sculpture", "statue", "exhibition", "mosaic",
    },
}


def heat_and_regions(rows):
    """rows [(uid, lat, lon), ...] -> (heat geojson, regions geojson, stats).

    Heatmap points: res-10 cells (~66m) with log-scaled photographer score.
    Raw photo points don't work — KDE sums them, so the center clamps long
    before sparse areas register. Log compression must happen before
    rendering. Positions are each cell's photo centroid, not the hex
    center, so no lattice shows through the blur.
    """
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
    raw = {cell: math.log1p(wcount(u)) for cell, u in heat_users.items()}
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
    wmap: dict[str, float] = {}
    for cell, val in smoothed.items():
        p = h3.cell_to_parent(cell, 7)
        local_max = max(parent_max.get(n, 0.0) for n in h3.grid_disk(p, 1))
        ref = max(local_max, LOCAL_FLOOR)
        ref = ref + LOCAL_BLEND * (hmax - ref)
        wmap[cell] = val / ref
    heat_feats = []
    for cell, w in wmap.items():
        if w < 0.02:
            continue
        heat_feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": cell_coords(cell, heat_pos)},
                "properties": {"w": round(w, 3)},
            }
        )
    heat_out = {"type": "FeatureCollection", "features": heat_feats}
    regions_out, n_parents, n_children = extract_regions(wmap, heat_users, heat_pos)
    return heat_out, regions_out, (len(heat_feats), n_parents, n_children)


def point_sources() -> str:
    """YFCC baseline plus any fresh harvests (Flickr API, Wikimedia Commons)."""
    files = [POINTS.as_posix()]
    for src in ("flickr", "commons"):
        files += sorted(p.as_posix() for p in (DATA / src).glob("*.parquet"))
    return "[" + ", ".join(f"'{f}'" for f in files) + "]"


def build_city(con: duckdb.DuckDBPyConnection, city: str) -> None:
    lon_min, lat_min, lon_max, lat_max = all_cities()[city]
    tagged = con.execute(
        f"""
        SELECT uid, lat, lon, usertags, datetaken
        FROM read_parquet({point_sources()}, union_by_name=true)
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
        """,
        [lon_min, lon_max, lat_min, lat_max],
    ).fetchall()
    rows = [(uid, lat, lon) for uid, lat, lon, _, _ in tagged]
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
            (math.log1p(wcount(u)) for u in cells.values()), default=1.0
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
                        "score": round(math.log1p(wcount(uids)) / max_score, 4),
                    },
                }
            )

    WEB_DATA.mkdir(parents=True, exist_ok=True)
    heat_out, regions_out, (n_heat, n_parents, n_children) = heat_and_regions(rows)
    heat_path = WEB_DATA / f"{city}_heat.geojson"
    heat_path.write_text(json.dumps(heat_out, separators=(",", ":")), encoding="utf-8")
    regions_path = WEB_DATA / f"{city}_regions.geojson"
    regions_path.write_text(json.dumps(regions_out, separators=(",", ":")), encoding="utf-8")

    # golden hour: time-of-day signal, independent of tagging discipline
    flags = golden_flags(tagged, city)
    g_heat, g_regions, p0, g_parents = golden_layers(flags)
    (WEB_DATA / f"{city}_heat_golden.geojson").write_text(
        json.dumps(g_heat, separators=(",", ":")), encoding="utf-8"
    )
    (WEB_DATA / f"{city}_regions_golden.geojson").write_text(
        json.dumps(g_regions, separators=(",", ":")), encoding="utf-8"
    )

    cat_stats = [f"golden {len(flags):,}t/{p0:.0%}/{g_parents}r"]
    for cat, keywords in CATEGORIES.items():
        sub = [
            (uid, lat, lon)
            for uid, lat, lon, tags, _ in tagged
            if tags and not keywords.isdisjoint(tags.split(","))
        ]
        c_heat, c_regions, (nh, np_, nc) = heat_and_regions(sub)
        (WEB_DATA / f"{city}_heat_{cat}.geojson").write_text(
            json.dumps(c_heat, separators=(",", ":")), encoding="utf-8"
        )
        (WEB_DATA / f"{city}_regions_{cat}.geojson").write_text(
            json.dumps(c_regions, separators=(",", ":")), encoding="utf-8"
        )
        cat_stats.append(f"{cat} {len(sub):,}p/{np_}r")

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
    print(
        f"{n9:,} res-9 cells, {n_heat:,} heat points, "
        f"{n_parents} regions / {n_children} sub-spots; " + ", ".join(cat_stats)
    )


def main() -> None:
    cities = all_cities()
    targets = sys.argv[1:] or ["helsinki"]
    unknown = [c for c in targets if c not in cities]
    if unknown:
        sys.exit(f"unknown cities: {unknown}; {len(cities)} known")
    con = duckdb.connect()
    for city in targets:
        build_city(con, city)


if __name__ == "__main__":
    main()
