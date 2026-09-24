# Rasdaman tiling audit — zeus.snap.uaf.edu

Tools and findings from an audit of every coverage on SNAP's production rasdaman server (Zeus): how each one is tiled, how much disk it occupies and why, and whether its tiling matches how it is actually queried.

Everything here should be reproducible as of 9/23/26. Any revision to the current collection of coverages will change the audit results. The scripts are Python 3 standard library only (except the workbook builder, which needs `openpyxl`), read-only against Zeus, and re-runnable after any change.

If you want to run the workbook builder, just clone the base conda environment on Zeus and add the `openpyxl` package:

```
conda create --name rasda-audit --clone base
conda install openpyxl
```

## Terminology: coverage vs. collection

Mixing these up is the fastest way to misread everything in this repo!

- **Coverage** is petascope's word, and the OGC WCS standard's. A coverage has a `COVERAGEID`, is built with `InsertCoverage` (what `wcst_import` sends under the hood), removed with `DeleteCoverage`, and described with `DescribeCoverage`. Its metadata — extents, axis labels, the `crs` string, null values, and a pointer to where the actual data lives — is a row in `petascopedb`, an ordinary PostgreSQL database.
- **Collection** is rasdaman's own word for the stored array itself: what `rasql` addresses as `COLLECTION`, and what `RAS_MDDCOLLNAMES` catalogues inside RASBASE (a SQLite file, not something you connect to as a database server). rasdaman has no concept of "coverage" at all — that's a layer petascope adds on top of it.
- **The two are joined by one pointer, not fused into one system.** `petascopedb`'s `coverage` table links to `rasdaman_range_set.collection_name` — a single row saying "coverage X's data lives in collection Y." Nothing else connects them, and nothing guarantees the pointer stays correct: `wcst_import` doesn't reliably name the collection after the coverage (Section 9 in the tiling guide has the mapping query), and the ordinary day-to-day tooling for creating and deleting coverages can silently move or drop that pointer without touching the other side — see the audit doc's Finding 5 for what that looks like in practice.

This repo uses **coverage** only for the petascope/OGC-layer object and **collection** only for the rasdaman-layer array, consistently across every doc here. Keep the two straight, especially once they start appearing in the same sentence.

## What we found

