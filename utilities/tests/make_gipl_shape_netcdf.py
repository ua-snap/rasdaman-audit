#!/usr/bin/env python3
"""
Builds a netCDF with crrel_gipl_outputs_nc's REAL dimensions (time=100,
model=3, scenario=2, y=1941, x=2471; ten float32 bands; EPSG:3338), per
CRREL_GIPL_tiling.md sections 1-2 -- but sparsely, so it stays well under
1 MB on disk instead of the real coverage's ~115 GB.

recommend_tiling.py only reads a file's declared shape/dtype/CRS -- it never
needs the actual data values -- so each variable here is created with HDF5
chunking and only a single cell is ever written. Every other chunk is never
allocated, and xarray/netCDF4 still report the full, real logical shape (and
size) on open. This is how docs/CRREL_GIPL_tiling.md's utilities-generated
recommendations were produced without needing the real, much larger file.
"""
import netCDF4 as nc
import numpy as np

TIME, MODEL, SCENARIO, Y, X = 100, 3, 2, 1941, 2471
# Real values, confirmed against the live source file with ncdump -- CRREL_GIPL_tiling.md
# itself only gives cell counts, not real-world spacing:
#   ncdump -v x gipl_outputs_optimized.nc  -> x = -979291.709, -978291.709, ... (spacing 1000.0 m)
#   ncdump -v y gipl_outputs_optimized.nc  -> y = 2374979.751, 2373979.751, ... (spacing -1000.0 m)
Y_RES_M, X_RES_M = 1000.0, 1000.0
Y0, X0 = 2374979.751, -979291.709

OUT = __file__.rsplit("/", 1)[0] + "/gipl_shape.nc"

BANDS = ["magt05m_degC", "magt1m_degC", "magt2m_degC", "magt3m_degC", "magt4m_degC",
         "magt5m_degC", "magtsurface_degC", "permafrostbase_m", "permafrosttop_m",
         "talikthickness_m"]


def main():
    ds = nc.Dataset(OUT, "w", format="NETCDF4")
    ds.crs = "EPSG:3338"

    ds.createDimension("time", TIME)
    ds.createDimension("model", MODEL)
    ds.createDimension("scenario", SCENARIO)
    ds.createDimension("y", Y)
    ds.createDimension("x", X)

    y = ds.createVariable("y", "f8", ("y",))
    x = ds.createVariable("x", "f8", ("x",))
    y[:] = Y0 - np.arange(Y) * Y_RES_M  # descending, matching the real file
    x[:] = X0 + np.arange(X) * X_RES_M
    ds.createVariable("time", "i8", ("time",))[:] = np.arange(TIME)
    ds.createVariable("model", "i8", ("model",))[:] = np.arange(MODEL)
    ds.createVariable("scenario", "i8", ("scenario",))[:] = np.arange(SCENARIO)

    for b in BANDS:
        v = ds.createVariable(b, "f4", ("time", "model", "scenario", "y", "x"),
                               fill_value=-9999.0, zlib=False,
                               chunksizes=(1, 1, 1, 128, 128))
        v[0, 0, 0, 0, 0] = 1.0  # materialize the variable; every other chunk
                                 # stays unallocated

    ds.close()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
