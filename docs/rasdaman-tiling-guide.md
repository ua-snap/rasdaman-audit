# Rasdaman Tiling Guide

How a netCDF file becomes stored tiles, what those tiles cost you, and how to choose a tiling that matches how the data will actually be queried.

Read this before the [tiling audit](rasdaman-tiling-audit.md), which applies these ideas to the coverages on our server.

---

## 1. The one idea everything rests on

**A tile is the smallest unit rasdaman reads.** Ask for a single cell and the server fetches, decompresses and hands back the entire tile containing it.

That single fact drives every decision below. It means the *shape* of a tile matters more than its size, because the shape decides how much irrelevant data comes along with every answer. A tiling that is excellent for drawing a map can be catastrophic for reading a time series at one point, and nothing about the ingest will warn you.

---

## 2. The pipeline

Start with `ncdump -h`. The dimension list on your target variable(s) is the single source of truth for how the file is physically laid out:

```
float magt05m_degC(time, model, scenario, y, x) ;
```

**`gridOrder`** is each axis's 0-indexed position in that tuple, left to right, exactly as printed — no reversal, no arithmetic. Here `time=0, model=1, scenario=2, y=3, x=4`. It tells wcst_import which dimension of the source array feeds which named axis, *and* it fixes the order rasdaman stores the array in.

**`crs`** is an `@`-joined string whose expanded axis order is what OGC clients see — what `DescribeCoverage` reports. It does not need to match the file's order:

```
"crs": "OGC/0/Index1D?axis-label=\"model\"@OGC/0/Index1D?axis-label=\"scenario\"@OGC/0/AnsiDate?axis-label=\"time\"@EPSG/0/3338"
```

That groups the index axes first, then time, then the two spatial axes from `EPSG/0/3338` — a deliberate presentation choice, unrelated to storage.

**`tiling`** lists one range per axis **in `gridOrder` order**, not `crs` order:

```
"tiling": "REGULAR [<time>, <model>, <scenario>, <Y>, <X>] tile size N"
```

This is the pairing people get wrong: **the tiling bracket is positional, and its Nth entry is the Nth axis in `gridOrder`.** Since `crs` order is usually different, reading the bracket against `crs` will attach every chunk size to the wrong axis. Section 9 shows how to confirm which is which on a coverage that already exists.

> **Note that the spatial pair is its own trap:** `ncdump` commonly lists `y, x`, while `EPSG:3338` declares Easting (X) before Northing (Y). Both are correct; they describe different things. Most projected CRSs (UTM, State Plane, Albers, Web Mercator) put X first, while the EPSG registry declares geographic CRSs like `EPSG:4326` as **latitude before longitude** — the opposite of the `lon,lat` convention most GIS software uses. Check the CRS rather than assuming. For our purposes, we can basically ignore the order in the `crs`, as it has no effect on tiling.

---

## 3. What a tile costs you


### 3.1 Read amplification is the cost that matters

**Read amplification is bytes read ÷ bytes the query actually wanted.** A tile is indivisible, so rasdaman must fetch and decompress every tile your query touches, in full, and then throw away the parts you didn't ask for. If you want one cell and the tile holds a million, you read a million. Amplification of 1× means you read exactly what you asked for; 600× means you read six hundred times more than you asked for.

It depends entirely on whether the tile's shape resembles the query's shape.

Consider our permafrost coverage `crrel_gipl_outputs_nc` as an example to show a hypothetical extreme that isolates the arithmetic cleanly (see [`CRREL_GIPL_tiling.md`](CRREL_GIPL_tiling.md) for an analysis of the real, measured shape). Imagine its 100 (time) × 3 (models) × 2 (scenarios) × 1941 (Y) × 2471 (X) cells — 2,877,726,600 in total — tiled as *one single tile spanning the entire array*. Every query, whatever it asks for, would read all of it:

