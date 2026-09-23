"""
tiling_lib.py

Shared core for recommend_tiling.py: netCDF introspection, the budget-solving
math behind every tiling recommendation, and rasdaman bracket formatting.

Every recommendation this module produces is a FULLY EXPLICIT numeric
bracket -- no `0:*` wildcards. This is a deliberate choice, not an
oversight: rasdaman-tiling-audit.md's Finding 3 found that ALIGNED's
wildcard-fill doesn't reliably match the byte-budget math this whole audit
(and rasdaman's own docs) describe, once more than one axis is wildcarded --
real, tile-dump-confirmed shapes on this server diverged from the
theoretical solve by orders of magnitude for some coverages. Since every
axis's real extent is already known once a netCDF file is in hand, there is
no reason to delegate anything to rasdaman here -- compute every chunk size
directly and hand over a bracket rasdaman cannot reinterpret.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import xarray as xr

# Same heuristic already used in scripts/rasdaman_tiling_audit.py, reused
# here so "what counts as a spatial axis" means the same thing everywhere
# in this repo.
SPATIAL_NAMES = {"x", "y", "lat", "lon", "long", "latitude", "longitude",
                  "e", "n", "easting", "northing"}
TIME_NAMES = {"time", "ansi", "date", "t"}


class TilingLibError(Exception):
    pass


# --------------------------------------------------------------- netCDF introspection

@dataclass
class VariableGroup:
    """One or more data variables that share an identical dimension
    signature (same dim names, same order, same sizes) -- i.e. the set of
    'bands' rasdaman would tile together as one struct cell."""
    dims: tuple                  # dimension names, in the file's own order
    sizes: dict                  # dim name -> extent
    band_vars: list              # variable names in this group
    bytes_per_band: list         # one dtype size per band, in file order
    dtype_names: list

    @property
    def band_count(self):
        return len(self.band_vars)

    @property
    def bytes_per_cell(self):
        """Every band counts (guide, section 4) -- not just one band's width."""
        return sum(self.bytes_per_band)


def inspect_netcdf(path, variable=None):
    """Group every data variable in the file by its dimension signature and
    return the VariableGroup rasdaman would tile as one array -- the
    largest group (most bands) by default, or the group containing
    `variable` if given.

    Coordinate variables (anything that is itself a dimension, e.g. `time`,
    `x`, `y`) are excluded -- they aren't tiled as data bands.
    """
    ds = xr.open_dataset(path, decode_times=False)
    try:
        groups = {}
        for name, da in ds.data_vars.items():
            if name in ds.dims:
                continue
            dims = tuple(da.dims)
            if not dims:
                continue
            key = dims
            groups.setdefault(key, []).append(name)

        if not groups:
            raise TilingLibError(
                f"No gridded data variables found in {path} (only coordinate "
                f"variables / scalars?).")

        if variable is not None:
            match = None
            for dims, names in groups.items():
                if variable in names:
                    match = dims
                    break
            if match is None:
                raise TilingLibError(
                    f"--variable {variable!r} not found among this file's data "
                    f"variables: {sorted(v for names in groups.values() for v in names)}")
            chosen_dims = match
        else:
            # Default: the group with the most bands sharing one domain --
            # this is almost always "the" variable set a coverage is built
            # from (e.g. ten bands sharing one (time,model,scenario,y,x)
            # domain, per CRREL_GIPL_tiling.md).
            chosen_dims = max(groups, key=lambda k: len(groups[k]))

        band_vars = groups[chosen_dims]
        sizes = {d: ds.sizes[d] for d in chosen_dims}
        bytes_per_band = []
        dtype_names = []
        for v in band_vars:
            dt = ds[v].dtype
            bytes_per_band.append(dt.itemsize)
            dtype_names.append(str(dt))

        return VariableGroup(dims=chosen_dims, sizes=sizes, band_vars=band_vars,
                              bytes_per_band=bytes_per_band, dtype_names=dtype_names), ds
    except Exception:
        ds.close()
        raise


@dataclass
class AxisRoles:
    spatial: tuple    # exactly 2 dim names, in the file's own dimension order
    time: str or None
    other: list       # every other non-spatial, non-time dim


