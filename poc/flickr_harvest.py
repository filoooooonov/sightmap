"""Harvest fresh geotagged photo metadata from the live Flickr API.

YFCC ends in 2014; this pulls photos taken 2015->today for the preset city
bboxes via flickr.photos.search. A single query returns at most ~4000
unique results, so dense bboxes are recursively quartered until each leaf
fits, then paged through (geo queries cap at 250/page). Only metadata is
stored (id, owner, coords, date, tags) — no images.

Needs a free API key: https://www.flickr.com/services/apps/create/apply/
Put it in poc/.flickr_key (gitignored) or the FLICKR_API_KEY env var.

Usage: python flickr_harvest.py [city ...]   (default: all presets)
Output: data/flickr/<city>.parquet, same schema as yfcc_points.parquet.
"""

import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

import duckdb

from aggregate import CITIES

HERE = pathlib.Path(__file__).parent
OUT_DIR = HERE / "data" / "flickr"
API = "https://api.flickr.com/services/rest/"
MIN_TAKEN = "2015-01-01"  # YFCC covers everything before this
PER_PAGE = 250            # geo queries cap at 250 per page
MAX_TOTAL = 3500          # split the bbox when a query claims more
MAX_PAGES = 16            # 16 x 250 = the ~4000-unique-results ceiling
MIN_SPAN = 0.002          # ~200m; below this stop splitting, take what fits

KEY = ""
calls = 0


def load_key() -> str:
    key = os.environ.get("FLICKR_API_KEY", "")
    keyfile = HERE / ".flickr_key"
    if not key and keyfile.exists():
        key = keyfile.read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("No API key. Put it in poc/.flickr_key or set FLICKR_API_KEY.\n"
                 "Get one at https://www.flickr.com/services/apps/create/apply/")
    return key


def call(params: dict, tries: int = 4) -> dict:
    global calls
    q = {
        "method": "flickr.photos.search",
        "api_key": KEY,
        "format": "json",
        "nojsoncallback": 1,
        "has_geo": 1,
        "min_taken_date": MIN_TAKEN,
        "extras": "geo,date_taken,tags",
        "per_page": PER_PAGE,
        **params,
    }
    url = API + "?" + urllib.parse.urlencode(q)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.load(r)
            if data.get("stat") == "ok":
                calls += 1
                time.sleep(1.0)  # stay under 3600 req/hour
                return data["photos"]
            raise RuntimeError(f"flickr: {data.get('message', 'unknown error')}")
        except Exception as e:
            if attempt == tries - 1:
                raise
            print(f"  retry ({e})")
            time.sleep(10 * (attempt + 1))


def bbox_str(b) -> str:
    return ",".join(f"{v:.6f}" for v in b)


def add(resp: dict, photos: dict) -> None:
    for ph in resp.get("photo", []):
        try:
            lat, lon = float(ph["latitude"]), float(ph["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if lat == 0 and lon == 0:
            continue
        photos[ph["id"]] = {
            "photoid": ph["id"],
            "uid": ph["owner"],
            "lat": lat,
            "lon": lon,
            "datetaken": ph.get("datetaken", ""),
            # API tags are space-separated; the pipeline expects commas
            "usertags": ",".join(ph.get("tags", "").split()),
        }


def harvest(bbox, photos: dict, depth: int = 0) -> None:
    first = call({"bbox": bbox_str(bbox), "page": 1})
    total = int(first["total"])
    lon_min, lat_min, lon_max, lat_max = bbox
    if total > MAX_TOTAL and (lon_max - lon_min) > MIN_SPAN:
        lon_mid = (lon_min + lon_max) / 2
        lat_mid = (lat_min + lat_max) / 2
        for quad in (
            (lon_min, lat_min, lon_mid, lat_mid),
            (lon_mid, lat_min, lon_max, lat_mid),
            (lon_min, lat_mid, lon_mid, lat_max),
            (lon_mid, lat_mid, lon_max, lat_max),
        ):
            harvest(quad, photos, depth + 1)
        return
    add(first, photos)
    for page in range(2, min(int(first["pages"]), MAX_PAGES) + 1):
        add(call({"bbox": bbox_str(bbox), "page": page}), photos)
    if depth <= 2:
        print(f"  depth-{depth} leaf done: total~{total:,}, collected {len(photos):,}")


def main() -> None:
    global KEY
    KEY = load_key()
    targets = sys.argv[1:] or list(CITIES)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for city in targets:
        print(f"{city}: harvesting {MIN_TAKEN}->today")
        t0 = time.time()
        photos: dict = {}
        harvest(CITIES[city], photos)
        tmp = OUT_DIR / f"{city}.jsonl"
        with tmp.open("w", encoding="utf-8") as f:
            for row in photos.values():
                f.write(json.dumps(row) + "\n")
        out = OUT_DIR / f"{city}.parquet"
        con.execute(
            f"""
            COPY (
                SELECT CAST(photoid AS BIGINT) AS photoid, uid,
                       CAST(lat AS DOUBLE) AS lat, CAST(lon AS DOUBLE) AS lon,
                       datetaken, usertags
                FROM read_json('{tmp.as_posix()}', format='newline_delimited')
            ) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        tmp.unlink()
        print(f"{city}: {len(photos):,} photos, {calls} API calls, "
              f"{(time.time() - t0) / 60:.1f} min -> {out.name}")


if __name__ == "__main__":
    main()
