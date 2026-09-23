# Rasdaman Tiling Audit — zeus.snap.uaf.edu

**23 September 2026 · 273 coverages · rasdaman v10.4.7**

This audit examines every coverage on the production rasdaman server: how it is tiled, how much disk it occupies and why, and whether its tiling matches how it is actually queried. It exists to decide what to re-ingest, what to delete, and in what order.

**Read [the tiling guide](rasdaman-tiling-guide.md) first.** It explains what a tile is, what it costs, and how a recipe becomes stored tiles.

Read it alongside `rasdaman_tiling_audit.xlsx` for the per-coverage detail, and `data/unreferenced_collections.csv` for the cleanup list. The **Cleanup** tab ranks unreferenced collections by real, `PhysicalSize`-based GB; the **Column Guide** tab defines every column, including which are measured facts and which are modelled.

Everything here was produced by `rasdaman_tiling_audit.py` and `rasdaman_physical_size.py`, both re-runnable after any change to confirm what rasdaman actually built and what it actually occupies.

---

## The short version

**273 live coverages occupy 9,172 GB on disk — matching rasdaman's own UI figure ("9.17 TB") almost exactly.**

**Of that, essentially nothing is duplicate-tile waste.** Twenty-six coverages have duplicate entries in their tile *index* — the same domain listed two, four, up to sixteen times, confirmed by dumping the domains directly — but every one of them, checked against RASBASE's own `PhysicalSize` field, stores exactly its real, unique data and not a byte more. The index entries are redundant; the bytes on disk are not. There is no re-ingest campaign to run for disk space (see Finding 1 below).

**2,037 GB is collections nothing points at, and is real.** Abandoned ingests, personal experiments, test runs, and twelve arrays squatting on live coverage names — invisible to WCS, droppable without touching a live coverage. A further 1,268 GB sits in a single orphan collection that turned out to be almost entirely *file-referenced* rather than stored — its data exists "in-situ", but not inside rasdaman, so dropping it will not recover that GB figure. (see Finding 5 below).

**Separately, and unaffected by either of the above, most coverages are tiled against their own access pattern.** Of 253 coverages we can model, 52 read more than a thousand times the bytes a typical query returns, and 50 more than ten thousand times — for the point and small-AOI lookups the data portal issues constantly. This is the problem re-tiling solves, and it has *no* storage consequence in either direction, correction or no correction. (see Finding 2 below).

Underneath sit eleven coverages whose stored array disagrees with what WCS advertises (Finding 4), 24 live coverages with no ingest recipe on `origin/main` (see Finding 6 below), and seven test coverages holding real (if modest) disk in the public catalogue.

**The 171 `cmip6_downscaled` v2 coverages are the healthiest group on the server for storage** at 0.500×–1.009× storage overhead with no duplicates at all — though their real tiling carries a serious map-query cost (Finding 3).

A note on what is measured. Reading petascope's own coverage-to-collection pointer out of `petascopedb` made `dbinfo` succeed for **all 273 coverages with no failures**, each one confirmed against the coverage's own array. Every disk-size figure in this document is `RAS_MDDOBJECTS.PhysicalSize`, read directly from RASBASE, not `dbinfo`'s `totalSize` (see Finding 1 and the Method section for why that distinction matters). Nothing below is modelled from ingest recipes.

---

## Finding 1 — The tile index has duplicate entries; the disk does not

This finding went through three wrong explanations for *why* excess storage existed, and then — after all three were resolved — one wrong conclusion about whether it existed at all. All four are worth naming, because the wrong ones are exactly as intuitive as the right one.

**Padding is not the cause.** rasdaman's own documentation describes boundary tiles under both `REGULAR` and `ALIGNED` as shrinking to fit, not padding to full declared size, and the `era5_4km_elevation` example shows zero inflation in its tile-index accounting to begin with, so there was no padding there to take up disk. 

**Tile shape is not the cause either.** Comparing tile counts against the geometry the recipe implies looks like a diagnostic and is not one — a wildcard bracket makes the geometric count meaningless. `era5_4km_daily_t2_mean` scores 2,339× by that test but stores at exactly 1.00×.

**The tile index genuinely does have duplicate entries.** This part held up. Dumping every tile domain with `dbinfo(c,"printtiles=embedded")` and counting them proves it directly. Here is one cell of `cmip6_fwi` — model 2, time 100, lat 10, lon 10 — and every entry in the collection's index that contains it:

```
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
```

Four byte-identical domains, indexed four separate times.

![The same tile, stored several times](figures/rasdaman-tile-duplication.svg)

Three coverages had every domain dumped and saved (`data/tile-dumps/`):

