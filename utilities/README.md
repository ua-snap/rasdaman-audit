# Tiling recommendation tool

Given a netCDF file, recommends rasdaman tiling schemes for four query workflows, at four tile-size budgets, using the file's own real dimensions, band count and dtype — not a hypothetical. Every recommendation is a fully explicit numeric `ALIGNED` bracket (no `0:*` wildcards): see `tiling_lib.py`'s module docstring for why that's a deliberate choice, not an oversight.

Read [`../docs/rasdaman-tiling-guide.md`](../docs/rasdaman-tiling-guide.md) first if you haven't — this tool automates that guide's method (sections 4 and 6), it doesn't replace understanding it.

## The four workflows

1. **point** — single x/y, every non-spatial axis (time + anything else) kept whole inside the tile, spatial footprint as small as the budget allows.
2. **polygon** — same non-spatial handling as point, but the spatial footprint is shaped and sized to match a real query polygon (small/medium/large, drawn from SNAP's own boundary layer — see `fetch_boundaries.py`) instead of a generic square.
3. **map** — every non-spatial axis (including time) pinned to one index, spatial footprint as large as the budget allows.
4. **wcps_condense** — like `map`, but the time axis is chunked to a window of `N` steps instead of pinned to one (e.g. a 30-year climatology, or a 12-month-to-annual condense). `N` is never guessed — you always pass it.

## Setup

```bash
mamba env create -f environment.yml
mamba activate rasdaman-tiling-utils
```

## Usage

```bash
python3 recommend_tiling.py --netcdf /path/to/file.nc
```

That's the whole ask in the common case — dimensions, bands, dtype and CRS are all read from the file. Everything else has a sensible default:

| Flag | Default | When you need it |
|---|---|---|
| `--variable NAME` | the data-variable group with the most bands | file has more than one incompatible variable group and you want a specific one |
| `--x-dim` / `--y-dim` / `--time-dim` | name-matched (`x`/`lon`/... , `y`/`lat`/..., `time`/`ansi`/...) | dimension names don't match the common conventions |
| `--crs EPSG:NNNN` | read from a `crs`/`spatial_ref`/... attribute | the file doesn't declare one (or declares one this tool can't parse) |
| `--tile-sizes 1,2,3,4` | `1,2,3,4` (MiB) | you want a different set of budgets |
| `--condense-n N` | *(workflow 4 is skipped without it)* | you want the WCPS-condense workflow |
| `--polygon-buckets PATH` | `data/polygon_area_buckets.json` | you've re-run `fetch_boundaries.py` and want to point at fresh buckets |
| `--out report.json` | *(stdout only)* | you want the full structured report saved |

Output is a readable summary on stdout, plus (with `--out`) the same data as JSON: one row per workflow × tile-size-budget, each with a ready-to-use `ALIGNED [...] tile size N` string, the resulting real tile size, and a note whenever the tool had to deviate from a naive plan (e.g. shrinking a non-spatial axis because it didn't fit any spatial footprint otherwise).

## The polygon size buckets

`fetch_boundaries.py` pulls every polygon out of SNAP's `all_boundaries:all_areas` WFS layer (paginated — it's ~17,784 features, and the fetch deliberately pauses between pages rather than hammering the server), computes each one's area and bounding box in EPSG:3338 (Alaska Albers Equal Area — an equal-area CRS, unlike the WFS's native EPSG:4326, so the histogram isn't distorted by latitude), and splits them into small/medium/large by quantile (equal count per bucket, not equal area range — the distribution is heavily right-skewed, so an equal-*range* split would put nearly everything in "small").

One feature is dropped before the size analysis by its stable `id` (`--exclude-ids`, default `NC12`): "The Aleut Corporation," a Native corporation boundary at ~1.05M km² — a full order of magnitude past the next-largest polygon in the whole layer. It's real data, not bad data, but it isn't the shape of any realistic query AOI, and left in it would drag "large" toward a shape nothing actually queries.

**The ~17,784-polygon raw snapshot (`data/boundaries.geojson`, 214 MB uncompressed / 41-64 MB compressed) is intentionally *not* committed to this repo** — several times larger than expected once actually measured. Only the small derived `data/polygon_area_buckets.json` (median area + bbox width/height per bucket, a couple KB) is checked in, and that's all `recommend_tiling.py` reads at runtime — it never needs network access. Re-run the fetch yourself if the boundary layer ever changes:

```bash
python3 fetch_boundaries.py
```

Current buckets (fetched 2026-09-23, 17,771 valid polygons after dropping a handful of null/invalid geometries and the one excluded outlier above):

| Bucket | n | Median area | Median bbox (EPSG:3338) |
|---|---|---|---|
| small  | 5,924 | 61.6 km² | 10.9 km × 11.3 km |
| medium | 5,923 | 121.1 km² | 15.6 km × 16.3 km |
| large  | 5,924 | 449.9 km² (max 522,756 km², "Doyon, Limited"; see Known limitations) | 30.6 km × 31.2 km |

`recommend_tiling.py` converts each bucket's bbox (meters, EPSG:3338) into grid cells at your netCDF's own resolution and CRS via `tiling_lib.polygon_bbox_to_cells()` — this works for a projected or geographic target CRS alike, with no hardcoded meters-per-degree approximation: it builds the box around your file's own spatial centroid and measures it back in your file's native coordinate units through `pyproj`.

## Testing

`tests/make_test_netcdf.py` builds a small synthetic file mirroring `crrel_gipl_outputs_nc`'s real structure (`time, model, scenario, y, x`, ten float32 bands, EPSG:3338) — enough to exercise every workflow without needing the real, much larger coverage on hand:

```bash
python3 tests/make_test_netcdf.py
python3 recommend_tiling.py --netcdf tests/synthetic_gipl_like.nc --condense-n 10
```

## Known limitations

- Polygon shape is approximated by its bounding box, not its true footprint — a long, thin polygon and a chunky one of the same bbox area get the same recommendation. Reasonable for a first pass; a tighter fit would need the tool to look at more than just width/height.
- The "large" bucket's area range still spans several orders of magnitude (150 km² to 522,756 km²) even after excluding the single largest outlier (NC12, Aleut Corporation), because the source layer mixes boundary types at very different natural scales — HUC watersheds alongside boroughs, climate divisions, fire zones, ecoregions, census areas, and regional Native corporation boundaries (not surveyed exhaustively — see `fetch_boundaries.py`'s docstring). A representative bbox for "large" is necessarily a rougher approximation than for the tighter small/medium buckets, which are almost entirely HUC watersheds.
- `--condense-n` is a step *count*, not a calendar unit — if your time axis isn't evenly spaced (irregular `directPositions`, per the guide's section 5), N steps won't necessarily mean N of whatever calendar unit you have in mind. Check your own time axis before picking N.