Type | Query | Wants | Reads | Amplification |
|---|---|---|---|---|
|Point| Full time series at one x/y, all models, all scenarios | 600 cells | 2,877,726,600 | **4,796,211×** |
|Map (WMS)| One full map frame for a single timestep, single model, single scenario | 4,796,211 cells | 2,877,726,600 | **600×** |
|Map (WCPS)|One full map frame for mean of 30 timesteps, single model, single scenario|143,886,330 cells |2,877,726,600| **20x**

Same tiling, same data, three amplification values that are orders of magnitude apart.  Computing the amplification value is useful here to gauge how well our tiling scheme matches our query behavior.

The principle that follows: **keep the axes a query reads whole inside as few tiles as possible, and aggressively chunk the other axes it only touches a narrow slice of.** 

For a point time series, that might mean the whole time axis in one tile, single model and single scenario chunks, and a small spatial footprint. For a single timestep map query, it might mean a large XY footprint and single time, model, and scenario chunks. For a WCPS query that condenses timesteps, it might mean a balance between the XY footprint size and the size of the condensed dimension (e.g., 30 years for a climatology computed on the fly via WCPS). 

### 3.2 Boundary tiles 

When we explicitly set tile sizes, the tiles may not divide evenly into the shape of the array. Using the GIPL coverage example above, if we set our Y and X tile footprint to be 20x20, that would not divide evenly into the 1941 (Y) × 2471 (X) footprint of the data. 

**So what happens to the "extra" portion of the tile that has no data values? Are they filled somehow, and does that cost us disk space if there is a large, unused portion of the tile?**

Rasdaman's own Query Language Guide suggests that for both `ALIGNED` and `REGULAR` tiling strategies:

> "This line below dictates, for a 2-D MDD, tiles to be of size 1024 x 1024, **except for border tiles (which can be smaller)**." — *Storage Layout Language*, Regular Tiling

> "The upper array limits constitute an exception: for filling the remaining gap (which usually occurs) **tiles can be smaller** and deviate from the configuration sizings." — *Storage Layout Language*, Aligned Tiling

Neither section mentions fill values or padding at all. Both describe boundary tiles shrinking to fit, not being built oversized and padded.

For example, `era5_4km_elevation` (460 × 442 cells, `ALIGNED` tiling with a 128 × 128 target), is `dbinfo`-measured at 21 tiles totalling **exactly 813,280 bytes** — precisely `460 × 442 × 4` bytes, the coverage's true, unpadded logical size, with `PhysicalSize` matching it exactly too. If boundary tiles were padded to the full 128 × 128 declared size, the tile index would sum to `21 × 65,536 = 1,376,256` bytes — 69% more than what it actually reports. This strongly suggests that there is no padding baked into this coverage's storage accounting.

**A non-dividing chunk size does not cost bytes.** 


### 3.3 Duplicate tile entries — a real index quirk, but costs no real disk

**The same tile domain, indexed more than once.** Not a tiling defect: for a few dozen coverages, rasdaman's own tile index lists the same rectangular region two, four, even sixteen times. This was proven by dumping the complete index directly. What it costs is a second question, and the answer turned out to be **nothing measurable.** Both matter, so this section covers both: how we know the duplication is real, and how we know it's harmless.

![The same tile, stored several times](figures/rasdaman-tile-duplication.svg)

#### The duplication is real

Here is one cell of `cmip6_fwi` — model 2, time 100, lat 10, lon 10 — and every tile in the collection's own index that contains it:

```
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
```

Four identical domains, listed four separate times in `dbinfo(c, "printtiles=embedded")`'s output. Three coverages have had their full domain lists saved and counted (raw dumps in `data/tile-dumps/`):

| coverage | indexed tiles | unique domains | worst repeat |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | 4,678 | 4,678 | 1× (clean) |
| `era5_4km_daily_t2_mean_wcs` | 7,642 | 6,553 | 4× |
| `cmip6_fwi` | 266,724 | 101,745 | 8× |

