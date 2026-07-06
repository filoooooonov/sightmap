"""Attach OSM names to the regions extracted by aggregate.py.

One Overpass query per city fetches named tourist POIs, parks, monuments
and neighbourhood labels inside the city bbox; each region then takes the
best-scoring candidate near its centroid (class priority x distance).
Parent regions prefer area names (Montmartre), children prefer concrete
sights (Sacre-Coeur). Writes names back into <city>_regions.geojson.

Run after aggregate.py: python name_regions.py [city ...]
"""

import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

import duckdb

from aggregate import CITIES

WEB_DATA = pathlib.Path(__file__).parent / "web" / "data"
OVERTURE_DIR = pathlib.Path(__file__).parent / "data" / "overture"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "sightmap-poc/0.1 (github.com/filoooooonov/sightmap)"

# Overture category leaf -> coarse bucket, for region typing and for
# picking which Overture places may name a region
OVERTURE_BUCKETS = {
    "culture": ("museum", "gallery", "monument", "historical", "landmark",
                "tourist_attraction", "theater", "theatre", "church",
                "cathedral", "temple", "palace", "castle", "art_", "opera"),
    "nature": ("park", "garden", "beach", "nature", "zoo", "botanical",
               "forest", "trail", "viewpoint", "lake"),
    "food": ("restaurant", "cafe", "coffee", "bakery", "dessert", "bistro",
             "brunch", "pizzeria", "food_", "creperie", "ice_cream"),
    "nightlife": ("bar", "pub", "night_club", "nightlife", "brewery",
                  "lounge", "karaoke", "cocktail", "beer_"),
}


def bucket_of(category: str) -> str | None:
    for b, keys in OVERTURE_BUCKETS.items():
        if any(k in category for k in keys):
            return b
    return None


def load_overture(city: str):
    """(naming candidates, venue list) from the Overture places parquet.
    Only culture/nature places may *name* a region (a hotspot shouldn't be
    named after a random bistro); every bucketed venue votes on its type."""
    path = OVERTURE_DIR / f"{city}.parquet"
    if not path.exists():
        return [], []
    rows = duckdb.connect().execute(
        f"""
        SELECT names.primary, categories.primary, confidence,
               bbox.xmin AS lon, bbox.ymin AS lat
        FROM '{path.as_posix()}'
        WHERE names.primary IS NOT NULL AND categories.primary IS NOT NULL
        """
    ).fetchall()
    naming, venues = [], []
    for name, cat, conf, lon, lat in rows:
        b = bucket_of(cat)
        if b is None or conf is None:
            continue
        venues.append((lat, lon, b, conf))
        if b in ("culture", "nature") and conf >= 0.55 and len(name) <= 40:
            naming.append({
                "name": name, "lat": lat, "lon": lon,
                "kind": ("poi", 2.4 * (0.5 + conf / 2)), "sem": b,
            })
    return naming, venues


def region_type(region, venues, totals) -> str | None:
    """Type = the venue bucket most over-represented here relative to its
    citywide total. Absolute counts fail in dense centers (restaurants are
    everywhere, so everything types 'food'); lift makes five of the city's
    museums beat a hundred of its forty thousand restaurants."""
    lon, lat = region["geometry"]["coordinates"]
    r = max(250.0, region["properties"]["radius_m"])
    local: dict[str, float] = defaultdict(float)
    for vlat, vlon, b, conf in venues:
        if dist_m(lat, lon, vlat, vlon) <= r:
            local[b] += conf
    best, best_lift = None, 0.0
    for b, v in local.items():
        if v < 2.0 or totals.get(b, 0.0) <= 0:
            continue
        lift = v / totals[b]
        if lift > best_lift:
            best, best_lift = b, lift
    return best

QUERY = """
[out:json][timeout:120];
(
  nwr["name"]["tourism"~"^(attraction|museum|gallery|zoo|aquarium|theme_park|viewpoint)$"]({bbox});
  nwr["name"]["historic"~"^(castle|palace|monument|memorial|fort|citywalls|city_gate|ruins|archaeological_site)$"]({bbox});
  nwr["name"]["leisure"~"^(park|garden|stadium)$"]({bbox});
  nwr["name"]["man_made"~"^(tower|lighthouse|bridge)$"]({bbox});
  nwr["name"]["amenity"="place_of_worship"]({bbox});
  nwr["name"]["railway"="station"]({bbox});
  nwr["name"]["place"="square"]({bbox});
  node["name"]["place"~"^(suburb|neighbourhood|quarter|island|islet)$"]({bbox});
);
out center tags;
"""


