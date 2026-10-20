#!/usr/bin/env python3
"""
tile_calc.py -- rasdaman tile-shape calculator, Python 3 standard library only.

Two modes:

  recommend   Given an array's axes (in gridOrder / storage order) and its
              true bytes-per-cell, print fully explicit ALIGNED brackets for
              the point, map and (optionally) WCPS-condense access patterns
              at one or more tile-size budgets, with modelled read
              amplification, tile count and a per-axis divisibility check.

  check       Given an existing tiling string, report its real tile bytes,
              tile count, per-axis divisibility and modelled amplification.

Axis input, either:
  --dims "time=100,model=3,scenario=2,y=1941,x=2471" --bytes-per-cell 40
  --ncdump FILE   (output of `ncdump -h`; "-" for stdin). Dims, band count and
                  dtype widths are read from the largest group of data
                  variables sharing one dimension signature.

Examples:
  ncdump -h gipl.nc | python3 tile_calc.py --ncdump - --condense-n 30
  python3 tile_calc.py --dims "time=100,model=3,scenario=2,y=1941,x=2471" \\
      --bytes-per-cell 40 --check "ALIGNED [0:99,0:2,0:1,0:12,0:12] tile size 4056000"

The math is the same square-footprint method as the rasdaman-audit repo's
utilities/tiling_lib.py and docs/CRREL_GIPL_tiling.md sections 4-5b.
Amplification is MODELLED (bytes read / bytes wanted), not measured --
benchmark a candidate before adopting it.
"""

import argparse
import math
import re
import sys

MIB = 1024 * 1024
SPATIAL_NAMES = {"x", "y", "lat", "lon", "long", "latitude", "longitude",
                 "e", "n", "easting", "northing"}
TIME_NAMES = {"time", "ansi", "date", "t"}
NC_TYPE_BYTES = {"byte": 1, "ubyte": 1, "char": 1, "short": 2, "ushort": 2,
                 "int": 4, "uint": 4, "long": 4, "float": 4, "real": 4,
                 "double": 8, "int64": 8, "uint64": 8}


def prod(vals):
    out = 1
    for v in vals:
        out *= v
    return out


# ------------------------------------------------------------------ input parsing

def parse_dims(text):
    dims = []
    for part in text.split(","):
        name, _, size = part.strip().partition("=")
        if not size:
            sys.exit(f"bad --dims entry {part!r}; expected name=size")
        dims.append((name.strip(), int(size)))
    return dims


def parse_ncdump(text):
    """Return (dims [(name, size)] in variable order, bytes_per_cell, band names,
    {variable: calendar attribute})."""
    sizes = {}
    section = None
    groups = {}
    calendars = {}
    for raw in text.splitlines():
        line = raw.strip()
        cal = re.match(r'(\w+):calendar\s*=\s*"([^"]*)"', line)
        if cal:
            calendars[cal.group(1)] = cal.group(2)
        if line.startswith("dimensions:"):
            section = "dims"
            continue
        if line.startswith("variables:"):
            section = "vars"
            continue
        if line.startswith("//") and "global attributes" in line:
            section = None
        if section == "dims":
            m = re.match(r"(\S+)\s*=\s*(\d+|UNLIMITED)\s*;(?:\s*//\s*\((\d+) currently\))?", line)
            if m:
                sizes[m.group(1)] = int(m.group(3) if m.group(2) == "UNLIMITED" else m.group(2))
        elif section == "vars":
            m = re.match(r"(\w+)\s+(\S+)\(([^)]*)\)\s*;", line)
            if not m or m.group(1) not in NC_TYPE_BYTES:
                continue
            vtype, vname, vdims = m.group(1), m.group(2), tuple(
                d.strip() for d in m.group(3).split(",") if d.strip())
            if len(vdims) < 2:
                continue  # coordinate / scalar variables are not bands
            groups.setdefault(vdims, []).append((vname, NC_TYPE_BYTES[vtype]))
    if not groups:
        sys.exit("no multi-dimensional data variables found in ncdump text")
    sig = max(groups, key=lambda g: (len(groups[g]), len(g)))
    if len(groups) > 1:
        print(f"note: {len(groups)} variable groups found; using {sig} "
              f"({len(groups[sig])} bands). Others: "
              + "; ".join(f"{g} x{len(v)}" for g, v in groups.items() if g != sig),
              file=sys.stderr)
    missing = [d for d in sig if d not in sizes]
    if missing:
        sys.exit(f"dimension sizes not found for {missing}")
    bands = groups[sig]
    return [(d, sizes[d]) for d in sig], sum(b for _, b in bands), [n for n, _ in bands], calendars


