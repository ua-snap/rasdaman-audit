#!/usr/bin/env python3
"""Build a small synthetic netCDF mirroring crrel_gipl_outputs_nc's structure
(time, model, scenario, y, x; ten float32 bands) so recommend_tiling.py can
be exercised end to end without needing the real, much larger file. Only
the dimension sizes/dtype/coordinate spacing matter for testing the tiling
math -- the data values themselves are meaningless filler.

Y/X are given in meters on an EPSG:3338 grid (matching the real coverage's
own CRS, per CRREL_GIPL_tiling.md) so the polygon workflow's CRS-aware
bbox-to-cells conversion has something real to chew on.
"""
import numpy as np
import xarray as xr

TIME, MODEL, SCENARIO, Y, X = 30, 3, 2, 50, 60
RES_M = 4000.0  # 4 km pixels, same spirit as the real ERA5/GIPL grids

y = np.arange(Y) * RES_M + 1_000_000.0   # arbitrary EPSG:3338-ish offsets
x = np.arange(X) * RES_M + 100_000.0

bands = ["magt05m_degC", "magt1m_degC", "magt2m_degC", "magt3m_degC", "magt4m_degC",
         "magt5m_degC", "magtsurface_degC", "permafrostbase_m", "permafrosttop_m",
         "talikthickness_m"]

data_vars = {}
rng = np.random.default_rng(0)
for b in bands:
    data_vars[b] = (("time", "model", "scenario", "y", "x"),
                     rng.random((TIME, MODEL, SCENARIO, Y, X)).astype("float32"))

ds = xr.Dataset(
    data_vars=data_vars,
    coords={"time": np.arange(TIME), "model": np.arange(MODEL),
            "scenario": np.arange(SCENARIO), "y": y, "x": x},
    attrs={"crs": "EPSG:3338"},
)
for b in bands:
    ds[b].attrs["_FillValue"] = -9999.0

out = __file__.rsplit("/", 1)[0] + "/synthetic_gipl_like.nc"
ds.to_netcdf(out)
print("wrote", out)
print(ds)
