# Verifying coverages and auditing disk

Every command here is read-only. Host-specific values are for Zeus:
- rasql: `https://zeus.snap.uaf.edu/rasdaman/rasql`
- OWS: `https://zeus.snap.uaf.edu/rasdaman/ows`
- RASBASE: `/opt/rasdaman/data/RASBASE`

Credentials come from env vars: `RASDAMAN_USER`/`RASDAMAN_PASS` for rasql, and `PGHOST`/`PGPORT`/`PGUSER` plus the petascope password for `petascopedb`.

## Contents
1. Coverage → collection mapping
2. `dbinfo` and what its fields mean
3. Five post-ingest checks
4. Credential-free WCPS probe
5. Duplicate tile-index entries
6. Real disk: RASBASE `PhysicalSize`
7. In-situ (file-referenced) collections
8. Orphaned collections and safe deletion
9. The audit scripts (rasdaman-audit repo)

## 1. Coverage → collection mapping

Do this first. Every other step needs the real collection name.

```bash
psql -Atc "
  SELECT c.coverage_id, r.collection_name
    FROM coverage c
    JOIN rasdaman_range_set r ON r.rasdaman_range_set_id = c.rasdaman_range_set_id
   ORDER BY 1;" > mapping.txt
```

The output is pipe-separated: `coverage_id|collection_name`. If the table names differ on a different build, search `information_schema.columns` for columns matching `%collection%` or `%oid%`. Don't guess.

Known divergence patterns: `wcst_import` appends a timestamp (`cmip6_fwi_2026_05_01_09_29_49_8758`), and the v2 family moves `_v2` to the end (`..._historical_v2_wcs` → `..._historical_wcs_v2`). Twelve collections sit at a live coverage's name while holding something else. A successful `dbinfo` is not proof you queried the right array.

## 2. `dbinfo`

```bash
curl -sS -u "$RASDAMAN_USER:$RASDAMAN_PASS" \
  --data-urlencode 'query=select dbinfo(c) from COLLECTION as c' \
  https://zeus.snap.uaf.edu/rasdaman/rasql
```

The fields that matter:
- **`baseType`** lists every band and its type. Use it to get the true bytes per cell.
- **`tileNo`** counts tiles in the index, *including duplicate entries*.
- **`totalSize`** is the sum over index entries. **It is not disk.**
- **`tiling.tileConfiguration`** is the tile shape. It may contain `0:*`, so parse it accordingly or every later position shifts.
- **`index.type`**: `rpt_index` means an R+-tree.

`sdom` and `tileConfiguration` are in **storage order**, while `DescribeCoverage` is in catalogue order. Never pair one's axis names with the other's numbers.

`dbinfo(c,"printtiles=embedded")` lists every tile domain. On coverages with millions of tiles the response can arrive truncated, so write it to a file with `-o` rather than piping it into `head`.

## 3. Five post-ingest checks

1. **Did it build the grid I asked for?** Compare the product of ⌈extent ÷ chunk⌉ over all axes with `tileNo`. Equal is good, and lower (sparse) is fine. Higher means nothing on its own, because wildcards make the geometric count meaningless.
2. **Duplicate index entries?** Compute `totalSize ÷ (cells × bytes_per_cell)`. Above ~1.05 means duplicate entries, which suggests `wcst_import` was re-run. That's a hygiene signal, not a disk problem.
3. **Remainders?** Check `extent % chunk` on *each axis*. Only a per-axis check catches transposed chunk sizes.
4. **Which axis is in which bracket position?** Read `tiling.tileDomains` and match the distinct values at each position against known axis cardinalities. A position cycling through 2–3 values is a small categorical axis.
5. **Is this even the right array?** Cross-check the domain with the WCPS probe (section 4) and the mapping (section 1).

Also spot-check one known coordinate's value against the source netCDF.

## 4. Credential-free WCPS probe

A deliberate type error makes rasdaman describe the stored array. It needs no rasql credentials and no collection name:

```
for $c in (COVERAGE_ID) return encode($c * "x", "csv")
```

POST it to `/rasdaman/ows` with `request=ProcessCoverages`. The exception text carries the truth, e.g. `ARRAY (float) [D0(0:18249),D1(0:442),D2(0:459)] null values [-9999.000000]`. This is the most authoritative description of what is stored.

## 5. Duplicate tile-index entries

```bash
curl -sS -u "$RASDAMAN_USER:$RASDAMAN_PASS" \
  --data-urlencode 'query=select dbinfo(c,"printtiles=embedded") from COLLECTION as c' \
  https://zeus.snap.uaf.edu/rasdaman/rasql -o tiles.json
grep -a -o '"\[[-0-9:,]*\]"' tiles.json | sort | uniq -c | sort -rn | head
```

Any count above 1 is a duplicate index entry, and a top count of `1` means the index is clean. You need `-a` because rasql responses contain NUL bytes. Duplicates cost **no disk**: `PhysicalSize` matched the unique-domain bytes in all 26 affected coverages. Their latency cost is untested. The likely cause, inferred but not proven, is `wcst_import` being re-run against an existing coverage: the `_wcs` variants that were iterated during development carry duplicates, while once-ingested siblings are clean.

## 6. Real disk: RASBASE `PhysicalSize`

```bash
sqlite3 -readonly /opt/rasdaman/data/RASBASE "
SELECT cn.MDDCollName, count(*) n_objects, sum(o.PhysicalSize) physical_bytes
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 GROUP BY cn.MDDCollName ORDER BY physical_bytes DESC;"
```

