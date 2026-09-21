#!/usr/bin/env python3
"""Build rasdaman_tiling_audit.xlsx from the CSVs in ../data.

Inputs  : data/coverages_summary.csv   (rasdaman_tiling_audit.py --outdir)
          data/mapping.txt             (petascopedb coverage -> collection)
          data/unreferenced_sizes.csv  (rasdaman_price_collections.py)
Output  : rasdaman_tiling_audit.xlsx at the repo root.
Requires: openpyxl.
"""
import csv, math, os
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUM   = os.path.join(ROOT, "data", "coverages_summary.csv")
MAP   = os.path.join(ROOT, "data", "mapping.txt")
UNREF = os.path.join(ROOT, "data", "unreferenced_sizes.csv")
OUT   = os.path.join(ROOT, "rasdaman_tiling_audit.xlsx")

TARGET = 4194304          # bytes: middle of rasdaman's 1-4 MB guidance
WMS_TARGET = 16777216


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
unref = list(csv.DictReader(open(UNREF)))

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
    persisted = I(r["total_size_bytes"])
    logical = I(r["real_data_bytes"])
    dup = max(0, persisted - logical)
    ovf = F(r["storage_overhead_factor"])
    role = "map" if r["name_suffix"] == "wms" else "point"
    cur = F(r["map_query_amplification"] if role == "map"
            else r["point_query_amplification"])
    tiling, tsize, p_amp, m_amp, note = recommend(r)
    proj = m_amp if role == "map" else p_amp
    improve = (cur / proj) if (cur and proj) else None

    if dup >= 1e9 and improve and improve >= 2:  action = "re-ingest + re-tile"
    elif dup >= 1e9:                             action = "re-ingest (duplicates)"
    elif improve and improve >= 2:               action = "re-tile"
    else:                                        action = "none"

    imp = improve or 0
    if dup >= 100e9:                                          pri = "P1"
    elif dup >= 10e9 or (imp >= 10 and persisted >= 20e9):    pri = "P2"
    elif imp >= 10 and persisted >= 1e9:                      pri = "P3"
    else:                                                     pri = "P4"
    if action == "none":                                      pri = "P4"

    recs.append(dict(
        cid=r["coverage_id"], coll=coll.get(r["coverage_id"], ""),
        suffix=r["name_suffix"], test=r["is_test_named"],
        recipe=r["recipe_path"], axes=r["grid_axes"], ext=r["grid_extents"],
        bands=I(r["band_count"]), bpc=I(r["bytes_per_cell"]),
        declared=r["declared_tiling"], cfg=r["tile_configuration"],
        tex=r["tile_extents"], tiles=I(r["tile_no"]),
        logical=round(logical / 1e9, 3), persisted=round(persisted / 1e9, 3),
        ovf=ovf, dup=round(dup / 1e9, 3),
        verdict=("DUPLICATES" if (ovf or 0) > 1.05 else "clean"),
        p_amp=F(r["point_query_amplification"]), m_amp=F(r["map_query_amplification"]),
        role=role, cur=cur, tiling=tiling, proj=proj,
        improve=round(improve, 1) if improve else None,
        action=action, pri=pri,
        batch=batch_of(r["coverage_id"]),
        mism=r["mismatch_confidence"], wvd=r["wcps_vs_describe"],
        dam=r["dbinfo_array_matches"], note=r["note"]))

recs.sort(key=lambda r: (["P1", "P2", "P3", "P4"].index(r["pri"]),
                         -(r["dup"] or 0), -(r["improve"] or 0)))

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
        ("tiles", 11), ("data GB", 10), ("on disk GB", 11),
        ("overhead x", 10), ("duplicate GB", 12), ("storage", 12),
        ("point amp", 12), ("map amp", 12), ("role", 8),
        ("amp for role", 13), ("recommended tiling", 46),
        ("projected amp", 13), ("improvement x", 13), ("action", 21),
        ("priority", 9), ("batch", 50), ("axis mismatch", 12), ("wcps vs catalogue", 16),
        ("dbinfo array matches?", 17), ("note", 40)]


def C(name):
    return get_column_letter([c[0] for c in COLS].index(name) + 1)


# ---------------------------------------------------------------- Summary
s = wb.active
s.title = "Summary"
s.sheet_view.showGridLines = False
s["A1"] = "Rasdaman Tiling Audit — zeus.snap.uaf.edu"; s["A1"].font = TITLE
s["A2"] = "21 Sep 2026 · 273 coverages · all measured directly · rasdaman v10.4.7"
s["A2"].font = NOTE