For `cmip6_fwi` the full copies-per-domain distribution, counted directly from the saved dump, is uneven: 27,290 domains stored once, 29,847 twice, 2,364 three times, 41,218 four times, 10 five times, 186 six times, 30 seven times, and 800 eight times (101,745 domains total, matching the unique count above) — a pattern worth remembering for the next section.

Three more were checked with the `grep`/`sort`/`uniq -c` pipeline below rather than a saved full dump — enough to confirm they carry the same kind of duplication, without a retained unique-domain count:

| coverage | indexed tiles | worst repeat seen |
|---|---|---|
| `iem_cru_2km_taspr_seasonal` | 8,276 | 3× |
| `tas_2km_projected_wcs` | 4,265,164 | 16× |
| `conus_hydro_segments_stats_combined` | 1,806,720 | 16× |

To check a coverage of your own, dump its domains and count them:

```bash
curl -u rasadmin:$PASSWORD \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from $COLLECTION as c' \
  'https://<host>/rasdaman/rasql' > tiles.json

grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate index entry. If the top line reads `1`, the coverage's index is clean.

#### Why `dbinfo`'s `totalSize` says this costs disk, and why it's wrong

`totalSize` is not read off the filesystem. It's computed by walking the tile index and summing each **indexed entry's** own byte footprint — cell count times bytes-per-cell, adjusted for boundary padding. That's a sum over index entries, not over the array's actual unique data. A domain indexed four times contributes its footprint four times, because the summation has no way to know two entries describe the same bytes. This is exactly what the dumps above prove directly: `cmip6_fwi`'s 101,745 *unique* domains sum to precisely 52.0 GB — its real, physical size, to the byte — while all 266,724 *indexed* entries, duplicates included, sum to the 175.9 GB that `totalSize` reports.

#### The independent check: RASBASE's own object catalogue

RASBASE (rasdaman's own SQLite catalogue, `/opt/rasdaman/data/RASBASE`) keeps a `PhysicalSize` field on every stored array object — computed at the object level, not by summing tile-index entries, so duplicate index entries can't inflate it. Reading it directly for the `cmip6_fwi` coverage:

```sql
sqlite3 -readonly /opt/rasdaman/data/RASBASE "
SELECT cn.MDDCollName, o.PhysicalSize
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 WHERE cn.MDDCollName = 'cmip6_fwi_2026_05_01_09_29_49_8758';"
```

returns **52,018,435,200** — 52.0 GB, matching the unique-domain figure exactly, not the 175.9 GB `totalSize` reports. We checked this for every one of the 26 flagged coverages, not just this one: `PhysicalSize` matches the unique-domain figure every single time, with zero exceptions. See the [audit doc's Method section](rasdaman-tiling-audit.md#method-and-what-these-numbers-do-not-prove) for the full table and `scripts/rasdaman_physical_size.py` to run this yourself against any coverage.

#### A second, accidental experiment — same conclusion from a different angle

Someone once ingested `era5_4km_daily_t2_mean` three times at three different tile sizes and left the results on the server. All three hold the identical array:

| collection | tiles | on disk |
|---|---|---|
| `bigger_tile_era5_4km_daily_t2_mean` | 289 | 19.011 GB |
| `big_tile_era5_4km_daily_t2_mean` | 1,172 | 19.011 GB |
| `era5_4km_daily_t2_mean` (live) | 4,678 | 19.011 GB |

A sixteen-fold difference in tile count, and the byte totals — this time `PhysicalSize`, not `totalSize` — are identical to three decimal places. **Tile count and tile size do not affect how much disk a coverage occupies, and neither does a duplicated index entry.** 

:star: This means that we can choose tiling for query performance and ignore storage entirely when making that decision.

#### Why the duplication happens anyway

The uneven copies-per-domain distribution is the clue, even though it no longer points at a disk cost. A region touched by one write has one index entry; a region touched by four writes has four. The working explanation — still **not independently confirmed** against `wcst_import`'s own source, which isn't reachable from here — is that it was re-run against a coverage that already existed, each pass adding another index entry for the tiles it touched instead of replacing the existing one. It fits the pattern across the server: coverages ingested once sit at a clean 1×; the `_wcs` variants, tuned and re-run during development, carry the duplicates.

What's new is *where* the extra writes land, and it's the same place in every coverage checked with a full domain dump so far — two for two: `era5_4km_elevation` and `crrel_gipl_outputs_nc` both show something anomalous specifically at the *first index along the coverage's leading (`gridOrder`-0) axis* — a lone extra tile-row (era5), and an offset partition plus 9 of its own 47 tiles double-indexed (crrel, at `time=0`). That's a more specific target than "re-run at some point" — it points at whatever `wcst_import` (or rasdaman's `ALIGNED` tiling itself) does differently for the very first slice along the primary axis, plausibly some kind of bootstrap or initialization write that's structurally separate from the bulk import and can end up re-touched independently of it. Still a theory, not a confirmed mechanism.

#### What this means for tiling decisions, and for the index

**Nothing changes about how you tile.** Tile shape, `0:*` versus explicit bounds, `REGULAR` versus `ALIGNED` — none of it causes or prevents this, and none of it costs disk either way (Section 3.2 already established that for padding; this section extends it to duplicate entries). Do not re-tile to fix this, and do not let a high `totalSize`/`PhysicalSize` ratio push you into changing a tiling that serves your queries well.

What we have **not** checked is whether a bloated index has any cost of its own — a spatial (R+-tree) index with sixteen entries for one region might, in principle, do sixteen times the lookup work for a query that touches it, even though the underlying blob is fetched only once. This project measured disk, not query latency, on the duplicated coverages, so treat that as an open question, not a settled one.

The practical rule stands regardless of the disk finding: **avoid re-running `wcst_import` against a coverage that already exists.** Delete the coverage first, or ingest under a new name and swap. It costs nothing to follow and keeps the index (and this kind of investigation) simpler the next time someone looks.

On our server, 247 of 273 coverages have a clean, 1:1 tile index (index inflation does not exceed 1.05×). Twenty-six carry duplicate entries. None of the 26 cost extra disk once you read `PhysicalSize` instead of `totalSize` — the worst of them, `tas_2km_projected_wcs`, reports 2,321.9 GB via `totalSize` and 313.3 GB via `PhysicalSize`, and 313.3 GB is the real number.


---

## 4. Sizing a tile

Rasdaman's guidance is that **1–4 MB per tile is optimal in most cases**.


```
tile bytes = (product of the tile's per-axis extents) × (sum of every band's byte width)
```

**Every band counts!** Rasdaman's Storage Layout Language paper makes this unambiguous with its own worked example: a 3-band RGB image tiled 512 × 512 declares `tile size 786432`, and 512 × 512 × 3 × 1 byte = 786,432 exactly.

For a 10-band `float32` coverage that is **40 bytes per cell**, not 4. Planning against one band's width under-counts the real footprint by the band count — a tenfold error that no validation catches, because nothing was technically violated.

---

## 5. `REGULAR` vs `ALIGNED`

**Both shrink boundary tiles; neither pads.**  Neither scheme affects how much disk the coverage uses, for the same reason: nothing about the choice between them changes the array's logical size, and boundary tiles in both cases end up sized to what's actually there.

So if not boundary padding, what *does* distinguish them? Two things, and rasdaman's own docs are explicit about both:

**Fixed vs Flexible:** `REGULAR [ranges] tile size N` declares an exact tile shape up front — you commit to a chunk size for every axis before ingest. `ALIGNED [0:*, ...] tile size N` lets you leave some axes as `*` — a *preferred direction of access* — and have rasdaman size them to hit the byte target once the axes you *do* pin are fixed. It's the only option when an axis's real chunk size isn't decided yet, or when the axes involved don't factor near your budget.

**Using `"irregular": true` on an axis does not rule out `REGULAR` tiling.** That flag lives in the recipe's `axes` block and governs how petascope declares the axis's real-world CRS coordinate values — a plain min/max/resolution sequence versus an explicit `directPositions` list, needed when dimensions are categorical lookups (like `model` and `scenario`) or when `time` is given as actual dates rather than a fixed step. The tiling bracket operates entirely in a different space — integer grid-index counts — and nothing in rasdaman's Storage Layout Language documentation ties the two together.

**The two "regular/irregular" vocabularies come from two different guides, and they aren't describing the same thing.** The Query Language Guide's Storage Layout Language section defines tiling's regular/irregular split by tile *geometry* — nothing about axes or coordinates:

> "A tiling is aligned if tiles are defined through axis-parallel hyperplanes cutting all through the domain. Aligned tiling is further classified into **regular** and **aligned irregular** depending on whether the parallel hyperplanes are equidistant (except possibly for border tiles) or not." — *Query Language Guide*, Storage Layout Language, §4.20.1

The Geo Services Guide's ingest-recipe `axes` block defines a completely separate regular/irregular split, by axis *coordinate spacing*:

> "`resolution` — The resolution of the axis from the input file; if this axis is irregular, the resolution is set to 1 ... `irregular` — Set to true to specify that this axis is irregular, e.g. a time axis with irregular datetime indexes; if not specified, it is set to false by default." — *Geo Services Guide*, §5.9.12 (`general_coverage` recipe)

Neither section cross-references the other, and two of Rasdaman's own worked examples confirm neither constrains the other in practice: a GRIB import declares `"tiling": "REGULAR [0:0, 0:20, 0:1023, 0:1023]"` with no irregular axes at all, while a separate netCDF import with an explicit `"irregular": true` time axis declares `"tiling": "ALIGNED [0:13, 0:999, 0:999] TILE SIZE 4000000"`. Neither choice was forced by the other.

More examples, from SNAP's own production recipes:
- `REGULAR` + `irregular:true` + `directPositions`, together: 5 of the `rasdaman-ingest` repo's 6 `REGULAR`-tiled recipes do exactly this. `ardac/hydroviz/arctic/stats_mhit.json` declares `"tiling": "REGULAR [0:6, 0:2, 0:0, 0:1] tile size 1048576"` at the top level and, in the same file, its `stream_id` axis has `"irregular": true` and `"directPositions": "${netcdf:variable:stream_id}"`. It's live in production right now.
- `ALIGNED` with zero irregular axes: `era5_4km_elevation`'s  recipe uses `"tiling": "ALIGNED [0:127, 0:127] tile size 65536"` with both its `X` and `Y` axes as plain min/max/resolution. No irregular key anywhere in the file.

Checking these working ingests directly, it appears that `ALIGNED` doesn't require irregular, and `REGULAR` doesn't forbid it.

**What `irregular`, `resolution`, and `directPositions` actually do together, on one axis.** A regular axis's full coefficient list is *computed*: `min`, `min + resolution`, `min + 2·resolution`, ... up to `max`. An irregular axis has no such formula, so `resolution` is accepted but meaningless there (forced to `1` internally, per the quote above) and something else has to supply the real values. Which mechanism you need depends on how the source data arrives:
- **The axis's whole coefficient list already exists inside one input file** (e.g. a netCDF file with an internal irregular time dimension) — supply `directPositions`, computed from that file's own values, as in the netCDF example quoted above.
- **Each input file contributes exactly one coefficient** (e.g. a date parsed out of each filename, with `"data_bound": false`) — no `directPositions` needed at all; `wcst_import` appends one coefficient per file as it imports, incrementally.

`areas_of_validity` / `validity` are further, irregular-axis-only adjuncts on top of this (mutually exclusive with each other): by default an irregular coefficient is a single point, so slicing must hit it exactly; these settings extend each coefficient into a `[start, end]` interval instead.

**When to use each?** Rasdaman's own tiling guidelines single out `REGULAR` for this case: *not* knowing the query shape, or a client that always requests same-size regions —

> "Nothing is known about access patterns: choose regular tiling with a maximum tile size... map viewing clients typically send several requests of fixed extent per mouse click to maintain a cache of tiles in the browser for faster panning. So the extent of the tile is known — or at least that tiles are quadratic."

— which describes a map/WMS-style client, not a point query. `ALIGNED` is what the docs reach for specifically to describe a point/time-series pattern:

> "...either a time slice is read... or a time series is extracted for one particular position (x, y)... An axis which never participates in any subsetting box is called a preferred direction of access."

A point time series is precisely "never subsets the time axis" — the textbook case for `ALIGNED`'s wildcard, not for `REGULAR`. Section 4's WCS point scheme in `CRREL_GIPL_tiling.md` uses `ALIGNED` for this reason — it's the tool the documentation itself recommends for that access pattern — and also because neither spatial axis factors close to the byte budget it needs.

One caveat, lower confidence than the rest of this section: an older, unversioned rasdaman wiki page describes a *regular computed index* (`rc_index`) — available only under `REGULAR` tiling — that locates a tile by direct arithmetic on its fixed size and position rather than an R+-tree lookup, which would be a genuine index-speed argument for `REGULAR` wherever its restrictions don't rule it out. This didn't turn up in the current Query Language Guide, so treat it as plausible, not confirmed. `dbinfo`'s own JSON output carries the field that would settle it — `"index": {"type": ...}` — and both saved dumps this audit has for `ALIGNED` coverages report `"rpt_index"` (R+-tree), for `cmip6_fwi` and `era5_4km_daily_t2_mean` (`data/tile-dumps/`). Neither is `REGULAR`-tiled, so this doesn't confirm or rule out `rc_index` either way — dump a `REGULAR`-tiled coverage's `dbinfo` output the same way to check.

A wildcard is a real delegation of control. `ALIGNED [0:*, 0:31, 0:31] tile size 16777216` pins the spatial chunks at 32 and lets rasdaman choose the first axis. On the `cmip6_downscaled` v2 family's ~18,250- and ~31,390-step time axes, the real, tile-dump-confirmed result isn't one uniform chunk: most tiles span nearly the entire time axis at a 4–7-cell spatial footprint, and a smaller remainder covers just the last day at the full 32 × 32 footprint. That is a decision you did not make, and it may not suit your query pattern — or match what the byte-budget math alone would predict.

---

## 6. Choosing a tiling

Start from the question(s) the coverage exists to answer. Choose for queries and ignore disk.

**For point and small-AOI reads** — the dominant pattern for our data — keep every non-spatial axis whole inside a tile and make the spatial footprint as small as a sane tile size allows. One tile then answers a whole time series. If using `ALIGNED`, make the time axis the wildcard and set the max tile size to ~4MB.

**For map rendering (WMS)** — do the opposite: pin the non-spatial axes to one index each and make the spatial footprint large. If using `ALIGNED`, explicitly set the X and Y bounds. Do the math to solve the budget for XY tiling (X x Y = 4MB / [bands x all non-spatial axes x bytes]). *Don't make the X and Y axes the wildcards and set the max tile size to ~4MB! You will end up with long skinny map tiles, not square-ish XY blocks!*

**Identify the axis that is large relative to the others** and chunk that one. A stream dataset with 56,460 site IDs and a handful of small categorical axes wants `stream_id` chunked to 1 and everything else at full extent: one tile per stream, one tile per query.

---

## 7. Worked example

**Gridded permafrost dataset — `crrel_gipl_outputs_nc`.** This one gets a full document of its own: [`CRREL_GIPL_tiling.md`](CRREL_GIPL_tiling.md). It starts from the source file's `ncdump`, works out the true storage order the hard way (this coverage is one of Finding 4's catalogue disagreements — `DescribeCoverage` reports the wrong axis order, so the recipe's `gridOrder` has to settle it), and designs two separate schemes — a point/time-series tiling and a map-rendering tiling — both held to a strict 4 MB target, ready to be ingested as test coverages and timed against the original. Worth reading in full for how it picks `ALIGNED` on both spatial axes not because `REGULAR` was disqualified (Section 5's correction: a recipe's `"irregular": true` on an axis doesn't rule out `REGULAR` tiling, that flag is about CRS coordinate declaration, not storage layout) but because neither 1941 nor 2471 factors anywhere near the chunk sizes a 4 MB budget needs.

The general lesson that document's section 4 draws out is worth stating here too: a chunk size that divides one axis exactly is not guaranteed to divide the *other* axis it might get paired with if the two spatial axes are ever swapped — `1941 = 3 × 647` and `2471 = 7 × 353` share no common factor, so a transposition between them doesn't degrade gracefully, it breaks divisibility on both sides at once. **A per-axis divisibility check, not a total-byte check, is what catches a transposition** — the byte arithmetic comes out identical either way, because tile volume doesn't care which axis contributed which factor. And whatever it costs, it isn't disk: more tiles and padded edges mean more bytes fetched per read (a latency cost, paid forever), not more bytes stored (Section 3.2) — a transposition is a performance bug, not a capacity one.

---

## 8. When the math is wrong

Rasdaman has hard-failure errors for structural tiling problems — error 219 (tile size smaller than the base type), 220 (tiling strategy incompatible with the marray), 224 (tile configuration incompatible with the marray domain). None of them fire for the most likely mistake, which is sizing a tile against one band instead of all of them.

What happens instead depends on the bracket:

- **With wildcards**, rasdaman solves for a shape that hits your declared byte target using the *true* multi-band cell width. If you picked the target using one band, the tiles it builds land at roughly (band count) × the size you intended — silently, because nothing was violated.
- **With an explicit bracket**, the bracket determines the shape; the `tile size` number is descriptive. An inaccurate number means your estimate was wrong, not that rasdaman built something different.

Either way there is no error naming the cause. It surfaces as an ingest that is slow and memory-hungry — tiles that overflow the configured tile cache (`--cachelimit`) spill to disk, and when tiles are several times larger than planned, that is consistent with exhausting RAM rather than failing cleanly.

---

## 9. Verifying what was actually built

Planning is Sections 1–8. This is how to check what exists.

```bash
curl -u rasadmin:$PASSWORD \
  -d 'query=select dbinfo(c) from $COVERAGE_ID as c' \
  'https://<host>/rasdaman/rasql'
