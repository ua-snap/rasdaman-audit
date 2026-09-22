# Rasdaman Tiling Guide

How a netCDF file becomes stored tiles, what those tiles cost you, and how to
choose a tiling that matches how the data will actually be queried.

Read this before the [tiling audit](rasdaman-tiling-audit.md), which applies
these ideas to the coverages on our server.

---

## 1. The one idea everything rests on

**A tile is the smallest unit rasdaman reads.** Ask for a single cell and the
server fetches, decompresses and hands back the entire tile containing it.

That single fact drives every decision below. It means the *shape* of a tile
matters more than its size, because the shape decides how much irrelevant data
comes along with every answer. A tiling that is excellent for drawing a map can
be catastrophic for reading a time series at one point, and nothing about the
ingest will warn you.

---

## 2. The pipeline

Three things travel from the source file into storage, and they are easy to
confuse because they describe the same axes in different orders.

![From file to stored tiles](figures/rasdaman-tiling-pipeline.svg)

Start with `ncdump -h`. The dimension list on your target variable is the
single source of truth for how the file is physically laid out:

```
float magt05m_degC(time, model, scenario, y, x) ;
```

**`gridOrder`** is each axis's 0-indexed position in that tuple, left to right,
exactly as printed — no reversal, no arithmetic. Here `time=0, model=1,
scenario=2, y=3, x=4`. It tells wcst_import which dimension of the source array
feeds which named axis, *and* it fixes the order rasdaman stores the array in.

**`crs`** is an `@`-joined string whose expanded axis order is what OGC clients
see — what `DescribeCoverage` reports. It does not need to match the file's
order:

```
"crs": "OGC/0/Index1D?axis-label=\"model\"@OGC/0/Index1D?axis-label=\"scenario\"@OGC/0/AnsiDate?axis-label=\"time\"@EPSG/0/3338"
```

That groups the index axes first, then time, then the two spatial axes from
`EPSG/0/3338` — a deliberate presentation choice, unrelated to storage.

**`tiling`** lists one range per axis **in `gridOrder` order**, not `crs` order:

```
"tiling": "REGULAR [<time>, <model>, <scenario>, <Y>, <X>] tile size N"
```

This is the pairing people get wrong, and it is worth stating bluntly: **the
tiling bracket is positional, and its Nth entry is the Nth axis in `gridOrder`.**
Since `crs` order is usually different, reading the bracket against `crs` will
attach every chunk size to the wrong axis. Section 9 shows how to confirm which
is which on a coverage that already exists.

> **The spatial pair is its own trap.** `ncdump` commonly lists `y, x`, while
> `EPSG:3338` declares Easting (X) before Northing (Y). Both are correct; they
> describe different things. Most projected CRSs (UTM, State Plane, Albers, Web
> Mercator) put X first, while the EPSG registry declares geographic CRSs like
> `EPSG:4326` as **latitude before longitude** — the opposite of the `lon,lat`
> convention most GIS software uses. Check the CRS rather than assuming.

---

## 3. What a tile costs you

Three distinct costs, which get confused constantly. Only the first two are
about tile shape at all — the first always bites, the second is mostly harmless,
and the third, which is where the real money goes, turns out to have nothing to
do with how you tile.

### 3.1 Read amplification — the cost that always bites

**Read amplification is bytes read ÷ bytes the query actually wanted.** A tile
is indivisible, so rasdaman must fetch and decompress every tile your query
touches, in full, and then throw away the parts you didn't ask for. If you want
one cell and the tile holds a million, you read a million. Amplification of 1×
means you read exactly what you asked for; 600× means you read six hundred times
more than you kept.

It depends entirely on whether the tile's shape resembles the query's shape.

![The same array, tiled two ways](figures/rasdaman-tile-shape.svg)

Our permafrost coverage `crrel_gipl_outputs_nc` makes the point — not as it is
actually tiled today (that's an unremarkable auto-`ALIGNED` 4 MB split; see
[`CRREL_GIPL_tiling.md`](CRREL_GIPL_tiling.md) for the real, measured shape),
but as a hypothetical extreme that isolates the arithmetic cleanly: imagine
its 100 × 3 × 2 × 1941 × 2471 cells — 2,877,726,600 in total — tiled as *one
single tile spanning the entire array*. Every query, whatever it asks for,
would read all of it:

| Query | Wants | Reads | Amplification |
|---|---|---|---|
| Full time series at one x/y | 600 cells | 2,877,726,600 | **4,796,211×** |
| One full map frame | 4,796,211 cells | 2,877,726,600 | **600×** |

The second row is the one that trips people up. A map frame reads "one tile,"
which sounds efficient — but one tile here *is* the whole array, and a frame is
only 1/600th of it. You wanted one map; you read all six hundred maps to get it.
It would be 1× only if the tile held exactly one frame and nothing else.

Note the symmetry: each row's "wants" is the other row's amplification factor.
That is not a coincidence — it falls out of the two queries slicing the same
array along complementary axes.

