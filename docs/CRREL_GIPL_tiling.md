# A worked tiling example: `crrel_gipl_outputs_nc`

This is a full, reproducible worked example: one real coverage, several tiling schemes designed from scratch for different access patterns, all held to the upper limit of rasdaman's 1–4 MB guidance, ready to be ingested as throwaway test coverages and timed against the original. It designs a point-query scheme, a map-rendering scheme, and a polygon/AOI scheme sized from SNAP's own real query-polygon distribution, and it ends with what actually happened when they were tested against the original on Zeus (section 7).

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

**Total logical size is 115.109 GB** — `100 × 3 × 2 × 1941 × 2471 × 40` bytes. This is what every tiling scheme below is a rearrangement of. It does not change: tile shape is a read-performance decision, not a storage one (guide, section 6; audit, Finding 1). `RAS_MDDOBJECTS.PhysicalSize` for the live `crrel_gipl_outputs_nc` collection already reads 115,109,064,000 bytes — matching this exactly — so whatever the two new schemes below do to query speed, neither should move that number at all. That is itself worth confirming after ingest (not yet checked — section 7).

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

**Confirmed against the full tile-domain dump (`data/tile-dumps/crrel_gipl_outputs_nc.json.gz`):** this single sampled domain is representative of 599 of the coverage's 600 (time, model, scenario) combinations — chunk `1, 1, 1, 42, 2471` really is the ordinary shape. The naive extrapolation from it (`⌈1941÷42⌉ × 100 × 3 × 2 = 28,200` tiles) undercounts the real, measured 29,109 for two reasons, now both identified rather than lumped into one unexplained gap: one combination — `(time=0, model=0, scenario=0)` — uses a different, offset partition entirely (the same corner-splitting behavior `era5_4km_elevation` shows independently — guide, Section 3.3), and 907 of the 29,109 indexed entries are plain duplicate index entries (28,202 unique domains, not 28,200 or 29,109 — Finding 1's mechanism, guide Section 3.3, now confirmed on this coverage too). The guide's Section 3.3 discusses the pattern; the per-domain counts come from the dump above.

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

