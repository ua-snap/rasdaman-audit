#!/usr/bin/env python3
"""
rasdaman_physical_size.py

The true disk cost of every rasdaman collection, read directly out of
RASBASE's own catalogue -- not derived from rasql's dbinfo().

Why this script exists
-----------------------
dbinfo(c, "printtiles=embedded")'s totalSize field is NOT bytes on disk. It
is computed from the tile index: cells-per-tile x bytes-per-cell x the
number of INDEXED tile entries. If the same tile domain is indexed more than
once -- which happens when wcst_import is re-run against a coverage that
already exists, without dropping it first -- totalSize counts that domain's
bytes once per index entry, not once per unique domain. Six coverages were
dumped tile-by-tile (see data/tile-dumps/) and proved this directly: the
same domain, e.g. [1:3,0:25679,10:11,10:11], appears 2 to 16 times in one
coverage's own tile list, byte-identical every time.

RASBASE carries a second, independent number that does not have this
problem: RAS_MDDOBJECTS.PhysicalSize, one row per stored array object.
Checked against all 26 coverages this project had flagged as "duplicated"
by the totalSize test, PhysicalSize matches the UNIQUE-domain byte count
exactly, every single time -- never the inflated totalSize sum. Summed
across the 273 live, petascope-mapped coverages, it also matches rasdaman's
own UI ("N coverages, total volume") to four significant figures. This
script reads PhysicalSize directly, so nobody has to take that on faith --
run it yourself and compare.

The join
--------
    RAS_MDDCOLLNAMES.MDDCollId                  (the name registry)
        -> RAS_MDDCOLLECTIONS.MDDCollId         (junction table -- note the
           name collision with RAS_MDDCOLLNAMES; easy tables to mix up)
        -> RAS_MDDCOLLECTIONS.MDDId
        -> RAS_MDDOBJECTS.MDDId
        -> RAS_MDDOBJECTS.PhysicalSize

An ordinary WCS coverage's collection holds exactly one array
(n_objects == 1). If you ever see n_objects > 1 for something you expected
to be a single-array coverage, look at it before trusting the number --
that shape wasn't seen anywhere in this audit but nothing rules it out for
a collection this script hasn't looked at yet.

Real disk vs. referenced disk -- the "in situ" problem
--------------------------------------------------------
PhysicalSize is honest about how big the array's data is. It is NOT honest
about whether that data lives inside rasdaman's own storage. wcst_import can
ingest "in situ": instead of copying source file bytes into rasdaman, it
leaves each tile as a pointer into the original NetCDF/GeoTIFF/etc. and
records that pointer in RAS_FILETILES (FilePath, LoadDomain, ...) instead of
writing a blob into RAS_TILES. A collection ingested this way reports a
real, accurate PhysicalSize -- that many bytes of real data genuinely
exist -- but dropping the collection recovers none of rasdaman's own disk,
because rasdaman never held a copy in the first place. This is how one
orphan collection in this audit (cmip6_downscaled_tasmax_complete_
crstephenson_2025_09_22_..., 1,268 GB) turned out to cost roughly nothing to
drop: 163.3 million of its tiles are file references, which is 98% of every
file reference on the whole server.

There is no column that says "this collection is in situ." The only signal
this project found is indirect: RAS_FILETILES.FilePath holds the source
file path for every externally-referenced tile. If a large share of a
suspect collection's declared tile count shows up as FilePath rows
containing that collection's name, its owner's ingest directory, or its
recipe's known source filenames, treat it as in situ and expect near-zero
real recovery from dropping it. This is a heuristic, not a certificate --
see --check-fileref below, and sanity-check its "matching_rows" count
against the collection's own declared tile count before concluding
anything. A cleaner test would walk RAS_HIERIX's per-object index and count
which entries resolve to RAS_TILES vs. RAS_FILETILES directly, but
RAS_HIERIX stores its entries packed into an opaque DynData BLOB with no
public schema for it -- not something plain SQL can decompose.

Usage
-----
    export RASBASE=/opt/rasdaman/data/RASBASE

    # Every collection's real size, split into live vs. orphan via mapping.txt
    python3 rasdaman_physical_size.py \\
        --rasbase "$RASBASE" --mapping-file data/mapping.txt \\
        --out data/physical_sizes.csv

    # Check whether a specific orphan looks file-referenced before trusting
    # its GB figure as "recoverable"
    python3 rasdaman_physical_size.py --rasbase "$RASBASE" \\
        --check-fileref crstephenson

    --self-test runs entirely offline against a small in-memory database
    built to match RASBASE's schema, and touches no network and no real
    RASBASE. Run it after any rasdaman upgrade before trusting this script
    again -- a schema change would fail loudly here first.
"""