| | |
|---|---|
| Coverages | 273, all measured directly |
| On disk, live coverages | **9,172 GB** (matches rasdaman UI [here](https://zeus.snap.uaf.edu/rasdaman/ows#/services) |
| Duplicate tile-index entries | 26 coverages, confirmed real, cost **~0 GB** |
| Collections nothing references | 105, totalling 3,305 GB |
| — of which confirmed file-referenced ("in-situ"), not real rasdaman disk | 1 collection, 1,268 GB |
| **Genuinely recoverable** | **2,037 GB**, pending spot-checks on the next few largest orphans |

Three things are worth knowing before you read further:

#### **A duplicate tile-index entry is not a duplicate byte on disk.** 
Six coverages had every tile domain dumped and counted directly: some domains really are indexed two, four, even eight times. But `RAS_MDDOBJECTS.PhysicalSize` — read straight from RASBASE, not derived from the tile index — shows every one of those coverages stores exactly its unique data and nothing more. `dbinfo`'s `totalSize` inflates because it sums index entries, including duplicates; `PhysicalSize` doesn't, because it isn't computed from the index at all. **There is no re-ingest campaign to run for disk space here.** Tile shape, tile size and tile count don't affect disk usage either — the same array exists on this server at 289, 1,172 and 4,678 tiles and occupies 19.011 GB every time.

#### **Not every "unreferenced" GB is recoverable.** 
One 1,268 GB orphan collection turned out to be ingested "in situ" — its tiles are pointers into source netCDF files, never copied into rasdaman's own storage. Its `PhysicalSize` is an honest count of real data; it just isn't data rasdaman is holding. Check `RAS_FILETILES` before assuming a large orphan's GB is real disk (see step 3 below).

#### **A coverage ID is not a collection name.** 
`wcst_import` does not reliably name the rasdaman collection after the coverage — it may append a timestamp, or collections may be manually renamed without affecting the coverage name. 186 of 273 diverge. Any tool that assumes they match will silently measure the wrong array, or fail outright with `Object Unknown`. Read the mapping out of `petascopedb` and hand it to the scripts (step 1 below). ("Coverage" and "collection" are two different systems' words for two different things joined by exactly one pointer — see [Terminology: coverage vs. collection](#terminology-coverage-vs-collection) above.)

#### **The ingest/delete scripts don't verify either side of that pointer.** 
`add_coverage.sh` (`wcst_import.sh`) doesn't check whether a coverage ID already exists before importing into it, and `delete_coverage.sh` (WCS-T `DeleteCoverage`) only checks an HTTP status code, discarding the response body — neither one confirms that petascope's metadata and rasdaman's collection actually ended up in sync. That gap is the leading, evidenced explanation for most of the 105 unreferenced collections above: see the tiling audit's [Finding 5](docs/rasdaman-tiling-audit.md) for the timestamped-re-ingest pattern that proves it.


## Repo Layout
```
docs/          the guide, the audit, and the figures they reference
scripts/       five read-only audit tools plus the workbook builder
data/          the inputs and outputs of the 2026-09-21/22/23 runs
data/tile-dumps/   raw tile domains for seven coverages, gzipped
data/physical_sizes.csv   every collection's real disk size, from RASBASE
rasdaman_tiling_audit.xlsx    seven tabs, all live formulas
utilities/     a netCDF-in, tiling-recommendations-out CLI tool -- see utilities/README.md
```

## Docs
Start with **[docs/rasdaman-tiling-guide.md](docs/rasdaman-tiling-guide.md)** — what "coverage" and "collection" each mean and how loosely they're joined, then what a tile is, what it costs, how a recipe becomes stored tiles. Then
**[docs/rasdaman-tiling-audit.md](docs/rasdaman-tiling-audit.md)** for what is true of our server, and `rasdaman_tiling_audit.xlsx` for the per-coverage
numbers behind it. **[docs/CRREL_GIPL_tiling.md](docs/CRREL_GIPL_tiling.md)** walks the whole process end to end on one real coverage — from `ncdump` to two tiling schemes to a place to record how they actually perform — and is the place to start if you're about to tile something yourself. **[utilities/](utilities/README.md)** automates that same method: point it at a netCDF file and it recommends tiling schemes for point, polygon, full-domain-map, and WCPS-condense queries at several tile-size budgets, using the file's own real dimensions rather than a worked-by-hand example.


## Running it yourself

### 0. Credentials

Every script reads the same two environment variables and sends them as HTTP basic auth. Everything is read-only. Nothing is ever written to the database.

```bash
export RASDAMAN_USER=rasadmin
export RASDAMAN_PASS='...'
```

### 1. Get the coverage → collection mapping

This is the step that makes everything else work, and it needs read access to `petascopedb` — ordinary PostgreSQL, no sudo required. Set `PGHOST`/`PGPORT`/`PGUSER` from petascope's own properties file (`/opt/rasdaman/etc/petascope.properties`, which carries the JDBC URL and credentials), then:

```bash
psql -Atc "
  SELECT c.coverage_id, r.collection_name
    FROM coverage c
    JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id
   ORDER BY 1;" > data/mapping.txt
```

`-At` gives pipe-separated pairs with no header, which every script here reads. Left column is the coverage ID; right is the rasdaman collection.

If the table names differ on your build, find them rather than guessing:

```bash
psql -Atc "
  SELECT table_name, column_name FROM information_schema.columns
   WHERE table_schema='public'
     AND (column_name ILIKE '%collection%' OR column_name ILIKE '%oid%')
   ORDER BY 1,2;"
```

### 2. Audit every coverage

```bash
python3 scripts/rasdaman_tiling_audit.py \
  --url 'https://zeus.snap.uaf.edu/rasdaman/ows?&SERVICE=WCS&ACCEPTVERSIONS=2.1.0&REQUEST=GetCapabilities' \
  --outdir ~/tiling_audit \
  --recipes /path/to/rasdaman-ingest --recipes-ref origin/main \
  --mapping-file data/mapping.txt \
  --wcps-domains
```

About 2.5 minutes for 273 coverages. Writes `_summary.csv` (54 columns), a `_compact/` directory of per-coverage JSON, and `_errors.log`.

`--recipes-ref origin/main` reads recipes from that git ref rather than your working tree.

Useful flags: `--limit 5` for a trial run, `--coverage ID` (repeatable) to target specific coverages, `--verify-tiles-all --verify-max-mb 16` to sample real tile domains, `--save-raw` to keep the raw `dbinfo` responses.

### 3. Find collections nothing points at

rasdaman's own catalogue lives in a SQLite file, not a database you can connect to. Compare its collection list against the 273 names in the mapping:

```bash
sqlite3 -readonly /opt/rasdaman/data/RASBASE \
  "SELECT * FROM RAS_MDDCOLLNAMES;" -header -csv > data/rasbase_collections.csv
```

Anything in there but not in `mapping.txt` is unreferenced. Price it — from RASBASE's own `PhysicalSize`, not `dbinfo`'s `totalSize` (step 4 explains why that distinction matters; pricing off `totalSize` here previously overstated the unreferenced total by about 218 GB):

```bash
python3 scripts/rasdaman_price_collections.py \
  --rasql-url https://zeus.snap.uaf.edu/rasdaman/rasql \
  --rasbase /opt/rasdaman/data/RASBASE \
  --csv data/unreferenced_collections.csv \
  --out data/unreferenced_sizes.csv
```

Each row carries a `drop collection` statement **for review, not for running!**. Verify each `oid` against `rasdaman_range_set` before deleting anything.

### 4. Get the real, physical size of every collection

**Do this before trusting any GB figure from `dbinfo`.** `dbinfo`'s `totalSize` sums the tile *index*, not the filesystem — a coverage whose index has duplicate entries (step 5) reports a `totalSize` well above what it actually occupies. RASBASE keeps a field that doesn't have this problem: `RAS_MDDOBJECTS.PhysicalSize`, computed per stored array object, not by walking the tile index.

```bash
python3 scripts/rasdaman_physical_size.py \
  --rasbase /opt/rasdaman/data/RASBASE --mapping-file data/mapping.txt \
  --out data/physical_sizes.csv
```

Prints the server-wide `RAS_TILES` (real blob tiles) and `RAS_FILETILES` (external file references) counts, then the live/orphan split. Summed across the 273 live coverages this should land close to whatever rasdaman's own UI reports as total volume — on 2026-09-22 it matched "9.17 TB" to four significant figures, which is how the original `totalSize`-based figures in this repository were found to be wrong by about 2×.

**Not every collection's `PhysicalSize` is disk rasdaman is actually holding.** `wcst_import` can ingest "in situ" — leaving tiles as pointers into the original source file instead of copying them in, recorded in `RAS_FILETILES` rather than `RAS_TILES`. A collection ingested this way reports a real, honest `PhysicalSize`, but dropping it recovers none of rasdaman's own disk. Check a suspect collection (usually a large item on the Cleanup list) before counting its GB as recoverable:

```bash
python3 scripts/rasdaman_physical_size.py --rasbase /opt/rasdaman/data/RASBASE \
  --check-fileref <owner-or-source-directory-token>
```

This is a substring match against `RAS_FILETILES.FilePath` — a heuristic, not a certificate. There is no column that says "this collection is in situ"; sanity-check the matching-row count against the collection's own declared tile count before concluding anything. `docs/rasdaman-tiling-audit.md`'s Method section and Finding 5 show the full worked example (one 1,268 GB orphan collection that turned out to be almost entirely file-referenced).

`--self-test` runs the join and the heuristic offline against a synthetic database matching RASBASE's schema — run it after any rasdaman upgrade before trusting this script again; a schema change fails loudly there first.

### 5. Check any coverage for duplicate tile-index entries

The single most useful index diagnostic here, and it needs no scripts. Note this checks the *index*, not disk usage — pair it with step 4 to know whether a coverage's `totalSize`/`PhysicalSize` gap is duplication (harmless, per step 4) or something else:

```bash
curl -sS -u "$RASDAMAN_USER:$RASDAMAN_PASS" \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from COLLECTION as c' \
  https://zeus.snap.uaf.edu/rasdaman/rasql > tiles.json

grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate index entry. If the top line reads `1`, the coverage's index is clean. The `-a` matters: rasql responses contain NUL bytes and GNU grep will otherwise refuse to read them.

Seven worked examples are in `data/tile-dumps/`, gzipped.

### 6. Rebuild the workbook

```bash
pip install openpyxl
python3 scripts/build_workbook.py
```

Reads `data/coverages_summary.csv`, `data/mapping.txt`, `data/physical_sizes.csv` and `data/unreferenced_sizes.csv`; writes `rasdaman_tiling_audit.xlsx` at the repo root. Every Summary figure is a live formula over the other tabs, so the numbers move when the data does. Disk-size columns come from `PhysicalSize` (step 4), not `totalSize`.

## The other scripts

`rasdaman_physical_size.py` is step 4 above: the real, `PhysicalSize`-based disk figure for every collection, plus the file-reference heuristic. Read its module docstring — it explains the RASBASE join and the "in situ" problem in full.

`rasdaman_collection_reconcile.py` compares petascope's coverage catalogue against rasdaman's collections and reports where they diverge — matched, name
collision, renamed, or a shadow collection squatting on a live coverage's name. Run it with `--mapping-file` for authoritative answers, or without for a no-privilege check that only uses the coverage's own name.

`rasdaman_collection_discover.py` probes candidate naming rules when no mapping is available. It exists for the case where `petascopedb` is unreachable; if you have the mapping, you do not need it. `--self-test` runs offline.

All three have `--self-test` and detailed module docstrings.

## Caveats

**A bloated tile index's cost, if any, beyond disk is untested.** This audit measured disk usage (nothing, per `PhysicalSize`) but not query latency. An
R+-tree with sixteen entries for one region might do sixteen times the lookup work even though the blob underneath is fetched once — plausible, not measured.

**Not every large "unreferenced" collection has been checked for file-referencing.** One (1,268 GB) is confirmed in situ via `RAS_FILETILES`. The next few largest share owners or naming patterns with it but have not been individually checked — run `--check-fileref` (step 4) before treating their GB as certainly recoverable.

**A second, smaller gap between logical size and `PhysicalSize` is unexplained.** Summed across the 273 live coverages, logical size (cells × bytes-per-cell) is 421.9 GB more than `PhysicalSize` — and it isn't the 26 duplicate-index coverages above (their `PhysicalSize` matches logical size
exactly). It sits in 41 other, clean-index coverages where `PhysicalSize` is genuinely smaller than their nominal cell count implies. Why is not investigated; it isn't a disk-recovery opportunity, just a gap worth flagging rather than leaving silent.

**The cause of the tile-index duplication is inferred, not proven.** The evidence — uneven copy counts, and the pattern that iterated `_wcs` variants duplicate while their once-ingested siblings do not — points to `wcst_import` being re-run against a coverage that already exists. The decisive test is to ingest a throwaway coverage, measure it, re-run the same recipe without dropping it, and measure again. **That test has not been run.**

**Read amplification is modelled, not benchmarked.** It is bytes a representative query must read over bytes it returns, derived from the measured tile shape. Sound for ranking; not a substitute for timing a real query. 

**Recommended tiling strings are a starting point.** They target a ~4 MB tile for each coverage's role. Verify each against the source file's real dimension order before ingesting — the tiling bracket is positional and follows `gridOrder`, not `crs`, and getting that backwards is the most common way these go wrong.

**Eleven coverages have a stored array that disagrees with what WCS advertises.** They are identified on the Data Integrity tab but not adjudicated; each needs a human decision about which layer is right.

## One process rule

Never run `wcst_import` against a coverage that already exists. Delete the coverage first, or ingest under a new name and swap. It costs nothing to follow. It no longer prevents lost disk space — Finding 1 showed there wasn't any to lose — but it keeps the tile index clean and avoids finding out the hard way whether a bloated index has some other cost.