A condense over `time(0:29)` lands inside one time-block and touches only the 1,386 spatial tiles it needs. A condense over `time(19:48)` or `time(49:78)` straddles two 30-wide blocks (their start offsets aren't multiples of 30), so it costs up to 2× that — still far short of the 30× a time-chunk-of-1 scheme would force on every condensed style. This variant is a real trade against scheme B: worse for a genuine single-slice `GetMap` (30× the unwanted time data per tile instead of none), better for the condense styles this coverage actually ships today. Worth testing as a third candidate, not a replacement for section 5 — section 7 has its results.

**Measured, it lost.** On the 30-year condense map B′ was about 14× slower than both B and the current coverage (section 7). The tile-count argument above counts tiles per spatial block and leaves out that each of B's tiles covers 30× more area than B′'s: over the whole frame B touches 1,680 tiles to B′'s 1,386, and all three schemes read roughly 6–7 GB for this query. Read volume doesn't explain the gap, and its cause hasn't been investigated.

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
| Current (as ingested) | 1, 1, 1, 42, 2471 (599/600 combos; one combo offset — section 3) | 4.16 MB typical / 28,202 unique domains confirmed | 29,109 indexed (28,202 unique + 907 duplicate entries — section 3) | 103,782× | ≈1.02× |
| A — WCS point/time-series | 100, 3, 2, 13, 13 | 3.87 MiB | 28,650 | **169×** | 606× |
| B — WMS/map | 1, 1, 1, 323, 323 | 3.98 MiB | 33,600 | 104,329× | **1.22×** |
| B′ — WCPS condense, 30-step (served as a WMS style) | 30, 1, 1, 59, 59 | 3.98 MiB | 33,264 | not modelled (not its job) | 30.2× for a single slice; ≈1.2–2.4× for an aligned 30-step condense |
| C — polygon/AOI (small/medium/large, 4 MB) | 100, 3, 2, 14, 14 | 4.486 MiB | 24,603 | not modelled (not its job) | not modelled (not its job) |

Read at face value, the current scheme is already close to map-optimal (barely better than the hand-designed scheme B, by sweeping X instead of chunking it) and just as bad for point queries as scheme B — meaning scheme A should be the one that shows the biggest before/after contrast when tested.

**Measured (section 7):** that last prediction held — A answered point queries about 5× faster than the current coverage, the biggest change in the test. The map predictions did less well. B beat the current coverage on single-year maps (about 1.5× faster) instead of trailing it, the two tied on the 30-year condense map, and B′ was the slowest scheme on both kinds of map.

---

## 7. Testing — results

Run against Zeus on 2026-10-01 with Apache JMeter, using the request builders and test plans in `rasdaman-ingest/benchmarks/` plus the uncommitted changes described in [`benchmark-harness.md`](benchmark-harness.md). Requests ran one at a time from a single thread. The test coverages were ingested under different names from the placeholders this section originally planned (since removed, along with their recipes):

| Scheme | Coverage on Zeus | Recipe in `rasdaman-ingest/ardac/gipl/` |
|---|---|---|
| Current | `crrel_gipl_outputs_nc` | `ingest_with_nc.json` |
| A — point | `crrel_gipl_outputs_nc_wcs` | `ingest_with_nc_wcs.json` |
| B — map | `crrel_gipl_outputs_nc_wms` | `ingest_with_nc_wms.json` |
| B′ — condense | `crrel_gipl_outputs_nc_wms_30yr` | `ingest_with_nc_wcps_30yr.json` |
| C — polygon | `crrel_gipl_outputs_nc_polygon` | `ingest_with_nc_polygon.json` |

Each test recipe uses its scheme's tiling bracket from sections 4–5c exactly. They declare `tile size 4194304` (C: `5000000`) rather than the byte counts computed above. With a fully explicit bracket the bracket sets the shape and the number is descriptive (guide, Section 8), so this shouldn't change what was built — but the test coverages' tile domains haven't been dumped to confirm it.

### What was queried

The tests went through the OGC services clients actually call, not the direct WCPS queries this section originally sketched (those are kept at the end as follow-ups).

- **Point / time-series (WCS).** `GetCoverage` slicing X and Y only, so each response is one cell's full time series — all 100 time steps, 3 models, 2 scenarios and 10 bands — as JSON. 50 random points per coverage within ±150 km of the coverage's centroid, shuffled across coverages.
- **Single-year map (WMS).** Statewide `GetMap` — the full coverage extent in EPSG:3338, rendered as an 800 × 600 PNG — for `model=0`, `scenario=0`, `time=2021`, using a colormap-only style (no WCPS) made for this test and since removed. Changing `time` was checked to change the map. 10 requests per coverage.
- **Condense map (WMS).** The same statewide `GetMap` using the production 30-year style: `arctic_eds_gipl_magt1m_nearcentury2` on the current coverage, `arctic_eds_gipl_magt1m_nearcentury` on B and B′. Both run section 5b's condense, and all three coverages return byte-identical PNGs. 10 requests per coverage.

### Results

Median latency in seconds, p95 in parentheses. Latency is JMeter's `Latency` field (time to first byte), which is what `benchmarks/analyze_*_results.py` report; medians were computed from the same raw JMeter files. The ratio is the current coverage's median ÷ the scheme's median, so above 1× is faster than current and below 1× is slower. Every request succeeded except C's map request.

| Query | Current | A (point) | B (map) | B′ (condense) | C (polygon) |
|---|---|---|---|---|---|
| Point / time-series (WCS, n=50) | 1.04 (2.21) | **0.20 (0.23)** — 5.1× | not tested | not tested | 0.21 (0.26) — 4.8× |
| Single-year map (WMS, n=10) | 1.39 (2.10) | not tested | **0.94 (1.25)** — 1.5× | 38.3 (42.0) — 0.04× (27× slower) | did not finish (timed out after ~20 min; 1 request, 2026-09-29) |
| Condense map (WMS, n=10) | **1.47 (1.69)** | not tested | 1.55 (1.59) — 0.95× (a tie) | 21.3 (21.6) — 0.07× (14× slower) | not tested |
| Polygon / AOI | not tested | not tested | not tested | not tested | not tested |

**Cold vs. warm.** Latency depends heavily on whether a coverage's tiles are already in memory, and the repeated requests above are mostly warm. The first request of each run is the closest thing to a cold read:

- **Single-year map, Oct 1 — likely cold for all three.** Nothing had read the `scenario=0`, `time=2021` tiles recently, and every map scheme here (current included — section 3) keeps each scenario in separate tiles. First requests: current 2.59 s, B 1.42 s, B′ 44.4 s.
- **Condense map — no clean cold comparison.** On Oct 1 those tiles had been read shortly before the run. The only earlier figures are single requests from 2026-09-29: current 41.5 s, B 2.7 s, B′ 35.4 s. B reads slightly more than the current coverage for this query (see below), from the same storage, so its 2.7 s almost certainly wasn't a cold read — it had recently been ingested.
- **Point queries.** First requests: current 1.12 s, A 0.85 s, C 0.27 s. An earlier 10-point run on 2026-09-29 gave the same ordering with a wider gap (means: current 4.35 s, A 0.29 s, C 0.44 s).

### Measured vs. modelled

- **Point queries: the model's direction held, its size didn't.** A and C are about 5× faster than the current coverage at the median and about 9× better at p95 (2.21 s → 0.23–0.26 s) — the clearest win in the test. That is far smaller than the gap in modelled amplification (103,782× vs. 169×); at ~0.2 s, fixed per-request overhead (HTTP, petascope, JSON encoding) is plausibly most of what's left. C performs like A, as expected from its nearly identical chunk.
- **Single-year maps: B beat the current coverage, against the model.** Section 6 put current (≈1.02×) slightly ahead of B (≈1.22×); B measured 1.5× faster. B′ was 27× slower than current, in line with its modelled 30.2× single-slice amplification — drawing one year reads all 30 years in each tile. C couldn't produce a statewide map at all, consistent with section 6's 606× map amplification for A-style chunks.
- **Condense maps: B′ was expected to win and came last.** Section 5b expected B′'s 30-step time chunk to beat B on the shipped condense styles. Instead current and B tied and B′ was 14× slower than both. By this document's own arithmetic all three read about the same amount for this query — ≈5.9 GB for current (1,410 tiles), ≈7.0 GB for B (1,680), ≈5.8 GB for B′ (1,386) — so read volume doesn't explain the gap. Why B′ is slower has not been investigated.
- **The current coverage is a strong baseline for maps.** Section 3's reading of the current tiling as already close to map-optimal held up: it ties B on the condense map and trails it by under half a second on single-year maps. The payoff from re-tiling this coverage is in point queries, not maps.

### A WMS gotcha found along the way

The default WMS style on all three map layers is a 30-year condense. A `GetMap` with an empty `STYLES` parameter renders the condense and ignores `time`, `model` and `scenario`: `time=2021` and `time=2090` return byte-identical PNGs (checked on the current coverage and B). Any WMS benchmark of a single time slice on these layers has to name a non-condense style explicitly. An earlier round of this test didn't, and was measuring the condense without knowing it. This also confirms section 5b's premise that the shipped styles are condenses.

### Not yet covered

- Polygon/AOI queries — scheme C's actual target — and the cross-checks that weren't run: point queries against B and B′, and map queries against A.
- Direct WCPS timings. The queries below, sent straight to rasdaman, would separate rasdaman's time from petascope's WMS rendering. That's the obvious next step for explaining B′'s condense result.
- Cold-cache runs with more than one sample each.
- Tile-domain dumps and `PhysicalSize` for the test coverages. Section 1 expects `PhysicalSize` to stay at 115,109,064,000 bytes for every scheme.

### Reproducing

The `rasdaman-ingest/benchmarks/` options these runs depended on were never committed there. [`benchmark-harness.md`](benchmark-harness.md) has the patch, the exact commands, how the test coverages and styles were set up, and the gotchas hit along the way. The test coverages, their recipes and the single-year style have since been removed from Zeus, so a re-run starts by re-ingesting.

### Follow-up: direct WCPS queries

Not yet run. Each takes the coverage name in place of `crrel_gipl_outputs_nc`.

**Point/time-series** — one location, full time series, one band:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[Y(1000), X(1200)].magt1m_degC, "csv")
```

**Map** — one full frame, one band, fixed time/model/scenario:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[time(50), model(0), scenario(1)].magt1m_degC, "png")
```

