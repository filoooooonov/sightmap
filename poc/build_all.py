"""Build every known city end to end: Overture places, aggregation,
naming/typing, and the web app's city index.

Resumable: cities that already have <city>_regions.geojson are skipped,
so it is safe to kill and rerun. Designed to run detached; progress goes
to stdout (redirect to build_all.log).

Usage: python build_all.py [city ...]   (default: everything unbuilt)
"""

import json
import pathlib
import subprocess
import sys
import time

from aggregate import all_cities, CITIES
from gen_cities import QUEUE

HERE = pathlib.Path(__file__).parent
WEB_DATA = HERE / "web" / "data"
OVERTURE = HERE / "data" / "overture"

PRESET_LABELS = {
    "helsinki": "Helsinki", "paris": "Paris", "london": "London",
    "barcelona": "Barcelona", "newyork": "New York",
}


def label_of(slug: str) -> str:
    if slug in PRESET_LABELS:
        return PRESET_LABELS[slug]
    if slug in QUEUE:
        return QUEUE[slug].split(",")[0]
    return slug.capitalize()


def sh(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True)


def write_city_index() -> int:
    have = sorted(
        p.stem for p in WEB_DATA.glob("*.geojson") if "_" not in p.stem
    )
    index = {slug: label_of(slug) for slug in have}
    (WEB_DATA / "cities.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )
    return len(index)


def main() -> None:
    cities = all_cities()
    targets = sys.argv[1:] or list(cities)
    todo = [c for c in targets if not (WEB_DATA / f"{c}_regions.geojson").exists()]
    print(f"{len(todo)} cities to build: {', '.join(todo[:10])}...", flush=True)
    OVERTURE.mkdir(parents=True, exist_ok=True)
    failed = []
    for i, city in enumerate(todo):
        t0 = time.time()
        b = cities[city]
        ov = OVERTURE / f"{city}.parquet"
        if not ov.exists() or ov.stat().st_size < 100_000:  # 8KB = empty stub
            ov.unlink(missing_ok=True)
            r = sh(["overturemaps", "download", f"--bbox={b[0]},{b[1]},{b[2]},{b[3]}",
                    "-f", "geoparquet", "-t", "place", "-o", str(ov)])
            if r.returncode != 0 or not ov.exists():
                print(f"{city}: overture failed, continuing without types "
                      f"({r.stderr.strip()[-160:]})", flush=True)
                ov.unlink(missing_ok=True)
        ok = True
        for step in (["python", "aggregate.py", city], ["python", "name_regions.py", city]):
            r = sh(step)
            if r.returncode != 0:
                print(f"{city}: {step[1]} FAILED: {r.stderr.strip()[-300:]}", flush=True)
                failed.append(city)
                ok = False
                break
        if ok:
            print(f"[{i + 1}/{len(todo)}] {city} ok, {(time.time() - t0) / 60:.1f} min", flush=True)
    n = write_city_index()
    print(f"DONE: {len(todo) - len(failed)} built, {len(failed)} failed "
          f"({', '.join(failed) or 'none'}); cities.json lists {n}", flush=True)


if __name__ == "__main__":
    main()
