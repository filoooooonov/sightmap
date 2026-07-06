"""Harvest fresh geotagged photo metadata from Wikimedia Commons.

Free, no API key (Flickr gates new keys behind Pro). Uses generator=geosearch
on the Commons API: each call returns up to 500 File: pages in a bbox with
coordinates + uploader + upload timestamp; tiles that hit the cap are
recursively quartered. Only metadata is stored — no images.

Usage: python commons_harvest.py [city ...]   (default: all presets)
Output: data/commons/<city>.parquet, same schema as yfcc_points.parquet
(usertags empty — Commons categories could fill them later).
"""

import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

import duckdb

from aggregate import CITIES

HERE = pathlib.Path(__file__).parent
OUT_DIR = HERE / "data" / "commons"
API = "https://commons.wikimedia.org/w/api.php"
UA = "sightmap-poc/0.1 (https://github.com/filoooooonov/sightmap)"
TILE_CAP = 490   # ggslimit is 500; at ~this many results assume truncation
MIN_SPAN = 0.002  # ~200m; below this stop splitting
MAX_TILE = 0.05   # geosearch rejects big bboxes ("Bounding box is too big")
calls = 0


def all_cities() -> dict:
    """Preset cities plus the phase-1 queue (cities_phase1.json)."""
    cities = dict(CITIES)
    extra = HERE / "cities_phase1.json"
    if extra.exists():
        cities.update(
            {k: tuple(v) for k, v in json.loads(extra.read_text(encoding="utf-8")).items()}
        )
    return cities


def call(params: dict, tries: int = 4) -> dict:
    global calls
    q = {"action": "query", "format": "json", "formatversion": 2, "maxlag": 5, **params}
    url = API + "?" + urllib.parse.urlencode(q)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            if "error" in data:
                raise RuntimeError(data["error"].get("info", "api error"))
            calls += 1
            time.sleep(0.2)
            return data
        except Exception as e:
            if attempt == tries - 1:
                raise
            print(f"  retry ({e})")
            time.sleep(8 * (attempt + 1))


def bbox_str(b) -> str:
    return ",".join(f"{v:.6f}" for v in b)


def fetch_tile(bbox) -> list[dict]:
    """All File: pages in bbox with coords/user/timestamp, following continues."""
    lon_min, lat_min, lon_max, lat_max = bbox
    base = {
        "generator": "geosearch",
        "ggsbbox": f"{lat_max}|{lon_min}|{lat_min}|{lon_max}",
        "ggsnamespace": 6,
        "ggslimit": 500,
        "prop": "coordinates|imageinfo",
        "iiprop": "user|timestamp",
        "colimit": "max",
    }
    pages: dict[int, dict] = {}
    cont: dict = {}
    for _ in range(40):  # imageinfo pages 50 at a time over 500 results
        data = call({**base, **cont})
        for p in data.get("query", {}).get("pages", []):
            cur = pages.setdefault(p["pageid"], {})
            if p.get("coordinates"):
                cur["lat"] = p["coordinates"][0]["lat"]
                cur["lon"] = p["coordinates"][0]["lon"]
            if p.get("imageinfo"):
                cur["uid"] = p["imageinfo"][0].get("user", "")
                cur["ts"] = p["imageinfo"][0].get("timestamp", "")
            cur["pageid"] = p["pageid"]
        if "continue" not in data:
            break
        cont = data["continue"]
    return [p for p in pages.values() if "lat" in p and "uid" in p]


