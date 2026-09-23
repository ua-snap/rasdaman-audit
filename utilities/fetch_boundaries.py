#!/usr/bin/env python3
"""
fetch_boundaries.py

Pulls every polygon out of SNAP's "all_boundaries:all_areas" WFS layer,
paginated (the server default caps a single request well under the
~17,784 total features, and hammering it for everything in one request
risks overloading it), and writes two files:

    data/boundaries.geojson           -- every feature, as fetched
    data/polygon_area_buckets.json    -- small/medium/large area buckets,
                                          each with a representative bbox
                                          (width, height in meters, EPSG:3338)
                                          for recommend_tiling.py to consume

Areas and bounding boxes are computed in EPSG:3338 (Alaska Albers Equal
Area) regardless of the WFS's own CRS (EPSG:4326 lon/lat, confirmed by
inspecting a live response) -- lon/lat degrees are not an area-preserving
unit, so any histogram or bbox computed directly from them would be
distorted by latitude. This is the same CRS this repo's own worked
example (CRREL_GIPL_tiling.md) uses for the Alaska domain.

Usage
-----
    mamba activate rasdaman-tiling-utils
    python3 fetch_boundaries.py

Re-run this only if the boundary layer changes -- data/boundaries.geojson
is committed to the repo specifically so recommend_tiling.py never needs
network access.
"""

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import requests

WFS_URL = "https://gs.snap.uaf.edu/geoserver/all_boundaries/ows"
TYPE_NAME = "all_boundaries:all_areas"
PAGE_SIZE = 1000
TARGET_CRS = "EPSG:3338"

# Excluded from the size analysis by the feature's own stable `id` field,
# not by an area threshold (a threshold would silently start excluding
# different features if the layer changes). 'NC12' is "The Aleut
# Corporation" -- a Native corporation boundary at ~1.05M km^2, a full
# order of magnitude past the next-largest polygon in the whole layer
# (~523K km^2). It's a real polygon, not bad data, but it's a regional
# corporation boundary, not the kind of query-AOI shape the small/medium/
# large buckets are meant to represent -- keeping it in skewed "large"
# toward a shape no realistic query looks like.
EXCLUDED_IDS = {"NC12"}

HERE = Path(__file__).resolve().parent
OUT_GEOJSON = HERE / "data" / "boundaries.geojson"
OUT_BUCKETS = HERE / "data" / "polygon_area_buckets.json"


def fetch_page(start_index, max_features, timeout):
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": TYPE_NAME,
        "outputFormat": "application/json",
        "maxFeatures": max_features,
        "startIndex": start_index,
    }
    r = requests.get(WFS_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_all(page_size=PAGE_SIZE, sleep_s=0.5, timeout=60):
    """Paginate startIndex until every feature is collected. One request
    per page, with a short pause between them -- this is a shared public
    server, not a private one, and there's no reason to hit it back to
    back for what is at most ~18 requests."""
    features = []
    start = 0
    total = None
    crs = None
    while total is None or start < total:
        page = fetch_page(start, page_size, timeout)
        if total is None:
            total = page["totalFeatures"]
            crs = page.get("crs")
            print(f"totalFeatures reported by server: {total}")
        got = page["features"]
        if not got:
            break
        features.extend(got)
        print(f"  fetched {len(features)}/{total}")
        start += len(got)
        if start < total:
            time.sleep(sleep_s)
    return features, crs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--sleep", type=float, default=0.5,
                     help="Seconds to pause between page requests")
    ap.add_argument("--n-buckets", type=int, default=3,
                     help="How many area size classes to split into (small/medium/large = 3)")
    ap.add_argument("--exclude-ids", default=",".join(sorted(EXCLUDED_IDS)),
                     help="Comma-separated feature `id`s to drop before computing the "
                          "size buckets (default: known non-representative outliers, "
                          "e.g. regional corporation boundaries). Pass '' to keep everything.")
    ap.add_argument("--skip-fetch", action="store_true",
                     help="Reuse the existing data/boundaries.geojson instead of "
                          "re-fetching from the server -- useful for re-deriving buckets "
                          "(e.g. after changing --exclude-ids) without hitting the WFS again.")
    args = ap.parse_args()
    exclude_ids = {s for s in args.exclude_ids.split(",") if s}

    OUT_GEOJSON.parent.mkdir(parents=True, exist_ok=True)

    if args.skip_fetch:
        if not OUT_GEOJSON.exists():
            raise SystemExit(f"--skip-fetch given but {OUT_GEOJSON} doesn't exist")
        print(f"Reusing existing {OUT_GEOJSON}")
    else:
        features, crs = fetch_all(page_size=args.page_size, sleep_s=args.sleep)
        print(f"Fetched {len(features)} features total.")
        geojson = {"type": "FeatureCollection", "features": features, "crs": crs}
        with open(OUT_GEOJSON, "w") as f:
            json.dump(geojson, f)
        print(f"Wrote {OUT_GEOJSON} ({OUT_GEOJSON.stat().st_size / 1e6:.1f} MB)")

    # ---- area/bbox stats, reprojected to an equal-area CRS ----
    gdf = gpd.read_file(OUT_GEOJSON)
    gdf = gdf[gdf.geometry.notnull() & gdf.geometry.is_valid]
    gdf = gdf.set_crs("EPSG:4326", allow_override=True) if gdf.crs is None else gdf

    excluded = gdf[gdf["id"].isin(exclude_ids)]
    if len(excluded):
        names = ", ".join(f"{r['id']} ({r['name']!r})" for _, r in excluded.iterrows())
        print(f"\nExcluding {len(excluded)} feature(s) from the size analysis: {names}")
    gdf = gdf[~gdf["id"].isin(exclude_ids)]

    gdf_proj = gdf.to_crs(TARGET_CRS)

    areas_m2 = gdf_proj.geometry.area
    bounds = gdf_proj.geometry.bounds  # minx, miny, maxx, maxy
    widths_m = bounds["maxx"] - bounds["minx"]
    heights_m = bounds["maxy"] - bounds["miny"]

    print("\nArea distribution (km^2), EPSG:3338:")
    print(areas_m2.describe().apply(lambda v: v / 1e6))

    import mapclassify

    labels = (["small", "medium", "large"] if args.n_buckets == 3
              else [f"bucket_{i}" for i in range(args.n_buckets)])
    classifier = mapclassify.Quantiles(areas_m2.values, k=args.n_buckets)
    class_of = classifier.yb  # 0-indexed bucket per feature

    buckets = {}
    for i, label in enumerate(labels):
        mask = class_of == i
        n = int(mask.sum())
        if n == 0:
            continue
        buckets[label] = {
            "n_polygons": n,
            "area_m2_min": float(areas_m2[mask].min()),
            "area_m2_max": float(areas_m2[mask].max()),
            "area_m2_median": float(areas_m2[mask].median()),
            "bbox_width_m_median": float(widths_m[mask].median()),
            "bbox_height_m_median": float(heights_m[mask].median()),
        }

    out = {
        "crs": TARGET_CRS,
        "method": "quantile classification (equal count per bucket) over polygon area in EPSG:3338",
        "n_polygons_total": int(len(gdf_proj)),
        "excluded_ids": sorted(exclude_ids),
        "buckets": buckets,
    }
    with open(OUT_BUCKETS, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT_BUCKETS}:")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
