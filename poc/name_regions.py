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

from aggregate import CITIES

WEB_DATA = pathlib.Path(__file__).parent / "web" / "data"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "sightmap-poc/0.1 (github.com/filoooooonov/sightmap)"

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
        cls, weight = kind(tags)
        # fame proxies: famous things have wikipedia articles, and real
        # sights have physical footprints (way/relation, not a plaque node)
        if "wikipedia" in tags or "wikidata" in tags:
            weight *= 1.8
        if el.get("type") in ("way", "relation"):
            weight *= 1.25
        out.append({"name": name, "lat": lat, "lon": lon, "kind": (cls, weight)})
    return out


def kind(tags) -> tuple[str, float]:
    """(class, weight) — how strongly a candidate 'explains' a hotspot."""
    if tags.get("tourism") in {"museum", "zoo", "aquarium", "theme_park", "viewpoint", "gallery"}:
        return ("poi", 3.0)
    if tags.get("tourism") == "attraction":
        return ("poi", 2.6)  # noisy tag: any curiosity can be an "attraction"
    if tags.get("historic") in {"memorial"}:
        return ("poi", 1.6)  # every square has a statue; rarely the reason
    if "historic" in tags:
        return ("poi", 2.7)
    if tags.get("place") == "square":
        return ("poi", 2.6)
    if tags.get("man_made"):
        return ("poi", 2.4)
    if tags.get("leisure") in {"park", "garden"}:
        return ("poi", 2.3)
    if tags.get("railway") == "station":
        return ("poi", 2.2)
    if tags.get("leisure") == "stadium":
        return ("poi", 2.0)
    if tags.get("amenity") == "place_of_worship":
        return ("poi", 2.0)
    return ("place", 1.9)  # suburb / neighbourhood / island


def dist_m(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians(lat1))
    dy = (lat2 - lat1) * 110_540
    return math.hypot(dx, dy)


def pick_name(region, candidates) -> str | None:
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
    return best["name"] if best else None


def main() -> None:
    targets = sys.argv[1:] or list(CITIES)
    for city in targets:
        path = WEB_DATA / f"{city}_regions.geojson"
        data = json.loads(path.read_text(encoding="utf-8"))
        candidates = fetch_candidates(CITIES[city])
        named = 0
        for f in data["features"]:
            name = pick_name(f, candidates)
            if name:
                f["properties"]["name"] = name
                named += 1
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        print(f"{city}: {len(candidates):,} candidates, named {named}/{len(data['features'])} regions")
        time.sleep(3)  # be polite to overpass


if __name__ == "__main__":
    main()
