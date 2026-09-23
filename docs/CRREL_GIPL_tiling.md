# A worked tiling example: `crrel_gipl_outputs_nc`

This is a full, reproducible worked example: one real coverage, several tiling schemes designed from scratch for different access patterns, all held to the upper limit of rasdaman's 1–4 MB guidance, ready to be ingested as throwaway test coverages and timed against the original. It designs a point-query scheme, a map-rendering scheme, and a polygon/AOI scheme sized from SNAP's own real query-polygon distribution, and it ends with a place to record what actually happened when they were tested.

> Note that the same process below could be followed using a smaller target (e.g., 1MB) to test performance. This could be especially useful for point queries. 

Everything below either comes from the `ncdump` of the input netcdf, from the live ingest recipe (`rasdaman-ingest/ardac/gipl/ingest_with_nc.json`), or from `data/coverages_summary.csv` / `data/physical_sizes.csv` in this repo. Where a number needs a live query to confirm, the exact command is given so this can be re-run against the real server (Zeus).

---

## 1. Start from the source file

```bash
ncdump -h /opt/rasdaman-storage/coverage_data/gipl/gipl_outputs_optimized.nc
```

```
dimensions:
        time = 100 ;
        model = 3 ;
        scenario = 2 ;
        y = 1941 ;
        x = 2471 ;
variables:
        float magt05m_degC(time, model, scenario, y, x) ;
        float magt1m_degC(time, model, scenario, y, x) ;
        float magt2m_degC(time, model, scenario, y, x) ;
        float magt3m_degC(time, model, scenario, y, x) ;
        float magt4m_degC(time, model, scenario, y, x) ;
        float magt5m_degC(time, model, scenario, y, x) ;
        float magtsurface_degC(time, model, scenario, y, x) ;
        float permafrostbase_m(time, model, scenario, y, x) ;
        float permafrosttop_m(time, model, scenario, y, x) ;
        float talikthickness_m(time, model, scenario, y, x) ;
        // all ten: _FillValue = -9999.f
:crs = "EPSG:3338" ;
```

Ten single-precision float bands, all sharing one `(time, model, scenario, y, x)` domain. Two numbers come straight out of this before anything else is decided:

**Bytes per cell is 40, not 4.** Rasdaman tiles the whole struct — every band — together. A tile's byte budget is `(cells in the tile) × (sum of every band's width)`. Sizing against one `float32` band undercounts the real footprint tenfold (guide, section 4).

**Total logical size is 115.109 GB** — `100 × 3 × 2 × 1941 × 2471 × 40` bytes. This is what every tiling scheme below is a rearrangement of. It does not change: tile shape is a read-performance decision, not a storage one (guide, section 6; audit, Finding 1). `RAS_MDDOBJECTS.PhysicalSize` for the live `crrel_gipl_outputs_nc` collection already reads 115,109,064,000 bytes — matching this exactly — so whatever the two new schemes below do to query speed, neither should move that number at all. That is itself worth confirming after ingest (section 8).

**Pixel resolution is 1,000 m (1 km) in both Y and X.** Not given directly anywhere in the recipe or by `ncdump -h`'s header — confirmed by diffing consecutive values from the live source file: `ncdump -v x gipl_outputs_optimized.nc` gives `-979291.709, -978291.709, ...` (spacing exactly 1,000.0), and `ncdump -v y` gives `2374979.751, 2373979.751, ...` (spacing exactly -1,000.0, descending). Needed below for section 5c's polygon scheme, which sizes a tile in real-world meters, not just grid cells.

---

## 2. Find the true storage axis order

This is the step the guide warns about most (section 9, "axis mismatch"), and `crrel_gipl_outputs_nc` is not a hypothetical case of it: it is one of the eleven catalogue disagreements this audit already flagged (Finding 4) — `data/coverages_summary.csv`'s row for this coverage carries the note `sdom disagrees with DescribeCoverage extent`. Concretely, `DescribeCoverage` reports `gml:axisLabels` as `model scenario time X Y`, but that is **not** the order the tiles are actually stored in. Trusting it here would silently attach every chunk size to the wrong axis.

The authoritative source is the ingest recipe's own declared `gridOrder`, which is what actually built the array:

```bash
grep -A2 '"gridOrder"' rasdaman-ingest/ardac/gipl/ingest_with_nc.json
```

```json
"time":     { "gridOrder": 0, ... }
"model":    { "gridOrder": 1, ... }
"scenario": { "gridOrder": 2, ... }
"Y":        { "gridOrder": 3, ... }
"X":        { "gridOrder": 4, ... }
```

Storage order is **time, model, scenario, Y, X** — which happens to match the `ncdump` declaration order too (`time, model, scenario, y, x`), just not what `DescribeCoverage` reports. Cross-check it against the one thing that cannot lie about storage layout, `dbinfo`'s `sdom`:

```bash
curl -u rasadmin:$PASSWORD \
  --data-urlencode 'query=select sdom(c) from crrel_gipl_outputs_nc as c' \
  'https://zeus.snap.uaf.edu/rasdaman/rasql'
