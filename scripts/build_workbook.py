#!/usr/bin/env python3
"""Build rasdaman_tiling_audit.xlsx from the CSVs in ../data.

Inputs  : data/coverages_summary.csv   (rasdaman_tiling_audit.py --outdir)
          data/mapping.txt             (petascopedb coverage -> collection)
          data/physical_sizes.csv      (rasdaman_physical_size.py --out)
          data/unreferenced_collections.csv  (category/why per orphan)
          data/unreferenced_sizes.csv  (rasdaman_price_collections.py; sdom/oid/tiles)
Output  : rasdaman_tiling_audit.xlsx at the repo root.
Requires: openpyxl.

Disk-size columns come from RAS_MDDOBJECTS.PhysicalSize (physical_sizes.csv),
NOT dbinfo's totalSize. See docs/rasdaman-tiling-audit.md's Method section and
Finding 1 for why: totalSize sums the tile INDEX, including duplicate entries,
and is not a disk read at all. PhysicalSize is computed per stored object and
matches rasdaman's own UI to four significant figures.
"""
import csv, math, os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUM   = os.path.join(ROOT, "data", "coverages_summary.csv")
MAP   = os.path.join(ROOT, "data", "mapping.txt")
PHYS  = os.path.join(ROOT, "data", "physical_sizes.csv")
UNREF_CAT = os.path.join(ROOT, "data", "unreferenced_collections.csv")
UNREF_SIZES = os.path.join(ROOT, "data", "unreferenced_sizes.csv")
OUT   = os.path.join(ROOT, "rasdaman_tiling_audit.xlsx")

TARGET = 4194304          # bytes: middle of rasdaman's 1-4 MB guidance
WMS_TARGET = 16777216

# The one orphan confirmed (via RAS_FILETILES) to be almost entirely
# file-referenced rather than stored -- see Finding 5. Its PhysicalSize is
# real data, not real rasdaman disk. Flagged here rather than silently
# excluded, so the spreadsheet doesn't quietly know something the docs don't.
CONFIRMED_FILE_REFERENCED = {
    "cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874",
}


def I(v):
    try: return int(str(v).replace(",", "").strip())
    except Exception: return 0

def F(v):
    try: return float(str(v).strip())
    except Exception: return None

def ints(s):
    return [int(x) for x in str(s).split(",") if x.strip().lstrip("-").isdigit()]


# ---------------------------------------------------------------- load
rows = list(csv.DictReader(open(SUM)))
coll = dict(l.strip().split("|") for l in open(MAP) if "|" in l)
phys = {r["collection"]: I(r["physical_bytes"]) for r in csv.DictReader(open(PHYS)) if r["collection"]}
unref_cat = {r["collection"]: r for r in csv.DictReader(open(UNREF_CAT))}
unref_extra = {r["collection"]: r for r in csv.DictReader(open(UNREF_SIZES))}

SPATIAL = {"x", "y", "lat", "lon", "latitude", "longitude", "e", "n"}


def split_axes(axes, extents):
    """Return (spatial index positions, non-spatial index positions)."""
    sp = [i for i, a in enumerate(axes) if a.strip().lower() in SPATIAL]
    if len(sp) != 2:                       # fall back: the two largest trailing axes
        sp = sorted(range(len(extents)), key=lambda i: -extents[i])[:2]
        sp.sort()
    ns = [i for i in range(len(extents)) if i not in sp]
    return sp, ns