`RAS_MDDCOLLECTIONS` is a junction table, and its name is easy to confuse with `RAS_MDDCOLLNAMES`. A normal coverage's collection has `n_objects == 1`. RASBASE is a SQLite file, so always open it with `-readonly`. Example: `cmip6_fwi` reports `totalSize` 175.9 GB but `PhysicalSize` 52.0 GB, and 52.0 GB is real.

`PhysicalSize` can be *below* logical size (cells × bytes). 41 clean coverages total 421.9 GB under, cause unexplained. That's not a recovery opportunity, just a known gap.

## 7. In-situ (file-referenced) collections

`wcst_import` can leave tiles as pointers into source files (`RAS_FILETILES`) instead of blobs (`RAS_TILES`). `PhysicalSize` is still reported honestly, but dropping such a collection frees no rasdaman disk.

```bash
sqlite3 -readonly /opt/rasdaman/data/RASBASE "SELECT count(*) FROM RAS_TILES;"       # real blobs
sqlite3 -readonly /opt/rasdaman/data/RASBASE "SELECT count(*) FROM RAS_FILETILES;"   # file refs
sqlite3 -readonly /opt/rasdaman/data/RASBASE \
  "SELECT count(*) n, FilePath FROM RAS_FILETILES GROUP BY FilePath ORDER BY n DESC LIMIT 15;"
```

No column says "in situ", and the per-collection index (`RAS_HIERIX.DynData`) is an opaque BLOB. The check is a heuristic: substring-match an owner or source-directory token against `FilePath`, then compare the match count with the collection's own declared tile count before concluding anything. `scripts/rasdaman_physical_size.py --check-fileref TOKEN` automates it.

## 8. Orphaned collections and safe deletion

An orphan is any name in `RAS_MDDCOLLNAMES` that is absent from the mapping's right-hand column.

```bash
sqlite3 -readonly /opt/rasdaman/data/RASBASE "SELECT * FROM RAS_MDDCOLLNAMES;" -header -csv
```

Before *proposing* a `drop collection` for a human to run:
1. Confirm nothing in `rasdaman_range_set` references it. Check both by name and by its object id.
   - The audit never recorded the oid column's exact name. Find it rather than guessing:
     ```bash
     psql -Atc "SELECT column_name FROM information_schema.columns WHERE table_name='rasdaman_range_set';"
     ```
   - Compare against the collection's ids from RASBASE: `MDDCollId` from `RAS_MDDCOLLNAMES` (the audit's orphan CSVs carry it as `mdd_coll_id`), and `MDDId` via `RAS_MDDCOLLECTIONS`.
   - A row matching by name **or** by id means the collection is not an orphan.
2. Price it by `PhysicalSize`, not `totalSize`. Pricing by `totalSize` once overstated orphans by ~218 GB.
3. For anything above a few GB, run the in-situ check (section 7).
4. Treat 4-byte stubs from failed ingests as free to drop but still reviewed.

**Whether a drop actually frees disk is unmeasured on Zeus.**
- **Where the data lives.** Tile blobs sit in a TILES directory on the filesystem, not inside the RASBASE SQLite file; the audit's `du -sh` of that directory was 9.9 TiB. So dropping a collection should release blob files, while RASBASE itself is small and only shrinks after a `VACUUM`.
- **What hasn't been checked.** The audit never dropped anything, so it's unconfirmed whether the space comes back immediately or only after some later cleanup.
- **How to confirm it.** Before relying on any "recoverable GB" total, have whoever runs the first real drop record `df -h` on the data volume and `du -sh` of the TILES directory before and after, and compare the change with that collection's `PhysicalSize`.

How orphans arise: a re-ingest into a live coverage ID mints a new timestamped collection and moves petascope's pointer to it. The old collection is stranded, and no coverage ID is left for `delete_coverage.sh` to target. Separately, a WCS-T `DeleteCoverage` can delete the petascope row while the rasql `drop` fails, for example under concurrent load (rasdaman ticket #1466, unconfirmed on our version). `delete_coverage.sh` discards the response body, so after any delete, confirm the collection is gone from `RAS_MDDCOLLNAMES`.

## 9. Audit scripts (in `rasdaman-audit/scripts/`)

All are Python 3 stdlib and read-only. All but the workbook builder support `--self-test`; run it after any rasdaman upgrade.

| Script | Purpose | Typical call |
|---|---|---|
| `rasdaman_tiling_audit.py` | Per-coverage tiling, dbinfo, recipe and amplification census. Takes ~2.5 min for 273 coverages and writes `_summary.csv`, `_compact/` and `_errors.log`. | `--url '<WCS GetCapabilities URL>' --outdir ~/tiling_audit --recipes /path/to/rasdaman-ingest --recipes-ref origin/main --mapping-file mapping.txt --wcps-domains` (add `--limit 5` or `--coverage ID` for a trial run) |
| `rasdaman_physical_size.py` | Real disk per collection, the live/orphan split, and the in-situ heuristic | `--rasbase /opt/rasdaman/data/RASBASE --mapping-file mapping.txt --out physical_sizes.csv` / `--check-fileref TOKEN` |
| `rasdaman_price_collections.py` | Prices a list of collections and emits *review-only* drop statements | `--rasql-url ... --rasbase ... --csv unreferenced_collections.csv --out unreferenced_sizes.csv` |
| `rasdaman_collection_reconcile.py` | Petascope vs. rasdaman divergence: matched, collision, renamed, shadow | `--url ... --outdir ... --mapping-file mapping.txt` |
| `rasdaman_collection_discover.py` | Guesses naming rules when `petascopedb` is unreachable | Not needed if you have the mapping |
| `build_workbook.py` | Rebuilds `rasdaman_tiling_audit.xlsx` (needs `openpyxl`) | Reads `data/*.csv` |

After any re-tile, delete or re-ingest, re-run the audit and `physical_size` for the affected coverages.