| Coverage | Indexed tiles | Unique domains | Worst repeat |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | 4,678 | 4,678 | 1× (clean) |
| `era5_4km_daily_t2_mean_wcs` | 7,642 | 6,553 | 4× |
| `cmip6_fwi` | 266,724 | 101,745 | 8× |

Three more were checked with the `grep`/`sort`/`uniq -c` pipeline below, confirming the same pattern without a retained unique-domain count: `iem_cru_2km_taspr_seasonal` (8,276 indexed tiles, worst repeat 3×), `tas_2km_projected_wcs` (4,265,164 indexed tiles, worst repeat 16×), and `conus_hydro_segments_stats_combined` (1,806,720 indexed tiles, worst repeat 16×). For `cmip6_fwi` the full copies-per-domain distribution, counted directly from the saved dump, is uneven: 27,290 domains stored once, 29,847 twice, 2,364 three times, 41,218 four times, 10 five times, 186 six times, 30 seven times, and 800 eight times (101,745 domains total, matching the unique count above) — which is the clue behind the leading theory for why this happens (below).

```bash
curl -u rasadmin:$PASSWORD \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from $COLLECTION as c' \
  'https://<host>/rasdaman/rasql' > tiles.json
grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate index entry.

### Duplicate entries do not cost disk

Originally we assumed the duplicates were separately-stored copies, because `dbinfo`'s `totalSize` agreed: summing the unique domains' bytes matched each array's real size exactly, and summing *all* indexed entries (duplicates included) matched `totalSize` exactly, for every one of the six coverages above. That identity is real. What was wrong was treating `totalSize` as bytes on disk.

It isn't. `totalSize` is computed by summing, for every entry in the tile **index**, that entry's own byte footprint. It has no way to know that two entries describe the same region — so a domain indexed four times is counted four times, whether or not rasdaman actually stored four copies. The dumps above prove the *index* has duplicate entries. They do not prove the *disk* does.

The distinction only became visible by checking a second, independent number: `RAS_MDDOBJECTS.PhysicalSize`, a field in RASBASE's own catalogue computed per stored array object, not by walking the tile index. It cannot be inflated by a duplicate index entry, because it isn't summed from index entries at all.

```sql
sqlite3 -readonly /opt/rasdaman/data/RASBASE "
SELECT cn.MDDCollName, o.PhysicalSize
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 WHERE cn.MDDCollName = 'cmip6_fwi_2026_05_01_09_29_49_8758';"

