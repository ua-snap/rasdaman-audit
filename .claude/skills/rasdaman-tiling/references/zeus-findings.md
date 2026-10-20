# Zeus audit snapshot (as of 2026-09-23, rasdaman v10.4.7)

These figures go stale as soon as coverages change. Quote them with their date, and re-run the scripts (`references/verify-and-audit.md` §9) for current numbers.

## Headline numbers

| | |
|---|---|
| Live coverages | 273 |
| Real disk, live (`PhysicalSize`) | **9,172 GB**, matching the rasdaman UI's "9.17 TB" |
| `totalSize` claim, live | 14,000.5 GB (wrong; inflated by duplicate index entries) |
| Coverages with duplicate index entries | 26; extra disk cost **~0 GB** |
| Clean 1:1 index | 247 |
| Unreferenced (orphan) collections | 105, totalling 3,305 GB |
| — of which file-referenced (in situ) | 1 collection, 1,268 GB, not real rasdaman disk |
| **Genuinely recoverable** | **~2,037 GB**, pending in-situ checks on the next-largest orphans |
| Coverage ID ≠ collection name | 186 of 273 |

## Findings in one line each

1. **Duplicate index entries cost no disk.** Example: `tas_2km_projected_wcs` reports a `totalSize` of 2,321.9 GB against a `PhysicalSize` of 313.3 GB. Don't re-tile or re-ingest for space.
2. **Tiling often fights the access pattern.** Of 253 modelled coverages, 52 read more than 1,000× what a typical query returns, and 50 more than 10,000×. Same 460×442 ERA5 grid, three tilings:

   | Coverage | Tiling | Point amp | Map amp |
   |---|---|---|---|
   | `era5_4km_daily_t2_mean` | default, 5×460×442 | 203,355× | 5× |
   | `_wcs` variant | whole time axis, 8×8 spatial | 128× | 23,898× |
   | `cmip6_fwi` | whole time axis, 2×2 spatial | 4× | 25,725× |
3. **cmip6_downscaled v2 family (171 `_wcs` coverages): storage is healthy (0.50–1.01×), but map reads are terrible.** The tiling `ALIGNED [0:*, 0:31, 0:31] tile size 16777216` gives point reads of 256–448× but map reads of ~19,000–33,000×. Re-tiling for maps is the top performance item. Re-tile one, measure it, then batch the rest.
4. **11 coverages have a stored array that disagrees with `DescribeCoverage`.** They include the `ak_hydro_segments_*` and `conus_hydro_segments_*` groups (schema drift) and `cmip6_downscaled_pr_CNRM_CM6_1_HR_historical_v2_wcs`, which advertises 1×1×1 but stores 18,250×443×460 (a petascope bug worth reporting upstream). `crrel_gipl_outputs_nc`'s axis order is also wrong in `DescribeCoverage`. These need a human decision on which layer is authoritative.
5. **The 105 orphans by category:**

   | Category | Count | GB |
   |---|---|---|
   | Abandoned timestamped ingests | 23 | 1,398.7 |
   | Unclear | 16 | 882.0 |
   | Personal (named after a user) | 10 | 496.3 |
   | Test/scratch | 26 | 310.4 |
   | Shadow (at a live coverage's name) | 12 | 217.4 |
   | Superseded | 17 | 0 |

   50 are stubs of ≤1 MB. `alfresco_relative_flammability_30yr_crstephenson` was re-ingested 12 times on 2025-01-29. The 1,268 GB in-situ orphan is `cmip6_downscaled_tasmax_complete_crstephenson_2025_09_22_12_03_04_2874`. Not yet in-situ-checked: `cmip6_downscaled_tasmax_complete_crstephenson` (229.7 GB), `big_tile_ardac_chukchi_daily_slie` (599.7 GB) and `crrel_gipl_outputs` (115.1 GB).
6. **Reproducibility gaps:**
   - 24 live coverages have no recipe on `rasdaman-ingest` `origin/main`.
   - 45 declare no tiling and got rasdaman's default (~4 MB cubes, the wrong shape for point queries).
   - 7 test coverages (351 GB) are visible in the public `GetCapabilities`.

## Recommended sequence (from the audit)

1. Drop orphans: stubs first, then the in-situ orphan (catalog cleanup, ~0 GB), then the rest, largest first. Each drop needs `oid` verification and an in-situ check first.
2. Re-tile for read performance, starting with the v2 family for maps, then the 45 default-tiled coverages and the worst amplifiers. This has no storage effect.
3. Resolve the 11 integrity disagreements.
4. Housekeeping: delete the test coverages, commit the missing recipes, and enforce the never-re-ingest-into-a-live-ID rule.

## Open questions (don't present these as settled)

- Does a bloated tile index (up to 16 entries per domain) cost query latency? Untested.
- What causes the duplicates? Re-running `wcst_import` is the leading theory, and the decisive test hasn't been run: ingest a throwaway coverage, re-run the recipe without dropping it, and measure. Anomalies cluster at index 0 of the leading `gridOrder` axis.
- Why did the 30-step condense tiling lose by 14×? Uninvestigated.
- Does `REGULAR` get a faster computed index (`rc_index`)? Claimed only on an old wiki page. Dump a `REGULAR` coverage's `dbinfo` `index.type` to check.
- Does `drop collection` return its blob space to the filesystem immediately? No drop has been measured yet. Record `df`/`du` of the TILES directory before and after the first one.
- What explains the 421.9 GB by which `PhysicalSize` falls below logical size across 41 coverages? Unexplained.
