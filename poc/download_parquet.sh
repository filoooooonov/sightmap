#!/bin/sh
# Download the YFCC15M parquet shards sequentially with retries.
# HF rate-limits parallel anonymous range requests, so we fetch whole files
# one at a time instead of letting DuckDB scan them remotely.
set -e
dir="$(dirname "$0")/data/parquet"
mkdir -p "$dir"
for i in 0 1 2 3 4 5 6 7 8 9; do
  f="$dir/000$i.parquet"
  url="https://huggingface.co/datasets/mehdidc/yfcc15m/resolve/refs%2Fconvert%2Fparquet/default/partial-train/000$i.parquet"
  if [ -s "$f" ]; then echo "skip 000$i"; continue; fi
  echo "downloading 000$i..."
  curl -sSL --retry 8 --retry-delay 15 --retry-all-errors -o "$f" "$url"
  sleep 3
done
echo "done"
