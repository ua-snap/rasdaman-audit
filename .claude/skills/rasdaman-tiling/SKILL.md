---
name: rasdaman-tiling
description: Operational know-how for SNAP's rasdaman/petascope server (Zeus) — choosing a tiling for a wcst_import ingest recipe, sizing tiles, checking what an existing coverage actually built, measuring real disk usage, finding orphaned collections, and safely re-ingesting or deleting coverages. Use this whenever someone is writing or reviewing a rasdaman ingest recipe ("tiling", "gridOrder", ALIGNED/REGULAR, "tile size"), asks why a coverage is slow for point or map queries, reads dbinfo/totalSize output, wants to know how much disk a coverage or collection uses, is cleaning up RASBASE, is about to re-run wcst_import or add_coverage.sh/delete_coverage.sh, or is benchmarking WCS/WMS/WCPS performance on Zeus — even if they don't say "tiling" explicitly.
---

# rasdaman tiling & coverage operations

This distills a full audit of the 273 coverages on SNAP's production rasdaman server (`zeus.snap.uaf.edu`, rasdaman v10.4.7, Sept 2026) plus a benchmarked worked example. The source repo is `rasdaman-audit`. Its docs (`docs/rasdaman-tiling-guide.md`, `docs/rasdaman-tiling-audit.md`, `docs/CRREL_GIPL_tiling.md`, `docs/benchmark-harness.md`) are the long-form versions of everything here. If you're working inside that repo, prefer its scripts and docs for depth.

Several conclusions here contradict what rasdaman's own outputs seem to say (`totalSize`, `DescribeCoverage` axis order). Rely on the measured findings below, and say so when you correct someone.

## The five ideas everything rests on

