# Benchmark harness for tiling tests

How the `crrel_gipl_outputs_nc` tiling schemes were timed (results: [`CRREL_GIPL_tiling.md`](CRREL_GIPL_tiling.md), section 7), and how to run the same kind of test on another coverage. It is written so a new session can pick it up cold.

The harness is the JMeter setup in `rasdaman-ingest/benchmarks/` (its README covers installing Java and JMeter). That code benchmarks *every* coverage on Zeus. To test a handful of coverages against each other, it was modified locally, and **those changes were never committed to `rasdaman-ingest`**. They're saved here as [`benchmark-harness.patch`](benchmark-harness.patch).

## 1. Apply the patch

```bash
cd ~/rasdaman-ingest
git apply ~/rasdaman-audit/docs/benchmark-harness.patch   # checked to apply cleanly to main, Oct 2026
```

If `benchmarks/` has changed since then and the patch no longer applies, section 2 describes each change well enough to redo by hand.

## 2. What the patch changes

| File | Change |
|---|---|
| `build_wcs_benchmark_requests.py` | `--match REGEX` keeps only matching coverage IDs. `--keep-twins` keeps a base coverage even when a `<id>_wcs` twin exists, which the script otherwise drops. |
| `build_wms_benchmark_requests.py` | `--match` and `--keep-twins` as above, for `<id>_wms` twins. `--num-requests N` repeats each request N times (default 1). `--style NAME` sets `STYLES=` for all coverages, and `--style COVERAGE=NAME` overrides it for one; both can be repeated. `--no-dims` leaves out the non-spatial axis parameters (`time=`, `model=`, …), for WCPS styles that subset those axes themselves. Coverage IDs containing `polygon` are now skipped, like `wcs` already was. |
| `analyze_wcs_results.py`, `analyze_wms_results.py` | New `latency_first_ms` column: the first request per coverage, the closest thing to a cold-cache read. |
| `wms_benchmarks.jmx` | Requests file and `.jtl` path can be overridden with `-Jrequests=…` and `-Jjtl=…`, so one test plan can run several request lists. Response timeout raised from 120 s to 600 s. |

`wcs_benchmarks.jmx` is unchanged.

## 3. Run a test

Everything runs from `rasdaman-ingest/benchmarks/`; the paths inside the scripts and test plans are relative. Replace the regex and style names to suit:

```bash
cd ~/rasdaman-ingest/benchmarks
python get_zeus_capabilities.py                  # writes zeus_coverages.csv (all coverages)

python build_wcs_benchmark_requests.py --match '^crrel_gipl_outputs_nc' --keep-twins --num-locations 50 --seed 1
python build_wms_benchmark_requests.py --match '^crrel_gipl_outputs_nc' --keep-twins --num-requests 10 \
  --style <single-slice style> --out zeus_wms_slice_requests.csv
python build_wms_benchmark_requests.py --match '^crrel_gipl_outputs_nc' --keep-twins --num-requests 10 --no-dims \
  --style arctic_eds_gipl_magt1m_nearcentury \
  --style crrel_gipl_outputs_nc=arctic_eds_gipl_magt1m_nearcentury2 \
  --out zeus_wms_condense_requests.csv

rm -f *_results.csv *_results.jtl                # JMeter appends to existing files
jmeter -n -t wcs_benchmarks.jmx -l wcs_benchmark_results.csv
jmeter -n -t wms_benchmarks.jmx -Jrequests=zeus_wms_slice_requests.csv -Jjtl=zeus_wms_slice_results.jtl -l wms_slice_results.csv
jmeter -n -t wms_benchmarks.jmx -Jrequests=zeus_wms_condense_requests.csv -Jjtl=zeus_wms_condense_results.jtl -l wms_condense_results.csv

python analyze_wcs_results.py --results wcs_benchmark_results.csv
python analyze_wms_results.py --results wms_slice_results.csv --out-summary wms_slice_summary.csv
python analyze_wms_results.py --results wms_condense_results.csv --out-summary wms_condense_summary.csv
```

What the requests are:
- **WCS:** one `GetCoverage` per random point within ±150 km of the coverage centroid, slicing X/Y only. It returns that cell's full time series for every other axis and band, as JSON.
- **WMS:** one statewide `GetMap` per request: the full coverage extent at 800 × 600 PNG, with non-spatial axes at their lower bounds unless `--no-dims` is given.