def recommend(r):
    """(bracket, tile size, projected point amp, projected map amp, note)."""
    ext = ints(r["grid_extents"])
    axes = [a for a in (r["grid_axes"] or "").split() if a]
    bpc = I(r["bytes_per_cell"]) or 4
    if not ext or len(axes) != len(ext):
        return "", "", None, None, "axis names unavailable"
    sp, ns = split_axes(axes, ext)
    ns_cells = 1
    for i in ns:
        ns_cells *= ext[i]
    role = "map" if r["name_suffix"] == "wms" else "point"

    if role == "point":
        budget = TARGET // (ns_cells * bpc) if ns_cells * bpc else 1
        side = max(1, int(math.isqrt(max(1, budget))))
        side = min(side, min(ext[sp[0]], ext[sp[1]]))
        chunk = {i: ext[i] for i in ns}
        chunk[sp[0]] = side
        chunk[sp[1]] = side
        p_amp = side * side
    else:
        per = 1
        for i in ns:
            per *= 1
        budget = WMS_TARGET // (per * bpc)
        side = max(1, int(math.isqrt(max(1, budget))))
        side = min(side, max(ext[sp[0]], ext[sp[1]]))
        chunk = {i: 1 for i in ns}
        chunk[sp[0]] = min(side, ext[sp[0]])
        chunk[sp[1]] = min(side, ext[sp[1]])
        p_amp = chunk[sp[0]] * chunk[sp[1]] * ns_cells

    tile_cells = 1
    for i in range(len(ext)):
        tile_cells *= chunk[i]
    frame = ext[sp[0]] * ext[sp[1]]
    n_tiles = math.ceil(ext[sp[0]] / chunk[sp[0]]) * math.ceil(ext[sp[1]] / chunk[sp[1]])
    m_amp = (n_tiles * tile_cells) / frame if frame else None

    bracket = "[" + ", ".join("0:%d" % (chunk[i] - 1) for i in range(len(ext))) + "]"
    return ("ALIGNED %s tile size %d" % (bracket, tile_cells * bpc),
            tile_cells * bpc, p_amp, m_amp,
            "role: %s" % role)


def batch_of(cid):
    if cid.startswith("cmip6_downscaled_") and cid.endswith("_v2_wcs"):
        return "cmip6_downscaled v2 — 171 coverages, ONE re-tiling decision"
    if cid in ("cmip6_bui", "cmip6_dc", "cmip6_dmc", "cmip6_ffmc",
               "cmip6_fwi", "cmip6_isi"):
        return "fire weather — 6 coverages, identical, one decision"
    if cid.startswith("era5_4km_daily_"):
        return "era5 4km daily — one decision per wcs/base pair"
    if cid.startswith(("conus_hydro_segments", "ak_hydro_segments")):
        return "hydro segments"
    if cid.startswith("tas_2km") or cid.startswith("ardac_"):
        return ""
    return ""


# ---------------------------------------------------------------- derive
recs = []
for r in rows:
    cid = r["coverage_id"]
    collection = coll.get(cid, cid)
    declared = I(r["total_size_bytes"])          # dbinfo totalSize -- inflated by duplicate index entries
    logical = I(r["real_data_bytes"])             # cells x bytes-per-cell
    real = phys.get(collection)                   # RAS_MDDOBJECTS.PhysicalSize -- the real disk figure
    real_note = ""
    if real is None:
        real = declared
        real_note = "no PhysicalSize row found -- falling back to totalSize; re-run rasdaman_physical_size.py"
    idx_dup = max(0, declared - real)              # index-entry inflation, NOT extra disk
    ovf = F(r["storage_overhead_factor"])          # declared/logical, kept for reference
    role = "map" if r["name_suffix"] == "wms" else "point"
    cur = F(r["map_query_amplification"] if role == "map"
            else r["point_query_amplification"])
    tiling, tsize, p_amp, m_amp, note = recommend(r)
    proj = m_amp if role == "map" else p_amp
    improve = (cur / proj) if (cur and proj) else None

    action = "re-tile" if (improve and improve >= 2) else "none"

    imp = improve or 0
    real_gb = real / 1e9
    if action == "none":
        pri = "P4"
    elif imp >= 20 and real_gb >= 20:
        pri = "P1"
    elif imp >= 10:
        pri = "P2"
    elif imp >= 2:
        pri = "P3"
    else:
        pri = "P4"

    full_note = r["note"]
    if real_note:
        full_note = (full_note + "; " if full_note else "") + real_note

    recs.append(dict(
        cid=cid, coll=collection,
        suffix=r["name_suffix"], test=r["is_test_named"],
        recipe=r["recipe_path"], axes=r["grid_axes"], ext=r["grid_extents"],
        bands=I(r["band_count"]), bpc=I(r["bytes_per_cell"]),
        declared=r["declared_tiling"], cfg=r["tile_configuration"],
        tex=r["tile_extents"], tiles=I(r["tile_no"]),
        logical=round(logical / 1e9, 3),
        declared_gb=round(declared / 1e9, 3),
        real_gb=round(real_gb, 3),
        ovf=ovf, idx_dup=round(idx_dup / 1e9, 3),
        idx_verdict=("duplicate entries" if (ovf or 0) > 1.05 else "clean"),
        p_amp=F(r["point_query_amplification"]), m_amp=F(r["map_query_amplification"]),
        role=role, cur=cur, tiling=tiling, proj=proj,
        improve=round(improve, 1) if improve else None,
        action=action, pri=pri,
        batch=batch_of(cid),
        mism=r["mismatch_confidence"], wvd=r["wcps_vs_describe"],
        dam=r["dbinfo_array_matches"], note=full_note))