import argparse
import csv
import sqlite3


PHYSICAL_SIZE_QUERY = """
SELECT cn.MDDCollName AS collection,
       cn.MDDCollId   AS mdd_coll_id,
       count(*)       AS n_objects,
       sum(o.PhysicalSize) AS physical_bytes
  FROM RAS_MDDCOLLNAMES cn
  JOIN RAS_MDDCOLLECTIONS mc ON mc.MDDCollId = cn.MDDCollId
  JOIN RAS_MDDOBJECTS o      ON o.MDDId = mc.MDDId
 GROUP BY cn.MDDCollName, cn.MDDCollId
 ORDER BY physical_bytes DESC;
"""

SYSTEM_TOTALS_QUERIES = {
    "ras_tiles": "SELECT count(*) FROM RAS_TILES;",
    "ras_filetiles": "SELECT count(*) FROM RAS_FILETILES;",
}


def connect_ro(path):
    # Read-only connection. This script never writes to RASBASE.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def physical_sizes(conn):
    return [dict(r) for r in conn.execute(PHYSICAL_SIZE_QUERY)]


def system_totals(conn):
    return {key: conn.execute(q).fetchone()[0] for key, q in SYSTEM_TOTALS_QUERIES.items()}


def load_mapping(path):
    """coverage_id|collection_name pairs, one per line (psql -At output).
    Tolerates a tab instead of a pipe, and skips a header row if present."""
    mapping = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = [p for p in line.replace("\t", "|").split("|") if p != ""]
            if len(parts) < 2:
                continue
            if parts[0].strip().lower() in ("coverage_id", "coverage"):
                continue
            mapping[parts[0].strip()] = parts[1].strip()
    return mapping


def classify(rows, mapping):
    """Tags each row 'live' (with its coverage_id) or 'orphan', by whether
    some coverage's mapped collection name matches this row's collection."""
    coverage_of = {coll: cov for cov, coll in mapping.items()}
    for r in rows:
        cov = coverage_of.get(r["collection"])
        r["coverage_id"] = cov or ""
        r["status"] = "live" if cov else "orphan"
    return rows


def check_fileref(conn, token):
    """Heuristic only -- see the module docstring's 'in situ' section.
    Counts RAS_FILETILES rows whose FilePath contains the given substring.
    A large, roughly tile-count-matching result is evidence FOR an in-situ
    collection; zero is evidence against it, not proof of blob storage (the
    collection could be in-situ under a path that doesn't contain this
    token). Always compare matching_rows against the collection's own
    declared tile count before concluding anything."""
    q = "SELECT count(*), count(DISTINCT FilePath) FROM RAS_FILETILES WHERE FilePath LIKE ?;"
    n, distinct_files = conn.execute(q, (f"%{token}%",)).fetchone()
    return {"token": token, "matching_rows": n, "distinct_files": distinct_files}


def fmt_gb(n_bytes):
    return (n_bytes or 0) / 1e9


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rasbase", help="Path to the RASBASE sqlite file (read-only)")
    ap.add_argument("--mapping-file",
                     help="coverage_id|collection pairs from petascopedb -- see README step 1")
    ap.add_argument("--out", help="Write the full per-collection CSV here")
    ap.add_argument("--check-fileref", action="append", default=[], metavar="TOKEN",
                     help="Check RAS_FILETILES for this substring (repeatable). "
                          "Prints a heuristic verdict; does not affect --out.")
    ap.add_argument("--self-test", action="store_true",
                     help="Run offline against a synthetic in-memory database and exit")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    if not args.rasbase:
        ap.error("--rasbase is required (or pass --self-test)")

    conn = connect_ro(args.rasbase)

    totals = system_totals(conn)
    print(f"RAS_TILES (stored blobs):    {totals['ras_tiles']:>14,}")
    print(f"RAS_FILETILES (file refs):   {totals['ras_filetiles']:>14,}")

    rows = physical_sizes(conn)
    grand_total = sum(r["physical_bytes"] or 0 for r in rows)
    print(f"\n{len(rows)} collections, {fmt_gb(grand_total):,.1f} GB total physical size (decimal GB)")

    if args.mapping_file:
        mapping = load_mapping(args.mapping_file)
        rows = classify(rows, mapping)
        live = [r for r in rows if r["status"] == "live"]
        orphan = [r for r in rows if r["status"] == "orphan"]
        print(f"  live ({len(live)}, via {args.mapping_file}):   {sum(fmt_gb(r['physical_bytes']) for r in live):>10,.1f} GB")
        print(f"  orphan ({len(orphan)}, unreferenced):    {sum(fmt_gb(r['physical_bytes']) for r in orphan):>10,.1f} GB")
        print("  (an orphan's GB is only recoverable disk if it is NOT file-referenced -- see --check-fileref)")

    if args.out:
        fieldnames = ["collection", "mdd_coll_id", "n_objects", "physical_bytes"]
        if args.mapping_file:
            fieldnames += ["coverage_id", "status"]
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        print(f"\nWrote {args.out}")

    for token in args.check_fileref:
        result = check_fileref(conn, token)
        pct = (result["matching_rows"] / totals["ras_filetiles"] * 100) if totals["ras_filetiles"] else 0
        print(f"\n--check-fileref '{token}': {result['matching_rows']:,} RAS_FILETILES rows "
              f"({pct:.1f}% of all file references on the server), across "
              f"{result['distinct_files']} distinct source files.")
        print("  Heuristic substring match, not a certified answer -- see the module docstring.")