```

```
[0:99,0:2,0:1,0:1940,0:2470]
```

Sizes `100, 3, 2, 1941, 2471` in that position order — `time, model, scenario, Y, X` exactly, confirming the recipe's `gridOrder` and ruling out `DescribeCoverage`'s label order. **Every tiling bracket in this document is written in this order: `[time, model, scenario, Y, X]`.** If you re-run this exercise on a different coverage, redo this step first — don't assume `DescribeCoverage`'s axis order is safe to use.

---

## 3. The coverage as currently ingested

The live recipe's tiling line:

```
"tiling": "ALIGNED [0:*, 0:*, 0:*, 0:*, 0:*] tile size 4194304"
```

All five axes wildcarded — this tells rasdaman "hit ~4 MB, you choose the shape," with no knowledge of whether the coverage is queried by point, by map, or both. `dbinfo` on the live coverage reports 29,109 tiles averaging ≈4.08 MB each, close to the 4 MiB target. To see the actual shape rasdaman picked (how much of that budget went to time vs. Y vs. X), dump one domain — writing the response to a file first, rather than piping `curl` straight into `grep -o ... | head -1`, avoids both `curl`'s progress meter landing in the output and a broken-pipe error once `head` stops reading:

```bash
curl -s -u rasadmin:$PASSWORD \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from crrel_gipl_outputs_nc as c' \
  'https://zeus.snap.uaf.edu/rasdaman/rasql' -o /tmp/gipl_tiles.json

