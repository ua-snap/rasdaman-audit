# Worked example: tiling `crrel_gipl_outputs_nc`

Condensed from `docs/CRREL_GIPL_tiling.md`. It shows the full method on one real coverage, from source file to scheme design to measured results.

## 1. Source file

`ncdump -h gipl_outputs_optimized.nc` reports dims `time=100, model=3, scenario=2, y=1941, x=2471`, and ten `float` bands, each declared as `(time, model, scenario, y, x)`. CRS is EPSG:3338 at 1 km resolution. The resolution isn't in the header; diff consecutive values from `ncdump -v x`.

- Bytes per cell = **40** (10 × 4).
- Logical size = 100·3·2·1941·2471·40 = **115.109 GB**. `PhysicalSize` matches exactly, and no tiling changes it.

## 2. Storage order

`DescribeCoverage` reported `model scenario time X Y`. **That was wrong.** The recipe's `gridOrder` (time=0, model=1, scenario=2, Y=3, X=4) and `select sdom(c)` → `[0:99,0:2,0:1,0:1940,0:2470]` agree. Every bracket below is in `[time, model, scenario, Y, X]` order. Repeat this step for every coverage.

The spatial pair has its own trap: `ncdump` lists `y, x`, while EPSG:3338 declares Easting first. Both are correct, and neither affects the bracket, which follows `gridOrder`.

## 3. Current tiling (all wildcards)

`ALIGNED [0:*, 0:*, 0:*, 0:*, 0:*] tile size 4194304` → rasdaman built chunk `(1,1,1,42,2471)`: a horizontal band sweeping all of X. That is map-friendly (≈1.02× map amplification) and point-hostile (103,782×). The full dump also showed an offset partition at `(time=0, model=0, scenario=0)` and 907 duplicate index entries, giving 29,109 indexed tiles over 28,202 unique domains.

## 4–5. Schemes (4 MiB budget)

Method:
- Fixed cells = product of the non-spatial chunk sizes.
- Spatial budget = ⌊4,194,304 ÷ (fixed × 40)⌋ cells.
- Side = ⌊√budget⌋.

| Scheme | Chunk (t,m,s,Y,X) | Tiling string | Tile bytes | Tiles | Point amp | Map amp |
|---|---|---|---|---|---|---|
| Current | 1,1,1,42,2471 | all `0:*` | ~4.16 MB | 29,109 idx | 103,782× | 1.02× |
| A — point | 100,3,2,13,13 | `ALIGNED [0:99, 0:2, 0:1, 0:12, 0:12] tile size 4056000` | 4,056,000 | 28,650 | **169×** | 606× |
| B — map | 1,1,1,323,323 | `ALIGNED [0:0, 0:0, 0:0, 0:322, 0:322] tile size 4173160` | 4,173,160 | 33,600 | 104,329× | **1.22×** |
| B′ — condense 30 | 30,1,1,59,59 | `ALIGNED [0:29, 0:0, 0:0, 0:58, 0:58] tile size 4177200` | 4,177,200 | 33,264 | — | 30.2× single slice |
| C — polygon | 100,3,2,14,14 | `ALIGNED [0:99, 0:2, 0:1, 0:13, 0:13] tile size 4194304` | ~4.49 MiB | 24,603 | — | — |

`1941 = 3 × 647` and `2471 = 7 × 353`, so no chunk near budget divides either axis. That alone is reason to use `ALIGNED`. A per-axis `extent % chunk` check catches transposed chunk sizes; a byte total doesn't, because volume is the same either way.

Scheme C came from `utilities/recommend_tiling.py`. SNAP's small/medium/large polygon buckets have median bboxes of about 11, 16 and 31 km. At a 4 MB budget all three buckets land on one chunk. Smaller budgets (1–2 MB) differentiate them.

## 7. Measured (Zeus, JMeter, 2026-10-01, single thread)

Median latency in seconds, p95 in parentheses:

| Query | Current | A | B | B′ | C |
|---|---|---|---|---|---|
| Point time series (WCS, n=50) | 1.04 (2.21) | **0.20 (0.23)** | — | — | 0.21 (0.26) |
| Single-year map (WMS, n=10) | 1.39 (2.10) | — | **0.94 (1.25)** | 38.3 (42.0) | timed out (~20 min) |
| 30-yr condense map (WMS, n=10) | **1.47 (1.69)** | — | 1.55 (1.59) | 21.3 (21.6) | — |

Lessons:
- **Point schemes win as modelled, but by ~5× (p95 ~9×), not 600×.** At ~0.2 s, per-request overhead (HTTP, petascope, JSON) dominates.
- **The model got maps wrong.** It ranked current slightly ahead of B; B was 1.5× faster on single-year maps.
- **The condense-window scheme lost badly**, 14× slower on the very query it was designed for, even though all three schemes read ~6–7 GB. The cause is uninvestigated. Don't recommend condense-window tiling without a benchmark.
- **A point-tiled coverage can't serve maps at all** (C never finished a statewide map). Keep point and map coverages separate.
- For this coverage the re-tiling payoff is in point queries. The wildcard tiling it had was already a strong map baseline.

Still untested: polygon queries against C, direct WCPS timings (to separate rasdaman from petascope rendering), multi-sample cold-cache runs, and `PhysicalSize` of the test coverages.