class CityState:
    """Crash-resumable progress: every finished leaf tile appends its rows
    to a jsonl and its bbox to a ledger, so a killed run restarts where it
    stopped instead of redoing an hour of API calls."""

    def __init__(self, city: str):
        self.jsonl = OUT_DIR / f"{city}.jsonl"
        self.done_f = OUT_DIR / f"{city}.done"
        self.splits_f = OUT_DIR / f"{city}.splits"
        self.photos: dict = {}
        self.done: set = set()
        self.splits: set = set()
        if self.jsonl.exists():
            with self.jsonl.open(encoding="utf-8") as f:
                for line in f:
                    try:  # a kill mid-append can truncate the last line
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.photos[row["photoid"]] = row
        if self.done_f.exists():
            self.done = set(self.done_f.read_text(encoding="utf-8").split())
        if self.splits_f.exists():
            self.splits = set(self.splits_f.read_text(encoding="utf-8").split())
        if self.photos:
            print(f"  resuming: {len(self.photos):,} files, {len(self.done)} tiles done")

    def leaf_done(self, key: str, new_rows: list) -> None:
        with self.jsonl.open("a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row) + "\n")
        with self.done_f.open("a", encoding="utf-8") as f:
            f.write(key + "\n")
        self.done.add(key)

    def mark_split(self, key: str) -> None:
        with self.splits_f.open("a", encoding="utf-8") as f:
            f.write(key + "\n")
        self.splits.add(key)

    def cleanup(self) -> None:
        for p in (self.jsonl, self.done_f, self.splits_f):
            p.unlink(missing_ok=True)


def split(bbox, st: CityState, depth: int) -> None:
    lon_min, lat_min, lon_max, lat_max = bbox
    lon_mid = (lon_min + lon_max) / 2
    lat_mid = (lat_min + lat_max) / 2
    for quad in (
        (lon_min, lat_min, lon_mid, lat_mid),
        (lon_mid, lat_min, lon_max, lat_mid),
        (lon_min, lat_mid, lon_mid, lat_max),
        (lon_mid, lat_mid, lon_max, lat_max),
    ):
        harvest(quad, st, depth + 1)


def harvest(bbox, st: CityState, depth: int = 0) -> None:
    key = bbox_str(bbox)
    lon_min, lat_min, lon_max, lat_max = bbox
    too_big = (lon_max - lon_min) > MAX_TILE or (lat_max - lat_min) > MAX_TILE
    if too_big or key in st.splits:
        split(bbox, st, depth)
        return
    if key in st.done:
        return
    rows = fetch_tile(bbox)
    if len(rows) >= TILE_CAP and (lon_max - lon_min) > MIN_SPAN:
        st.mark_split(key)
        split(bbox, st, depth)
        return
    new_rows = []
    for r in rows:
        if r["pageid"] in st.photos:
            continue
        row = {
            "photoid": r["pageid"],
            "uid": r["uid"],
            "lat": r["lat"],
            "lon": r["lon"],
            "datetaken": r.get("ts", ""),
            "usertags": "",
        }
        st.photos[r["pageid"]] = row
        new_rows.append(row)
    st.leaf_done(key, new_rows)
    if depth <= 2:
        print(f"  depth-{depth} tile: {len(rows)} files, collected {len(st.photos):,}", flush=True)


def main() -> None:
    cities = all_cities()
    targets = sys.argv[1:] or list(cities)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for city in targets:
        out = OUT_DIR / f"{city}.parquet"
        if out.exists():
            print(f"{city}: {out.name} already exists, skipping")
            continue
        print(f"{city}: harvesting Wikimedia Commons", flush=True)
        t0 = time.time()
        st = CityState(city)
        harvest(cities[city], st)
        con.execute(
            f"""
            COPY (
                SELECT CAST(photoid AS BIGINT) AS photoid, uid,
                       CAST(lat AS DOUBLE) AS lat, CAST(lon AS DOUBLE) AS lon,
                       datetaken, usertags
                FROM read_json('{st.jsonl.as_posix()}', format='newline_delimited')
            ) TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        st.cleanup()
        print(f"{city}: {len(st.photos):,} files, {calls} API calls, "
              f"{(time.time() - t0) / 60:.1f} min -> {out.name}", flush=True)


if __name__ == "__main__":
    main()
