# Coverage metadata that breaks encoding: the word "into"

If a coverage's metadata contains the word **"into"** with a space on each side, every request that *encodes* the coverage returns **HTTP 200 with an empty body**: GetCoverage as JSON, GeoTIFF or netCDF, and WCPS `encode()`. Requests that don't encode keep working, so the coverage looks healthy until a client tries to parse nothing. To fix it, reword the metadata, delete the coverage and re-ingest.

This isn't a tiling problem. It lives here because it was found on Zeus while ingesting, and the check for it belongs in the same pre- and post-ingest routine as the [tiling guide](rasdaman-tiling-guide.md)'s Sections 9 and 10.

---

## 1. The rule

**Don't write "into" anywhere in coverage metadata.** That includes recipe `metadata`, and every attribute of every netCDF variable that wcst_import copies across (Section 4). Usually "to" or "in" works, or rephrase the sentence:

| Instead of | Write |
|---|---|
| flux of water into the atmosphere | flux of water to the atmosphere |
| does not take fuel type into account | does not account for fuel type |
| the rule turns the three classes into the hazard class | the rule then gives the hazard class |

The exact trigger is narrower than "the word into" (Section 3). But rewording every "into" costs nothing, and you don't have to know how wcst_import or petascope reformats whitespace on the way in.

No other rasql keyword matters. `insert`, `values`, `drop`, `create`, `update`, `delete`, `alter`, `commit`, `select`, `from` and `where` were all tested, and none of them has any effect (Section 3). 17 live coverages on Zeus use "values" in their descriptions, and all of them encode fine.

---

## 2. Symptoms

- GetCoverage with `format=application/json`, `image/tiff` or `application/netcdf` returns HTTP 200, `Content-Length: 0` and the correct `Content-Type`, with no exception report.
- Front ends fail when they parse the empty response, with errors that point nowhere near the cause. One example: `TypeError: Cannot read properties of null (reading 'split')`.
- **These still work**, which is what makes it confusing:
  - DescribeCoverage;
  - WMS GetMap;
  - GetCoverage with no `format` (GML);
  - WCPS queries that return a scalar without `encode()`, e.g. `$c[X(..), Y(..)]`;
  - WCPS `encode()` of a coverage built inside the query (`coverage n over $k x(0:2) values ...`), which has no stored metadata.
- The same request against a different coverage works.

---

## 3. What triggers it (measured on Zeus, 2026-11-11)

WCPS `encode()` accepts a `metadata` object in its format parameters. That lets you reproduce the bug without ingesting anything: build a three-cell coverage inside the query and attach a description:

```bash
probe() {
  curl -sS -o /dev/null -w '%{http_code} %{size_download} bytes\n' \
    https://zeus.snap.uaf.edu/rasdaman/ows \
    --data-urlencode service=WCS --data-urlencode version=2.0.1 \
    --data-urlencode request=ProcessCoverages \
    --data-urlencode "query=for \$c in (dot_precip) return encode(coverage n over \$k x(0:2) values 1, \"application/json\", \"{\\\"metadata\\\": {\\\"description\\\": \\\"$1\\\"}}\")"
}
probe 'flux of water to the atmosphere'     # 200 7 bytes  ([1,1,1])
probe 'flux of water into the atmosphere'   # 200 0 bytes
```

The `for $c in (dot_precip)` clause is never used, but the WCPS parser rejects the query without it. The query only reads data.

Results for each description string, with format `application/json` unless noted:

| Empty body (broken) | Encodes normally |
|---|---|
| `a into b` | `into` |
| `a INTO b`, `a Into b` | `into b` |
| `take into account` | `b into` |
| `from a into b` | `insert into`, `values into` |
| ` into b` (leading space) | `a`*tab*`into`*tab*`b`, `a`*newline*`into`*newline*`b` |
| `, into b` | `a into`*tab*`b`, `a`*tab*`into b` |
| `a  into  b` (two spaces) | `(into b`, `a.into b`, `a-into-b`, `a_into_b` |
| `flux into air` as `application/netcdf` | `intoxicated`, `onto the grid` |
| | `insert these values`, `drop the first year`, `create a table`, `update the record`, `delete old rows`, `alter the units`, `commit to it`, `select a from b where c` |