recs.sort(key=lambda r: (["P1", "P2", "P3", "P4"].index(r["pri"]),
                         -(r["improve"] or 0), -(r["real_gb"] or 0)))

# ---------------------------------------------------------------- style
Fn = "Arial"
H_FILL = PatternFill("solid", fgColor="1F3864")
H_FONT = Font(name=Fn, bold=True, color="FFFFFF", size=10)
TITLE = Font(name=Fn, bold=True, size=14, color="1F3864")
SUB = Font(name=Fn, bold=True, size=11, color="1F3864")
BODY = Font(name=Fn, size=10)
NOTE = Font(name=Fn, size=9, italic=True, color="595959")
P1F = PatternFill("solid", fgColor="F8CBAD")
P2F = PatternFill("solid", fgColor="FFE699")
BAD = PatternFill("solid", fgColor="FFC7CE")
GOOD = PatternFill("solid", fgColor="D6E9D5")

wb = Workbook()


def header(ws, cols, row=1):
    for i, (name, w) in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=name)
        c.font, c.fill = H_FONT, H_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=2)
    ws.auto_filter.ref = ws.dimensions


COLS = [("coverage_id", 44), ("rasdaman collection", 44), ("suffix", 8),
        ("test?", 7), ("recipe path", 34), ("axes (storage order)", 24),
        ("grid extents", 24), ("bands", 7), ("bytes/cell", 9),
        ("declared tiling", 34), ("tile config", 22), ("tile extents", 20),
        ("tiles (indexed)", 13), ("data GB", 10),
        ("declared GB (totalSize)", 15), ("real disk GB (PhysicalSize)", 17),
        ("index overhead x", 13), ("index dup weight GB", 15), ("index", 15),
        ("point amp", 12), ("map amp", 12), ("role", 8),
        ("amp for role", 13), ("recommended tiling", 46),
        ("projected amp", 13), ("improvement x", 13), ("action", 14),
        ("priority", 9), ("batch", 50), ("axis mismatch", 12), ("wcps vs catalogue", 16),
        ("dbinfo array matches?", 17), ("note", 46)]


def C(name):
    return get_column_letter([c[0] for c in COLS].index(name) + 1)


# ---------------------------------------------------------------- Summary
s = wb.active
s.title = "Summary"
s.sheet_view.showGridLines = False
s["A1"] = "Rasdaman Tiling Audit — zeus.snap.uaf.edu"; s["A1"].font = TITLE
s["A2"] = "21 Sep 2026, corrected 22 Sep 2026 · 273 coverages · all measured directly · rasdaman v10.4.7"
s["A2"].font = NOTE

