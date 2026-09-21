# Rasdaman Tiling Audit — zeus.snap.uaf.edu

**21 September 2026 · 273 coverages · rasdaman v10.4.7**

*Built from the audit run of 2026-09-21 20:30 UTC, with tile domains dumped for
six coverages and the full collection inventory read from RASBASE on the same
date.*

This audit examines every coverage on the production rasdaman server: how it is
tiled, how much disk it occupies and why, and whether its tiling matches how it
is actually queried. It exists to decide what to re-ingest, what to delete, and
in what order.

**Read [the tiling guide](rasdaman-tiling-guide.md) first.** It explains what a
tile is, what it costs, and how a recipe becomes stored tiles. This document
assumes all of that and goes straight to what is true of our server.

Read it alongside `rasdaman_tiling_audit.xlsx` for the per-coverage detail, and
`rasdaman_unreferenced_collections.csv` for the cleanup list. The **Re-ingest
Queue** tab ranks coverages by benefit with a recommended tiling string each;
the **Column Guide** tab defines every column, including which are measured
facts and which are modelled.

Everything here was produced by `rasdaman_tiling_audit.py`, which can be re-run
after any change to confirm what rasdaman actually built.

---

## The short version

Three independent problems, with three different remedies. Two of them touch no
recipe at all.

**About 8.1 TB of the 17.5 TB on disk is recoverable — 46% of the server**, in two separate piles.

**4,591 GB is duplicate tiles inside live coverages.** Not padding, not
fragmentation, not a tiling defect — the same tile domains stored two, four,
even eight times over. Six coverages had every tile domain dumped and counted,
and the *unique* domains cover each array exactly, to the byte. One coverage,
`tas_2km_projected_wcs`, accounts for 2,009 GB. The remedy is a clean re-ingest
into a fresh collection with the recipe unchanged. Finding 1.

**3,523 GB is collections nothing points at** — abandoned ingests, personal
experiments, test runs and twelve arrays squatting on live coverage names.
A single leftover holds 1,268 GB of it. These are invisible to WCS and can be
dropped without touching a live coverage. Finding 5.

**Separately, most coverages are tiled against their own access pattern.** Of
253 coverages we can model, 234 read more than a thousand times the bytes a
typical query returns, and 59 more than ten thousand times — for the point and
small-AOI lookups the data portal issues constantly. This is the problem
re-tiling solves, and it is worth stressing that it has *no* storage
consequence in either direction. Finding 2.

Underneath sit eleven coverages whose stored array disagrees with what WCS
advertises (Finding 4), 24 live coverages with no ingest recipe on `origin/main`
(Finding 6), and seven test coverages holding 512 GB in the public catalogue.

The largest single change in this version is that the 171 `cmip6_downscaled` v2
coverages — previously unmeasurable and assumed to be the problem — are now
measured, and they are the healthiest group on the server at 0.500×–1.009×
storage overhead with no duplicates at all.

A note on what is measured. Reading petascope's own coverage-to-collection
pointer out of `petascopedb` made `dbinfo` succeed for **all 273 coverages with
no failures**, each one confirmed against the coverage's own array. Nothing
below is modelled from recipes.

---

## Finding 1 — The same tiles, stored several times

Two suspects were wrong before the right one turned up, and both are worth
naming because both are intuitive.

**Padding is not the cause.** It is real and computable, but on this server it
costs almost nothing on disk: rasdaman compresses tiles, zero-fill compresses to
nearly nothing, and 19 of the 22 coverages carrying more than 1.2× geometric
padding show no disk penalty at all. `design_freezing_index` has 109× geometric
padding and still sits at 0.56× its uncompressed size.

**Tile shape is not the cause either.** Comparing tile counts against the
geometry the recipe implies looks like a diagnostic and is not one — a wildcard
bracket makes the geometric count meaningless. `era5_4km_daily_t2_mean` scores
2,339× by that test and stores at exactly 1.00×.

### What the tile domains actually show

Dumping every tile domain with `dbinfo(c,"printtiles=embedded")` and counting
them settles it. Here is one cell of `cmip6_fwi` — model 2, time 100, lat 10,
lon 10 — and every tile in the collection holding it:

```
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
```

Four byte-identical domains.

![The same tile, stored several times](figures/rasdaman-tile-duplication.svg)

Across three coverages dumped in full:

| Coverage | Tiles | Unique domains | Duplicate bytes | Array | On disk |
|---|---|---|---|---|---|
| `era5_4km_daily_t2_mean` | 4,678 | 4,678 | 0 GB | 19.0 GB | 19.0 GB (1.00×) |
| `era5_4km_daily_t2_mean_wcs` | 7,642 | 6,553 | 6.1 GB | 19.0 GB | 25.1 GB (1.32×) |
| `cmip6_fwi` | 266,724 | 101,745 | 123.9 GB | 52.0 GB | 175.9 GB (3.38×) |