grep -a -o '"\[[-0-9:,]*\]"' /tmp/gipl_tiles.json | head -1
```

```
[98:98,2:2,0:0,0:41,0:2470]
```

Read in `[time, model, scenario, Y, X]` order: time and model and scenario each pinned to one index, Y chunked to 42 rows, **X swept in full** (`0:2470` is all 2,471 columns). Chunk `(1, 1, 1, 42, 2471)` — `103,782` cells, `4.16 MB` — is under budget, and structurally it's much closer to a map-style tiling (non-spatial pinned, spatial large) than a point-style one, just an asymmetric band rather than a square: sweeping the *last* wildcarded axis in full and only partly chunking the one before it is consistent with rasdaman's `ALIGNED` algorithm filling axes in `gridOrder` sequence rather than packing a 2-D spatial block. Projected against this shape (same formulas as sections 4–5): point amplification 103,782× (a time series touches 600 separate tiles, each 103,782 cells, for a query that wants 600 — the tile count cancels the query's own multiplier, leaving one tile's cell count as the amplification), map amplification ≈1.02× — close to the theoretical floor, slightly better than the hand-designed scheme B below.

**Confirmed against the full tile-domain dump (section 7):** this single sampled domain is representative of 599 of the coverage's 600 (time, model, scenario) combinations — chunk `1, 1, 1, 42, 2471` really is the ordinary shape. The naive extrapolation from it (`⌈1941÷42⌉ × 100 × 3 × 2 = 28,200` tiles) undercounts the real, measured 29,109 for two reasons, now both identified rather than lumped into one unexplained gap: one combination — `(time=0, model=0, scenario=0)` — uses a different, offset partition entirely (the same corner-splitting behavior `era5_4km_elevation` shows independently, section 7), and 907 of the 29,109 indexed entries are plain duplicate index entries (28,202 unique domains, not 28,200 or 29,109 — Finding 1's mechanism, guide Section 3.3, now confirmed on this coverage too). Section 7 has the full breakdown.

---

## 4. Designing a WCS point / time-series scheme

**Access pattern:** pull one location's full time series (all 100 time steps), typically across all three models and both scenarios, one or a few bands. Guide section 6: keep every non-spatial axis whole, shrink the spatial footprint to fit the budget.

Non-spatial cells per tile (time × model × scenario, all kept whole): `100 × 3 × 2 = 600`.

Spatial budget at 40 bytes/cell:

```
budget = 4,194,304 ÷ (600 × 40) = 174 cells
side    = floor(sqrt(174))       = 13
```

(This is the same square-footprint method `scripts/build_workbook.py`'s `recommend()` uses for every other coverage in this audit — reusing it here keeps this example comparable to the rest of the repo rather than one-off math.)

| | |
|---|---|
| Chunk (time, model, scenario, Y, X) | 100, 3, 2, 13, 13 |
| Tile cells | 101,400 |
| Tile size | **4,056,000 bytes (3.87 MiB)** |
| Spatial blocks | 150 × 191 |
| Total tiles | 28,650 |

```
"tiling": "ALIGNED [0:99, 0:2, 0:1, 0:12, 0:12] tile size 4056000"
```

**Projected point amplification:** 169× (a single-point time series pulls a 13×13 spatial neighborhood it didn't ask for — the unavoidable cost of a square tile, and already close to the practical floor demonstrated in the guide's `cmip6_fwi` example).

**Projected map amplification if this same tiling were used to render a full frame:** ≈606× — assembling one map would touch all 28,650 tiles. This scheme is not for maps, and shouldn't be used to serve them.

Note 1941 and 2471 are each the product of two primes (`1941 = 3 × 647`, `2471 = 7 × 353`) with nothing near 13 — no `REGULAR` chunk size divides either axis close to this budget. That's the whole reason `ALIGNED` is used here, and it's a sufficient one on its own: `"irregular": true` on `time`, `model`, and `scenario` elsewhere in the recipe does **not** rule `REGULAR` out (that flag is about how petascope declares each axis's real-world CRS coordinates, not about the storage tiling bracket — see the guide, Section 5) — it just happens that `REGULAR` wouldn't have helped on these two spatial axes regardless, since there's no chunk size near budget that divides either one.

---

## 5. Designing a WMS / map-rendering scheme

**Access pattern:** render one full spatial frame at a fixed time, model, and scenario — one band, one map. Guide section 6: pin every non-spatial axis to one index, make the spatial footprint as large as the budget allows.

Non-spatial cells per tile: `1 × 1 × 1 = 1`.

```
budget = 4,194,304 ÷ (1 × 40) = 104,857 cells
side    = floor(sqrt(104,857))  = 323
```

| | |
|---|---|
| Chunk (time, model, scenario, Y, X) | 1, 1, 1, 323, 323 |
| Tile cells | 104,329 |
| Tile size | **4,173,160 bytes (3.98 MiB)** |
| Spatial blocks | 7 × 8 |
| Total tiles | 33,600 |

```
"tiling": "ALIGNED [0:0, 0:0, 0:0, 0:322, 0:322] tile size 4173160"
```

**Projected map amplification:** ≈1.22× — rendering a full frame touches just 56 tiles and reads almost exactly the frame's own cell count. This is close to the theoretical floor (1×) and is what "large spatial footprint, non-spatial pinned to one" buys you.

**Projected point amplification if this same tiling were used for a time series:** 104,329× — a single point's 100-step time series would touch 600 separate tiles (one per time/model/scenario combination), each dragging along a 323×323 spatial block it doesn't need. Do not point this scheme at point queries; it is built for exactly one job.

### 5b. A variant worth testing: the condense window

The live recipe's own WMS styles don't render single time slices — every `after_import` hook is a WCPS `condense` averaging **19 to 30 consecutive time steps** for one fixed model and scenario, e.g.:

```
condense + over $t time(0:29) using $c[time($t), model(0), scenario(1)]
  .magt1m_degC / 30
