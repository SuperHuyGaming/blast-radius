#!/bin/sh
# maps/ (built by pack_maps.py, pack_calib.py) -> mapsgz/ (committed; gunzipped in the image)
set -e
cd "$(dirname "$0")/.."
rm -rf mapsgz && mkdir mapsgz
for f in maps/*; do gzip -9 -c "$f" > "mapsgz/$(basename "$f").gz"; done
du -sh mapsgz