The summaries report JMeter's `Latency` (time to first byte) as mean, p95 and first. They don't report a median. To get one from the raw results:

```python
import pandas as pd
from urllib.parse import urlparse, parse_qs
d = pd.read_csv("wms_slice_results.csv")
q = d.URL.map(lambda u: parse_qs(urlparse(u).query))
d["cov"] = q.map(lambda x: (x.get("LAYERS") or x.get("COVERAGEID"))[0])
print(d.groupby("cov", sort=False).Latency.agg(["size", "median", lambda s: s.quantile(0.95), "first"]))
```

## 4. Test coverages and styles

**Test coverages.** Each GIPL test coverage was ingested from a copy of `ardac/gipl/ingest_with_nc.json` with three edits:
- a new `coverage_id`;
- the `tiling` line (the strings are in `CRREL_GIPL_tiling.md`, sections 4–5c);
- in the `hooks`, `COVERAGEID=` changed to the new ID so the WMS styles land on the test layer. The point and polygon recipes simply dropped the hooks, since those layers don't serve maps.

Those recipes lived on the `rasdaman-ingest` branch `gipl_ingest_testing` (commits `480ccdd`…`2f4ec96`), and the coverages and recipes have since been removed. **Don't put `test` in a coverage ID you want to benchmark:** `get_zeus_capabilities.py` silently skips any coverage whose ID contains it.

**Single-slice style.** Comparing single-time-slice maps needs a style that does *not* run WCPS. The one used here was a colormap-only style: the same `admin/layer/style/add` call the recipe hooks make (`COLORTABLETYPE=ColorMap` plus `COLORTABLEDEFINITION`), with no `WCPSQUERYFRAGMENT`. It was removed after the test.

**Check a style before benchmarking with it.** Request a small box at two different times. A single-slice style must return different images; a condense style returns identical ones. The box below is about 100 km × 100 km near Fairbanks, which has data and reads only a few tiles:

```bash
B='https://zeus.snap.uaf.edu/rasdaman/ows?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&CRS=EPSG:3338&BBOX=250000,1600000,350000,1700000&WIDTH=64&HEIGHT=64&FORMAT=image/png&LAYERS=<layer>&STYLES=<style>&model=0&scenario=0'
curl -s "$B&time=2021-01-01T00:00:00.000Z" | md5
curl -s "$B&time=2090-01-01T00:00:00.000Z" | md5
```

To list a layer's styles, use the WMS `GetCapabilities` document. On Zeus it is about 138 MB and takes about 40 s; parse it with `xml.etree.ElementTree` (namespace `http://www.opengis.net/wms`) rather than grep, because the `<Dimension>` lines are enormous.

## 5. Gotchas

- **An empty `STYLES=` is not "no style".** WMS uses the layer's default style. On every GIPL map layer that was a 30-year WCPS condense, which ignores `time`/`model`/`scenario` entirely. One test round measured the condense without knowing it. Always name the style.
- **The builders choose coverages by name.**
  - The WCS builder skips IDs containing `wms`. The WMS builder skips IDs containing `wcs` or `polygon`.
  - Without `--keep-twins`, a base coverage is dropped whenever an `_wcs`/`_wms` twin exists. That's how the current coverage silently went missing from one run.
  - Name test coverages with this in mind.
- **Cold vs. warm can dominate.** The current coverage's statewide condense map took 41.5 s cold and about 1.5 s warm.
  - Anything that reads the same tiles beforehand warms them: a curl sanity check, opening the map in a browser, or an earlier test.
  - Do sanity checks with a small BBOX, and compare `latency_first_ms`, not just the mean.
  - Repeating identical requests is fine: Zeus showed no response cache, and a shifted BBOX was no slower than an exact repeat.
- **A timeout can run much longer than the setting.** The polygon-tiled coverage's statewide map never finished; one request failed after ~20 min with `SocketTimeoutException`. JMeter's timeout doesn't stop the server, which may keep working on an abandoned request.
- **Run from `benchmarks/`.** From anywhere else JMeter can't find the `.jmx`, and an `rm` of old results silently deletes nothing.