STANDARD_CALENDARS = {"standard", "gregorian", "proleptic_gregorian"}


def calendar_warning(time, extent, calendar):
    """AnsiDate counts real Gregorian days; model calendars without leap days
    (or with 360-day years) drift against it and are a recipe problem to raise
    before ingest, separate from tiling."""
    if calendar is not None:
        if calendar.lower() in STANDARD_CALENDARS:
            return None
        return (f"'{time}' uses calendar \"{calendar}\", which won't line up with AnsiDate's "
                "Gregorian days (dates drift; missing/extra days) -- resolve in the recipe before ingest")
    for year in (365, 360):
        if extent % year == 0:
            return (f"'{time}' has {extent} steps = {extent // year} x {year}: possibly a "
                    f"{'noleap/365_day' if year == 365 else '360_day'} calendar. Check "
                    f"time:calendar in `ncdump -h` before mapping it onto AnsiDate")
    return None


def classify(dims, spatial_arg, time_arg):
    names = [d for d, _ in dims]
    if spatial_arg:
        spatial = [s.strip() for s in spatial_arg.split(",")]
    else:
        spatial = [d for d in names if d.lower() in SPATIAL_NAMES]
    if len(spatial) != 2 or any(s not in names for s in spatial):
        sys.exit(f"could not identify exactly two spatial axes among {names}; pass --spatial y,x. "
                 "(No spatial axes at all, e.g. stream_id/era/model? Chunk the one axis that is "
                 "large relative to the others to 1 and keep the rest whole -- no calculator needed.)")
    if time_arg:
        time = time_arg
    else:
        time = next((d for d in names if d.lower() in TIME_NAMES and d not in spatial), None)
    return spatial, time


# ------------------------------------------------------------------ the math

def analyse(dims, chunk, bpc, spatial, condense_axis=None, condense_n=None):
    """Modelled metrics for a fully explicit chunk {axis: size}."""
    ext = dict(dims)
    nonsp = [d for d, _ in dims if d not in spatial]
    tile_cells = prod(chunk[d] for d, _ in dims)
    frame = prod(ext[s] for s in spatial)
    sp_tiles = prod(math.ceil(ext[s] / chunk[s]) for s in spatial)
    out = {
        "tile_bytes": tile_cells * bpc,
        "total_tiles": prod(math.ceil(ext[d] / chunk[d]) for d, _ in dims),
        # point: one x/y, every non-spatial value. Tiles read = ceil(ns/chunk) per axis.
        "point_amp": prod(math.ceil(ext[d] / chunk[d]) for d in nonsp) * tile_cells
                     / prod(ext[d] for d in nonsp),
        # map: one full frame at one value of each non-spatial axis.
        "map_amp": sp_tiles * tile_cells / frame,
        "remainders": {d: ext[d] % chunk[d] for d, _ in dims},
    }
    if condense_axis and condense_n:
        c = chunk[condense_axis]
        blocks_aligned = math.ceil(condense_n / c)
        blocks_straddle = math.ceil((condense_n - 1) / c) + 1 if c > 1 else condense_n
        wanted = condense_n * frame
        out["condense_amp_aligned"] = blocks_aligned * sp_tiles * tile_cells / wanted
        out["condense_amp_straddling"] = blocks_straddle * sp_tiles * tile_cells / wanted
    return out