def fetch_candidates(bbox) -> list[dict]:
    lon_min, lat_min, lon_max, lat_max = bbox
    q = QUERY.format(bbox=f"{lat_min},{lon_min},{lat_max},{lon_max}")
    body = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(OVERPASS, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as resp:
                elements = json.load(resp)["elements"]
            break
        except Exception as e:  # 429/504 happen; back off and retry
            if attempt == 2:
                raise
            print(f"  overpass retry ({e})")
            time.sleep(20 * (attempt + 1))
    out = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name:en") or tags.get("name")
        if not name or len(name) > 40:
            continue
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None:
            continue
        cls, weight, sem = kind(tags)
        # fame proxies: famous things have wikipedia articles, and real
        # sights have physical footprints (way/relation, not a plaque node)
        if "wikipedia" in tags or "wikidata" in tags:
            weight *= 1.8
        if el.get("type") in ("way", "relation"):
            weight *= 1.25
        out.append({"name": name, "lat": lat, "lon": lon, "kind": (cls, weight), "sem": sem})
    return out


def kind(tags) -> tuple[str, float, str | None]:
    """(class, weight, semantic bucket) — how strongly a candidate
    'explains' a hotspot, and what its own nature implies about the spot."""
    if tags.get("tourism") in {"museum", "zoo", "aquarium", "theme_park", "viewpoint", "gallery"}:
        return ("poi", 3.0, "culture")
    if tags.get("tourism") == "attraction":
        return ("poi", 2.6, "culture")  # noisy tag: any curiosity is an "attraction"
    if tags.get("historic") in {"memorial"}:
        return ("poi", 1.6, "culture")  # every square has a statue; rarely the reason
    if "historic" in tags:
        return ("poi", 2.7, "culture")
    if tags.get("place") == "square":
        return ("poi", 2.6, None)
    if tags.get("man_made"):
        return ("poi", 2.4, "culture")
    if tags.get("leisure") in {"park", "garden"}:
        return ("poi", 2.3, "nature")
    if tags.get("railway") == "station":
        return ("poi", 2.2, None)
    if tags.get("leisure") == "stadium":
        return ("poi", 2.0, None)
    if tags.get("amenity") == "place_of_worship":
        return ("poi", 2.0, "culture")
    return ("place", 1.9, None)  # suburb / neighbourhood / island


def dist_m(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians(lat1))
    dy = (lat2 - lat1) * 110_540
    return math.hypot(dx, dy)


def pick_name(region, candidates) -> tuple[str, str | None] | None:
    p = region["properties"]
    # anchor on the strongest cell: a big region's centroid can drift off
    # its actual sight (the Louvre region's centroid sits by the river)
    lon, lat = p.get("peak_at", region["geometry"]["coordinates"])
    search_r = max(300.0, p["radius_m"] * 1.3)
    best, best_score = None, 0.0
    for c in candidates:
        d = dist_m(lat, lon, c["lat"], c["lon"])
        if d > search_r:
            continue
        cls, weight = c["kind"]
        # parents are districts -> area names count extra (strongly so for
        # big ones: Montmartre should beat the museum at its foot); children
        # are concrete sights -> POI names count extra
        if p["level"] == "parent" and cls == "place":
            weight *= 2.4 if p["radius_m"] >= 450 else 1.6
        if p["level"] == "child" and cls == "place":
            weight *= 0.7
        score = weight * (1.0 - d / (1.5 * search_r))
        if score > best_score:
            best, best_score = c, score
    return (best["name"], best.get("sem")) if best else None


def main() -> None:
    targets = sys.argv[1:] or list(CITIES)
    for city in targets:
        candidates = fetch_candidates(CITIES[city])
        ov_naming, venues = load_overture(city)
        candidates = candidates + ov_naming
        totals: dict[str, float] = defaultdict(float)
        for _vlat, _vlon, b, conf in venues:
            totals[b] += conf
        # one candidates fetch names the base regions and every category's
        # regions (<city>_regions.geojson, <city>_regions_<cat>.geojson)
        counts = []
        for path in sorted(WEB_DATA.glob(f"{city}_regions*.geojson")):
            data = json.loads(path.read_text(encoding="utf-8"))
            named = typed = 0
            for f in data["features"]:
                picked = pick_name(f, candidates)
                sem = None
                if picked:
                    f["properties"]["name"], sem = picked
                    named += 1
                # the name's own nature beats the venue mix: a region named
                # after the Pantheon is culture even in a bar district
                rtype = sem or region_type(f, venues, totals)
                if rtype:
                    f["properties"]["type"] = rtype
                    typed += 1
            path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            counts.append(
                f"{path.stem.replace(city + '_regions', '') or 'base'} {named}/{len(data['features'])}"
            )
        print(
            f"{city}: {len(candidates):,} candidates ({len(ov_naming):,} overture), "
            f"{len(venues):,} venues; named " + ", ".join(counts)
        )
        time.sleep(3)  # be polite to overpass


if __name__ == "__main__":
    main()