```

The fields that matter: `baseType` (count and type of every band, so you can read the true per-cell width), `tileNo` (tiles indexed — including duplicate entries, see Section 3.3), `totalSize` (**not** bytes on disk — it's cells × bytes-per-cell summed over every *indexed tile entry*, so a duplicated entry gets counted again; see Section 3.3 and the check below), and `tiling.tileConfiguration` (the tile shape). For the real on-disk figure, read `RAS_MDDOBJECTS.PhysicalSize` from RASBASE directly — Section 3.3 and the [audit doc's Method section](rasdaman-tiling-audit.md#method-and-what-these-numbers-do-not-prove) have the query.

Adding `dbinfo(c,"printtiles=embedded")` also lists every individual tile domain. Be careful with it: on a coverage with millions of tiles the response is large enough to arrive truncated. Consider piping the output to a text file.

**Five checks worth running.**

*Did it build the grid I asked for?* Compute the tile count the geometry requires — product of `extent ÷ chunk` across all axes — and compare against `tileNo`. Equal means the tiling behaved. Lower means sparse materialization, which is fine. Higher does **not** by itself mean anything is wrong: a wildcard bracket makes the geometric count meaningless, and `era5_4km_daily_t2_mean` scores 2,339× by this test while storing at exactly 1.00×.

*Does my index have duplicate entries?* Divide `totalSize` by (cells × bytes-per-cell). Above about 1.05 means the tile index has duplicate entries for this coverage (Section 3.3) — a sign `wcst_import` was re-run against it without dropping it first. It is **not** a disk check: it does not cost extra bytes on disk (confirm the real figure with `RAS_MDDOBJECTS.PhysicalSize`), but it's still worth knowing, both as a process-hygiene signal and because what a bloated index costs query latency is untested.

*Is there padding?* Check each axis separately: `extent % chunk == 0`. The byte arithmetic will look right even when two chunk sizes are transposed, so only the per-axis check catches that.

*Which axis is in which bracket position?* Read `tiling.tileDomains` and match the distinct values at each position against each axis's known cardinality — a position cycling through 2 or 3 values is a small categorical axis. This is more reliable than trusting how the recipe was written.

*Is this even the right array?* A successful `dbinfo` is not proof. `wcst_import` does not always name the collection after the coverage, so a collection can sit at a coverage's name while holding something else entirely — we found twelve such cases, including two where `dbinfo` returned a single 4-byte cell while WCS served real data from elsewhere. Cross-check the domain against the WCPS probe below, and get the authoritative name from petascope rather than guessing:

```sql
SELECT c.coverage_id, r.collection_name
  FROM coverage c
  JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id;