N = len(recs) + 1
def rng(col): return "Coverages!{0}2:{0}{1}".format(C(col), N)
REALGB, IDXVERDICT = rng("real disk GB (PhysicalSize)"), rng("index")
CLEAN_N = len(recs)
blocks = [
 ("The server", [
  ("Coverages published", '=COUNTA(%s)' % rng("coverage_id")),
  ("Measured directly (dbinfo succeeded)", '=COUNTIF(%s,"yes")' % rng("dbinfo array matches?")),
  ("On disk, live coverages (GB) — PhysicalSize, not totalSize", '=ROUND(SUM(%s),0)' % REALGB),
  ("Actual/logical data (GB)", '=ROUND(SUM(%s),0)' % rng("data GB")),
  ("Unreferenced collections, all (GB)", '=ROUND(SUM(Cleanup!D2:D106),0)'),
  ("  — of which confirmed file-referenced, not real disk (GB)", '=ROUND(SUMIF(Cleanup!H2:H106,"CONFIRMED",Cleanup!D2:D106),0)'),
 ]),
 ("Recoverable", [
  ("Duplicate tile-index entries: real disk cost (GB)", 0),
  ("Genuinely unreferenced collections (GB)", '=ROUND(SUM(Cleanup!D2:D106)-SUMIF(Cleanup!H2:H106,"CONFIRMED",Cleanup!D2:D106),0)'),
  ("TOTAL genuinely recoverable (GB)", '=ROUND(SUM(Cleanup!D2:D106)-SUMIF(Cleanup!H2:H106,"CONFIRMED",Cleanup!D2:D106),0)'),
  ("Coverages with duplicate index entries (0 GB real cost, see Finding 1)", '=COUNTIF(%s,"duplicate entries")' % IDXVERDICT),
  ("Coverages with a clean tile index", '=COUNTIF(%s,"clean")' % IDXVERDICT),
 ]),
 ("Read performance (modelled)", [
  ("Amplification over 10,000x", '=COUNTIF(%s,">10000")' % rng("amp for role")),
  ("Amplification over 1,000x", '=COUNTIF(%s,">1000")' % rng("amp for role")),
  ("Would improve 10x or more if re-tiled", '=COUNTIF(%s,">=10")' % rng("improvement x")),
  ("Would improve 2x or more if re-tiled", '=COUNTIF(%s,">=2")' % rng("improvement x")),
 ]),
 ("Action", [
  ("P1 — large read-performance win on a large coverage", '=COUNTIF(%s,"P1")' % rng("priority")),
  ("P2", '=COUNTIF(%s,"P2")' % rng("priority")),
  ("P3", '=COUNTIF(%s,"P3")' % rng("priority")),
  ("P4 — low value or no action", '=COUNTIF(%s,"P4")' % rng("priority")),
  ("Queue rows that are one batch decision", '=COUNTIF(%s,"cmip6_downscaled*")' % rng("batch")),
 ]),
 ("Hygiene", [
  ("No ingest recipe on origin/main", '=COUNTBLANK(%s)' % rng("recipe path")),
  ("Test-named coverages in production", '=COUNTIF(%s,"yes")' % rng("test?")),
  ("Stored array disagrees with catalogue", '=COUNTA(\'Data Integrity\'!A2:A12)'),
  ("Unreferenced collections", '=COUNTA(Cleanup!A2:A106)'),
 ]),
]
r = 4
for title, items in blocks:
    s.cell(row=r, column=1, value=title).font = SUB
    r += 1
    for label, formula in items:
        s.cell(row=r, column=1, value=label).font = BODY
        c = s.cell(row=r, column=2, value=formula)
        c.font = Font(name=Fn, size=10, bold=True)
        c.alignment = Alignment(horizontal="right")
        r += 1
    r += 1
s.column_dimensions["A"].width = 58
s.column_dimensions["B"].width = 14
s.cell(row=r, column=1,
       value="Every figure is a live formula over the other tabs, sourced from "
             "RAS_MDDOBJECTS.PhysicalSize, not dbinfo's totalSize. Read "
             "rasdaman-tiling-audit.md's correction note and Finding 1 for why "
             "that distinction matters, and Finding 5 for the file-referenced "
             "orphan.").font = NOTE

# ---------------------------------------------------------------- Coverages
cv = wb.create_sheet("Coverages")
header(cv, COLS)
for i, d in enumerate(recs, 2):
    vals = [d["cid"], d["coll"], d["suffix"], d["test"], d["recipe"], d["axes"],
            d["ext"], d["bands"], d["bpc"], d["declared"], d["cfg"], d["tex"],
            d["tiles"], d["logical"], d["declared_gb"], d["real_gb"],
            d["ovf"], d["idx_dup"], d["idx_verdict"], d["p_amp"], d["m_amp"],
            d["role"], d["cur"], d["tiling"], d["proj"], d["improve"],
            d["action"], d["pri"], d["batch"], d["mism"], d["wvd"], d["dam"],
            d["note"]]
    for j, v in enumerate(vals, 1):
        c = cv.cell(row=i, column=j, value=v)
        c.font = BODY
    if d["pri"] == "P1": cv.cell(row=i, column=28).fill = P1F
    if d["pri"] == "P2": cv.cell(row=i, column=28).fill = P2F
    cv.cell(row=i, column=19).fill = BAD if d["idx_verdict"] == "duplicate entries" else GOOD
    for col in (14, 15, 16, 18):
        cv.cell(row=i, column=col).number_format = "#,##0.0"
    cv.cell(row=i, column=13).number_format = "#,##0"
    for col in (20, 21, 23, 25):
        cv.cell(row=i, column=col).number_format = "#,##0"
    cv.cell(row=i, column=17).number_format = "0.000"
