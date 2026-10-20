# Benchmarking a tiling on Zeus

Modelled amplification only ranks candidates. Time them before adopting one. The harness is the JMeter setup in `rasdaman-ingest/benchmarks/`. To compare a handful of coverages it needs the uncommitted patch saved at `rasdaman-audit/docs/benchmark-harness.patch` (see `docs/benchmark-harness.md` for the full run book).

## What the patch adds

- **WCS and WMS request builders:** `--match REGEX` to select coverages, and `--keep-twins` so a base coverage isn't dropped when a `_wcs`/`_wms` twin exists.
- **WMS builder only:** `--num-requests N`, `--style NAME`, `--style COVERAGE=NAME`, and `--no-dims` (for WCPS styles that subset the non-spatial axes themselves).
- **Analyzers:** a `latency_first_ms` column, the closest thing to a cold read.
- **`wms_benchmarks.jmx`:** `-Jrequests=` / `-Jjtl=` overrides, and a 600 s timeout.

## Run (from `rasdaman-ingest/benchmarks/` — paths are relative)

```bash
git apply ~/rasdaman-audit/docs/benchmark-harness.patch
python get_zeus_capabilities.py
python build_wcs_benchmark_requests.py --match '^<prefix>' --keep-twins --num-locations 50 --seed 1
python build_wms_benchmark_requests.py --match '^<prefix>' --keep-twins --num-requests 10 --style <style> --out zeus_wms_slice_requests.csv
rm -f *_results.csv *_results.jtl      # JMeter appends to existing files
jmeter -n -t wcs_benchmarks.jmx -l wcs_benchmark_results.csv
jmeter -n -t wms_benchmarks.jmx -Jrequests=zeus_wms_slice_requests.csv -Jjtl=zeus_wms_slice_results.jtl -l wms_slice_results.csv
python analyze_wcs_results.py --results wcs_benchmark_results.csv
python analyze_wms_results.py --results wms_slice_results.csv --out-summary wms_slice_summary.csv
```

The summaries report mean, p95 and first-request latency, but no median; compute it from the raw CSV with pandas. Latency is JMeter's time to first byte.

## Gotchas that have bitten us

- **An empty `STYLES=` is not "no style".** WMS falls back to the layer default, which on the GIPL layers was a 30-year WCPS condense that ignores `time`/`model`/`scenario`. One whole test round measured the condense by accident. Always name a style, and verify it: request a small bbox (e.g. `BBOX=250000,1600000,350000,1700000`, 64×64, near Fairbanks) at two different times and compare md5s. A single-slice style returns different images.
- **Don't put `test` in a coverage ID you want to benchmark.** `get_zeus_capabilities.py` skips any ID containing it. The WCS builder skips IDs containing `wms`; the WMS builder skips `wcs` and `polygon`.
- **Cold vs. warm dominates.** One statewide condense map took 41.5 s cold and ~1.5 s warm. Any earlier read warms the tiles, including curl sanity checks and opening the map in a browser. Sanity-check with a small bbox and compare `latency_first_ms`. Zeus has no response cache, so repeated identical requests are fine.
- **Timeouts don't stop the server.** A point-tiled coverage's statewide map ran ~20 min before a `SocketTimeoutException`, and the server may keep working after the client gives up.
- **WMS GetCapabilities is ~138 MB and takes ~40 s.** Parse it with `xml.etree.ElementTree` (namespace `http://www.opengis.net/wms`), not grep.
- **Test-coverage recipes** are a copy of the production recipe with three changes: a new `coverage_id`, the new `tiling` line, and `COVERAGEID=` in the style hooks (or hooks dropped entirely for point/polygon layers).