def square_side(budget_cells, spatial, ext):
    """Largest square spatial footprint within budget; if one axis clamps to
    its full extent, hand the leftover budget to the other axis."""
    side = max(1, math.isqrt(max(1, budget_cells)))
    a, b = spatial
    ca, cb = min(side, ext[a]), min(side, ext[b])
    if ca < side:
        cb = min(ext[b], max(1, budget_cells // ca))
    elif cb < side:
        ca = min(ext[a], max(1, budget_cells // cb))
    return {a: ca, b: cb}


def fit_nonspatial(fixed, bpc, budget):
    """Shrink the largest fixed axis until a 1x1 spatial footprint fits."""
    fixed = dict(fixed)
    note = ""
    while prod(fixed.values()) * bpc > budget:
        big = max(fixed, key=fixed.get)
        if fixed[big] <= 1:
            return fixed, "NOT VIABLE: even 1 cell per axis exceeds the budget"
        others = prod(v for k, v in fixed.items() if k != big)
        fixed[big] = max(1, min(fixed[big] - 1, budget // (bpc * others)))
        note = f"non-spatial axes did not fit whole; chunked '{big}' to {fixed[big]}"
    return fixed, note


def recommend(dims, bpc, spatial, time, budget, workflow, condense_n=None):
    ext = dict(dims)
    nonsp = [d for d, _ in dims if d not in spatial]
    note = ""
    if workflow == "point":
        fixed, note = fit_nonspatial({d: ext[d] for d in nonsp}, bpc, budget)
    elif workflow == "map":
        fixed = {d: 1 for d in nonsp}
    else:  # condense
        fixed = {d: 1 for d in nonsp}
        fixed[time] = min(condense_n, ext[time])
        fixed, note = fit_nonspatial(fixed, bpc, budget)
    budget_cells = budget // (bpc * prod(fixed.values()))
    chunk = {**fixed, **square_side(budget_cells, spatial, ext)}
    return chunk, note


def bracket(dims, chunk, tile_bytes):
    return "ALIGNED [" + ", ".join(f"0:{chunk[d] - 1}" for d, _ in dims) + f"] tile size {tile_bytes}"


def parse_tiling(text, dims):
    m = re.search(r"(ALIGNED|REGULAR)\s*\[([^\]]*)\]", text, re.IGNORECASE)
    if not m:
        sys.exit(f"could not parse tiling string {text!r}")
    parts = [p.strip() for p in m.group(2).split(",")]
    if len(parts) != len(dims):
        sys.exit(f"bracket has {len(parts)} positions but {len(dims)} axes were given -- "
                 "the bracket is positional in gridOrder; check the axis list")
    chunk = {}
    for (d, _), p in zip(dims, parts):
        lo, _, hi = p.partition(":")
        if hi.strip() == "*" or lo.strip() == "*":
            sys.exit(f"axis '{d}' is a wildcard ({p}); rasdaman chooses its size, so it cannot be "
                     "modelled from the string -- dump real tile domains with "
                     "dbinfo(c,\"printtiles=embedded\") instead")
        chunk[d] = int(hi) - int(lo) + 1
    return m.group(1).upper(), chunk


# ------------------------------------------------------------------ output

def fmt_amp(x):
    return f"{x:,.0f}x" if x >= 100 else f"{x:,.2f}x"


def report(dims, chunk, bpc, spatial, time, condense_n, label, note="", scheme="ALIGNED",
           budget=None):
    a = analyse(dims, chunk, bpc, spatial, time, condense_n)
    shape = ", ".join(f"{d}={chunk[d]}" for d, _ in dims)
    print(f"\n[{label}]")
    print(f"  chunk        {shape}")
    print(f"  tiling       {bracket(dims, chunk, a['tile_bytes']) if scheme == 'ALIGNED' else '(as given)'}")
    print(f"  tile size    {a['tile_bytes']:,} bytes ({a['tile_bytes'] / MIB:.2f} MiB)")
    print(f"  total tiles  {a['total_tiles']:,}")
    print(f"  point amp    {fmt_amp(a['point_amp'])}   map amp {fmt_amp(a['map_amp'])}", end="")
    if "condense_amp_aligned" in a:
        print(f"   condense-{condense_n} amp {fmt_amp(a['condense_amp_aligned'])} aligned / "
              f"{fmt_amp(a['condense_amp_straddling'])} straddling", end="")
    print()
    rem = {d: r for d, r in a["remainders"].items() if r}
    if rem:
        print("  boundary     " + ", ".join(f"{d}: {dict(dims)[d]} % {chunk[d]} = {r}" for d, r in rem.items())
              + "  (no disk cost; prefer ALIGNED over REGULAR here -- REGULAR OOM risk)")
    if budget is None:
        # check mode: judge an existing tiling against rasdaman's 1-4 MB guidance
        if a["tile_bytes"] > 4 * MIB:
            print("  warning      tile is above rasdaman's 1-4 MB guidance")
        elif a["tile_bytes"] < 1_000_000:
            print("  note         tile is below rasdaman's 1-4 MB guidance (fine if deliberate)")
    elif a["tile_bytes"] > budget:
        print("  warning      tile exceeds the requested budget")
    elif a["tile_bytes"] < budget // 2:
        full = [d for d, s in dims if chunk[d] == s]
        print("  note         tile uses under half the budget"
              + (f" because {', '.join(full)} already span their full extent" if full else ""))
    if note:
        print(f"  note         {note}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dims", help='axes in gridOrder, e.g. "time=100,model=3,y=1941,x=2471"')
    src.add_argument("--ncdump", help="file containing `ncdump -h` output, or - for stdin")
    ap.add_argument("--bytes-per-cell", type=int,
                    help="sum of EVERY band's byte width (required with --dims)")
    ap.add_argument("--spatial", help="the two spatial axis names, comma separated (default: by name)")
    ap.add_argument("--time", help="time axis name (default: by name)")
    ap.add_argument("--budget-mib", default="4", help="comma-separated tile budgets in MiB (default 4)")
    ap.add_argument("--condense-n", type=int, help="also recommend a WCPS condense window of N time steps")
    ap.add_argument("--check", help="analyse this existing tiling string instead of recommending")
    args = ap.parse_args()

    calendars = {}
    if args.ncdump:
        text = sys.stdin.read() if args.ncdump == "-" else open(args.ncdump).read()
        dims, bpc, bands, calendars = parse_ncdump(text)
        print(f"bands ({len(bands)}): {', '.join(bands)}")
        if args.bytes_per_cell and args.bytes_per_cell != bpc:
            print(f"note: --bytes-per-cell {args.bytes_per_cell} overrides ncdump-derived {bpc}")
            bpc = args.bytes_per_cell
    else:
        if not args.bytes_per_cell:
            sys.exit("--bytes-per-cell is required with --dims (sum of every band's width)")
        dims, bpc = parse_dims(args.dims), args.bytes_per_cell

    spatial, time = classify(dims, args.spatial, args.time)
    logical = prod(s for _, s in dims) * bpc
    print("axes (gridOrder, as given): " + ", ".join(f"{d}={s}" for d, s in dims))
    print(f"bytes per cell: {bpc}   spatial: {spatial}   time: {time}")
    print(f"logical size: {logical:,} bytes ({logical / 1e9:.3f} GB) -- tiling does not change this")
    print("CHECK: bracket positions follow the recipe's gridOrder, not crs/DescribeCoverage order.")
    if time:
        warning = calendar_warning(time, dict(dims)[time], calendars.get(time))
        if warning:
            print(f"CALENDAR: {warning}")

    if args.check:
        scheme, chunk = parse_tiling(args.check, dims)
        report(dims, chunk, bpc, spatial, time, args.condense_n, f"check: {scheme}", scheme=scheme)
        if scheme == "REGULAR":
            print("  caution      REGULAR crashed Zeus (OOM) on a GIPL ingest where the identical ALIGNED "
                  "bracket succeeded; prefer ALIGNED unless every axis divides evenly")
        return

    if args.condense_n and not time:
        sys.exit("--condense-n needs a time axis; pass --time")
    for mib in [float(x) for x in args.budget_mib.split(",")]:
        budget = int(mib * MIB)
        print(f"\n===== budget {mib:g} MiB ({budget:,} bytes) =====")
        for wf in ["point", "map"] + (["condense"] if args.condense_n else []):
            chunk, note = recommend(dims, bpc, spatial, time, budget, wf, args.condense_n)
            label = {"point": "point / time-series (WCS)", "map": "map (WMS single slice)",
                     "condense": f"WCPS condense over {args.condense_n} {time} steps"}[wf]
            if wf == "condense":
                note = (note + "; " if note else "") + ("only benchmarked case (GIPL, N=30) was ~14x "
                                                         "SLOWER than a plain map scheme -- time it")
            report(dims, chunk, bpc, spatial, time, args.condense_n, label, note, budget=budget)


if __name__ == "__main__":
    main()