-- 52018435200  (52.0 GB — the unique-domain figure, not totalSize's 175.9 GB)
```

Checked against all 26 coverages this audit had flagged by the `totalSize` test, `PhysicalSize` matched the unique-domain figure exactly, every time, with zero exceptions:

| Coverage | `totalSize` (declared) | `PhysicalSize` (real) |
|---|---|---|
| `tas_2km_projected_wcs` | 2,321.9 GB | 313.3 GB |
| `ardac_beaufort_daily_slie_wcs` | 956.0 GB | 491.8 GB |
| `ardac_chukchi_daily_slie_wcs` | 1,160.2 GB | 599.7 GB |
| `cmip6_monthly_cf_wcs` | 417.6 GB | 113.1 GB |
| `conus_hydro_segments_stats_combined` | 254.2 GB | 20.1 GB |
| `tas_2km_historical_wcs` | 238.5 GB | 63.2 GB |
| six fire-weather coverages, each | 175.9 GB | 52.0 GB |
| **sum across all 26 flagged coverages** | **7,025.2 GB** | **2,433.9 GB** |

Not one of the 26 shows a `PhysicalSize` between the two figures, or above `totalSize`. Every gap is exactly, and only, duplicate index weight. `scripts/rasdaman_physical_size.py` reproduces this whole table from RASBASE directly — see the [Method section](#method-and-what-these-numbers-do-not-prove) for the full query and how to run it.

**247 of 273 coverages have a clean, 1:1 tile index (index inflation does not exceed 1.05×).** The other 26 carry duplicate entries. Total phantom `totalSize` inflation across all 273 — clean and dirty coverages alike, since even a clean index carries a little rounding noise — is 4,406.7 GB.

But what to do about it? **Nothing, for disk space**  — `PhysicalSize` is the real number to use in the census.


### Why the duplication happens anyway

The uneven copies-per-domain distribution is still the best clue, even though it no longer points at a disk cost. A region touched by one write has one index entry; a region touched by four writes has four. **Not independently confirmed** — the working theory is that `wcst_import` was re-run against a coverage that already existed, each pass adding another index entry for the tiles it touched instead of replacing the one already there.

The server-wide pattern fits. Sixteen coverages exist as matched pairs, the same source ingested twice under different names, and the base version has a clean, unduplicated index in every case while the `_wcs` variant — the one tuned and re-run during development — carries the duplicate entries:

| Pair | Base | `_wcs` variant |
|---|---|---|
| `era5_4km_daily_t2_mean` | 1.00× | 1.32× |
| `ardac_chukchi_daily_slie` | 1.00× | 1.93× |
| `tas_2km_historical` | 1.00× | 3.77× |
| `tas_2km_projected` | 1.00× | **7.41×** |

(These ratios are `totalSize`/`PhysicalSize` — the index-inflation factor, not a disk multiplier.)

The decisive test would be to ingest a throwaway coverage, measure it, re-run the same recipe without dropping it, measure again. This has not been run. Treat this explanation as well-founded, not confirmed.

### What to do

**Nothing, for disk space.** There is no GB to reclaim here, and no re-ingest campaign to schedule for that reason. Do not re-tile to fix this, and do not let a `totalSize`/`PhysicalSize` gap argue for changing a tiling that serves its queries well — Finding 2 is the one re-tiling solves, and it is independent of this one.

**Still avoid re-running `wcst_import` against a coverage that already exists.** It costs nothing to follow this rule, and while we now know it doesn't cost disk, we have not checked whether a bloated spatial index costs anything in query latency — an R+-tree with sixteen entries for one region might do sixteen times the lookup work even though the underlying blob is fetched once. That's an open question, not a settled one, and the cheapest way to not need an answer is to not create the duplicate entries in the first place. Delete a coverage before re-ingesting it, or ingest under a new name and swap.

247 of 273 coverages have a clean, 1:1 tile index (index inflation does not exceed 1.05×). The 26 that don't are listed on the spreadsheet's Coverages tab; none of them need action for storage reasons.

## Finding 2 — Tiling fights the access pattern

This is the problem re-tiling actually solves, and it is independent of Finding 1. Storage and read cost are unrelated: the `era5_4km_daily_t2_mean` array exists on this server at 289, 1,172 and 4,678 tiles and occupies exactly 19.011 GB in all three. Tiling is a pure query-performance decision.

Most SNAP coverages are queried at a point or over a small AOI polygon: pick an x/y, return the full time series. The tiling that suits this keeps the non-spatial axes whole inside a tile and makes the spatial footprint small (guide, section 3.1). Of 253 coverages we can model, **52 read more than a thousand times the bytes a typical query returns, and 50 more than ten thousand times.**

The trade-off is visible directly in coverages we already have, on the same 460 × 442 4km ERA5 grid:

| Coverage | Tile | Point query | Map query |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | rasdaman's choice — 5 × 460 × 442 | 203,355× | 5× |
| `era5_4km_daily_t2_mean_wcs` | whole time axis, 8 × 8 spatial | 128× | 23,898× |
| `cmip6_fwi` and its five siblings | whole time axis, 2 × 2 spatial | 4× | 25,725× |

Shrinking the spatial footprint to 8 × 8 improves point reads roughly 1,600-fold — and makes map reads about 4,800 times worse, because a frame that used to fit in one tile (it already covered the whole grid) now has to assemble thousands of small ones. Going on to 2 × 2 buys a further 32× on point queries, at almost no additional cost to map rendering: it was already so fragmented that shrinking the spatial footprint further barely changes it.

## Finding 3 — The cmip6_downscaled v2 family

175 coverages, the largest single group on the server. **Storage-wise, they are the healthiest coverages we have** — read on for a real cost this same finding turned up.

Our usual `rasql` `dbinfo` query could not reach them because `wcst_import` had not named their collections after their coverage IDs. The rule turned out to be mundane: `_v2` migrates from the middle of the name to the end, so `cmip6_downscaled_pr_7ModelAvg_historical_v2_wcs` is stored as `cmip6_downscaled_pr_7ModelAvg_historical_wcs_v2`. Reading petascope's own pointer out of `petascopedb` resolves all 175 without guessing:

```sql
SELECT c.coverage_id, r.collection_name
  FROM coverage c
  JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id;
```

With that mapping, `dbinfo` succeeds on every one. They hold **3,981 GB persisted against 3,947 GB of data — a storage overhead between 0.500× and 1.009×.** Several store below their uncompressed size. There are no duplicate tile-index entries anywhere in the family.

What remains is a read-performance question. All 171 `_wcs` members declare `ALIGNED [0:*, 0:31, 0:31] tile size 16777216`. Sampling real tile domains directly — checked on six coverages spanning `pr`/`tasmin`/`tasmax`, `historical` and three `ssp` scenarios, and the `7ModelAvg` ensemble product, with the identical distribution in every one — shows two tile populations, not one: **73–79% of tiles span nearly the entire time axis** (all but the last day) at a narrow 4–7-cell spatial chunk, and the remaining **12–17% cover just that last day** at a 32 × 32 spatial chunk.

A full time series at one point touches only the 2 tiles that cover it — the one large one plus the last-day one — at **256×** (the three `ssp` scenarios) or **448×** (`historical`). A single time-slice map, by contrast, touches nearly every spatial-position tile, each dragging in almost the whole time axis, at **~32,827×** (`ssp`) or **~19,257×** (`historical`).

Why the tiling comes out this way is not established. `wcst_import`'s write pattern for this family — plausibly one large write per small spatial subregion spanning the whole time axis — is the leading candidate, not a confirmed mechanism.

Applied across 171 coverages serving the public data portal, the map-query cost is the one worth fixing here — point queries are already reasonable, map rendering is not. Because it touches no storage, this work can be scheduled independently of the re-ingest work in Finding 1.

## Finding 4 — Stored arrays that disagree with the catalogue

Eleven coverages have a stored array whose geometry does not match what WCS advertises. These are cases where petascope's advertised axis lengths differ from the stored array's:

| Coverage | Catalogue | Stored |
|---|---|---|
| `ak_hydro_segments_stats_combined` | source 3, era 2, model 7 | 7, 3, 7 |
| `conus_hydro_segments_stats_diff` | model 14, scenario 4, era 3 | 14, 5, 4 |
| `cmip6_downscaled_pr_CNRM_CM6_1_HR_historical_v2_wcs` | 1 × 1 × 1 | 18,250 × 443 × 460 |

The `ak_hydro_segments_*` group (five coverages) and the `conus_hydro_segments_*` group look like genuine schema drift — an axis gained or lost a member and one layer was not updated. Those need a decision about which is authoritative.

The `cmip6_downscaled_pr_CNRM_CM6_1_HR_historical_v2_wcs` row is different in kind: `DescribeCoverage` reports a 1 × 1 × 1 grid for a coverage that demonstrably holds 18,250 × 443 × 460 and serves real data. That is petascope mis-serving its own metadata for one coverage, not a storage problem, and it is worth reporting upstream.

The remaining rows are the two `crrel_gipl_outputs_nc_regular_*_test` coverages and `conus_hydro_segments_jp_test*`, all test coverages already slated for deletion. The spreadsheet's **Data Integrity** tab lists all eleven with both geometries side by side.

## Finding 5 — Collections nothing points at, and one that isn't really there

Separate from anything inside a live coverage, rasdaman's catalogue holds **105 collections that no coverage references.** Comparing RASBASE's `RAS_MDDCOLLNAMES` against the 273 names in petascope's mapping finds them; they are invisible to WCS and unreachable through any OGC request. Priced by `RAS_MDDOBJECTS.PhysicalSize` (one, `cmip6_downscaled_pr_wms_crstephenson`, has no object row at all — a true empty shell, nothing to price), the 104 priced collections total **3,304.8 GB**:

| Category | Count | Size (real, `PhysicalSize`) |
|---|---|---|
| Abandoned ingest — timestamped copy of a coverage that no longer exists | 23 | 1,398.7 GB |
| Unreferenced — origin unclear | 16 | 882.0 GB |
| Personal — named after a user | 10 | 496.3 GB |
| Test / scratch | 26 | 310.4 GB |
| Shadow — sits at a live coverage's name | 12 | 217.4 GB |
| Superseded ingest | 17 | 0.0 GB |

### Why these come to exist: the ingest/delete tooling has no safety net

The two scripts operators actually run — `/usr/local/bin/add_coverage.sh` (wraps `wcst_import.sh`) and `/usr/local/bin/delete_coverage.sh` (wraps WCS-T `DeleteCoverage`) — aren't part of this repo, but reading them explains most of the categories above. (See the [tiling guide's coverage-vs-collection note](rasdaman-tiling-guide.md#0-coverage-vs-collection) for what a "coverage" and a "collection" each are and how loosely they're joined — that's the gap these two scripts fall into.)

`add_coverage.sh` is a one-line wrapper:

```bash
/opt/rasdaman/bin/wcst_import.sh -c 0 ${PATH_TO_INGEST_DOC}
```

It does not check whether the coverage ID in the ingest document already exists, and it does not check `wcst_import`'s own exit code — there's no success or failure message at all. Nothing stops, or even warns about, running it twice against the same coverage ID, which is exactly the hazard Finding 1's "one process rule" calls out for the tile index. `data/unreferenced_collections.csv` shows it's a hazard for the catalogue too, not just the index. `alfresco_relative_flammability_30yr_crstephenson` was re-ingested **12 times** on 2025-01-29 alone (some pairs under a minute apart), plus once more on 2025-02-04 and again on 2025-04-15 — all 12 January collections are now sitting in the "abandoned ingest" category, timestamped, with nothing in petascope pointing at any of them. `conus_hydro_segments_test_exsitu_reg2` (3 copies inside about 3 hours) and `test_nan` (5 copies inside 47 minutes) show the same pattern at smaller scale.

That's the leading, directly evidenced explanation for the 23-collection, 1,398.7 GB "abandoned ingest" category: each re-ingest of a live coverage ID gets `wcst_import` to mint a fresh, uniquely timestamped rasdaman collection, and petascope's `coverage → rasdaman_range_set` pointer moves to the newest one. The previous collection is stranded — and by the time it's orphaned, there is no coverage ID left to hand `delete_coverage.sh`, because an orphaned collection is, by definition, one no coverage ID resolves to anymore. `add_coverage.sh` has no step that would catch this before it happens.

(`add_coverage.sh` also hardcodes `-c 0`. Checked directly against `wcst_import.sh -h` on the host: `-c`/`--gdal-cache-size` controls how many open GDAL datasets `wcst_import` keeps cached, and `0` disables that cache entirely — a resource-tuning choice, unrelated to re-ingest safety. `wcst_import.sh` has no flag that checks for or protects against an already-existing coverage ID; `-a`/`--analyze` is the closest thing, a dry run that prints the WCS-T requests and a recommended tiling scheme without importing, and `add_coverage.sh` doesn't invoke it. The absence of any existing-coverage check stands as stated above.)

`delete_coverage.sh` closes the other half of the loop, but not reliably:

```bash
response=$(curl --user "$RASCURL" --insecure -s -o /dev/null -w "%{http_code}" \
  "https://localhost/rasdaman/ows?SERVICE=WCS&VERSION=2.0.1&REQUEST=DeleteCoverage&COVERAGEID=${COVERAGEID}")
```

WCS-T `DeleteCoverage` is documented to remove the coverage's rasdaman collection, its WMS layer (if any), and its petascope metadata as one request — but the two backing stores (`petascopedb` over SQL, rasdaman over `rasql`) aren't one transaction. rasdaman's own issue tracker has a report of exactly this splitting under load: the `petascopedb` row deletes, the `rasql drop collection` step throws a serialization exception because rasdaman is busy with something else (a concurrent ingest, for instance), and the collection survives with nothing pointing at it (rasdaman tracker, ticket #1466 — old, closed as "invalid" by the reporter, and its ticket page wasn't reliably reachable when we checked it, so treat this as a documented failure mode worth knowing about, not a confirmed defect in the version we run).

`delete_coverage.sh` gives you no way to notice if this happens: `-o /dev/null` throws away the response body, so any WCS `ExceptionReport` that would explain a partial failure is discarded; the script checks the HTTP status once with no retry; and there's no follow-up check against `RAS_MDDCOLLNAMES` to confirm the collection is actually gone. A `200` tells you petascope accepted the request, not that both halves of the delete completed.

**Practical takeaway:** don't treat `add_coverage.sh` as protection against re-ingesting into a live coverage, and don't treat a `200` from `delete_coverage.sh` as proof the rasdaman collection is gone — verify independently (`RAS_MDDCOLLNAMES` vs. the `petascopedb` mapping, the same reconciliation this audit runs) before assuming either script did what it was asked.

### The 1,268 GB collection that isn't on this server's disk

One collection is more than a third of the total by itself: `cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874`, **1,268.0 GB across 163,341,150 tiles.** Its `PhysicalSize` is honest — that much data genuinely exists — but it is not inside rasdaman's own storage. `wcst_import` can ingest "in situ": instead of copying source file bytes into rasdaman, it leaves each tile as a pointer into the original file and records the pointer in `RAS_FILETILES` instead of writing a blob into `RAS_TILES`. This collection is that case, confirmed two ways:

```sql
-- 1. Virtually the entire server's file-reference table is one directory
sqlite3 -readonly /opt/rasdaman/data/RASBASE \
  "SELECT count(*), FilePath FROM RAS_FILETILES GROUP BY FilePath ORDER BY count(*) DESC LIMIT 15;"
-- all 15 rows: /opt/rasdaman-storage/.../crstephenson/.../tasmax_{MODEL}_{SCENARIO}_adjusted.nc,
-- each with exactly 3,295,950 rows

-- 2. That owner's directory alone accounts for 166.1M of the server's 166.1M file references
python3 scripts/rasdaman_physical_size.py --rasbase "$RASBASE" --check-fileref crstephenson
-- ~166,070,985 matching rows -- essentially all of RAS_FILETILES
```

That second check can't by itself tell two same-owner collections apart, so the confirmation that it's specifically *this* collection, and not its smaller sibling below, is the tile count: this collection's own declared tile count (163,341,150) accounts for 98.4% of the 166.1M references by itself, leaving no real room for anything else to be a major contributor. Its sibling's declared tile count (23,089 — three orders of magnitude smaller) rules it out as part of that population.

Dropping this collection removes a catalog entry, not rasdaman disk — the underlying netCDF files stay exactly where they are, used or not, regardless of whether this collection exists. **The genuinely recoverable total across the other 104 orphan collections is 2,036.8 GB.**

Its smaller, un-timestamped sibling `cmip6_downscaled_tasmax_complete_crstephenson` (229.7 GB, "personal", 23,089 tiles — almost certainly real blob storage on the tile-count evidence above, but not individually confirmed) shares the same owner and naming pattern. Neither it nor `big_tile_ardac_chukchi_daily_slie` (599.7 GB) nor `crrel_gipl_outputs` (115.1 GB), the next-largest items, has been checked as rigorously as the collection above. Run `--check-fileref` against each owner/source-directory token before treating a large orphan's GB as certainly recoverable.

Fifty of the 105 are 1 MB or smaller — four-byte stubs left by failed ingests. They cost nothing but clutter the catalogue.

`data/unreferenced_collections.csv` lists all 105 with a category and a `drop collection` statement per row, for review; `data/physical_sizes.csv` (or a fresh run of `rasdaman_physical_size.py`) has the real GB for each. **Nothing should be dropped without checking its `oid` against `rasdaman_range_set` first**, since the mapping is what proves a collection is genuinely unreferenced, and nothing sized above a few GB should be treated as recoverable disk without a `--check-fileref` pass first.

## Finding 6 — Reproducibility gaps

**24 live coverages have no ingest recipe on `origin/main`.** They cannot be rebuilt, reviewed, or reasoned about except by introspecting the server. Several are user-facing.


**45 coverages declare no tiling** and took rasdaman's default. On the evidence of Finding 1 that costs them nothing in storage, and rasdaman's default choice is a reasonable ~4 MB cube — but it is unexamined, and for point-query coverages it is the wrong shape (see the first row of Finding 2's table).

**Seven test coverages are live in the public catalogue**, holding 351 GB: `crrel_gipl_outputs_nc_regular_1_test` (115.0 GB), `crrel_gipl_outputs_nc_regular_2_test` (115.1 GB), `cp_test_gipl` (115.1 GB), `conus_hydro_segments_jp_test` and `_insitu` (2.7 GB each), `hydro_dh3_test` (0.5 GB), and `cmip6_downscaled_tasmax_v2_wms_test` (0 GB — a 1×1×1×1×1 stub). They appear in `GetCapabilities`, so external clients can see and query them.

---

## Recommended sequence

Two independent problems with real disk consequences, one performance problem with none, and one piece of housekeeping that prevents the index-duplication pattern from recurring even though it no longer costs space.

**First, drop what nothing points at.** Verify each `oid` against `rasdaman_range_set` before dropping anything. Run `--check-fileref` (Finding 5) against any large orphan before counting its GB:

1. The 50 empty stubs — free, no check needed.
2. `cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874` — confirmed file-referenced; drop for catalog cleanliness, expect ~0 GB recovered.
3. The rest of the 104 priced orphans, largest first — **2,036.8 GB** genuinely recoverable, pending the `--check-fileref` spot-checks on the next few largest (Finding 5).

**Second, re-tile for read performance — no storage consequence either way.** The v2 family first: 171 coverages. Point queries against them are already reasonable (256–448×); map rendering is not (~19,000–33,000×) and needs a re-tile. Do one, measure it, then batch the rest. Then the 45 coverages on default tiling and the worst of the modelled amplification list. This step does not touch disk usage in either direction; schedule it independently of the first.

**Third, resolve the integrity list** — 11 coverages, mostly a decision about which layer is authoritative, plus one petascope metadata bug worth reporting upstream.

**Fourth, housekeeping**: delete the seven test coverages, commit recipes for the 24 coverages that lack them, and keep the process rule from Finding 1 — `wcst_import` is never run against a coverage that already exists. That rule no longer prevents lost disk space, since Finding 1 showed there wasn't any to lose, but it keeps the tile index clean and avoids finding out the hard way whether a bloated index has some other cost. When deleting any of these, don't trust `delete_coverage.sh`'s exit message alone — Finding 5's tooling note explains why a `200` doesn't guarantee the rasdaman collection is actually gone.

After each change, re-run `rasdaman_tiling_audit.py` and `rasdaman_physical_size.py` against the affected coverages and check both `storage_overhead_factor` (index health) and `PhysicalSize` (real disk).

## Method, and what these numbers do not prove

Six sources, in order of authority:
- A **WCPS type-error probe** makes rasdaman describe its own array — extents, cell type, null value — needing neither credentials nor a collection name. It reached all 273 and is the authority for what is *stored*, structurally.
- **petascope's own pointer**, read from `petascopedb`, gives each coverage's true rasdaman collection name
-  **rasql `dbinfo`** — the tiling rasdaman built, band types, and with `printtiles` every individual tile domain — succeeded for all 273, with zero failures.
- **`RAS_MDDOBJECTS.PhysicalSize`**, read directly from RASBASE, is the authority for what is *persisted on disk* — see below for why it replaced `dbinfo`'s `totalSize` in that role. 
- **WCS DescribeCoverage** gives axis labels and the catalogue's view of the grid. 
- **RASBASE's `RAS_MDDCOLLNAMES`** gives the full collection inventory, which is how the 105 unreferenced collections were found. Declared tiling and `gridOrder` come from the recipes on `origin/main`.

### Why `totalSize` is the wrong source for disk usage, and what replaced it

`dbinfo`'s `totalSize` is computed by walking the tile **index** (`tiling.tileDomains`) and summing each indexed entry's own byte footprint — cell count times bytes-per-cell, adjusted for boundary padding. It is a sum over index entries, not a read of anything on disk. When the same domain is indexed more than once, `totalSize` counts it once per entry. Summed across the 273 live coverages it read 14,000.5 GB; run the same way against the 105 orphan/unreferenced collections (Finding 5 — `dbinfo` works directly against any collection name, WCS-registered or not) it added another 3,522.8 GB. The two together are **17,523 GB**.

Two independent checks prove that figure wrong. First, `du -sh` against the server's actual TILES directory came back at **9.9 TiB** (≈10.9 TB decimal) — roughly half. Second, and more precisely, `RAS_MDDOBJECTS.PhysicalSize` — a field on every stored array object in RASBASE, computed at the object level and never summed from tile-index entries — gives **9,171.8 GB** across the 273 live coverages when read directly:

```sql
sqlite3 -readonly /opt/rasdaman/data/RASBASE "
SELECT cn.MDDCollName, cn.MDDCollId, count(*) n_objects, sum(o.PhysicalSize) physical_bytes
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 GROUP BY cn.MDDCollName, cn.MDDCollId
 ORDER BY physical_bytes DESC;"
```

9,171.8 GB matches rasdaman's own UI — "273 coverages, total volume: 9.17 TB" — to four significant figures. That match, found independently of the UI figure, is why `PhysicalSize` is now treated as authoritative and `totalSize` is not: two sources that were never compared against each other agree with each other and disagree with the number this audit had been quoting. Every GB and TB figure in this document, the spreadsheet, and `data/*.csv` was computed from this join, via `scripts/rasdaman_physical_size.py` (`--self-test` runs its query logic offline against a synthetic database matching RASBASE's schema, so a future rasdaman upgrade that changes the schema fails loudly there before it corrupts a real run).

The join, spelled out once: `RAS_MDDCOLLNAMES.MDDCollId` (the name registry) joins `RAS_MDDCOLLECTIONS.MDDCollId` (a junction table — note the name collision with `RAS_MDDCOLLNAMES`, easy to mix up) to get `MDDId`, which joins `RAS_MDDOBJECTS.MDDId` to get `PhysicalSize`. An ordinary WCS coverage's collection holds exactly one object (`n_objects == 1` for all 377 populated rows found on this server, against 378 total collection names — the one gap is a genuinely empty collection with no object at all).

### How to tell whether an orphan's size is real: the "in situ" problem

`PhysicalSize` is honest about how big an array's data is. It says nothing about whether that data lives inside rasdaman's own storage. `wcst_import` can ingest **in situ**: rather than copying source file bytes into rasdaman, it leaves each tile as a pointer into the original file, recorded in `RAS_FILETILES` (`FilePath`, `LoadDomain`, ...) instead of a blob in `RAS_TILES`. A collection ingested this way reports a real, accurate `PhysicalSize` — that data genuinely exists — but dropping the collection recovers none of rasdaman's own disk, because rasdaman never held a copy.

There is no column that says "this collection is in situ." The signal this audit used is indirect and was built up over several queries, in this order:

```sql
-- How many tiles are real stored blobs, server-wide?
sqlite3 -readonly /opt/rasdaman/data/RASBASE "SELECT count(*) FROM RAS_TILES;"
-- 7,435,790

-- How many are file references instead, server-wide?
sqlite3 -readonly /opt/rasdaman/data/RASBASE "SELECT count(*) FROM RAS_FILETILES;"
-- 166,070,985

-- Which source files do those file references point at?
sqlite3 -readonly /opt/rasdaman/data/RASBASE \
  "SELECT count(*) n, FilePath FROM RAS_FILETILES GROUP BY FilePath ORDER BY n DESC LIMIT 15;"
-- all under one crstephenson-owned ingest directory, ~3.3M rows per source file
```

That directory's total (summed across every `FilePath` row it owns) accounts for essentially all 166.1M file references on the server, and one collection's own declared tile count (163,341,150) accounts for 98.4% of that by itself — which is how Finding 5's headline orphan was identified as file-referenced with high confidence, without ever being able to decompose rasdaman's own tile index directly (see below for why not).

This is a **heuristic, not a certificate.** A cleaner test would walk each collection's own index entries and check whether each one resolves to `RAS_TILES` or `RAS_FILETILES` directly — but that index (`RAS_HIERIX.DynData`) is stored as an opaque BLOB with no documented schema, not something plain SQL can decompose. `scripts/rasdaman_physical_size.py --check-fileref TOKEN` automates the substring-match version of this check; its docstring repeats this caveat, and its output tells you to sanity-check the match count against the collection's own declared tile count before concluding anything, which is what separated the confirmed 1,268 GB orphan from its unconfirmed, much-smaller sibling in Finding 5.

### Caveats

**The cause of the tile duplication is still inferred, not proven.**  The evidence — uneven copy counts, and the pattern that iterated `_wcs` variants duplicate while their once-ingested siblings do not — points to `wcst_import` being re-run against an existing coverage. The decisive test is to ingest a throwaway coverage, measure it, re-run the same recipe without dropping it, and measure again. That test has not been run.

**Axis alignment is the subtlety that makes or breaks this audit.** DescribeCoverage reports axes in *coverage* order; `sdom` and `tileConfiguration` are in *storage* order, and pairing one's names with the other's numbers silently attaches every per-axis result to the wrong axis. Storage order is taken from each recipe's declared `gridOrder` where available and inferred by matching extents otherwise.

**Read amplification is modelled, not measured.** It is the bytes a query must read over the bytes it wants, for a representative query matched to each coverage's role, derived from the measured tile shape. It is a sound basis for ranking, not a benchmark, and it never depended on `totalSize` or `PhysicalSize` — this whole correction leaves it untouched. Twenty coverages have no modelled figure because their role could not be classified.

**Recommended tiling strings are a starting point.** They target a ~4 MB tile against the coverage's role. Verify each against the source file's real dimension order before ingesting, and re-run the audit afterwards.

**What is still open.** Whether a bloated tile index costs anything in query latency (Finding 1) is untested — this audit measured disk, not timing. Whether any orphan besides the confirmed 1,268 GB item is file-referenced is unconfirmed for the next few largest (Finding 5) — `--check-fileref` is a heuristic, not a certificate, for any of them. The eleven catalogue disagreements in Finding 4 are identified but not adjudicated — each needs a human decision about which layer is right. And the `wcst_import`-re-run cause of the duplication itself remains untested, as it has throughout this audit.

**There is also a second, smaller gap this correction found but did not explain.** Summed across the 273 live coverages, cells × bytes-per-cell (the uncompressed logical size, 9,593.8 GB) exceeds `PhysicalSize` (9,171.8 GB) by 421.9 GB — and this is separate from Finding 1's duplicate-index coverages entirely: none of the 26 flagged there show any such gap, their `PhysicalSize` matches their logical size exactly. The 421.9 GB sits in 41 *other*, clean-index coverages where `PhysicalSize` is genuinely smaller than the array's nominal cell count would suggest — `ak_hydro_segments_mhit_stats_combined` is the starkest example, 1.07 GB logical against 0.31 GB `PhysicalSize`. Whether this is rasdaman compressing null or constant-value regions, a `real_data_bytes` computation that overcounts against a sparser stored array, or something else has not been investigated — it is not a disk-recovery opportunity (dropping none of these coverages is being proposed), just an unexplained accounting gap worth flagging rather than leaving silent.