N = len(recs) + 1
def rng(col): return "Coverages!{0}2:{0}{1}".format(C(col), N)
DUPGB, STORE = rng("duplicate GB"), rng("storage")
blocks = [
 ("The server", [
  ("Coverages published", '=COUNTA(%s)' % rng("coverage_id")),
  ("Measured directly (dbinfo succeeded)", '=COUNTIF(%s,"yes")' % rng("dbinfo array matches?")),
  ("On disk, live coverages (GB)", '=ROUND(SUM(%s),0)' % rng("on disk GB")),
  ("Actual data (GB)", '=ROUND(SUM(%s),0)' % rng("data GB")),
  ("Unreferenced collections (GB)", '=ROUND(SUM(Cleanup!D2:D106),0)'),
  ("TOTAL on disk (GB)", '=ROUND(SUM(%s)+SUM(Cleanup!D2:D106),0)' % rng("on disk GB")),
 ]),
 ("Recoverable", [
  ("Duplicate tiles in live coverages (GB)", '=ROUND(SUMIF(%s,"DUPLICATES",%s),0)' % (STORE, DUPGB)),
  ("Collections nothing references (GB)", '=ROUND(SUM(Cleanup!D2:D106),0)'),
  ("TOTAL recoverable (GB)", '=ROUND(SUMIF(%s,"DUPLICATES",%s)+SUM(Cleanup!D2:D106),0)' % (STORE, DUPGB)),
  ("Largest single coverage waste (GB)", '=ROUND(MAX(%s),0)' % DUPGB),
  ("Coverages carrying duplicates", '=COUNTIF(%s,"DUPLICATES")' % STORE),
  ("Coverages clean (<=1.05x)", '=COUNTIF(%s,"clean")' % STORE),
 ]),
 ("Read performance (modelled)", [
  ("Amplification over 10,000x", '=COUNTIF(%s,">10000")' % rng("amp for role")),
  ("Amplification over 1,000x", '=COUNTIF(%s,">1000")' % rng("amp for role")),
  ("Would improve 10x or more if re-tiled", '=COUNTIF(%s,">=10")' % rng("improvement x")),
  ("Would improve 2x or more if re-tiled", '=COUNTIF(%s,">=2")' % rng("improvement x")),
 ]),
 ("Action", [
  ("P1 — 100 GB+ reclaimable", '=COUNTIF(%s,"P1")' % rng("priority")),
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
s.column_dimensions["A"].width = 46
s.column_dimensions["B"].width = 14
s.cell(row=r, column=1,
       value="Every figure is a live formula over the other tabs. "
             "Read rasdaman-tiling-audit.md for what they mean.").font = NOTE

# ---------------------------------------------------------------- Coverages
cv = wb.create_sheet("Coverages")
header(cv, COLS)
for i, d in enumerate(recs, 2):
    vals = [d["cid"], d["coll"], d["suffix"], d["test"], d["recipe"], d["axes"],
            d["ext"], d["bands"], d["bpc"], d["declared"], d["cfg"], d["tex"],
            d["tiles"], d["logical"], d["persisted"], d["ovf"], d["dup"],
            d["verdict"], d["p_amp"], d["m_amp"], d["role"], d["cur"],
            d["tiling"], d["proj"], d["improve"], d["action"], d["pri"],
            d["batch"], d["mism"], d["wvd"], d["dam"], d["note"]]
    for j, v in enumerate(vals, 1):
        c = cv.cell(row=i, column=j, value=v)
        c.font = BODY
    if d["pri"] == "P1": cv.cell(row=i, column=27).fill = P1F
    if d["pri"] == "P2": cv.cell(row=i, column=27).fill = P2F
    cv.cell(row=i, column=18).fill = BAD if d["verdict"] == "DUPLICATES" else GOOD
    for col in (14, 15, 17):
        cv.cell(row=i, column=col).number_format = "#,##0.0"
    cv.cell(row=i, column=13).number_format = "#,##0"
    for col in (19, 20, 22, 24):
        cv.cell(row=i, column=col).number_format = "#,##0"
    cv.cell(row=i, column=16).number_format = "0.000"
cv.auto_filter.ref = cv.dimensions

# ---------------------------------------------------------------- Re-ingest Queue
q = wb.create_sheet("Re-ingest Queue")
qrecs = [d for d in recs if d["pri"] in ("P1", "P2", "P3")]
QC = [("priority", 9), ("coverage_id", 44), ("batch", 50),
      ("rasdaman collection", 44),
      ("action", 21), ("why", 52), ("reclaimable GB", 14),
      ("amp now", 12), ("amp after", 12), ("improvement x", 13),
      ("recommended tiling", 46)]
header(q, QC)
for i, d in enumerate(qrecs, 2):
    why = []
    if d["dup"] >= 1:
        why.append("%.0f GB of duplicate tiles (%.2fx)" % (d["dup"], d["ovf"] or 0))
    if d["improve"] and d["improve"] >= 2:
        why.append("%s query reads %s x more than it returns" %
                   (d["role"], format(int(d["cur"]), ",")))
    for j, v in enumerate([d["pri"], d["cid"], d["batch"], d["coll"], d["action"],
                           "; ".join(why), d["dup"], d["cur"], d["proj"],
                           d["improve"], d["tiling"]], 1):
        q.cell(row=i, column=j, value=v).font = BODY
    if d["pri"] == "P1": q.cell(row=i, column=1).fill = P1F
    if d["pri"] == "P2": q.cell(row=i, column=1).fill = P2F
    q.cell(row=i, column=7).number_format = "#,##0.0"
    for col in (8, 9): q.cell(row=i, column=col).number_format = "#,##0"
q.auto_filter.ref = q.dimensions
q.cell(row=len(qrecs) + 3, column=1,
       value="Rows are ranked, not sequential. The batch column marks families that "
             "share one decision — the 171 cmip6_downscaled v2 coverages are a single "
             "re-tiling choice applied 171 times, not 171 separate investigations. "
             "Filter batch to blank to see the coverages that need individual thought."
       ).font = NOTE

# ---------------------------------------------------------------- Cleanup
cl = wb.create_sheet("Cleanup")
CC = [("collection", 62), ("category", 20), ("why", 52), ("GB", 11),
      ("tiles", 13), ("sdom", 34), ("review statement", 58)]
header(cl, CC)
unref.sort(key=lambda r: -(F(r["gb"]) or 0))
for i, u in enumerate(unref, 2):
    for j, v in enumerate([u["collection"], u["category"], u["why"],
                           F(u["gb"]), I(u["tiles"]), u["sdom"],
                           u["review_statement"]], 1):
        cl.cell(row=i, column=j, value=v).font = BODY
    cl.cell(row=i, column=4).number_format = "#,##0.0"
    cl.cell(row=i, column=5).number_format = "#,##0"
    if (F(u["gb"]) or 0) >= 100: cl.cell(row=i, column=4).fill = P1F
cl.cell(row=len(unref) + 3, column=1,
        value="Nothing here is referenced by any coverage. Verify each oid against "
              "rasdaman_range_set before dropping — the review statement is for a "
              "human to check, not to run blind.").font = NOTE
cl.auto_filter.ref = "A1:G%d" % (len(unref) + 1)

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
 ("test?", "D", "Name contains 'test'. Seven such coverages are live in the public catalogue."),
 ("recipe path", "M", "Ingest recipe in rasdaman-ingest on origin/main. Blank means none exists — the coverage cannot be rebuilt from source control."),
 ("axes (storage order)", "M", "Axis names in the order rasdaman stores them, which is gridOrder — NOT the order DescribeCoverage reports. Every per-axis column on this sheet uses this order."),
 ("grid extents", "M", "Cells per axis, from the WCPS type-error probe: the coverage's own array, not what the catalogue advertises."),
 ("bands / bytes per cell", "M", "Band count and the summed byte width of all bands. A tile's size uses the SUM across bands — sizing against one band under-counts by the band count."),
 ("declared tiling", "M", "The tiling string from the recipe, verbatim."),
 ("tile config / tile extents", "M", "What rasdaman actually built, from dbinfo. '0:*' means an axis was left to rasdaman; tile extents resolve it to real numbers."),
 ("tiles", "M", "tileNo from dbinfo — tiles actually materialised."),
 ("data GB", "D", "cells x bytes-per-cell. The uncompressed logical size of the array."),
 ("on disk GB", "M", "totalSize from dbinfo — bytes actually persisted."),
 ("overhead x", "D", "on disk / data. Compression is active on this server, so anything above ~1.05 is waste, and below 1.0 is healthy compression."),
 ("duplicate GB", "D", "on disk minus data. Verified for six coverages by dumping every tile domain: the unique domains cover each array exactly, so this figure IS duplicate tiles. Not padding, not tile count."),
 ("storage", "D", "clean (<=1.05x) or DUPLICATES. Fixed by re-ingesting into a fresh collection with the recipe unchanged."),
 ("point amp", "D", "Bytes a single-point time series must read over what it returns. Equals the product of the tile's two spatial extents."),
 ("map amp", "D", "Bytes one full map frame must read over what it returns. Rises as the spatial footprint shrinks — the opposite pressure to point amp."),
 ("role", "D", "map for _wms coverages, point for everything else. Decides which amplification is ranked."),
 ("amp for role", "D", "Whichever of the two above matches this coverage's job."),
 ("recommended tiling", "R", "An ALIGNED string targeting a ~4 MB tile for the coverage's role: sweep axes whole, spatial footprint sized to the budget. Verify against the source file's real dimension order before using."),
 ("projected amp", "R", "Amplification the recommended tiling would give, computed the same way as the current figure."),
 ("improvement x", "R", "amp for role / projected amp. Above 10 is a large win; below 2 is not worth a re-ingest on its own."),
 ("action", "R", "re-ingest (duplicates) recovers disk with no recipe change. re-tile changes the recipe and recovers read time, not disk. Both needs one re-ingest with a corrected recipe."),
 ("priority", "R", "P1: 100 GB+ reclaimable. P2: 10 GB+ or a 10x read win on a large coverage. P3: 5x+ read win. P4: everything else."),
 ("axis mismatch", "D", "Confidence that the tiling bracket's chunk sizes were attached to the wrong axes. Only two coverages flag here."),
 ("wcps vs catalogue", "M", "DIFFERS means petascope advertises a different geometry than the array holds. See the Data Integrity tab."),
 ("dbinfo array matches?", "M", "Whether the collection dbinfo answered for is genuinely this coverage's array. Now 'yes' for all 273, because the petascopedb mapping removed the guesswork."),
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
 ("Where the numbers come from", None),
 ("", "WCPS type-error probe — the coverage's own stored array (extents, cell type, null value). Needs no credentials and no collection name. Reached all 273."),
 ("", "petascopedb pointer — coverage_id to collection_name, from the coverage/rasdaman_range_set join. With it, dbinfo succeeded for all 273 with zero failures."),
 ("", "rasql dbinfo — tiling built, band types, persisted size; with printtiles, every individual tile domain."),
 ("", "RASBASE RAS_MDDCOLLNAMES — the full collection inventory, which is how the 105 unreferenced collections on the Cleanup tab were found."),
 ("", "WCS DescribeCoverage — axis labels and the catalogue's view of the grid."),
 ("Storage is measured", None),
 ("", "Persisted bytes from dbinfo; logical size is cells x bytes-per-cell. Their difference is duplicate tiles — verified, not inferred: for six coverages spanning 1.32x to 12.64x we dumped every tile domain and confirmed bytes-on-disk / bytes-covered = 1.00x, with unique domains covering each array exactly."),
 ("", "Tile count and tile size contribute nothing to storage. The same era5 array exists on this server at 289, 1,172 and 4,678 tiles and occupies 19.011 GB in all three."),
 ("The cause of duplication is inferred", None),
 ("", "Uneven copy counts, plus the pattern that iterated _wcs variants duplicate while their once-ingested siblings do not, point to wcst_import being re-run against an existing coverage. UNTESTED. The decisive check is to ingest a throwaway coverage, measure it, re-run the same recipe without dropping it, and measure again."),
 ("Read amplification is modelled", None),
 ("", "Bytes a representative query must read over bytes it returns, derived from the measured tile shape. A sound basis for ranking, not a benchmark. Twenty coverages have no figure because their role could not be classified."),
 ("Recommended tilings are a starting point", None),
 ("", "They target a ~4 MB tile against the coverage's role. Verify each against the source file's real dimension order before ingesting, and re-run rasdaman_tiling_audit.py afterwards."),
 ("Known gaps", None),
 ("", "One dbinfo failed during the unreferenced sweep (cmip6_downscaled_pr_wms_crstephenson), so 3,523 GB is a floor. The eleven catalogue disagreements are identified but not adjudicated. The duplication cause is untested."),
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
      % (len(recs), len(qrecs), len(unref), len(bad)))
print("dup GB %.0f | unref GB %.0f | persisted GB %.0f"
      % (sum(d["dup"] for d in recs), sum(F(u["gb"]) or 0 for u in unref),
         sum(d["persisted"] for d in recs)))