1. **A tile is the smallest unit rasdaman reads.** Asking for one cell fetches and decompresses its whole tile. So tile *shape* relative to the query's shape decides performance. That relationship is measured as read amplification: bytes read ÷ bytes wanted.
2. **Tiling does not affect disk usage. Choose tiling for queries and ignore storage.** The same array stored at 289, 1,172 and 4,678 tiles occupies 19.011 GB each time. Boundary tiles shrink to fit; they are not padded. Duplicate index entries cost no disk.
3. **The tiling bracket is positional in `gridOrder` (storage order), not `crs` or `DescribeCoverage` order.** `gridOrder` is each axis's 0-based position in the netCDF variable's dimension tuple from `ncdump -h`, read left to right. Getting this backwards is the most common way recipes go wrong, and nothing errors.
4. **Every band counts.** tile bytes = (product of per-axis chunk sizes) × (sum of every band's byte width). Ten `float32` bands make 40 bytes per cell, not 4. No rasdaman error catches a single-band miscount. With wildcards, it silently produces tiles about N_bands × too large.
5. **Coverage ≠ collection.** A *coverage* is petascope/OGC metadata in PostgreSQL `petascopedb`. A *collection* is rasdaman's stored array in the RASBASE SQLite file. One pointer joins them (`coverage → rasdaman_range_set.collection_name`), and the names often differ: 186 of 273 diverged, for example with appended timestamps or `_v2` moved to the end of the name. Never assume coverage ID == collection name. Read the mapping (see `references/verify-and-audit.md`).

## Workflow: designing a tiling for a new ingest

1. **Get the true layout.** Run `ncdump -h file.nc`. Record the data variables' dimension tuple, band count and dtypes. For an existing coverage, confirm storage order with `select sdom(c) from <collection> as c`. Never take it from `DescribeCoverage`, which disagreed for 11 coverages, GIPL among them.
   - **Check the time calendar** (`time:units`, `time:calendar`) whenever the recipe maps time onto `AnsiDate`. Climate-model output often uses `noleap`/`365_day` or `360_day`. Those calendars don't line up with `AnsiDate`'s real Gregorian days: they drift about 21 days over 86 years and have no Feb 29.
   - A time length that is an exact multiple of 365 or 360 is a strong hint (e.g. 31,390 = 86 × 365). If the user only pasted dims and variables, ask them to run `ncdump -h` and look at the `time:` attributes.
   - Raise a mismatch before ingest as a separate problem from tiling. It's a recipe/axis decision for the user, so don't invent a fix. `tile_calc.py` warns about it when the attributes are in its input.
2. **Name the access pattern(s).** The coverage exists to answer certain queries. Which ones?
   - **Point / time series (WCS)**, SNAP's dominant pattern: keep every non-spatial axis whole and make the spatial footprint as small as the budget allows.
   - **Map (WMS single slice)**: pin every non-spatial axis to 1 and make the spatial footprint large and square-ish.
   - **Small AOI polygon**: like point. For real SNAP polygon sizes the 4 MB point footprint already covers it.
   - **An axis much larger than the others** (e.g. 56,460 `stream_id`s next to small categorical axes): chunk that axis to 1, or small, and keep everything else whole.
   - **WCPS condense window** (e.g. a 30-year mean): the theory says chunk time to N. The only real test came out **about 14× slower** than a plain map scheme, so don't recommend it without benchmarking.
   - One tiling can't serve point and map well at once; the gap is orders of magnitude either way. If both matter, the house pattern is two coverages, `<id>_wcs` (point) and `<id>_wms` (map).
3. **Compute the bracket with the bundled calculator.** Don't do the arithmetic by hand:
   ```bash
   ncdump -h file.nc | python3 <skill-dir>/scripts/tile_calc.py --ncdump - [--condense-n 30] [--budget-mib 1,4]
   python3 <skill-dir>/scripts/tile_calc.py --dims "time=100,model=3,scenario=2,y=1941,x=2471" --bytes-per-cell 40
   ```
   The calculator prints a fully explicit `ALIGNED [...] tile size N` for each pattern, with modelled point/map amplification, tile count and per-axis remainders. It takes axes in the order you give them, so give them in **`gridOrder`**. In the `rasdaman-audit` repo, `utilities/recommend_tiling.py --netcdf file.nc` does the same from the real file. It adds polygon-sized schemes from SNAP's boundary layer but needs the conda env in `utilities/environment.yml`.
4. **Choose the scheme keyword: prefer `ALIGNED` with every axis explicit.**
   - `REGULAR` OOM-crashed Zeus on the full ~115 GB GIPL ingest. An identical bracket under `ALIGNED` succeeded. On retry the `REGULAR` ingest hit a 400 GB memory limit, about 3.5× the dataset. The likely reason: `REGULAR` materializes full zero-filled boundary tiles during streamed partial updates. Avoid it unless every axis divides evenly by its chunk.
   - Avoid `0:*` wildcards. Rasdaman's fill doesn't follow the byte-budget math. One wildcard example: `ALIGNED [0:*, 0:31, 0:31] tile size 16777216` on the cmip6_downscaled v2 family built 4–7-cell-wide whole-time-axis tiles plus a separate last-day tile population. With all five axes wildcarded, GIPL got a 1×1×1×42×2471 band. If you know the extents, pin every axis.
   - `"irregular": true` on a recipe axis is unrelated. It governs CRS coordinate declaration, not storage tiling, and neither keyword is ruled in or out by it.
5. **Target 1–4 MB per tile** (rasdaman's guidance; the house default is ~4 MiB). Under an explicit bracket the `tile size` number is descriptive; the bracket sets the shape.
6. **Before the full run:**
   - Dry-run with `wcst_import.sh --analyze`.
   - Estimate logical size: product of extents × bytes per cell.
   - Test on a subset; a bad choice is expensive to discover halfway through a multi-hundred-GB run.
   - Ingest under a **new coverage ID**. Never re-run `wcst_import` against an existing coverage (see the hygiene section).
7. **After ingest, verify.** Run the five checks in `references/verify-and-audit.md`, and spot-check one known coordinate against the source file to confirm axis order.

For the full worked example (GIPL: ncdump → gridOrder → 4 schemes → measured results), read `references/design-worked-example.md`.

## Workflow: "how big is it really?" and cleanup

- **`dbinfo`'s `totalSize` is NOT disk usage.** It sums the tile *index*, so duplicate index entries inflate it. On Zeus it overstated live coverages by about 2× (14,000 GB claimed vs. 9,172 GB real).
- **The real figure is `RAS_MDDOBJECTS.PhysicalSize` in RASBASE**, which matched rasdaman's UI total to four significant figures. The join and queries are in `references/verify-and-audit.md`.
- **A large `PhysicalSize` may still not be rasdaman disk.** In-situ ingests record file pointers in `RAS_FILETILES` instead of blobs in `RAS_TILES`. One 1,268 GB orphan was almost entirely file references, so dropping it frees roughly 0 GB. Check with `rasdaman_physical_size.py --check-fileref TOKEN` before calling any large orphan recoverable.
- **Orphans** are collections in `RAS_MDDCOLLNAMES` that no petascope coverage points to. Before suggesting a `drop collection`, verify its `oid` against `rasdaman_range_set`, run the in-situ check, and have a human run it. Never run drops yourself; they're irreversible.

## Ingest/delete hygiene (why orphans and duplicate index entries exist)

- **Never re-run `wcst_import` (or `add_coverage.sh`) against a coverage ID that already exists.** Delete the coverage first, or ingest under a new name and swap. Re-runs either strand the previous timestamped collection as an orphan or add duplicate tile-index entries; one coverage was re-ingested 12 times in a day. `add_coverage.sh` (`wcst_import.sh -c 0 <recipe>`) checks neither existence nor exit code.
- **A `200` from `delete_coverage.sh` does not prove the collection is gone.** It discards the response body, and the petascope and rasdaman deletes aren't one transaction. Confirm afterwards that the collection no longer appears in `RAS_MDDCOLLNAMES`.
- **Duplicate index entries** (the same tile domain indexed 2–16×) cost no disk. Their query-latency cost is untested. Don't re-tile to "fix" them.

## Diagnosing a slow coverage

1. Get its real tile shape. Read `tiling.tileConfiguration` from `dbinfo`, or dump one domain with `printtiles=embedded`. Wildcards make the recipe string untrustworthy.
2. Run `tile_calc.py --check "<tiling string>"` with its axes in storage order to see modelled point and map amplification.
3. Compare against how the coverage is actually queried. Mismatches run to 10³–10⁵×. On Zeus, 52 of 253 modelled coverages read more than 1,000× what a typical query returns.
4. Amplification is a *ranking*, not a benchmark. On GIPL a 615× amplification gap became a ~5× latency gap, and two map predictions were wrong. Benchmark before adopting (see `references/benchmarking.md`).

## Reference files (read when needed)

- `references/design-worked-example.md`: the GIPL walkthrough, scheme math, and measured vs. modelled results. Read it when designing a tiling or explaining why one was chosen.
- `references/verify-and-audit.md`: the coverage → collection mapping query, `dbinfo` fields, the five post-ingest checks, the credential-free WCPS probe, the duplicate-index check, the RASBASE `PhysicalSize` and in-situ queries, and the audit scripts. Read it for any "what was actually built / how big is it / what can we drop" question.
- `references/benchmarking.md`: the JMeter harness and its gotchas, such as an empty `STYLES=` silently rendering a 30-year condense, cold vs. warm caches, and builders that skip IDs containing `test`. Read it before timing anything.
- `references/zeus-findings.md`: the audit snapshot (as of 2026-09-23). It has headline numbers, known problem coverages, and open questions. These numbers go stale whenever coverages change, so cite them with their date and re-run the scripts for current figures.

## Safety

The audit tooling is read-only: `sqlite3 -readonly`, rasql `select` and `dbinfo`, `psql` `SELECT`. Keep it that way. Present `drop collection`, `DeleteCoverage` and re-ingest commands for a human to review and run, together with the verification steps above. Don't execute them yourself. Credentials come from `RASDAMAN_USER`/`RASDAMAN_PASS` and the `PG*` env vars. Never hard-code them.