**The trigger is the literal text ` into `**: an ordinary ASCII space, then "into" in any case, then another ASCII space. A tab, a newline or punctuation on either side doesn't trigger it, and no other keyword does either.

### Likely mechanism (inferred, not confirmed in source)

- Petascope puts the coverage's metadata into the rasql query it builds for `encode()`, inside the format options. That's why only encoding requests break, and why a coverage built inside the query breaks only once metadata is attached.
- `SELECT ... INTO coll` and `INSERT INTO coll ...` are rasql write statements. The space-on-both-sides pattern looks like a plain substring check (`" into "`) that classifies the whole query as a write. A write returns no array, so the response is an empty 200 rather than an error.

Both points fit every result above. Neither has been checked against the Petascope source or the Petascope and rasserver logs. The Petascope log line for one failing request would settle it.

---

## 4. Where metadata comes from: check all of it

wcst_import stores more than the recipe's `metadata.global`:

- **Band metadata.** With a netCDF slicer and no explicit `metadata.bands` in the recipe, wcst_import copies **every attribute of each band variable** (`description`, `comment`, `long_name`, ...). The CMIP6 `evspsbl` and `isi` descriptions got in this way.
- **Axis metadata.** Attributes of the x/y (lat/lon) variables, and of the grid mapping variable (`crs_wkt`, ...).
- **Global metadata.** Whatever the recipe sets. If the recipe sets nothing, the netCDF global attributes.

So check the netCDF attributes as well as the recipe.

---

## 5. Checking

`scripts/rasdaman_metadata_check.py` is standard-library Python and read-only, and needs no credentials. It reports every whole-word "into" and marks the occurrences that have the exact trigger (`TRIGGER`). The rest are marked `word`: harmless as measured, but still worth rewording.

**Before ingest.** Scan the recipe and every netCDF attribute. The script exits 1 if it finds anything, so it can gate an ingest script:

```bash
ncdump -h source.nc | python3 scripts/rasdaman_metadata_check.py --text -
python3 scripts/rasdaman_metadata_check.py --text recipe.json
```

**After ingest, or for any coverage already on the server:**

```bash
python3 scripts/rasdaman_metadata_check.py --coverage COVERAGE_ID --probe all
python3 scripts/rasdaman_metadata_check.py --out ~/metadata_check.csv   # every coverage, ~1 min
```

The script runs DescribeCoverage and searches each text value in `<gmlcov:metadata>`. Then it **probes**: a WCPS `encode()` of one cell (the grid origin, using index-CRS subsets so time and irregular axes need no coordinate formatting), as JSON. HTTP 200 with an empty body means the coverage is `AFFECTED`. By default only coverages with a hit are probed. Use `--probe all` to probe everything, or `--probe none` to skip probing.

The probe is the real test, because it doesn't depend on reading the metadata correctly. If a coverage returns empty encodes and the scan finds no "into", the metadata trigger isn't the cause. Look elsewhere.

`--self-test` runs offline.