def classify_axes(dims, x_dim=None, y_dim=None, time_dim=None):
    """Which of this variable's dims are the spatial pair, which is time,
    and which are 'other' (model, scenario, band-like index axes, ...).

    Name-based heuristics can be overridden with explicit --x-dim/--y-dim/
    --time-dim, which recommend_tiling.py exposes for exactly the case
    where a file's dimension names don't match the common conventions.
    """
    lower = {d: d.lower() for d in dims}

    if x_dim and y_dim:
        if x_dim not in dims or y_dim not in dims:
            raise TilingLibError(f"--x-dim/--y-dim must name real dimensions of "
                                  f"this variable: {dims}")
        spatial = (y_dim, x_dim) if dims.index(y_dim) < dims.index(x_dim) else (x_dim, y_dim)
        # preserve file order regardless of which flag was x vs y
        spatial = tuple(d for d in dims if d in (x_dim, y_dim))
    else:
        found = [d for d in dims if lower[d] in SPATIAL_NAMES]
        if len(found) != 2:
            raise TilingLibError(
                f"Could not identify exactly 2 spatial axes by name among {dims} "
                f"(found {found}). Pass --x-dim/--y-dim explicitly.")
        spatial = tuple(found)

    if time_dim:
        if time_dim not in dims:
            raise TilingLibError(f"--time-dim {time_dim!r} is not one of this "
                                  f"variable's dimensions: {dims}")
        time = time_dim
    else:
        time_candidates = [d for d in dims if lower[d] in TIME_NAMES and d not in spatial]
        time = time_candidates[0] if time_candidates else None

    other = [d for d in dims if d not in spatial and d != time]
    return AxisRoles(spatial=spatial, time=time, other=other)


# --------------------------------------------------------------- budget math

def product(values):
    out = 1
    for v in values:
        out *= v
    return out


@dataclass
class SpatialChunk:
    chunk: dict          # spatial dim name -> chunk size (cells)
    budget_cells: int    # cells the spatial pair was allowed to spend
    note: str = ""


def solve_square_footprint(spatial_extents, fixed_cells, bytes_per_cell, budget_bytes):
    """Point / map / condense workflows: no particular aspect ratio wanted,
    just the largest square-ish footprint the remaining budget allows.
    Same method as scripts/build_workbook.py's recommend() and
    CRREL_GIPL_tiling.md sections 4/5, reused here for consistency."""
    fixed_cells = max(1, fixed_cells)
    budget_cells = budget_bytes // (bytes_per_cell * fixed_cells)
    if budget_cells < 1:
        return SpatialChunk(chunk={d: 1 for d in spatial_extents}, budget_cells=0,
                             note="budget_bytes too small for even a 1x1 spatial "
                                  "footprint at this fixed-axis size -- see the "
                                  "non-spatial fallback note, or raise the tile size")
    side = int(math.floor(math.sqrt(budget_cells)))
    side = max(1, side)
    chunk = {d: min(side, ext) for d, ext in spatial_extents.items()}
    return SpatialChunk(chunk=chunk, budget_cells=budget_cells)


def solve_rect_footprint(spatial_extents, fixed_cells, bytes_per_cell, budget_bytes,
                          aspect_dims_cells):
    """Polygon workflow: size the spatial footprint to the budget while
    preserving a target aspect ratio (a representative polygon bbox, in
    grid cells) rather than forcing a square -- a small tile shaped like a
    typical query polygon touches fewer tiles than a square one that's
    much wider or narrower than the shape actually being queried.

    aspect_dims_cells: {spatial_dim_name: representative_cells}, same keys
    as spatial_extents.
    """
    fixed_cells = max(1, fixed_cells)
    budget_cells = budget_bytes // (bytes_per_cell * fixed_cells)
    dims = list(spatial_extents.keys())
    w = aspect_dims_cells[dims[0]]
    h = aspect_dims_cells[dims[1]]
    if budget_cells < 1 or w <= 0 or h <= 0:
        return SpatialChunk(chunk={d: 1 for d in spatial_extents}, budget_cells=0,
                             note="budget too small for even a 1x1 spatial footprint")
    k = math.sqrt(budget_cells / (w * h))
    chunk = {
        dims[0]: max(1, min(int(math.ceil(k * w)), spatial_extents[dims[0]])),
        dims[1]: max(1, min(int(math.ceil(k * h)), spatial_extents[dims[1]])),
    }
    return SpatialChunk(chunk=chunk, budget_cells=budget_cells)


@dataclass
class NonSpatialFit:
    chunk: dict          # non-spatial dim name -> chunk size
    shrunk: bool
    note: str = ""