cv.auto_filter.ref = cv.dimensions

# ---------------------------------------------------------------- Re-tile Queue
q = wb.create_sheet("Re-tile Queue")
qrecs = [d for d in recs if d["pri"] in ("P1", "P2", "P3")]
QC = [("priority", 9), ("coverage_id", 44), ("batch", 50),
      ("rasdaman collection", 44),
      ("why", 52), ("real disk GB", 14),
      ("amp now", 12), ("amp after", 12), ("improvement x", 13),
      ("recommended tiling", 46)]
header(q, QC)
for i, d in enumerate(qrecs, 2):
    why = "%s query reads %s x more than it returns" % (
        d["role"], format(int(d["cur"]), ",")) if d["cur"] else ""
    for j, v in enumerate([d["pri"], d["cid"], d["batch"], d["coll"],
                           why, d["real_gb"], d["cur"], d["proj"],
                           d["improve"], d["tiling"]], 1):
        q.cell(row=i, column=j, value=v).font = BODY
    if d["pri"] == "P1": q.cell(row=i, column=1).fill = P1F
    if d["pri"] == "P2": q.cell(row=i, column=1).fill = P2F
    q.cell(row=i, column=6).number_format = "#,##0.0"
    for col in (7, 8): q.cell(row=i, column=col).number_format = "#,##0"
q.auto_filter.ref = q.dimensions
q.cell(row=len(qrecs) + 3, column=1,
       value="This tab is a read-performance queue only — re-tiling has no storage "
             "consequence in either direction (Finding 1). Rows are ranked, not "
             "sequential. The batch column marks families that share one decision — "
             "the 171 cmip6_downscaled v2 coverages are a single re-tiling choice "
             "applied 171 times, not 171 separate investigations. Filter batch to "
             "blank to see the coverages that need individual thought."
       ).font = NOTE

# ---------------------------------------------------------------- Cleanup
cl = wb.create_sheet("Cleanup")
orphans = [name for name in phys if name not in coll.values()]
orphan_recs = []
for name in orphans:
    cat = unref_cat.get(name, {})
    extra = unref_extra.get(name, {})
    orphan_recs.append(dict(
        collection=name,
        category=cat.get("category", ""),
        why=cat.get("why", ""),
        gb=round(phys[name] / 1e9, 3),
        tiles=I(extra.get("tiles", "")),
        sdom=extra.get("sdom", ""),
        review=extra.get("review_statement", "drop collection %s" % name),
        filereferenced="CONFIRMED" if name in CONFIRMED_FILE_REFERENCED else "",
    ))
orphan_recs.sort(key=lambda r: -r["gb"])
CC = [("collection", 62), ("category", 20), ("why", 52), ("GB (PhysicalSize)", 15),
      ("tiles", 13), ("sdom", 34), ("review statement", 58),
      ("file-referenced (in situ)?", 20)]
header(cl, CC)
for i, u in enumerate(orphan_recs, 2):
    for j, v in enumerate([u["collection"], u["category"], u["why"],
                           u["gb"], u["tiles"], u["sdom"],
                           u["review"], u["filereferenced"]], 1):
        cl.cell(row=i, column=j, value=v).font = BODY
    cl.cell(row=i, column=4).number_format = "#,##0.0"
    cl.cell(row=i, column=5).number_format = "#,##0"
    if u["filereferenced"] == "CONFIRMED":
        cl.cell(row=i, column=8).fill = BAD
        cl.cell(row=i, column=4).fill = BAD
    elif u["gb"] >= 100:
        cl.cell(row=i, column=4).fill = P1F
cl.cell(row=len(orphan_recs) + 3, column=1,
        value="GB is RAS_MDDOBJECTS.PhysicalSize, not dbinfo's totalSize (see "
              "rasdaman-tiling-audit.md's correction note). One collection is "
              "CONFIRMED file-referenced via RAS_FILETILES: its GB is real data "
              "but not rasdaman disk, and dropping it recovers ~0 GB. No other "
              "large orphan has been individually checked the same way -- run "
              "rasdaman_physical_size.py --check-fileref before treating a large "
              "unconfirmed row's GB as certainly recoverable. Verify oid against "
              "rasdaman_range_set before dropping anything.").font = NOTE
