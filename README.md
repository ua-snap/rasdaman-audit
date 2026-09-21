# Rasdaman tiling audit — zeus.snap.uaf.edu

Tools and findings from an audit of every coverage on SNAP's production rasdaman
server: how each one is tiled, how much disk it occupies and why, and whether
its tiling matches how it is actually queried.

Everything here is reproducible. The scripts are Python 3 standard library only
(except the workbook builder, which needs `openpyxl`), read-only against the
server, and re-runnable after any change.

## What we found

| | |
|---|---|
| Coverages | 273, all measured directly |
| On disk, live coverages | 14,000 GB |
| Actual data in them | 9,594 GB |
| **Duplicate tiles** | **4,591 GB across 26 coverages** |
| **Collections nothing references** | **3,523 GB across 105 collections** |
| Total on disk | 17,523 GB |
| **Recoverable** | **8,114 GB — 46% of the server** |

Two things are worth knowing before you read further.

**The excess storage is not a tiling problem.** It is literal duplicate tiles —
the same tile domain stored two, four, even eight times. We proved it by dumping
every tile domain for six coverages and counting them: the *unique* domains
cover each array exactly, to the byte. Tile shape, tile size and tile count do
not affect disk usage at all; the same array exists on this server at 289, 1,172
and 4,678 tiles and occupies 19.011 GB in every case. The remedy is a clean
re-ingest into a fresh collection with the recipe unchanged.

**A coverage ID is not a collection name.** `wcst_import` does not reliably name
the rasdaman collection after the coverage — it may append a timestamp, or move
a `_v2` suffix to the end. 186 of 273 diverge. Any tool that assumes they match
will silently measure the wrong array, or fail outright with `Object Unknown`.
Read the mapping out of `petascopedb` and hand it to the scripts.

Start with **[docs/rasdaman-tiling-guide.md](docs/rasdaman-tiling-guide.md)** —
what a tile is, what it costs, how a recipe becomes stored tiles. Then
**[docs/rasdaman-tiling-audit.md](docs/rasdaman-tiling-audit.md)** for what is
true of our server, and `rasdaman_tiling_audit.xlsx` for the per-coverage
numbers behind it.

## Layout

```
docs/          the guide, the audit, and the figures they reference
scripts/       four read-only audit tools plus the workbook builder
data/          the inputs and outputs of the 2026-09-21 run
data/tile-dumps/   raw tile domains for three coverages, gzipped
rasdaman_tiling_audit.xlsx    seven tabs, all live formulas
```

## Running it yourself

### 0. Credentials

Every script reads the same two environment variables and sends them as HTTP
basic auth. Nothing is ever written to the database.

```bash
export RASDAMAN_USER=rasadmin
export RASDAMAN_PASS='...'
```

### 1. Get the coverage → collection mapping

This is the step that makes everything else work, and it needs read access to
`petascopedb` — ordinary PostgreSQL, no sudo. Set `PGHOST`/`PGPORT`/`PGUSER`
from petascope's own properties file (`/opt/rasdaman/etc/petascope.properties`,
which carries the JDBC URL and credentials), then:

```bash
psql -Atc "
  SELECT c.coverage_id, r.collection_name
    FROM coverage c
    JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id
   ORDER BY 1;" > data/mapping.txt
```

`-At` gives pipe-separated pairs with no header, which every script here reads.
Left column is the coverage ID; right is the rasdaman collection.

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

About 2.5 minutes for 273 coverages. Writes `_summary.csv` (54 columns), a
`_compact/` directory of per-coverage JSON, and `_errors.log`.

`--recipes-ref origin/main` reads recipes from that git ref rather than your
working tree — worth doing, because a checkout on a feature branch will appear
to be missing recipes that exist.

Useful flags: `--limit 5` for a trial run, `--coverage ID` (repeatable) to
target specific coverages, `--verify-tiles-all --verify-max-mb 16` to sample
real tile domains, `--save-raw` to keep the raw `dbinfo` responses.

### 3. Find collections nothing points at

rasdaman's own catalogue lives in a SQLite file, not a database you can connect
to. Compare its collection list against the 273 names in the mapping:

```bash
sqlite3 -readonly /opt/rasdaman/data/RASBASE \
  "SELECT * FROM RAS_MDDCOLLNAMES;" -header -csv > data/rasbase_collections.csv
```

Anything in there but not in `mapping.txt` is unreferenced. Price it:

```bash
python3 scripts/rasdaman_price_collections.py \
  --rasql-url https://zeus.snap.uaf.edu/rasdaman/rasql \
  --csv data/unreferenced_collections.csv \
  --out data/unreferenced_sizes.csv
```

Each row carries a `drop collection` statement **for review, not for running**.
Verify each `oid` against `rasdaman_range_set` before deleting anything.

### 4. Check any coverage for duplicate tiles

The single most useful diagnostic here, and it needs no scripts:

```bash
curl -sS -u "$RASDAMAN_USER:$RASDAMAN_PASS" \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from COLLECTION as c' \
  https://zeus.snap.uaf.edu/rasdaman/rasql > tiles.json

grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate tile. If the top line reads `1`, the coverage
is clean. The `-a` matters: rasql responses contain NUL bytes and GNU grep will
otherwise refuse to read them.

Three worked examples are in `data/tile-dumps/`, gzipped.

### 5. Rebuild the workbook

```bash
pip install openpyxl
python3 scripts/build_workbook.py
```

Reads `data/coverages_summary.csv`, `data/mapping.txt` and
`data/unreferenced_sizes.csv`; writes `rasdaman_tiling_audit.xlsx` at the repo
root. Every Summary figure is a live formula over the other tabs, so the numbers
move when the data does.

## The other two scripts

`rasdaman_collection_reconcile.py` compares petascope's coverage catalogue
against rasdaman's collections and reports where they diverge — matched, name
collision, renamed, or a shadow collection squatting on a live coverage's name.
Run it with `--mapping-file` for authoritative answers, or without for a
no-privilege check that only uses the coverage's own name.

`rasdaman_collection_discover.py` probes candidate naming rules when no mapping
is available. It exists for the case where `petascopedb` is unreachable; if you
have the mapping, you do not need it. `--self-test` runs offline.

Both have `--self-test` and detailed module docstrings.

## Caveats

**The cause of the duplication is inferred, not proven.** The evidence — uneven
copy counts, and the pattern that iterated `_wcs` variants duplicate while their
once-ingested siblings do not — points to `wcst_import` being re-run against a
coverage that already exists. The decisive test is to ingest a throwaway
coverage, measure it, re-run the same recipe without dropping it, and measure
again. **That test has not been run.** Treat the remedy as well-founded rather
than confirmed.

**Read amplification is modelled, not benchmarked.** It is bytes a
representative query must read over bytes it returns, derived from the measured
tile shape. Sound for ranking; not a substitute for timing a real query.

**Recommended tiling strings are a starting point.** They target a ~4 MB tile
for each coverage's role. Verify each against the source file's real dimension
order before ingesting — the tiling bracket is positional and follows
`gridOrder`, not `crs`, and getting that backwards is the most common way these
go wrong.

**Eleven coverages have a stored array that disagrees with what WCS
advertises.** They are identified on the Data Integrity tab but not adjudicated;
each needs a human decision about which layer is right.

## One process rule

Never run `wcst_import` against a coverage that already exists. Delete the
coverage first, or ingest under a new name and swap. That single rule is what
prevents the 4,591 GB from coming back.