def fit_non_spatial_full(non_spatial_extents, bytes_per_cell, budget_bytes,
                          min_spatial_cells=1):
    """'Full non-spatiotemporal dimensions ... when possible' -- try keeping
    every non-spatial axis (time + other) whole inside the tile. If that
    alone would eat the entire byte budget and leave no room for even a
    1-cell spatial footprint, shrink the single largest non-spatial axis
    (repeating if needed) until at least `min_spatial_cells` is left over,
    rather than silently emitting an unusable tile.
    """
    chunk = dict(non_spatial_extents)
    if not chunk:
        return NonSpatialFit(chunk=chunk, shrunk=False)

    def fixed_bytes():
        return product(chunk.values()) * bytes_per_cell

    shrunk = False
    guard = 0
    while fixed_bytes() * min_spatial_cells > budget_bytes and guard < 10000:
        guard += 1
        biggest = max(chunk, key=lambda d: chunk[d])
        if chunk[biggest] <= 1:
            break  # every non-spatial axis is already at 1; nothing left to shrink
        other_fixed = product(v for d, v in chunk.items() if d != biggest) or 1
        target = budget_bytes // (bytes_per_cell * other_fixed * min_spatial_cells)
        target = max(1, min(int(target), chunk[biggest] - 1))
        chunk[biggest] = target
        shrunk = True

    note = ""
    if fixed_bytes() * min_spatial_cells > budget_bytes:
        note = ("could not fit even a 1x1 spatial footprint at this tile-size "
                "budget after shrinking every non-spatial axis to 1 -- this "
                "tile size is not viable for this variable's band count/dtype; "
                "try a larger tile-size budget")
    elif shrunk:
        note = "kept non-spatial axes whole where possible; shrank the largest one to fit the budget"

    return NonSpatialFit(chunk=chunk, shrunk=shrunk, note=note)


# --------------------------------------------------------------- CRS / polygon-to-cells

def detect_crs(ds):
    """Best-effort CRS detection from common netCDF conventions (a global
    or grid_mapping variable attribute naming an EPSG code, or a CF
    grid_mapping variable with spatial_ref/crs_wkt). Returns an EPSG string
    or None if nothing was found -- callers should require --crs from the
    user in that case rather than silently guessing."""
    import re
    candidates = []
    for attrs in (ds.attrs,):
        for k, v in attrs.items():
            if isinstance(v, str) and ("crs" in k.lower() or "proj" in k.lower()):
                candidates.append(v)
    for name, da in ds.variables.items():
        for k, v in da.attrs.items():
            if isinstance(v, str) and k.lower() in (
                    "crs", "spatial_ref", "crs_wkt", "esri_pe_string", "proj4"):
                candidates.append(v)
    for c in candidates:
        m = re.search(r"EPSG:?\s*(\d+)", c, re.IGNORECASE)
        if m:
            return f"EPSG:{m.group(1)}"
        candidates_wkt = c
    return None


def polygon_bbox_to_cells(width_m, height_m, netcdf_crs, center_x_native, center_y_native,
                           x_res_native, y_res_native):
    """Convert a representative polygon bbox (meters, EPSG:3338 -- see
    fetch_boundaries.py) into grid cells in THIS netCDF's own spatial
    resolution and CRS, whatever that CRS is. Builds a box of the given
    width/height centered on the netCDF's own spatial centroid (transformed
    into EPSG:3338), then measures that box back in the netCDF's native
    coordinate units -- correct for a projected or a geographic target CRS
    alike, with no hardcoded meters-per-degree approximation.
    """
    from pyproj import Transformer

    to_3338 = Transformer.from_crs(netcdf_crs, "EPSG:3338", always_xy=True)
    from_3338 = Transformer.from_crs("EPSG:3338", netcdf_crs, always_xy=True)

    cx, cy = to_3338.transform(center_x_native, center_y_native)
    x0, y0 = from_3338.transform(cx - width_m / 2, cy - height_m / 2)
    x1, y1 = from_3338.transform(cx + width_m / 2, cy + height_m / 2)

    native_width = abs(x1 - x0)
    native_height = abs(y1 - y0)
    cells_x = native_width / abs(x_res_native)
    cells_y = native_height / abs(y_res_native)
    return cells_x, cells_y


# --------------------------------------------------------------- formatting

def format_aligned(dims_order, chunk_by_dim, tile_size_bytes):
    """'ALIGNED [0:c1-1, 0:c2-1, ...] tile size N', axes in the variable's
    own dimension order (gridOrder) -- guide section 2's rule: the bracket
    is positional and follows gridOrder, never crs/catalogue order."""
    parts = ", ".join(f"0:{chunk_by_dim[d] - 1}" for d in dims_order)
    return f"ALIGNED [{parts}] tile size {tile_size_bytes}"