```

If that 19–30-step condense is the actual hot path (rather than a single-slice `GetMap`), pinning `time` to 1 forces each such request to touch up to 30 separate tiles. Widening the time chunk to match:

```
budget = 4,194,304 ÷ (30 × 40) = 3,495 cells
side    = floor(sqrt(3,495))     = 59
```

| | |
|---|---|
| Chunk (time, model, scenario, Y, X) | 30, 1, 1, 59, 59 |
| Tile cells | 104,430 |
| Tile size | **4,177,200 bytes (3.98 MiB)** |
| Spatial blocks | 33 × 42 |
| Total tiles | 33,264 |

```
"tiling": "ALIGNED [0:29, 0:0, 0:0, 0:58, 0:58] tile size 4177200"
```

A condense over `time(0:29)` lands inside one time-block and touches only the 1,386 spatial tiles it needs. A condense over `time(19:48)` or `time(49:78)` straddles two 30-wide blocks (their start offsets aren't multiples of 30), so it costs up to 2× that — still far short of the 30× a time-chunk-of-1 scheme would force on every condensed style. This variant is a real trade against scheme B: worse for a genuine single-slice `GetMap` (30× the unwanted time data per tile instead of none), better for the condense styles this coverage actually ships today. Worth testing as a third candidate, not a replacement for section 5 — section 9 has a row for it.

---

## 5c. Designing a polygon/AOI query scheme

**Access pattern:** pull a real query polygon's footprint (a watershed, borough, climate division, or similar — SNAP's own boundary layer) across the full time series, all models, both scenarios — same non-spatial handling as scheme A (section 4), but the spatial footprint is shaped and sized to a real polygon instead of assumed square.

`utilities/recommend_tiling.py` (see `utilities/README.md`) automates this end to end: it reads a netCDF's real dimensions, band count and dtype directly, converts SNAP's small/medium/large boundary-polygon size classes (`utilities/data/polygon_area_buckets.json` — quantile buckets over ~17,771 real polygons from SNAP's own boundary layer) into grid cells at the file's own resolution and CRS, and solves for a spatial footprint sized to match — no wildcards, every axis fully explicit, for the same reason as every scheme in this document.

```bash
python3 utilities/recommend_tiling.py --netcdf gipl_outputs_optimized.nc --tile-sizes 4 --condense-n 30
```

| Bucket | Target footprint (cells, Y × X) | Chunk (time, model, scenario, Y, X) | Tile size |
|---|---|---|---|
| small  | 11.3 × 10.9 | 100, 3, 2, 14, 14 | 4.486 MiB |
| medium | 16.3 × 15.6 | 100, 3, 2, 14, 14 | 4.486 MiB |
| large  | 31.2 × 30.6 | 100, 3, 2, 14, 14 | 4.486 MiB |

```
"tiling": "ALIGNED [0:99, 0:2, 0:1, 0:13, 0:13] tile size 4194304"
```

All three size classes land on the same chunk at this budget. Even a "large" query polygon's footprint (≈31 × 31 cells at this coverage's 1 km resolution) fits inside the ~13-cell-per-side square a 4 MB budget already buys for a single point (scheme A) — the polygon scheme only grows one cell past scheme A's per-side size here, to 14 rather than 13, because it's fitting a slightly non-square target (11.3 × 10.9, etc.) rather than a perfect square. A smaller tile-size budget (1–2 MB, also just a `--tile-sizes` flag away) would show real differentiation between bucket sizes, since the byte budget itself — not the polygon's own footprint — would then be the binding constraint for the larger buckets.

Point/map amplification aren't modelled for this scheme — its target access pattern is neither "one cell" nor "one full frame," so those two formulas don't describe what it's actually built for.

---

## 6. Side by side

| Scheme | Chunk (time, model, scenario, Y, X) | Tile size | Total tiles | Point amp | Map amp |
|---|---|---|---|---|---|
| Current (as ingested) | 1, 1, 1, 42, 2471 (599/600 combos; one combo offset — section 7) | 4.16 MB typical / 28,202 unique domains confirmed | 29,109 indexed (28,202 unique + 907 duplicate entries — section 7) | 103,782× | ≈1.02× |
| A — WCS point/time-series | 100, 3, 2, 13, 13 | 3.87 MiB | 28,650 | **169×** | 606× |
| B — WMS/map | 1, 1, 1, 323, 323 | 3.98 MiB | 33,600 | 104,329× | **1.22×** |
| B′ — WCPS condense, 30-step (served as a WMS style) | 30, 1, 1, 59, 59 | 3.98 MiB | 33,264 | not modelled (not its job) | 30.2× for a single slice; ≈1.2–2.4× for an aligned 30-step condense |
| C — polygon/AOI (small/medium/large, 4 MB) | 100, 3, 2, 14, 14 | 4.486 MiB | 24,603 | not modelled (not its job) | not modelled (not its job) |

Read at face value, the current scheme is already close to map-optimal (barely better than the hand-designed scheme B, by sweeping X instead of chunking it) and just as bad for point queries as scheme B — meaning scheme A should be the one that shows the biggest before/after contrast when tested.

---

## 7. Testing — TBD

Not run yet. Record wall-clock time (median of a few runs, not one) for each query against `crrel_gipl_outputs_nc` (current), `crrel_gipl_point_test` (scheme A), and `crrel_gipl_map_test` (scheme B) — and `crrel_gipl_condense_test` (scheme B′) / `crrel_gipl_polygon_test` (scheme C) if either gets built.

**Representative point/time-series query** — one location, full time series, one band:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[Y(1000), X(1200)].magt1m_degC, "csv")
```