def self_test():
    """Builds a tiny in-memory database matching RASBASE's schema (confirmed
    against the real server's own .schema output, 2026-09-21/22) and checks
    the join, the system totals, live/orphan classification, and the
    file-reference heuristic against known-good numbers. No network, no
    real RASBASE."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE RAS_MDDCOLLNAMES (MDDCollId INTEGER, SetTypeId INTEGER, MDDCollName TEXT);
        CREATE TABLE RAS_MDDCOLLECTIONS (MDDId BIGINT, MDDCollId BIGINT);
        CREATE TABLE RAS_MDDOBJECTS (MDDId INTEGER, BaseTypeOId BIGINT, DomainId INTEGER,
            PersRefCount INTEGER, StorageOId BIGINT, NodeOId BIGINT, PhysicalSize BIGINT DEFAULT 0);
        CREATE TABLE RAS_FILETILES (TileId INTEGER, FilePath TEXT, FileType INTEGER,
            LoadDomain TEXT, ExtraParameters TEXT);
        CREATE TABLE RAS_TILES (BlobId INTEGER, DataFormat INTEGER,
            BandLinearization TEXT, CellLinearization TEXT);

        INSERT INTO RAS_MDDCOLLNAMES VALUES (1, 59, 'clean_coverage');
        INSERT INTO RAS_MDDCOLLECTIONS VALUES (101, 1);
        INSERT INTO RAS_MDDOBJECTS VALUES (101, 9, 1, 1, 500, NULL, 52018435200);

        INSERT INTO RAS_MDDCOLLNAMES VALUES (2, 59, 'insitu_orphan');
        INSERT INTO RAS_MDDCOLLECTIONS VALUES (102, 2);
        INSERT INTO RAS_MDDOBJECTS VALUES (102, 9, 1, 1, 501, NULL, 1268025125604);
        -- pretend every tile in insitu_orphan is a file reference
        INSERT INTO RAS_FILETILES VALUES (1, '/data/crstephenson/tasmax_a.nc', 1, '[0:0]', '');
        INSERT INTO RAS_FILETILES VALUES (2, '/data/crstephenson/tasmax_b.nc', 1, '[0:0]', '');
        INSERT INTO RAS_TILES VALUES (1, 1, '', '');
    """)
    conn.row_factory = sqlite3.Row

    rows = physical_sizes(conn)
    assert len(rows) == 2, rows
    by_name = {r["collection"]: r for r in rows}
    assert by_name["clean_coverage"]["physical_bytes"] == 52018435200
    assert by_name["insitu_orphan"]["physical_bytes"] == 1268025125604

    totals = system_totals(conn)
    assert totals["ras_tiles"] == 1
    assert totals["ras_filetiles"] == 2

    classified = classify(rows, {"clean_coverage": "clean_coverage"})
    statuses = {r["collection"]: r["status"] for r in classified}
    assert statuses["clean_coverage"] == "live"
    assert statuses["insitu_orphan"] == "orphan"

    fr = check_fileref(conn, "crstephenson")
    assert fr["matching_rows"] == 2 and fr["distinct_files"] == 2
    fr_none = check_fileref(conn, "nonexistent_owner")
    assert fr_none["matching_rows"] == 0

    print("self-test OK: join, system totals, live/orphan classification, "
          "and file-reference heuristic all match expected values")


if __name__ == "__main__":
    main()