```

### Two gotchas in the output

**`tileConfiguration` can contain `*`.** An unbounded axis is reported as `0:*`, not a number. Parsing only numeric ranges silently drops that position and misaligns everything after it.

**`sdom` and `tileConfiguration` are in storage order; `DescribeCoverage` is in catalogue order.** They frequently differ. Pairing one's axis names with the other's numbers attaches every per-axis result to the wrong axis — the same trap as Section 2, now with real numbers attached.

### Reading a coverage without credentials

A deliberate WCPS type error makes rasdaman describe its own array, needing neither rasql credentials nor the collection name:

```
for $c in (COVERAGE_ID) return encode($c * "x", "csv")
```

POST that to `/rasdaman/ows` as `request=ProcessCoverages`. The exception text comes back carrying the real stored domain, cell type and null value:

```
ARRAY (float) [D0(0:18249),D1(0:442),D2(0:459)] null values [-9999.000000]
```

This works on coverages rasql cannot reach at all, and it is the most reliable answer to "what is actually stored," because it comes from the array itself.

---

## 10. Before a full ingest

1. `wcst_import.sh --recipe general_coverage --analyze` for a dry run that doesn't touch the database.
2. Estimate the size: `(product of all axis extents) × (bands) × (bytes per value)`. A source netCDF much smaller than this is normal — HDF5 compresses internally. Rasdaman compresses too, so `RAS_MDDOBJECTS.PhysicalSize` (Section 3.3) usually lands below this estimate once ingest finishes. If `dbinfo`'s `totalSize` reads well above it, don't panic — check `PhysicalSize` before concluding anything, since `totalSize` inflates on its own if the coverage already existed when the recipe ran (Section 3.3). `PhysicalSize` landing above the estimate would be the real warning sign; we have not seen that happen on this server.
3. Test the tiling on a small subset before committing to a multi-hundred-GB run. A bad tile choice is expensive to discover halfway through.
4. After ingest, run the five checks in Section 9, and spot-check one known coordinate against the source file to confirm axis order.

---

## Sources

- [Tiling — rasdaman wiki](http://rasdaman.org/trac/wiki/Tiling)
- [Storage Layout Language (PDF)](http://rasdaman.org/trac/attachment/wiki/FAQ/sstdm2010.pdf), via the [rasdaman FAQ](https://rasdaman.org/trac/wiki/FAQ)
- [Performance — rasdaman wiki](https://rasdaman.org/trac/wiki/Performance)
- [rasdaman error text definitions](https://github.com/hholzgra/rasdaman/blob/master/bin/errtxts)
- [Geo Services Guide — rasdaman 9.7.0](https://doc.rasdaman.org/9.7/05_geo-services-guide.html)