**Server status, 2026-11-11.** 294 coverages were scanned with `--probe all`. None has "into" in its metadata. The coverages that were hit earlier had all been fixed by then (Section 7). The probe encoded 292 of them. The other two, `conus_hydro_segments_jp_test` and `conus_hydro_segments_jp_test_insitu`, returned HTTP 500 `Referenced file not found or not readable`. Those are in-situ coverages whose source files have gone (compare the audit's [Finding 5](rasdaman-tiling-audit.md)), and the failure is unrelated to metadata.

---

## 6. Fixing an affected coverage

1. Reword the text at its source: the recipe, or the netCDF attribute (in a copy of the file, never the source dataset).
2. Run the `--text` check again on the recipe and on the new `ncdump -h`.
3. **Delete the coverage and re-ingest.** Don't run wcst_import again on top of the existing coverage. That duplicates tile-index entries ([tiling guide](rasdaman-tiling-guide.md), Section 3.3; [README](../README.md#one-process-rule)).
4. Run `--coverage ID --probe all` and check that the result is `ok`.

---

## 7. Where it has been hit

| Coverage / variable | Text that broke it | Fix |
|---|---|---|
| CMIP6 monthly `evspsbl` | "Evaporation at surface (also known as evapotranspiration): flux of water **into** the atmosphere due to conversion of both liquid and solid phases to vapor (from underlying surface and vegetation)" | Shortened to "Evaporation at surface (also known as evapotranspiration)." (rasdaman-ingest `3378ac427a`, 2025-10-30, `ardac/cmip6_common_grid/monthly/luts.py`) |
| CMIP6 fire weather `isi` | "...Like the rest of the FWI system components, ISI does not take fuel type **into** account. ..." | "The Initial Spread Index (ISI) is a numeric rating of the expected rate of fire spread, based on wind speed and FFMC." (ardac `d924132e43`, 2025-10-23, `curation/cmip6_fwi/combine.ipynb`) |
| `environmental_hazard_index_test` (Abstract) | "...the combining rule turns the three classes **into** the hazard class..." | "...the combining rule then gives the hazard class..." |

Nobody knew why the first two fixes worked when they were made. Each one removed a sentence containing " into ", and nothing else these descriptions have in common explains the failure. Coverages that work rule out the other candidates:

- **Length.** The BUI description is 297 characters and works; the broken ISI one was 254.
- **Parentheses, colons, commas.** All appear in descriptions that work.
- **Double quotes.** DOT's `crs_wkt` is full of them, and it works.

`environmental_hazard_index_test` was ingested three times on 2026-11-10 and 11, and on its own it doesn't prove the cause:

| Ingest | Metadata | Encode |
|---|---|---|
| 1 | "into" in the Abstract; `\uXXXX` escapes; a JSON object and a long comment in each band's metadata (from netCDF band attributes) | empty |
| 2 | Escapes removed; "into" and the band JSON/comments still there | empty |
| 3 | "into" and "values" removed, **and** band attributes cut down to `long_name`, `flag_values`, `flag_meanings`, `grid_mapping` | works |

Ingest 3 changed two things at once. The proof is the reproduction in Section 3, which isolates " into " with nothing else changed.

---

## 8. Other things seen during the same work

These turned up during the same ingest and weren't investigated further here. They're recorded so nobody has to rediscover them.

- **JSON no-data.** A multi-band point GetCoverage returns the bands as one space-separated string, in band order (`"1 0 0 0"`). A nil band appears as the word `null` inside the string (`"2 1 null 1"`), and an all-nil cell is `"null null null null"`.
- **GeoTIFF transposed for coverages stored Y first.** The values are right, but the rows hold X while the georeferencing says they hold Y. Seen on `crrel_gipl_outputs_nc_wcs` (gridOrder Y 3, X 4) and `environmental_hazard_index_test` (Y 0, X 1), each by comparing a 9 × 9 window to the source file. SNAP's CMIP6 prep scripts transpose to X-first before ingest ("for Rasdaman WMS compatibility"), which probably avoids it (not tested). For axis order in general, see the [tiling guide](rasdaman-tiling-guide.md) and [CRREL_GIPL_tiling.md](CRREL_GIPL_tiling.md) Section 2.
- **WMS outside EPSG:3338.** WMS output reprojected to EPSG:3857 or EPSG:4326 is off by several km, for GIPL as well. Measured against a PROJ reprojection, 3857 output agrees 57–62 %. Two correct renderings half a pixel apart agree 89 %. EPSG:3338 output matched the source 100 %.
- **Default WMS style.** With `styles=` empty, Zeus appears to use the first style the ingest hooks created. Set the default style explicitly after each ingest.