The unique domains cover each array **exactly** — 19.0 GB against 19.0 GB, 52.0
against 52.0. Bytes on disk divided by bytes the tiles cover is 1.00× in all
three. So every declared tiling did precisely what it was asked to do, and every
byte of excess is a second, third or fourth copy of a tile.

For `cmip6_fwi` the distribution is uneven: 27,290 domains stored once, 29,847
twice, 41,218 four times, 800 eight times.

Six coverages have now had their domains dumped and counted, spanning the whole
range of overhead. The worst multiplicity tracks the storage cost closely:

| coverage | overhead | most copies of one domain |
|---|---|---|
| `iem_cru_2km_taspr_seasonal` | 1.55× | 3 |
| `era5_4km_daily_t2_mean_wcs` | 1.32× | 4 |
| `cmip6_fwi` | 3.38× | 8 |
| `tas_2km_projected_wcs` | 7.41× | 16 |
| `conus_hydro_segments_stats_combined` | 12.64× | 16 |

So for every coverage in the re-ingest queue, `total_size` minus
(cells × bytes-per-cell) *is* the duplicate count, and the spreadsheet's
reclaimable column can be read directly.


### Why it happens

That unevenness is the clue. Regions touched by one write have one copy;
regions touched by four have four. These arrays were written into more than
once — `wcst_import` re-run against a coverage that already existed, each pass
laying down another copy of the tiles it touched rather than replacing them.

The server-wide pattern fits. Sixteen coverages exist as matched pairs, the same
source file ingested twice under different names, and the base version is clean
at 1.00× in every case while the `_wcs` variant — the one that was tuned and
re-run during development — carries the duplicates:

| Pair | Base | `_wcs` variant |
|---|---|---|
| `era5_4km_daily_t2_mean` | 1.00× | 1.32× |
| `ardac_chukchi_daily_slie` | 1.00× | 1.93× |
| `tas_2km_historical` | 1.00× | 3.77× |
| `tas_2km_projected` | 1.00× | **7.41×** |

### Where the money is

| Coverage | On disk | Overhead | Reclaimable |
|---|---|---|---|
| `tas_2km_projected_wcs` | 2,322 GB | 7.41× | **2,009 GB** |
| `ardac_chukchi_daily_slie_wcs` | 1,160 GB | 1.93× | 560 GB |
| `ardac_beaufort_daily_slie_wcs` | 956 GB | 1.94× | 464 GB |
| `cmip6_monthly_cf_wcs` | 418 GB | 3.69× | 305 GB |
| `conus_hydro_segments_stats_combined` | 254 GB | 12.64× | 234 GB |
| `tas_2km_historical_wcs` | 238 GB | 3.77× | 175 GB |
| six fire-weather coverages | 176 GB each | 3.38× | 124 GB each |

247 of 273 coverages are clean at 1.05× or better. Twenty-six carry the whole
4,591 GB, and the top five carry 3,572 GB of it.

### What to do

**Re-ingest into a fresh collection; never re-run `wcst_import` against a
coverage that already exists.** Delete the coverage first, or ingest under a new
name and swap. The recipes need no changes — `cmip6_fwi` goes from 175.9 GB to
52.0 GB with its tiling untouched.

Do not re-tile to fix this, and do not let a high storage overhead argue for
changing a tiling that serves its queries well. Storage overhead and read
amplification are separate problems with separate remedies; Finding 2 is the
one re-tiling solves.

To check any coverage:

```bash
curl -u rasadmin:$PASSWORD \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from $COLLECTION as c' \
  'https://<host>/rasdaman/rasql' > tiles.json
grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate.

## Finding 2 — Tiling fights the access pattern

This is the problem re-tiling actually solves, and it is independent of
Finding 1. Storage and read cost are unrelated: the `era5_4km_daily_t2_mean`
array exists on this server at 289, 1,172 and 4,678 tiles and occupies exactly
19.011 GB in all three. Tiling is a pure query-performance decision.

Most SNAP coverages are queried at a point or over a small AOI polygon: pick an
x/y, return the full time series. The tiling that suits this keeps the
non-spatial axes whole inside a tile and makes the spatial footprint small
(guide, section 3.1). Of 253 coverages we can model, **234 read more than a
thousand times the bytes a typical query returns, and 59 more than ten
thousand times.**

![The same array, tiled two ways](figures/rasdaman-tile-shape.svg)

`crrel_gipl_outputs_nc` is the clearest illustration. Its tile spans the entire
array — 100 × 3 × 2 × 1941 × 2471. A single-point time series therefore reads
**4,796,211×** more bytes than it returns. The same tiling scores 600× on a
whole-map read, so it is not badly built in the abstract; it is built for the
wrong question.

The trade-off is visible directly in coverages we already have, on the same
460 × 442 climate grid:

| Coverage | Tile | Point query | Map query |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | rasdaman's choice — 5 × 460 × 442 | 203,320× | 46,752× |
| `era5_4km_daily_t2_mean_wcs` | whole time axis, 8 × 8 spatial | **64×** | 23,899× |
| `cmip6_fwi` and its five siblings | whole time axis, 2 × 2 spatial | **4×** | 128,626× |

Shrinking the spatial footprint to 8 × 8 improves point reads three
thousand-fold and *also* improves map reads. Going on to 2 × 2 buys the last
factor of sixteen on point queries but makes map rendering five times worse,
because a frame must now assemble tens of thousands of tiny tiles.

A footprint around 8 × 8 to 12 × 12 with the sweep axis kept whole is the
setting to copy. It captures nearly all of the point-query benefit, keeps maps
usable, and costs nothing extra to store.

All six fire-weather coverages — `bui`, `dc`, `dmc`, `ffmc`, `fwi`, `isi` —
are identical: 266,724 tiles, 175.9 GB, 4× point, 128,626× map. They are the
2 × 2 case. Their tiling is defensible for the portal's point queries; what is
not defensible is the 123.9 GB of duplicates each one carries, which is
Finding 1's problem, not this one.

## Finding 3 — The cmip6_downscaled v2 family

175 coverages, the largest single group on the server. Every previous version of
this audit flagged them as unmeasurable. That is now resolved, and the answer is
that **they are the healthiest coverages we have.**

`rasql` could not reach them because `wcst_import` had not named their
collections after their coverage IDs. The rule turned out to be mundane: `_v2`
migrates from the middle of the name to the end, so
`cmip6_downscaled_pr_7ModelAvg_historical_v2_wcs` is stored as
`cmip6_downscaled_pr_7ModelAvg_historical_wcs_v2`. Reading petascope's own
pointer out of `petascopedb` resolves all 175 without guessing:

```sql
SELECT c.coverage_id, r.collection_name
  FROM coverage c
  JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id;
