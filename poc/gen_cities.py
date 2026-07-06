"""Generate cities_phase1.json: bboxes for the phase-1 harvest queue.

Geocodes each curated city via Nominatim (center point) and builds a
~24x24km bbox around it — administrative boundary boxes are wildly
inconsistent (Beijing municipality is the size of Belgium), fixed-size
boxes around the center are what we actually want for tourist maps.

One-off: python gen_cities.py   -> writes cities_phase1.json
"""

import json
import math
import pathlib
import time
import urllib.parse
import urllib.request

UA = "sightmap-poc/0.1 (https://github.com/filoooooonov/sightmap)"
OUT = pathlib.Path(__file__).parent / "cities_phase1.json"

# slug -> Nominatim query (country included to disambiguate)
QUEUE = {
    # Europe
    "rome": "Rome, Italy", "venice": "Venice, Italy", "florence": "Florence, Italy",
    "milan": "Milan, Italy", "naples": "Naples, Italy",
    "amsterdam": "Amsterdam, Netherlands", "prague": "Prague, Czechia",
    "vienna": "Vienna, Austria", "budapest": "Budapest, Hungary",
    "lisbon": "Lisbon, Portugal", "porto": "Porto, Portugal",
    "madrid": "Madrid, Spain", "seville": "Seville, Spain",
    "granada": "Granada, Spain", "valencia": "Valencia, Spain",
    "berlin": "Berlin, Germany", "munich": "Munich, Germany",
    "hamburg": "Hamburg, Germany", "cologne": "Cologne, Germany",
    "athens": "Athens, Greece", "istanbul": "Istanbul, Turkey",
    "dubrovnik": "Dubrovnik, Croatia", "split": "Split, Croatia",
    "edinburgh": "Edinburgh, UK", "dublin": "Dublin, Ireland",
    "stockholm": "Stockholm, Sweden", "oslo": "Oslo, Norway",
    "bergen": "Bergen, Norway", "copenhagen": "Copenhagen, Denmark",
    "reykjavik": "Reykjavik, Iceland", "tallinn": "Tallinn, Estonia",
    "riga": "Riga, Latvia", "vilnius": "Vilnius, Lithuania",
    "krakow": "Krakow, Poland", "warsaw": "Warsaw, Poland",
    "zurich": "Zurich, Switzerland", "geneva": "Geneva, Switzerland",
    "lucerne": "Lucerne, Switzerland", "brussels": "Brussels, Belgium",
    "bruges": "Bruges, Belgium", "nice": "Nice, France",
    "marseille": "Marseille, France", "lyon": "Lyon, France",
    "moscow": "Moscow, Russia", "saintpetersburg": "Saint Petersburg, Russia",
    "kyiv": "Kyiv, Ukraine",
    # Asia
    "tokyo": "Tokyo, Japan", "kyoto": "Kyoto, Japan", "osaka": "Osaka, Japan",
    "seoul": "Seoul, South Korea", "beijing": "Beijing, China",
    "shanghai": "Shanghai, China", "hongkong": "Hong Kong",
    "taipei": "Taipei, Taiwan", "singapore": "Singapore",
    "bangkok": "Bangkok, Thailand", "chiangmai": "Chiang Mai, Thailand",
    "hanoi": "Hanoi, Vietnam", "hochiminhcity": "Ho Chi Minh City, Vietnam",
    "kualalumpur": "Kuala Lumpur, Malaysia", "denpasar": "Denpasar, Bali, Indonesia",
    "delhi": "New Delhi, India", "mumbai": "Mumbai, India",
    "jaipur": "Jaipur, India", "agra": "Agra, India",
    "kathmandu": "Kathmandu, Nepal",
    # Middle East & Africa
    "dubai": "Dubai, UAE", "jerusalem": "Jerusalem",
    "telaviv": "Tel Aviv, Israel", "cairo": "Cairo, Egypt",
    "marrakech": "Marrakech, Morocco", "capetown": "Cape Town, South Africa",
    "nairobi": "Nairobi, Kenya",
    # Americas
    "sanfrancisco": "San Francisco, USA", "losangeles": "Los Angeles, USA",
    "lasvegas": "Las Vegas, USA", "seattle": "Seattle, USA",
    "chicago": "Chicago, USA", "boston": "Boston, USA",
    "washington": "Washington, DC, USA", "miami": "Miami, USA",
    "neworleans": "New Orleans, USA", "honolulu": "Honolulu, Hawaii, USA",
    "toronto": "Toronto, Canada", "vancouver": "Vancouver, Canada",
    "montreal": "Montreal, Canada", "quebeccity": "Quebec City, Canada",
    "mexicocity": "Mexico City, Mexico", "cancun": "Cancun, Mexico",
    "havana": "Havana, Cuba", "riodejaneiro": "Rio de Janeiro, Brazil",
    "buenosaires": "Buenos Aires, Argentina", "lima": "Lima, Peru",
    "cusco": "Cusco, Peru", "santiago": "Santiago, Chile",
    # Oceania
    "sydney": "Sydney, Australia", "melbourne": "Melbourne, Australia",
    "auckland": "Auckland, New Zealand", "queenstown": "Queenstown, New Zealand",
}

HALF_KM = 12.0  # half-size of the bbox in km


def geocode(q: str):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        hits = json.load(r)
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def main() -> None:
    out = {}
    for slug, q in QUEUE.items():
        hit = geocode(q)
        if not hit:
            print(f"  !! no geocode for {slug} ({q})")
            continue
        lat, lon = hit
        dlat = HALF_KM / 110.54
        dlon = HALF_KM / (111.32 * math.cos(math.radians(lat)))
        out[slug] = [round(lon - dlon, 4), round(lat - dlat, 4),
                     round(lon + dlon, 4), round(lat + dlat, 4)]
        print(f"  {slug}: {out[slug]}")
        time.sleep(1.1)  # nominatim policy: max 1 req/s
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{len(out)} cities -> {OUT.name}")


if __name__ == "__main__":
    main()