cl.auto_filter.ref = "A1:H%d" % (len(orphan_recs) + 1)

# ---------------------------------------------------------------- Data Integrity
di = wb.create_sheet("Data Integrity")
DC = [("coverage_id", 50), ("axes (storage order)", 26),
      ("stored array (WCPS probe)", 34), ("catalogue grid", 26),
      ("what to decide", 60)]
header(di, DC)
bad = [d for d in recs if d["wvd"] == "DIFFERS"]
for i, d in enumerate(bad, 2):
    what = ("test coverage already slated for deletion"
            if d["test"] == "yes" else
            "decide which layer is authoritative, then correct the other")
    for j, v in enumerate([d["cid"], d["axes"], d["ext"], "(see _compact JSON)", what], 1):
        di.cell(row=i, column=j, value=v).font = BODY
di.cell(row=len(bad) + 3, column=1,
        value="dbinfo now returns the coverage's own array for all 273 rows, so these "
              "are genuine petascope-vs-storage disagreements, not the name-divergence "
              "problem that earlier versions of this tab reported.").font = NOTE

# ---------------------------------------------------------------- Column Guide
g = wb.create_sheet("Column Guide")
g.sheet_view.showGridLines = False
g["A1"] = "What every column means"; g["A1"].font = TITLE
g["A2"] = "M = measured on the server.  D = derived by arithmetic.  R = recommendation."
g["A2"].font = NOTE
GG = [("column", 26), ("kind", 7), ("definition", 108)]
header(g, GG, row=4)
DEFS = [
 ("coverage_id", "M", "The ID published in GetCapabilities — what WCS/WCPS clients ask for."),
 ("rasdaman collection", "M", "The array petascope actually points at, read from petascopedb. Often NOT the coverage_id: wcst_import may append a timestamp or move a _v2 suffix to the end. This is the name to hand rasql."),
 ("suffix", "M", "Trailing _wcs / _wms on the coverage name. _wms marks map-rendering coverages, which want the opposite tiling from everything else."),
 ("test?", "D", "Name contains 'test'. Several such coverages are live in the public catalogue."),
 ("recipe path", "M", "Ingest recipe in rasdaman-ingest on origin/main. Blank means none exists — the coverage cannot be rebuilt from source control."),
 ("axes (storage order)", "M", "Axis names in the order rasdaman stores them, which is gridOrder — NOT the order DescribeCoverage reports. Every per-axis column on this sheet uses this order."),
 ("grid extents", "M", "Cells per axis, from the WCPS type-error probe: the coverage's own array, not what the catalogue advertises."),
 ("bands / bytes per cell", "M", "Band count and the summed byte width of all bands. A tile's size uses the SUM across bands — sizing against one band under-counts by the band count."),
 ("declared tiling", "M", "The tiling string from the recipe, verbatim."),
 ("tile config / tile extents", "M", "What rasdaman actually built, from dbinfo. '0:*' means an axis was left to rasdaman; tile extents resolve it to real numbers."),
 ("tiles (indexed)", "M", "tileNo from dbinfo — entries in the tile INDEX, including duplicate entries where they exist (see 'index' column). Not the same as tiles actually stored; see real disk GB."),
 ("data GB", "D", "cells x bytes-per-cell. The uncompressed logical size of the array."),
 ("declared GB (totalSize)", "M", "totalSize from dbinfo. NOT bytes on disk -- it sums every INDEXED tile entry's byte footprint, so a coverage with duplicate index entries reports too high. Kept for transparency; do not quote as disk usage."),
 ("real disk GB (PhysicalSize)", "M", "RAS_MDDOBJECTS.PhysicalSize, read directly from RASBASE. The authoritative disk figure -- matches rasdaman's own UI total to four significant figures when summed across live coverages. Use this column, not declared GB, for any disk-space claim."),
 ("index overhead x", "D", "declared GB / data GB. Above ~1.05 means the tile index has duplicate entries for this coverage (see 'index' column) -- an index-health signal, not a disk-usage one."),
 ("index dup weight GB", "D", "declared GB minus real disk GB. This is INDEX inflation from duplicate entries, confirmed to cost ~0 real bytes on disk for every coverage checked. Do not treat as reclaimable space."),
 ("index", "D", "clean or 'duplicate entries'. A coverage with duplicate entries has real disk usage exactly equal to its unique data (Finding 1) -- this flags an index-hygiene / process signal (wcst_import likely re-run against an existing coverage), not a storage problem."),
 ("point amp", "D", "Bytes a single-point time series must read over what it returns. Equals the product of the tile's two spatial extents."),
 ("map amp", "D", "Bytes one full map frame must read over what it returns. Rises as the spatial footprint shrinks — the opposite pressure to point amp."),
 ("role", "D", "map for _wms coverages, point for everything else. Decides which amplification is ranked."),
 ("amp for role", "D", "Whichever of the two above matches this coverage's job."),
 ("recommended tiling", "R", "An ALIGNED string targeting a ~4 MB tile for the coverage's role: sweep axes whole, spatial footprint sized to the budget. Verify against the source file's real dimension order before using."),
 ("projected amp", "R", "Amplification the recommended tiling would give, computed the same way as the current figure."),
 ("improvement x", "R", "amp for role / projected amp. Above 10 is a large win; below 2 is not worth a re-tile on its own. This is the ONLY basis for priority now -- disk usage plays no part, since Finding 1 showed tiling doesn't affect it."),
 ("action", "R", "re-tile changes the recipe and recovers read time, not disk — there is no 'recover disk' action any more (Finding 1)."),
 ("priority", "R", "Based entirely on read-performance improvement and coverage size. P1: 20x+ read win on a 20+ GB coverage. P2: 10x+ win. P3: 2x+ win. P4: everything else, including every coverage where action is 'none'."),
 ("axis mismatch", "D", "Confidence that the tiling bracket's chunk sizes were attached to the wrong axes. Only a couple of coverages flag here."),
 ("wcps vs catalogue", "M", "DIFFERS means petascope advertises a different geometry than the array holds. See the Data Integrity tab."),
 ("dbinfo array matches?", "M", "Whether the collection dbinfo answered for is genuinely this coverage's array. 'yes' for all 273, because the petascopedb mapping removed the guesswork."),
]
rr = 5
for name, kind, text in DEFS:
    g.cell(row=rr, column=1, value=name).font = Font(name=Fn, size=10, bold=True)
    g.cell(row=rr, column=2, value=kind).font = BODY
    c = g.cell(row=rr, column=3, value=text); c.font = BODY
    c.alignment = Alignment(wrap_text=True, vertical="top")
    g.row_dimensions[rr].height = 30
    rr += 1