Same tiling, same data, two answers three orders of magnitude apart. The tiling
is not badly built in the abstract; it is built for the wrong question. Most of
our coverages serve point and small-AOI lookups, so the first row is the one
that counts.

The rule that follows: **keep the axes a query sweeps inside one tile, and chunk
the axes it pins.** For a point time series, that means the whole time axis in
one tile and a small spatial footprint.

### 3.2 Boundary tiles — corrected 22 September 2026

**This section previously said the wrong thing about what happens at the
edges, and this correction was found the same way Finding 1 was: by checking
an assumption against rasdaman's own documentation and against this server's
actual measured numbers, rather than trusting it because it sounded
plausible.** The old version claimed a tile grid that doesn't divide the
array evenly gets built at full declared size anyway, with the overhang
zero-filled, and that this padding "compresses away" on disk. Rasdaman's own
Query Language Guide says the opposite, for both tiling strategies:

> "This line below dictates, for a 2-D MDD, tiles to be of size 1024 x 1024,
> **except for border tiles (which can be smaller)**." — *Storage Layout
> Language*, Regular Tiling

> "The upper array limits constitute an exception: for filling the remaining
> gap (which usually occurs) **tiles can be smaller** and deviate from the
> configuration sizings." — *Storage Layout Language*, Aligned Tiling

Neither section mentions zero-fill or padding at all. Both describe boundary
tiles shrinking to fit, not being built oversized and padded.

This server's own data backs the documentation, not the old claim. The
guide's own flagship example, `era5_4km_elevation` (460 × 442 cells, `ALIGNED`
tiling with a 128 × 128 target), is `dbinfo`-measured at 21 tiles totalling
**exactly 813,280 bytes** — precisely `460 × 442 × 4` bytes, the coverage's
true, unpadded logical size, with `PhysicalSize` matching it exactly too. If
boundary tiles were padded to the full 128 × 128 declared size, the tile
index would sum to `21 × 65,536 = 1,376,256` bytes — 69% more than what it
actually reports. It doesn't. There is no padding baked into this coverage's
storage accounting to begin with, which is a stronger claim than "padding
costs nothing on disk once compressed" — there was no padding to compress.

**What a non-dividing chunk size genuinely costs, then, is not wasted bytes —
it's more, smaller tiles, and — newly confirmed below — a grid that isn't
even uniformly shaped.** `era5_4km_elevation`'s 21 measured tiles against a
naive `⌈460÷128⌉ × ⌈442÷128⌉ = 16` was an open question as of the last
correction; it's now resolved, with a full tile-domain dump
(`data/tile-dumps/era5_4km_elevation.json.gz`, obtained via
`dbinfo(c,"printtiles=embedded")`) rather than inference from `totalSize`.
The real grid is not "16 tiles, two of them shrunk." It's this:

- **16 ordinary tiles** — a clean 4×4 grid, `128 × 128` everywhere except the
  two edges, which shrink exactly as Section 3.2's opening quotes describe
  (`X: 128,128,128,58`; `Y: 128,128,128,75`).
