#!/usr/bin/env python3
"""
recommend_tiling.py

Given a netCDF file, recommends rasdaman tiling schemes (fully explicit
ALIGNED brackets, no wildcards -- see tiling_lib.py's module docstring for
why) for four query workflows, at four tile-size budgets:

  1. point         -- single XY, every non-spatial axis whole (incl. time)
  2. polygon       -- XY sized to a small/medium/large real query polygon,
                       every non-spatial axis whole (incl. time)
  3. map           -- large XY, every non-spatial axis (incl. time) pinned to 1
  4. wcps_condense -- large XY, non-time non-spatial axes pinned to 1, time
                       chunked to a user-supplied N (never guessed -- pass
                       --condense-n)

Usage
-----
    mamba activate rasdaman-tiling-utils
    python3 recommend_tiling.py --netcdf /path/to/file.nc

    # with everything available:
    python3 recommend_tiling.py --netcdf file.nc \\
        --variable magt1m_degC --tile-sizes 1,2,4 --condense-n 30 \\
        --crs EPSG:3338 --out report.json

If the spatial/time axes can't be identified by name, override with
--x-dim/--y-dim/--time-dim. If the file's CRS can't be detected, pass
--crs explicitly (e.g. EPSG:3338, EPSG:4326).
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

import tiling_lib as tl

HERE = Path(__file__).resolve().parent
DEFAULT_BUCKETS = HERE / "data" / "polygon_area_buckets.json"

MIB = 1024 * 1024


def load_buckets(path):
    with open(path) as f:
        return json.load(f)


def axis_extent_dict(sizes, dims):
    return {d: sizes[d] for d in dims}


def spatial_resolution_and_centroid(ds, x_dim, y_dim):
    x = np.asarray(ds[x_dim].values, dtype=float)
    y = np.asarray(ds[y_dim].values, dtype=float)
    x_res = float(np.mean(np.abs(np.diff(x)))) if len(x) > 1 else 1.0
    y_res = float(np.mean(np.abs(np.diff(y)))) if len(y) > 1 else 1.0
    return x_res, y_res, float(x.mean()), float(y.mean())


def build_report(args):
    group, ds = tl.inspect_netcdf(args.netcdf, variable=args.variable)
    try:
        roles = tl.classify_axes(group.dims, x_dim=args.x_dim, y_dim=args.y_dim,
                                  time_dim=args.time_dim)
        # roles.spatial is in file order; figure out which is which by name
        # for reporting purposes only (doesn't affect the math, which is
        # symmetric in the two spatial axes).
        lower = {d: d.lower() for d in roles.spatial}
        x_dim = next((d for d in roles.spatial if lower[d] in
                      {"x", "lon", "long", "longitude", "e", "easting"}), roles.spatial[0])
        y_dim = next((d for d in roles.spatial if d != x_dim), roles.spatial[-1])

        bytes_per_cell = group.bytes_per_cell
        spatial_extents = axis_extent_dict(group.sizes, roles.spatial)
        other_extents = axis_extent_dict(group.sizes, roles.other)
        time_extent = {roles.time: group.sizes[roles.time]} if roles.time else {}

        tile_sizes_bytes = [int(round(mb * MIB)) for mb in args.tile_sizes]

        report = {
            "file": str(args.netcdf),
            "variable_group": {
                "bands": group.band_vars,
                "band_count": group.band_count,
                "dtype_names": group.dtype_names,
                "bytes_per_cell": bytes_per_cell,
                "dims_in_file_order": list(group.dims),
                "extents": dict(group.sizes),
            },
            "axis_roles": {
                "spatial": list(roles.spatial), "x_dim": x_dim, "y_dim": y_dim,
                "time": roles.time, "other": roles.other,
            },
            "tile_sizes_mib": args.tile_sizes,
            "workflows": {},
        }

        # ---- workflow 1: point ----
        full_non_spatial = dict(other_extents)
        full_non_spatial.update(time_extent)
        point_results = []
        for mb, budget in zip(args.tile_sizes, tile_sizes_bytes):
            fit = tl.fit_non_spatial_full(full_non_spatial, bytes_per_cell, budget)
            fixed_cells = tl.product(fit.chunk.values()) or 1
            spatial = tl.solve_square_footprint(spatial_extents, fixed_cells,
                                                 bytes_per_cell, budget)
            chunk = {**fit.chunk, **spatial.chunk}
            point_results.append(workflow_row(mb, budget, group, chunk, fit.note, spatial.note))
        report["workflows"]["point"] = point_results

        # ---- workflow 2: polygon (small/medium/large) ----
        if roles.time or roles.other:
            pass  # no special handling needed; same full_non_spatial as point
        polygon_results = {}
        buckets = load_buckets(args.polygon_buckets) if args.polygon_buckets else None
        if buckets:
            crs = args.crs or tl.detect_crs(ds)
            if not crs:
                report["workflows"]["polygon"] = {
                    "error": "Could not detect this file's CRS and none was given "
                             "via --crs; skipping the polygon workflow. Pass e.g. "
                             "--crs EPSG:3338 or --crs EPSG:4326."}
            else:
                x_res, y_res, cx, cy = spatial_resolution_and_centroid(ds, x_dim, y_dim)
                for label, stats in buckets["buckets"].items():
                    cells_x, cells_y = tl.polygon_bbox_to_cells(
                        stats["bbox_width_m_median"], stats["bbox_height_m_median"],
                        crs, cx, cy, x_res, y_res)
                    aspect = {x_dim: max(1, round(cells_x)), y_dim: max(1, round(cells_y))}
                    rows = []
                    for mb, budget in zip(args.tile_sizes, tile_sizes_bytes):
                        fit = tl.fit_non_spatial_full(full_non_spatial, bytes_per_cell, budget)
                        fixed_cells = tl.product(fit.chunk.values()) or 1
                        spatial = tl.solve_rect_footprint(spatial_extents, fixed_cells,
                                                           bytes_per_cell, budget, aspect)
                        chunk = {**fit.chunk, **spatial.chunk}
                        rows.append(workflow_row(mb, budget, group, chunk, fit.note, spatial.note))
                    polygon_results[label] = {
                        "source_bucket_stats": stats,
                        "target_footprint_cells_at_this_resolution":
                            {x_dim: cells_x, y_dim: cells_y},
                        "tile_sizes": rows,
                    }
                report["workflows"]["polygon"] = polygon_results
        else:
            report["workflows"]["polygon"] = {
                "error": "No polygon bucket file available (see --polygon-buckets / "
                         "fetch_boundaries.py); skipping the polygon workflow."}

        # ---- workflow 3: map (single time slice, full domain) ----
        pinned_other = {d: 1 for d in roles.other}
        pinned_all = dict(pinned_other)
        if roles.time:
            pinned_all[roles.time] = 1
        map_results = []
        for mb, budget in zip(args.tile_sizes, tile_sizes_bytes):
            fixed_cells = tl.product(pinned_all.values()) or 1
            spatial = tl.solve_square_footprint(spatial_extents, fixed_cells,
                                                 bytes_per_cell, budget)
            chunk = {**pinned_all, **spatial.chunk}
            map_results.append(workflow_row(mb, budget, group, chunk, "", spatial.note))
        report["workflows"]["map"] = map_results

        # ---- workflow 4: WCPS condensed map (N-step time slice) ----
        if not roles.time:
            report["workflows"]["wcps_condense"] = {
                "error": "This variable has no recognized time axis; the condense "
                         "workflow doesn't apply."}
        elif args.condense_n is None:
            report["workflows"]["wcps_condense"] = {
                "error": "pass --condense-n to size this workflow (never "
                         "guessed -- e.g. --condense-n 30 for a 30-step/year "
                         "climatology, --condense-n 12 for monthly-to-annual)."}
        else:
            n = args.condense_n
            if n > group.sizes[roles.time]:
                report["workflows"]["wcps_condense"] = {
                    "error": f"--condense-n {n} exceeds this variable's own time "
                             f"extent ({group.sizes[roles.time]})."}
            else:
                condense_results = []
                pinned_other_only = {d: 1 for d in roles.other}
                for mb, budget in zip(args.tile_sizes, tile_sizes_bytes):
                    fixed = dict(pinned_other_only)
                    fixed[roles.time] = n
                    fixed_cells = tl.product(fixed.values()) or 1
                    spatial = tl.solve_square_footprint(spatial_extents, fixed_cells,
                                                         bytes_per_cell, budget)
                    chunk = {**fixed, **spatial.chunk}
                    condense_results.append(
                        workflow_row(mb, budget, group, chunk, "", spatial.note,
                                     extra={"condense_n": n}))
                report["workflows"]["wcps_condense"] = condense_results

        return report
    finally:
        ds.close()


def workflow_row(mb, budget_bytes, group, chunk, non_spatial_note, spatial_note, extra=None):
    tile_cells = tl.product(chunk[d] for d in group.dims)
    row = {
        "tile_size_mib": mb,
        "chunk": {d: chunk[d] for d in group.dims},
        "tiling_string": tl.format_aligned(group.dims, chunk, budget_bytes),
        "tile_cells": tile_cells,
        "tile_bytes": tile_cells * group.bytes_per_cell,
        "tile_mib": round(tile_cells * group.bytes_per_cell / MIB, 3),
    }
    notes = [n for n in (non_spatial_note, spatial_note) if n]
    if notes:
        row["note"] = "; ".join(notes)
    if extra:
        row.update(extra)
    return row


def print_readable(report):
    print(f"\nFile: {report['file']}")
    vg = report["variable_group"]
    print(f"Variable group: {vg['bands']} ({vg['band_count']} band(s), "
          f"{vg['bytes_per_cell']} bytes/cell)")
    print(f"Dims (file order): {vg['dims_in_file_order']} = {vg['extents']}")
    ar = report["axis_roles"]
    print(f"Spatial axes: {ar['spatial']}  Time axis: {ar['time']}  Other: {ar['other']}")

    for wf_name, wf in report["workflows"].items():
        print(f"\n--- {wf_name} ---")
        if isinstance(wf, dict) and "error" in wf:
            print(f"  (skipped: {wf['error']})")
            continue
        if wf_name == "polygon":
            for label, info in wf.items():
                print(f"  [{label}] target footprint (cells): "
                      f"{info['target_footprint_cells_at_this_resolution']}")
                for row in info["tile_sizes"]:
                    print_row(row)
        else:
            for row in wf:
                print_row(row)


def print_row(row):
    note = f"  ({row['note']})" if row.get("note") else ""
    print(f"    {row['tile_size_mib']:>4} MiB target -> {row['tiling_string']}"
          f"  [{row['tile_mib']} MiB actual]{note}")


def parse_tile_sizes(s):
    return [float(x) for x in s.split(",")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--netcdf", required=True, help="Path to the netCDF file")
    ap.add_argument("--variable", default=None,
                     help="Which data variable's dimension group to target "
                          "(default: the group with the most bands)")
    ap.add_argument("--x-dim", default=None)
    ap.add_argument("--y-dim", default=None)
    ap.add_argument("--time-dim", default=None)
    ap.add_argument("--crs", default=None,
                     help="Override CRS detection, e.g. EPSG:3338 or EPSG:4326")
    ap.add_argument("--tile-sizes", default="1,2,3,4", type=parse_tile_sizes,
                     help="Comma-separated tile-size budgets in MiB (default: 1,2,3,4)")
    ap.add_argument("--condense-n", type=int, default=None,
                     help="Time steps per WCPS condense window (workflow 4). "
                          "Always required explicitly -- never guessed.")
    ap.add_argument("--polygon-buckets", default=str(DEFAULT_BUCKETS),
                     help="Path to polygon_area_buckets.json (see fetch_boundaries.py)")
    ap.add_argument("--out", default=None, help="Write the full JSON report here")
    args = ap.parse_args()

    if not Path(args.polygon_buckets).exists():
        args.polygon_buckets = None

    try:
        report = build_report(args)
    except tl.TilingLibError as exc:
        sys.exit(f"error: {exc}")

    print_readable(report)

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