# ---------------------------------------------------------------- Method
m = wb.create_sheet("Method & Caveats")
m.sheet_view.showGridLines = False
m["A1"] = "Method, and what these numbers do not prove"; m["A1"].font = TITLE
TEXT = [
 ("A correction, made 22 Sep 2026", None),
 ("", "The first version of this workbook used dbinfo's totalSize as bytes on disk and reported 17,523 GB used / 8,114 GB recoverable. totalSize sums the tile INDEX, not the filesystem, and double-counts a coverage whose index has duplicate entries. RAS_MDDOBJECTS.PhysicalSize, read directly from RASBASE, doesn't have this problem and gives 9,172 GB for the 273 live coverages -- matching rasdaman's own UI ('9.17 TB') to four significant figures. Every disk-size figure in this workbook now comes from PhysicalSize. See docs/rasdaman-tiling-audit.md's correction note and Finding 1 for the full story."),
 ("Where the numbers come from", None),
 ("", "WCPS type-error probe — the coverage's own stored array (extents, cell type, null value). Needs no credentials and no collection name. Reached all 273."),
 ("", "petascopedb pointer — coverage_id to collection_name, from the coverage/rasdaman_range_set join. With it, dbinfo succeeded for all 273 with zero failures."),
 ("", "rasql dbinfo — declared tiling, band types, tile-index entry count (tileNo), and with printtiles every individual tile domain. Its totalSize field is NOT used for disk-size figures (see above)."),
 ("", "RAS_MDDOBJECTS.PhysicalSize, via RASBASE — the authoritative disk-size figure, computed per stored array object, joined through RAS_MDDCOLLNAMES -> RAS_MDDCOLLECTIONS -> RAS_MDDOBJECTS. scripts/rasdaman_physical_size.py automates this join and a file-reference heuristic against RAS_FILETILES."),
 ("", "RASBASE RAS_MDDCOLLNAMES — the full collection inventory, which is how the 105 unreferenced collections on the Cleanup tab were found."),
 ("", "WCS DescribeCoverage — axis labels and the catalogue's view of the grid."),
 ("The tile index really does have duplicate entries", None),
 ("", "Verified, not inferred: for six coverages spanning 1.32x to 12.64x totalSize/PhysicalSize we dumped every tile domain and confirmed the unique domains cover each array exactly, and the duplicated entries account for the rest of totalSize."),
 ("...but they cost no measurable disk", None),
 ("", "Checked against all 26 flagged coverages, not just the six dumped in full: PhysicalSize matches the unique-domain figure exactly, every time, with zero exceptions. Tile count and tile size also contribute nothing to storage -- the same era5 array exists on this server at 289, 1,172 and 4,678 tiles and occupies 19.011 GB in all three, by PhysicalSize."),
 ("Not every unreferenced collection's GB is real rasdaman disk", None),
 ("", "One 1,268 GB orphan is confirmed, via RAS_FILETILES, to be almost entirely file-referenced ('in situ') rather than copied into rasdaman's storage -- its PhysicalSize is honest about the data's size, but dropping the collection recovers ~0 GB of rasdaman's own disk. See the Cleanup tab's file-referenced column and Finding 5. No other large orphan has been checked the same way."),
 ("A second, smaller, unexplained gap", None),
 ("", "Summed across all 273 live coverages, logical size (cells x bytes-per-cell, 9,593.8 GB) exceeds PhysicalSize (9,171.8 GB) by 421.9 GB -- and this is NOT the 26 duplicate-index coverages, whose PhysicalSize matches their logical size exactly. It sits in 41 other, clean-index coverages where PhysicalSize is genuinely smaller than the array's nominal cell count implies (ak_hydro_segments_mhit_stats_combined is the starkest: 1.07 GB logical vs 0.31 GB PhysicalSize). UNEXPLAINED -- possibly null/constant-region compression, possibly an overcounting logical-size calculation. Not a disk-recovery opportunity; flagged so it isn't silent."),
 ("The cause of the index duplication is inferred", None),
 ("", "Uneven copy counts, plus the pattern that iterated _wcs variants duplicate while their once-ingested siblings do not, point to wcst_import being re-run against an existing coverage. UNTESTED. The decisive check is to ingest a throwaway coverage, measure it, re-run the same recipe without dropping it, and measure again."),
 ("Read amplification is modelled", None),
 ("", "Bytes a representative query must read over bytes it returns, derived from the measured tile shape. A sound basis for ranking, not a benchmark. It never depended on totalSize or PhysicalSize, so this correction does not affect it. Twenty coverages have no figure because their role could not be classified."),
 ("Recommended tilings are a starting point", None),
 ("", "They target a ~4 MB tile against the coverage's role. Verify each against the source file's real dimension order before ingesting, and re-run rasdaman_tiling_audit.py afterwards."),
 ("Known gaps", None),
 ("", "One dbinfo failed during the unreferenced sweep (cmip6_downscaled_pr_wms_crstephenson) -- it has no RAS_MDDOBJECTS row at all, a true empty shell, so it's excluded rather than estimated. Whether a bloated tile index costs anything in query latency is untested. Whether any orphan besides the one confirmed item is file-referenced is unconfirmed. The eleven catalogue disagreements are identified but not adjudicated. The duplication cause above remains untested."),
]
rr = 3
for head, body in TEXT:
    if body is None:
        m.cell(row=rr, column=1, value=head).font = SUB
    else:
        c = m.cell(row=rr, column=1, value="•  " + body)
        c.font = BODY
        c.alignment = Alignment(wrap_text=True, vertical="top")
        m.row_dimensions[rr].height = 46
    rr += 1
m.column_dimensions["A"].width = 132

wb.save(OUT)
print("wrote", OUT)
print("coverages %d | queue %d | cleanup %d | integrity %d"
      % (len(recs), len(qrecs), len(orphan_recs), len(bad)))
print("real disk GB %.0f | declared GB %.0f | idx dup weight GB %.0f | unref GB %.0f (confirmed non-disk: %.0f)"
      % (sum(d["real_gb"] for d in recs), sum(d["declared_gb"] for d in recs),
         sum(d["idx_dup"] for d in recs), sum(u["gb"] for u in orphan_recs),
         sum(u["gb"] for u in orphan_recs if u["filereferenced"] == "CONFIRMED")))