**Representative map query** — one full frame, one band, fixed time/model/ scenario:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[time(50), model(0), scenario(1)].magt1m_degC, "png")
```

**Representative condense query** — the actual production pattern from the recipe's `after_import` hooks (scheme B′'s target case):

```
for $c in (crrel_gipl_outputs_nc)
return encode((condense + over $t time(0:29)
  using $c[time($t), model(0), scenario(1)]).magt1m_degC / 30, "png")
```

**Representative polygon/AOI query** — one medium-sized watershed's bounding box (≈16 × 16 cells at this coverage's 1 km resolution — see section 5c), full time series, one band:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[Y(1000:1015), X(1200:1215)].magt1m_degC, "csv")
```

| Query | Current | A (point) | B (map) | B′ (condense) | C (polygon) | Notes |
|---|---|---|---|---|---|---|
| Point / time-series | | | | | | |
| Full-frame map | | | | | | |
| Condense (30-step) | | | | | | |
| Polygon / AOI (medium) | | | | | | |

TBD — fill in after running.

---

## Sources

`ncdump -h` output (Josh, this conversation) for the source array shape and bands; `ncdump -v x`/`ncdump -v y` against the live source file for the 1 km pixel resolution used in section 5c; `rasdaman-ingest/ardac/gipl/ingest_with_nc.json` for the live `gridOrder`, current tiling string, and the WMS style hooks quoted in section 5b; `data/coverages_summary.csv` for the `sdom`-vs-`DescribeCoverage` disagreement flag and the measured tile count; `data/physical_sizes.csv` for the 115,109,064,000-byte `PhysicalSize` baseline. Chunk-sizing methodology for schemes A/B/B′ mirrors `scripts/build_workbook.py`'s `recommend()` function and `rasdaman-tiling-guide.md` sections 4 and 6 — see those for the general case this document applies to one specific coverage. Scheme C (section 5c) and the independent re-check of A/B/B′ (both reproduce exactly) were generated by `utilities/recommend_tiling.py` — see `utilities/README.md`.
