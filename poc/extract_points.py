"""Extract geotagged photo points from the YFCC15M parquet shards.

Reads the shards downloaded by download_parquet.sh (HF rate-limits remote
scans, so we work from local copies) and writes a compact parquet with just
the columns we need (photographer, coords, timestamp) for downstream
aggregation. One-time step; everything after runs on the output file.

Usage: python extract_points.py [shard.parquet ...]   (default: all downloaded)
Rerun any time more shards have finished downloading.
"""

import pathlib
import sys

import duckdb

SHARDS = pathlib.Path(__file__).parent / "data" / "parquet"

OUT = pathlib.Path(__file__).parent / "data" / "yfcc_points.parquet"
OUT.parent.mkdir(exist_ok=True)

files = sys.argv[1:] or sorted(SHARDS.glob("*.parquet"))
files = [pathlib.Path(f) for f in files]

con = duckdb.connect()

urls_sql = ", ".join(f"'{pathlib.Path(f).as_posix()}'" for f in files)
urls_sql = f"[{urls_sql}]"
print(f"extracting from {len(files)} shard(s)")
con.execute(
    f"""
    COPY (
        SELECT
            photoid,
            uid,
            TRY_CAST(latitude AS DOUBLE)  AS lat,
            TRY_CAST(longitude AS DOUBLE) AS lon,
            datetaken,
            usertags
        FROM read_parquet({urls_sql})
        WHERE TRY_CAST(latitude AS DOUBLE) IS NOT NULL
          AND TRY_CAST(longitude AS DOUBLE) IS NOT NULL
    ) TO '{OUT.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """
)

n = con.execute(f"SELECT COUNT(*) FROM '{OUT.as_posix()}'").fetchone()[0]
print(f"wrote {n:,} geotagged points to {OUT}")