- **5 more tiles from a single extra row.** `Y = 0` — one row, one cell
  thick — was split off as its own tile-row, and *that* row's `X` axis was
  chunked on completely different boundaries than every other row uses: `0:0,
  1:128, 129:256, 257:384, 385:441` (a lone single-cell tile, then four
  128-wide blocks *offset by one* from the main grid's `0:127, 128:255,
  256:383, 384:441`) instead of joining the ordinary grid's first row.

16 + 5 = 21, matching `dbinfo` exactly, and the 21 real domains' cell counts
sum to precisely 203,320 — `460 × 442`, the array's true size, with zero
overlap and zero gap. So both things are true at once: there is still no
padding anywhere (confirming the conclusion above), and the grid genuinely
isn't uniform — `ALIGNED` tiling can carve off a boundary row (or, as the
worked example below shows, an even smaller corner) onto a different,
offset partition rather than just shrinking it in place. What's confirmed as
the practical outcome is unchanged from before: more index entries per read,
more fetch-and-decompress round trips, and a boundary tile that's smaller
than the declared max is still indivisible — a query touching it still pays
for the whole thing. The fix is the same as before for a different reason:
pick chunk sizes that divide the axis where you can, and don't expect
`REGULAR` to save you from this by itself — see Section 5 for what actually
distinguishes it from `ALIGNED`.

The **19 of 22 / `design_freezing_index` at 0.56×** figures from the old
version of this section have not been re-verified against the corrected
mechanism above and are removed rather than carried forward unverified — they
were computed by hand before this correction, not from a script, and
re-deriving them properly means re-checking `real_data_bytes` against
`PhysicalSize` per coverage the way `crrel_gipl_outputs_nc`'s 421.9 GB gap was
checked in the audit's Method section, not against `totalSize`, which this
whole correction has already shown is not a reliable disk figure.

![The real, measured tile grid: 21 tiles, no padding, not uniform](figures/rasdaman-tile-padding.svg)

Redrawn 22 September 2026 directly from the dump above — every rectangle in
the figure is a real tile domain, not a schematic. The 16 ordinary tiles are
blue; the 5 tiles that make up the offset first row are amber.

A near-identical pattern shows up independently in `crrel_gipl_outputs_nc`'s
own dump — see below, and `CRREL_GIPL_tiling.md` section 7, which is where
that investigation and this one converged.

### 3.3 Duplicate tile entries — a real index quirk, costing no real disk

**The same tile domain, indexed more than once.** Not a tiling defect: for a
few dozen coverages, rasdaman's own tile index lists the same rectangular
region two, four, even sixteen times. That part is real, proven by dumping
the index directly. What it costs is a second question, and the answer —
found only after chasing three wrong theories and one wrong unit — turned out
to be **nothing measurable.** Both matter, so this section covers both: how we
know the duplication is real, and how we know it's harmless.

![The same tile, stored several times](figures/rasdaman-tile-duplication.svg)

#### The duplication is real

Here is one cell of `cmip6_fwi` — model 2, time 100, lat 10, lon 10 — and every
tile in the collection's own index that contains it:

```
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
[1:3,0:25679,10:11,10:11]
```

Four identical domains, listed four separate times in `dbinfo(c,
"printtiles=embedded")`'s output. Three coverages have had their **full**
domain lists saved and counted (raw dumps in `data/tile-dumps/`):

| coverage | indexed tiles | unique domains | worst repeat |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | 4,678 | 4,678 | 1× (clean) |
| `era5_4km_daily_t2_mean_wcs` | 7,642 | 6,553 | 4× |
| `cmip6_fwi` | 266,724 | 101,745 | 8× |

For `cmip6_fwi` the full copies-per-domain distribution, counted directly from
the saved dump, is uneven: 27,290 domains stored once, 29,847 twice, 2,364
three times, 41,218 four times, 10 five times, 186 six times, 30 seven times,
and 800 eight times (101,745 domains total, matching the unique count above)
— a pattern worth remembering for the next section.

Three more were checked with the `grep`/`sort`/`uniq -c` pipeline below rather
than a saved full dump — enough to confirm they carry the same kind of
duplication, without a retained unique-domain count:

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

Any count above 1 is a duplicate index entry. If the top line reads `1`, the
coverage's index is clean.

#### Why `dbinfo`'s `totalSize` says this costs disk, and why it's wrong

`totalSize` is not read off the filesystem. It's computed by walking the tile
index and summing each **indexed entry's** own byte footprint — cell count
times bytes-per-cell, adjusted for boundary padding. That's a sum over index
entries, not over the array's actual unique data. A domain indexed four times
contributes its footprint four times, because the summation has no way to
know two entries describe the same bytes. This is exactly what the dumps
above prove directly: `cmip6_fwi`'s 101,745 *unique* domains sum to precisely
52.0 GB — its real, physical size, to the byte — while all 266,724 *indexed*
entries, duplicates included, sum to the 175.9 GB that `totalSize` reports.

That means every "reclaimable GB" figure this project computed from
`totalSize` earlier — 4,591 GB of it, across 26 coverages — was real as an
index-inflation number and fictional as a disk number. We only found this out
by checking against a second, independent source that doesn't route through
the tile index at all.

#### The independent check: RASBASE's own object catalogue

RASBASE (rasdaman's own SQLite catalogue, `/opt/rasdaman/data/RASBASE`) keeps
a `PhysicalSize` field on every stored array object — computed at the object
level, not by summing tile-index entries, so duplicate index entries can't
inflate it. Reading it directly:

```sql
sqlite3 -readonly /opt/rasdaman/data/RASBASE "
SELECT cn.MDDCollName, o.PhysicalSize
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 WHERE cn.MDDCollName = 'cmip6_fwi_2026_05_01_09_29_49_8758';"
```

returns **52,018,435,200** — 52.0 GB, matching the unique-domain figure
exactly, not the 175.9 GB `totalSize` reports. We checked this for every one
of the 26 flagged coverages, not just this one: `PhysicalSize` matches the
unique-domain figure every single time, with zero exceptions. See the [audit
doc's Method section](rasdaman-tiling-audit.md#method-and-what-these-numbers-do-not-prove)
for the full table and `scripts/rasdaman_physical_size.py` to run this
yourself against any coverage.

#### A second, accidental experiment — same conclusion from a different angle

Someone once ingested `era5_4km_daily_t2_mean` three times at three different
tile sizes and left the results on the server. All three hold the identical
array:

| collection | tiles | on disk |
|---|---|---|
| `bigger_tile_era5_4km_daily_t2_mean` | 289 | 19.011 GB |
| `big_tile_era5_4km_daily_t2_mean` | 1,172 | 19.011 GB |
| `era5_4km_daily_t2_mean` (live) | 4,678 | 19.011 GB |

A sixteen-fold difference in tile count, and the byte totals — this time
`PhysicalSize`, not `totalSize` — are identical to three decimal places.
**Tile count and tile size do not affect how much disk a coverage occupies,
and neither does a duplicated index entry.** Choose your tiling for query
performance and ignore storage entirely when making that decision.

#### Why the duplication happens anyway — sharpened 22 September 2026

The uneven copies-per-domain distribution is the clue, even though it no
longer points at a disk cost. A region touched by one write has one index
entry; a region touched by four writes has four. The working explanation —
still **not independently confirmed** against `wcst_import`'s own source,
which isn't reachable from here — is that it was re-run against a coverage
that already existed, each pass adding another index entry for the tiles it
touched instead of replacing the existing one. It fits the pattern across the
server: coverages ingested once sit at a clean 1×; the `_wcs` variants, tuned
and re-run during development, carry the duplicates.

What's new is *where* the extra writes land, and it's the same place in
every coverage checked with a full domain dump so far — four for four:
`era5_4km_elevation`, `crrel_gipl_outputs_nc`, and (audit doc, Finding 1)
`cmip6_downscaled_tasmax_MIROC6_ssp245_v2_wcs` and
`cmip6_downscaled_pr_CESM2_historical_v2_wcs` all show something anomalous
specifically at the *first index along the coverage's leading (`gridOrder`-0)
axis* — a lone extra tile-row (era5), an offset partition plus 9 of its own
47 tiles double-indexed (crrel, at `time=0`), and now 22.6% and 12.8% of the
tiles covering `time=0` duplicated versus under 1% everywhere else, in two
completely unrelated CMIP6 coverages with a `time=0` slice tiled on a
different spatial grid than the rest of the array entirely. That's a much
more specific target than "re-run at some point" — it points at whatever
`wcst_import` (or rasdaman's `ALIGNED` tiling itself) does differently for
the very first slice along the primary axis, plausibly some kind of
bootstrap or initialization write that's structurally separate from the bulk
import and can end up re-touched independently of it. Still a theory, not a
confirmed mechanism, but now a specific and testable one rather than a
generic "something got re-run somewhere."

#### What this means for tiling decisions, and for the index

**Nothing changes about how you tile.** Tile shape, `0:*` versus explicit
bounds, `REGULAR` versus `ALIGNED` — none of it causes or prevents this, and
none of it costs disk either way (Section 3.2 already established that for
padding; this section extends it to duplicate entries). Do not re-tile to fix
this, and do not let a high `totalSize`/`PhysicalSize` ratio push you into
changing a tiling that serves your queries well.

What we have **not** checked is whether a bloated index has any cost of its
own — a spatial (R+-tree) index with sixteen entries for one region might, in
principle, do sixteen times the lookup work for a query that touches it, even
though the underlying blob is fetched only once. This project measured disk,
not query latency, on the duplicated coverages, so treat that as an open
question, not a settled one.

The practical rule stands regardless of the disk finding: **avoid re-running
`wcst_import` against a coverage that already exists.** Delete the coverage
first, or ingest under a new name and swap. It costs nothing to follow and
keeps the index (and this kind of investigation) simpler the next time
someone looks.

On our server, 247 of 273 coverages have a clean, 1:1 tile index. Twenty-six
carry duplicate entries. None of the 26 cost extra disk once you read
`PhysicalSize` instead of `totalSize` — the worst of them,
`tas_2km_projected_wcs`, reports 2,321.9 GB via `totalSize` and 313.3 GB via
`PhysicalSize`, and 313.3 GB is the real number.

#### A 27th coverage, found by a different route — the flagging test has a blind spot

The 26 above were found by the `totalSize`-vs-`PhysicalSize` sweep across all
273 coverages. `crrel_gipl_outputs_nc`'s full tile-domain dump (pulled while
investigating Section 3.2's boundary question, not this one — see
`CRREL_GIPL_tiling.md` section 7) turned up a 27th case that sweep never
flagged: 29,109 indexed tile entries, but only 28,202 *unique* domains — 907
duplicate entries. The signature is identical to every coverage above: the
28,202 unique domains sum to exactly 115,109,064,000 bytes, matching
`PhysicalSize` to the byte, while all 29,109 entries (duplicates included) sum
to exactly 118,874,274,960 bytes, matching `totalSize` to the byte. Same
mechanism, same proof, just a smaller gap — 3.77 GB, about 3.3% of the
coverage's real size, against the 26 flagged coverages' median gap of well
over 50%.

That gap is exactly why the original sweep missed it: whatever threshold or
sorting cut the flagged list at 26 was tuned for coverages losing hundreds of
gigabytes to phantom inflation, not a few percent. It says nothing about
whether 26 is the true count server-wide — it's the count of coverages whose
`totalSize` inflation was large enough to stand out. A coverage with a small
duplicate count, like this one, could easily hide inside `totalSize` numbers
that look otherwise unremarkable. **If you want the true count, the sweep in
Section 3.3's `grep`/`sort`/`uniq -c` command above needs to run against every
coverage's dump, not just the ones `totalSize` already made suspicious.**

The 907 duplicates aren't spread randomly either, which matters for the
"re-run `wcst_import`" theory above: 599 of them sit at exactly one Y-block
boundary (`Y = 672:713`) and appear in 599 of the coverage's 600
time/model/scenario combinations; another 299 sit at `Y = 798:839` in 299 of
the 600. That's not noise — it's what re-touching a specific spatial strip
across nearly the entire non-spatial domain in a later pass would produce, on
this coverage as much as any of the other 26.

#### The census this raised got run — 209 of 273, not 26

The `grep`/`uniq -c`-against-every-coverage sweep this section called for
turned out not to need new dumps at all: `data/coverages_summary.csv`
already carries `total_size_bytes` and `real_data_bytes` (the array's true
size from its extents, independent of tiling) for all 273 live coverages,
and their ratio is exactly the duplicate-index signal this section is built
on — validated against `era5_4km_elevation`'s real dump (reads a clean
1.0000×, correctly unaffected by that coverage's non-uniform-but-not-
duplicated grid from Section 3.2) and `crrel_gipl_outputs_nc`'s (reads
1.0327×, matching the 907 duplicates confirmed above to the byte).

The real count: **209 of 273 coverages carry at least one duplicate index
entry; 4,406.7 GB of phantom `totalSize` inflation server-wide.** Most of
that is the already-known 26. What's new is 197 further coverages carrying
146.6 GB between them, dominated by one cluster — 171 of the 178
`cmip6_downscaled_*_v2_wcs` coverages sit at almost exactly the same
inflation ratio (`1.0088×` or `1.0077×`), which reads like one shared batch
step, not 171 independent accidents. Full breakdown, the reasoning, and
commands to spot-check the new cluster against a real dump are in the audit
doc's [Finding 1](rasdaman-tiling-audit.md#the-full-census--26-was-never-the-real-count);
the census itself is `data/tile_duplicate_census.csv`.

---

## 4. Sizing a tile

Rasdaman's guidance is that **1–4 MB per tile is optimal in most cases**.

The byte budget is the trap:

```
tile bytes = (product of the tile's per-axis extents) × (sum of every band's byte width)
```

**Every band counts, not one.** Rasdaman's Storage Layout Language paper makes
this unambiguous with its own worked example: a 3-band RGB image tiled 512 × 512
declares `tile size 786432`, and 512 × 512 × 3 × 1 byte = 786,432 exactly.

For a 10-band `float32` coverage that is **40 bytes per cell**, not 4. Planning
against one band's width under-counts the real footprint by the band count —
a tenfold error that no validation catches, because nothing was technically
violated.

---

## 5. `REGULAR` vs `ALIGNED` — corrected 22 September 2026

**Both shrink boundary tiles; neither pads.** An earlier version of this
section claimed `REGULAR` fetches a full-size tile at every edge while
`ALIGNED` shrinks — checked against rasdaman's own Query Language Guide
(Storage Layout Language chapter), that's wrong for `REGULAR` too. Its own
example (`tiling regular [1024, 1024]`) is documented as producing tiles "of
size 1024 x 1024, **except for border tiles (which can be smaller)**" — the
same shrink-to-fit behavior `ALIGNED`'s own docs describe ("tiles can be
smaller and deviate from the configuration sizings" at the array's upper
limits). Section 3.2 has the fuller correction and the server evidence behind
it. Neither scheme affects how much disk the coverage uses, for the same
reason: nothing about the choice between them changes the array's logical
size, and boundary tiles in both cases end up sized to what's actually there.

So if not boundary padding, what *does* distinguish them? Two things, and
rasdaman's own docs are explicit about both:

**What you have to already know.** `REGULAR [ranges] tile size N` declares an
exact tile shape up front — you commit to a chunk size for every axis before
ingest. `ALIGNED [0:*, ...] tile size N` lets you leave some axes as `*` — a
*preferred direction of access* — and have rasdaman size them to hit the byte
target once the axes you *do* pin are fixed. It's the only option when an
axis's real chunk size isn't decided yet, or when the axes involved don't
factor near your budget (Section 4's case for `crrel_gipl_outputs_nc`'s
spatial axes).

**One thing this section previously got wrong: a recipe's `"irregular": true`
on an axis does not rule out `REGULAR` tiling.** That flag lives in the
recipe's `axes` block and governs how petascope declares the axis's
real-world CRS coordinate values — a plain min/max/resolution sequence versus
an explicit `directPositions` list, needed here because `model` and
`scenario` are categorical lookups and `time` is given as actual dates rather
than a fixed step. The tiling bracket operates entirely in a different
space — integer grid-index counts — and nothing in rasdaman's Storage Layout
Language documentation ties the two together; `crrel_gipl_outputs_nc` sets
`"irregular": true` on `time`, `model`, and `scenario` because the recipe
already committed to `ALIGNED` tiling elsewhere in the same file, not because
`REGULAR` would have been rejected. Thanks to Josh for the correction here —
I'd assumed the two were linked and hadn't checked. I could not find the
`wcst_import` source itself to confirm there's no validation check enforcing
this in some other way (rasdaman's canonical repository isn't reachable from
here), so if you hit a real ingest-time error tying `REGULAR` to an irregular
axis, that would be new information worth feeding back into this section —
but nothing in the documentation predicts one.

**What access pattern each is actually pitched at — and it runs opposite to
the "regular divides evenly, so use it for point queries" intuition.**
Rasdaman's own tiling guidelines single out `REGULAR` for exactly the
opposite case: *not* knowing the query shape, or a client that always
requests same-size regions —

> "Nothing is known about access patterns: choose regular tiling with a
> maximum tile size... map viewing clients typically send several requests
> of fixed extent per mouse click to maintain a cache of tiles in the
> browser for faster panning. So the extent of the tile is known — or at
> least that tiles are quadratic."

— which describes a map/WMS-style client, not a point query. `ALIGNED` is
what the docs reach for specifically to describe a point/time-series pattern:

> "...either a time slice is read... or a time series is extracted for one
> particular position (x, y)... An axis which never participates in any
> subsetting box is called a preferred direction of access."

A point time series is precisely "never subsets the time axis" — the textbook
case for `ALIGNED`'s wildcard, not for `REGULAR`. Section 4's WCS point
scheme in `CRREL_GIPL_tiling.md` uses `ALIGNED` for this reason — it's the
tool the documentation itself recommends for that access pattern — and also
because neither spatial axis factors close to the byte budget it needs, not
because `REGULAR` was ever off the table on legality grounds.

One caveat, lower confidence than the rest of this section: an older,
unversioned rasdaman wiki page describes a *regular computed index*
(`rc_index`) — available only under `REGULAR` tiling — that locates a tile by
direct arithmetic on its fixed size and position rather than an R+-tree
lookup, which would be a genuine index-speed argument for `REGULAR` wherever
its restrictions don't rule it out. This didn't turn up in the current Query
Language Guide, so treat it as plausible, not confirmed. `dbinfo`'s own JSON
output carries the field that would settle it — `"index": {"type": ...}` —
and both saved dumps this audit has for `ALIGNED` coverages report
`"rpt_index"` (R+-tree), for `cmip6_fwi` and `era5_4km_daily_t2_mean`
(`data/tile-dumps/`). Neither is `REGULAR`-tiled, so this doesn't confirm or
rule out `rc_index` either way — dump a `REGULAR`-tiled coverage's `dbinfo`
output the same way to check.

A wildcard is a real delegation of control. `ALIGNED [0:*, 0:31, 0:31] tile size
16777216` pins the spatial chunks at 32 and lets rasdaman choose the first axis;
on an 18,250-step time axis with one float band it chooses 4,096, giving a
4096 × 32 × 32 tile. That is a decision you did not make, and it may not suit
your query pattern.

**String-valued axes** (a `model` coordinate stored as netCDF strings) can't be
used with `REGULAR` directly, since rasdaman can't store strings as axis
coefficients. The usual fix is a small Python lookup module — loaded via a
`statements` block, e.g. `imp.load_source('luts', os.getenv('LUTS_PATH'))` —
mapping each label to a sequential integer (`'5ModelAvg' → 0`). Once every axis
is numeric, `REGULAR` becomes available.

---

## 6. Choosing a tiling

Start from the question the coverage exists to answer — and only that question.
Sections 3.2 and 3.3 settled the storage side: **tile shape, tile size and tile
count do not change how much disk a coverage occupies.** The three-way
`era5_4km_daily_t2_mean` experiment proved it at 289, 1,172 and 4,678 tiles for
an identical 19.011 GB. So tiling is a pure read-performance decision. Choose
for queries and ignore disk.

**For point and small-AOI reads** — the dominant pattern for our data — keep
every non-spatial axis whole inside a tile and make the spatial footprint as
small as a sane tile size allows. One tile then answers a whole time series.

**For map rendering (WMS)** — do the opposite: pin the non-spatial axes to one
index each and make the spatial footprint large.

**Identify the axis that is large relative to the others** and chunk that one.
A stream dataset with 56,460 site IDs and a handful of small categorical axes
wants `stream_id` chunked to 1 and everything else at full extent: one tile per
site, one tile per query.

The trade-off is visible directly in our own measurements. These three hold
climate data on the same 460 × 442 grid, tiled three different ways:

| Coverage | Tile | Point query | Map query |
|---|---|---|---|
| `era5_4km_daily_t2_mean` | rasdaman's choice — 5 × 460 × 442 | 203,320× | 46,752× |
| `era5_4km_daily_t2_mean_wcs` | whole time axis, 8 × 8 spatial | **64×** | 23,899× |
| `cmip6_fwi` | whole time axis, 2 × 2 spatial | **4×** | 128,626× |

Shrinking the spatial footprint from a full frame to 8 × 8 improves point reads
by a factor of three thousand. Going further to 2 × 2 gets you to 4×, close to
the floor — but the map query degrades from 24,000× to 129,000×, because a map
frame now has to assemble tens of thousands of tiny tiles.

The first two rows hold the *identical array* and occupy the identical space
for it: 19.0 GB, both of them, once you read the real figure
(`PhysicalSize`) rather than `totalSize`. `era5_4km_daily_t2_mean_wcs`'s
`totalSize` reports 1.32×, 25.1 GB, but that is the duplicate index entries
from a re-run ingest (Section 3.3) inflating the count, not a real byte on
disk. Its tiling costs nothing extra either way, and answers point queries
three thousand times faster than its sibling.

So the middle row is the one to copy. A spatial footprint of roughly 8 × 8 to
12 × 12 with the sweep axis kept whole captures nearly all of the point-query
benefit without making the coverage useless for maps, and it costs nothing
extra to store.

---

## 7. Worked examples

**Stream-segment dataset.** Native dimensions `era=4, doy=366, landcover=2,
model=14, scenario=5, stream_id=56460`; `gridOrder`, `crs` and `tiling` all use
that same order here (valid, though not required).

```
"tiling": "REGULAR [0:3, 0:365, 0:1, 0:13, 0:4, 0:0] tile size 1048576"
```

Every axis at full extent except `stream_id`, chunked to 1. Divisibility is
automatic — full extent divides itself, and 1 divides anything. Cells per tile:
4 × 366 × 2 × 14 × 5 × 1 = 204,960.

**Gridded permafrost dataset — `crrel_gipl_outputs_nc`.** This one gets a
full document of its own:
[`CRREL_GIPL_tiling.md`](CRREL_GIPL_tiling.md). It starts from the source
file's `ncdump`, works out the true storage order the hard way (this
coverage is one of Finding 4's catalogue disagreements — `DescribeCoverage`
reports the wrong axis order, so the recipe's `gridOrder` has to settle it),
and designs two separate schemes — a point/time-series tiling and a
map-rendering tiling — both held to a strict 4 MB target, ready to be
ingested as test coverages and timed against the original. Worth reading in
full for how it picks `ALIGNED` on both spatial axes not because `REGULAR`
was disqualified (Section 5's correction: a recipe's `"irregular": true` on
an axis doesn't rule out `REGULAR` tiling, that flag is about CRS coordinate
declaration, not storage layout) but because neither 1941 nor 2471 factors
anywhere near the chunk sizes a 4 MB budget needs.

The general lesson that document's section 4 draws out is worth stating here
too: a chunk size that divides one axis exactly is not guaranteed to divide
the *other* axis it might get paired with if the two spatial axes are ever
swapped — `1941 = 3 × 647` and `2471 = 7 × 353` share no common factor, so a
transposition between them doesn't degrade gracefully, it breaks divisibility
on both sides at once. **A per-axis divisibility check, not a total-byte
check, is what catches a transposition** — the byte arithmetic comes out
identical either way, because tile volume doesn't care which axis
contributed which factor. And whatever it costs, it isn't disk: more tiles
and padded edges mean more bytes fetched per read (a latency cost, paid
forever), not more bytes stored (Section 3.2) — a transposition is a
performance bug, not a capacity one.

---

## 8. When the math is wrong

Rasdaman has hard-failure errors for structural tiling problems — error 219
(tile size smaller than the base type), 220 (tiling strategy incompatible with
the marray), 224 (tile configuration incompatible with the marray domain). None
of them fire for the most likely mistake, which is sizing a tile against one
band instead of all of them.

What happens instead depends on the bracket:

- **With wildcards**, rasdaman solves for a shape that hits your declared byte
  target using the *true* multi-band cell width. If you picked the target using
  one band, the tiles it builds land at roughly (band count) × the size you
  intended — silently, because nothing was violated.
- **With an explicit bracket**, the bracket determines the shape; the `tile size`
  number is descriptive. An inaccurate number means your estimate was wrong, not
  that rasdaman built something different.

Either way there is no error naming the cause. It surfaces as an ingest that is
slow and memory-hungry — tiles that overflow the configured tile cache
(`--cachelimit`) spill to disk, and when tiles are several times larger than
planned, that is consistent with exhausting RAM rather than failing cleanly.

---

## 9. Verifying what was actually built

Planning is Sections 1–8. This is how to check what exists.

```bash
curl -u rasadmin:$PASSWORD \
  -d 'query=select dbinfo(c) from $COVERAGE_ID as c' \
  'https://<host>/rasdaman/rasql'
```

The fields that matter: `baseType` (count and type of every band, so you can
read the true per-cell width), `tileNo` (tiles indexed — including duplicate
entries, see Section 3.3), `totalSize` (**not** bytes on disk — it's cells ×
bytes-per-cell summed over every *indexed tile entry*, so a duplicated entry
gets counted again; see Section 3.3 and the check below), and
`tiling.tileConfiguration` (the tile shape). For the real on-disk figure, read
`RAS_MDDOBJECTS.PhysicalSize` from RASBASE directly — Section 3.3 and the
[audit doc's Method section](rasdaman-tiling-audit.md#method-and-what-these-numbers-do-not-prove)
have the query.

Adding `dbinfo(c,"printtiles=embedded")` also lists every individual tile
domain. Be careful with it: on a coverage with millions of tiles the response
is large enough to arrive truncated.

**Five checks worth running.**

*Did it build the grid I asked for?* Compute the tile count the geometry
requires — `∏ ⌈extent ÷ chunk⌉` — and compare against `tileNo`. Equal means the
tiling behaved. Lower means sparse materialisation, which is fine. Higher does
**not** by itself mean anything is wrong: a wildcard bracket makes the geometric
count meaningless, and `era5_4km_daily_t2_mean` scores 2,339× by this test while
storing at exactly 1.00×.

*Does my index have duplicate entries?* Divide `totalSize` by (cells ×
bytes-per-cell). Above about 1.05 means the tile index has duplicate entries
for this coverage (Section 3.3) — a sign `wcst_import` was re-run against it
without dropping it first. It is **not** a disk check: it does not cost extra
bytes on disk (confirm the real figure with `RAS_MDDOBJECTS.PhysicalSize`),
but it's still worth knowing, both as a process-hygiene signal and because
what a bloated index costs query latency is untested.

*Is there padding?* Check each axis separately: `extent % chunk == 0`. The byte
arithmetic will look right even when two chunk sizes are transposed, so only the
per-axis check catches that.

*Which axis is in which bracket position?* Read `tiling.tileDomains` and match
the distinct values at each position against each axis's known cardinality — a
position cycling through 2 or 3 values is a small categorical axis. This is more
reliable than trusting how the recipe was written.

*Is this even the right array?* A successful `dbinfo` is not proof. `wcst_import`
does not always name the collection after the coverage, so a collection can sit
at a coverage's name while holding something else entirely — we found twelve such
cases, including two where `dbinfo` returned a single 4-byte cell while WCS
served real data from elsewhere. Cross-check the domain against the WCPS probe
below, and get the authoritative name from petascope rather than guessing:

```sql
SELECT c.coverage_id, r.collection_name
  FROM coverage c
  JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id;
```

### Two gotchas in the output

**`tileConfiguration` can contain `*`.** An unbounded axis is reported as `0:*`,
not a number. Parsing only numeric ranges silently drops that position and
misaligns everything after it.

**`sdom` and `tileConfiguration` are in storage order; `DescribeCoverage` is in
catalogue order.** They frequently differ. Pairing one's axis names with the
other's numbers attaches every per-axis result to the wrong axis — the same trap
as Section 2, now with real numbers attached.

### Reading a coverage without credentials

A deliberate WCPS type error makes rasdaman describe its own array, needing
neither rasql credentials nor the collection name:

```
for $c in (COVERAGE_ID) return encode($c * "x", "csv")
```

POST that to `/rasdaman/ows` as `request=ProcessCoverages`. The exception text
comes back carrying the real stored domain, cell type and null value:

```
ARRAY (float) [D0(0:18249),D1(0:442),D2(0:459)] null values [-9999.000000]
```

This works on coverages rasql cannot reach at all, and it is the most reliable
answer to "what is actually stored," because it comes from the array itself.

---

## 10. Before a full ingest

1. `wcst_import.sh --recipe general_coverage --analyze` for a dry run that
   doesn't touch the database.
2. Estimate the size: `(product of all axis extents) × (bands) × (bytes per
   value)`. A source netCDF much smaller than this is normal — HDF5 compresses
   internally. Rasdaman compresses too, so `RAS_MDDOBJECTS.PhysicalSize`
   (Section 3.3) usually lands below this estimate once ingest finishes. If
   `dbinfo`'s `totalSize` reads well above it, don't panic — check
   `PhysicalSize` before concluding anything, since `totalSize` inflates on
   its own if the coverage already existed when the recipe ran (Section 3.3).
   `PhysicalSize` landing above the estimate would be the real warning sign;
   we have not seen that happen on this server.
3. Test the tiling on a small subset before committing to a multi-hundred-GB
   run. A bad tile choice is expensive to discover halfway through.
4. After ingest, run the five checks in Section 9, and spot-check one known
   coordinate against the source file to confirm axis order.

---

## Sources

- [Tiling — rasdaman wiki](http://rasdaman.org/trac/wiki/Tiling)
- [Storage Layout Language (PDF)](http://rasdaman.org/trac/attachment/wiki/FAQ/sstdm2010.pdf), via the [rasdaman FAQ](https://rasdaman.org/trac/wiki/FAQ)
- [Performance — rasdaman wiki](https://rasdaman.org/trac/wiki/Performance)
- [rasdaman error text definitions](https://github.com/hholzgra/rasdaman/blob/master/bin/errtxts)
- [Geo Services Guide — rasdaman 9.7.0](https://doc.rasdaman.org/9.7/05_geo-services-guide.html)

Every measurement in this guide comes from the 273 coverages on
`zeus.snap.uaf.edu`, collected by `rasdaman_tiling_audit.py`, with tile domains
dumped and counted for six of them and every disk figure cross-checked against
`RAS_MDDOBJECTS.PhysicalSize` via `rasdaman_physical_size.py`. Where
documentation and direct introspection disagreed, introspection won — and more
than once introspection itself was wrong first. The cause of the excess
`totalSize` figures took three wrong theories before the tile dump found the
real one (duplicate index entries); what that duplication actually costs on
disk took a wrong unit assumption and a second RASBASE field
(`PhysicalSize`) before landing on "nothing measurable." Both corrections are
in Section 3.3, in full, including the reasoning that turned out to be wrong.