```

With that mapping, `dbinfo` succeeds on every one. They hold **3,981 GB
persisted against 3,947 GB of data — a storage overhead between 0.500× and
1.009×.** Several store below their uncompressed size. There are no duplicate
tiles anywhere in the family. They were ingested once, cleanly, and left alone.

What remains is a read-performance question. All 171 `_wcs` members declare
`ALIGNED [0:*, 0:31, 0:31] tile size 16777216`, from which rasdaman derives a
4096 × 32 × 32 tile. A full time series at one point touches five of them —
about **83.9 MB read to return 73 KB**, a median of **1,024×** across the
family. Shrinking the spatial chunk to 7 × 7 gives a ~3.6 MB tile at 49×; 4 × 4
gives ~1.2 MB at 16×.

Applied across 171 coverages serving the public data portal, that is the largest
*performance* win available — and because it touches no storage, it can be
scheduled independently of the re-ingest work in Finding 1.

## Finding 4 — Stored arrays that disagree with the catalogue

Eleven coverages have a stored array whose geometry does not match what WCS
advertises. This is a smaller and different list than earlier versions of this
audit reported, because most of what was on that list was the name-divergence
problem instead.

**Resolved, and no longer integrity issues.** `cmip6_fwi` was reported as
storing 44,165 time slices against an advertised 25,680; `era5_4km_daily_rh2_max`
as 21,915 against 23,376; `temperature_anomaly_anomalies` and
`temperature_anomaly_baselines` as single 4-byte cells. In every case the
measured object was an abandoned collection sitting at the coverage's name while
petascope served the real array from elsewhere. With the `petascopedb` mapping
in place, `dbinfo` now returns the coverage's own array for **all 273**, and the
`dbinfo array matches?` column reads `yes` for every row. Those four are
cleanup candidates (Finding 6), not data problems.

**Genuine disagreements.** The remaining eleven are cases where petascope's
advertised axis lengths differ from the stored array's:

| Coverage | Catalogue | Stored |
|---|---|---|
| `ak_hydro_segments_stats_combined` | source 3, era 2, model 7 | 7, 3, 7 |
| `conus_hydro_segments_stats_diff` | model 14, scenario 4, era 3 | 14, 5, 4 |
| `cmip6_downscaled_pr_CNRM_CM6_1_HR_historical_v2_wcs` | 1 × 1 × 1 | 18,250 × 443 × 460 |

The `ak_hydro_segments_*` group (five coverages) and the `conus_hydro_segments_*`
group look like genuine schema drift — an axis gained or lost a member and one
layer was not updated. Those need a decision about which is authoritative.

The `cmip6_downscaled_pr_CNRM_CM6_1_HR_historical_v2_wcs` row is different in
kind: `DescribeCoverage` reports a 1 × 1 × 1 grid for a coverage that demonstrably
holds 18,250 × 443 × 460 and serves real data. That is petascope mis-serving its
own metadata for one coverage, not a storage problem, and it is worth reporting
upstream.

The remaining rows are the two `crrel_gipl_outputs_nc_regular_*_test` coverages
and `conus_hydro_segments_jp_test*`, all test coverages already slated for
deletion. The spreadsheet's **Data Integrity** tab lists all eleven with both
geometries side by side.

## Finding 5 — Collections nothing points at

Separate from anything inside a live coverage, rasdaman holds **105 collections
that no coverage references, totalling 3,523 GB.** Comparing RASBASE's
`RAS_MDDCOLLNAMES` against the 273 names in petascope's mapping finds them; they
are invisible to WCS and unreachable through any OGC request.

| Category | Count | Size |
|---|---|---|
| Abandoned ingest — timestamped copy of a coverage that no longer exists | 23 | 1,400.5 GB |
| Unreferenced — origin unclear | 16 | 959.4 GB |
| Personal — named after a user | 11 | 624.0 GB |
| Test / scratch | 26 | 317.8 GB |
| Shadow — sits at a live coverage's name | 12 | 221.1 GB |
| Superseded ingest | 17 | 0.0 GB |

One collection is a third of the total:
`cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874`, **1,268
GB across 163,341,150 tiles.** Next are `big_tile_ardac_chukchi_daily_slie` at
600 GB, `cmip6_downscaled_tasmax_complete_crstephenson` at 356 GB, and
`crrel_gipl_outputs` at 192 GB.

Fifty of the 105 are 1 MB or smaller — four-byte stubs left by failed ingests.
They cost nothing but clutter the catalogue.

`rasdaman_unreferenced_collections.csv` lists all 105 with a category and a
`drop collection` statement per row, for review. **Nothing should be dropped
without checking its `oid` against `rasdaman_range_set` first**, since the
mapping is what proves a collection is genuinely unreferenced.

## Finding 6 — Reproducibility gaps

**24 live coverages have no ingest recipe on `origin/main`.** They cannot be
rebuilt, reviewed, or reasoned about except by introspecting the server. Several
are user-facing.

This number was originally reported as 43. That was wrong: 23 recipe files carry
embedded WMS style definitions containing literal newlines inside a JSON string,
which strict `json.loads` rejects — and the scanner skipped them silently, so a
parse failure was indistinguishable from an absent recipe.
`ardac/gipl/ingest_with_nc.json` was one of them. Both faults are fixed: the
parser is lenient, falls back to regex, and reports every file it could not read
to `_recipe_problems.log`.

**45 coverages declare no tiling** and took rasdaman's default. On the evidence
of Finding 1 that costs them nothing in storage, and rasdaman's default choice is
a reasonable ~4 MB cube — but it is unexamined, and for point-query coverages it
is the wrong shape (see the first row of Finding 2's table).

**Seven test coverages are live in the public catalogue**, holding 512 GB:
`crrel_gipl_outputs_nc_regular_1_test` (255 GB),
`crrel_gipl_outputs_nc_regular_2_test` (132 GB), `cp_test_gipl` (119 GB),
`conus_hydro_segments_jp_test` and `_insitu` (2.7 GB each), `hydro_dh3_test`,
and `cmip6_downscaled_tasmax_v2_wms_test`. They appear in `GetCapabilities`, so
external clients can see and query them.

**The recipe repository is behind the server.** The v2 coverages went live from
scripts on `origin/main`, but a checkout sitting on another branch shows no trace
of them — which is how an earlier pass of this audit concluded, wrongly, that
the recipes did not exist. Confirm your branch before auditing recipes.

---

## Recommended sequence

Three independent problems, three different remedies. Conflating them wastes
effort, and two of the three touch no recipe at all.

**First, drop what nothing points at — 3,523 GB, no risk to any live coverage.**
Start with `cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874`
alone: 1,268 GB, a third of the total, in one statement. Then the 50 empty stubs,
which are free. Verify each `oid` against `rasdaman_range_set` before dropping.

**Second, re-ingest the duplicated giants — 3,572 GB across five coverages.**
`tas_2km_projected_wcs` (2,009 GB reclaimable), the two `ardac_*_daily_slie_wcs`
coverages, `cmip6_monthly_cf_wcs` and `conus_hydro_segments_stats_combined`. Drop
and re-ingest each into a fresh collection **with its recipe unchanged**, then
dump the tile domains and confirm no domain appears more than once. The six
fire-weather coverages follow, at 124 GB each.

**Third, re-tile for read performance.** The v2 family first: 171 coverages,
roughly 20× less I/O per point query, no storage consequence either way. Do one,
measure it, then batch the rest. Then the 45 coverages on default tiling and the
worst of the modelled amplification list.

**Fourth, resolve the integrity list** — 11 coverages, mostly a decision about
which layer is authoritative, plus one petascope metadata bug worth reporting
upstream.

**Fifth, housekeeping**: delete the seven test coverages (512 GB), commit recipes
for the 24 coverages that lack them, and set a process rule that
`wcst_import` is never run against a coverage that already exists — that single
rule is what prevents Finding 1 from recurring.

After each change, re-run the audit script against the affected coverages and
check `storage_overhead_factor` and `per_axis_divisibility`.

## Method, and what these numbers do not prove

Five sources, in order of authority. A **WCPS type-error probe** makes rasdaman
describe its own array — extents, cell type, null value — needing neither
credentials nor a collection name. It reached all 273 and is the authority for
what is stored. **petascope's own pointer**, read from `petascopedb`, gives each
coverage's true rasdaman collection name; with it, **rasql `dbinfo`** — the
tiling rasdaman built, band types, persisted size, and with `printtiles` every
individual tile domain — succeeded for **all 273, with zero failures**. **WCS
DescribeCoverage** gives axis labels and the catalogue's view of the grid.
**RASBASE's `RAS_MDDCOLLNAMES`** gives the full collection inventory, which is
how the 105 unreferenced collections were found. Declared tiling and `gridOrder`
come from the recipes on `origin/main`.

**Two ways to add up the same thing.** Summing each coverage's excess over the
26 that carry duplicates gives **4,591 GB** — what a re-ingest campaign would
actually recover, and the figure used throughout. Subtracting total data from
total persisted gives 4,407 GB instead, because healthy compression elsewhere
(coverages storing *below* their uncompressed size) nets off 184 GB of it. The
first number is the actionable one; the second describes the server as a whole.

**Storage figures are measured, and the identity behind them is verified.**
Persisted size comes from `dbinfo`; logical size is cells × bytes-per-cell.
Their difference is duplicate tiles, which is not an inference: for six
coverages spanning 1.32× to 12.64× we dumped every tile domain and confirmed
that bytes on disk divided by bytes the tiles cover is 1.00×, and that the
*unique* domains cover each array exactly. Padding and tile count contribute
nothing, which the three-way `era5_4km_daily_t2_mean` comparison demonstrates
independently.

**The cause of the duplication is inferred, not proven.** The evidence — uneven
copy counts, and the pattern that iterated `_wcs` variants duplicate while their
once-ingested siblings do not — points to `wcst_import` being re-run against an
existing coverage. The decisive test is to ingest a throwaway coverage, measure
it, re-run the same recipe without dropping it, and measure again. Until that
runs, treat the remedy as well-founded rather than confirmed.

**Axis alignment is the subtlety that makes or breaks this.** DescribeCoverage
reports axes in *coverage* order; `sdom` and `tileConfiguration` are in *storage*
order, and pairing one's names with the other's numbers silently attaches every
per-axis result to the wrong axis. Storage order is taken from each recipe's
declared `gridOrder` where available and inferred by matching extents otherwise.

**Read amplification is modelled, not measured.** It is the bytes a query must
read over the bytes it wants, for a representative query matched to each
coverage's role, derived from the measured tile shape. It is a sound basis for
ranking, not a benchmark. Twenty coverages have no modelled figure because their
role could not be classified.

**Recommended tiling strings are a starting point.** They target a ~4 MB tile
against the coverage's role. Verify each against the source file's real dimension
order before ingesting, and re-run the audit afterwards.

**What is still open.** One `dbinfo` failed during the unreferenced-collection
sweep (`cmip6_downscaled_pr_wms_crstephenson`, unparseable response), so 3,523 GB
is a floor rather than an exact figure. The eleven catalogue disagreements in
Finding 4 are identified but not adjudicated — each needs a human decision about
which layer is right. And the duplication cause above remains untested.