**Condense** — the production pattern from the recipe's `after_import` hooks:

```
for $c in (crrel_gipl_outputs_nc)
return encode((condense + over $t time(0:29)
  using $c[time($t), model(0), scenario(1)]).magt1m_degC / 30, "png")
```

**Polygon/AOI** — one medium-sized watershed's bounding box (≈16 × 16 cells at 1 km — section 5c), full time series, one band:

```
for $c in (crrel_gipl_outputs_nc)
return encode($c[Y(1000:1015), X(1200:1215)].magt1m_degC, "csv")
```

---

## Sources

`ncdump -h` output (Josh, this conversation) for the source array shape and bands; `ncdump -v x`/`ncdump -v y` against the live source file for the 1 km pixel resolution used in section 5c; `rasdaman-ingest/ardac/gipl/ingest_with_nc.json` for the live `gridOrder`, current tiling string, and the WMS style hooks quoted in section 5b; `data/coverages_summary.csv` for the `sdom`-vs-`DescribeCoverage` disagreement flag and the measured tile count; `data/physical_sizes.csv` for the 115,109,064,000-byte `PhysicalSize` baseline. Chunk-sizing methodology for schemes A/B/B′ mirrors `scripts/build_workbook.py`'s `recommend()` function and `rasdaman-tiling-guide.md` sections 4 and 6 — see those for the general case this document applies to one specific coverage. Scheme C (section 5c) and the independent re-check of A/B/B′ (both reproduce exactly) were generated by `utilities/recommend_tiling.py` — see `utilities/README.md`. Section 7's timings come from JMeter runs against Zeus on 2026-09-29 and 2026-10-01 using `rasdaman-ingest/benchmarks/` with the changes in `benchmark-harness.patch` (see `benchmark-harness.md`); medians and first-request figures were computed from the raw JMeter results files, which live in that directory and aren't committed to either repo.
