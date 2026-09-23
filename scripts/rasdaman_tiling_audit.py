#!/usr/bin/env python3
"""
rasdaman_tiling_audit.py

One-pass tiling audit for every coverage on a rasdaman server.

Run it once. It produces a single CSV that says, per coverage, what tiling was
actually built, whether it wastes space on padding, and how much I/O a typical
query costs. No second step, no manual probing.

WHY IT COLLECTS WHAT IT DOES
----------------------------
Three independent sources, because no single one is sufficient and each covers
the others' failure modes:

  1. WCS DescribeCoverage (NO credentials needed)
     Grid-order axis labels and the real grid extent. This is the backbone:
     it works even when rasql refuses, and it is the only thing that tells you
     WHICH axis sits in which tiling-bracket position. Without axis identity
     you cannot tell a good tile shape from a bad one.

  2. rasql dbinfo  -> the tiling rasdaman actually built, plus band types.
     Queried WITHOUT printtiles by default: the per-tile domain listing adds
     nothing the audit needs and, on coverages with millions of tiles, produces
     a response large enough to arrive truncated.

  3. rasql sdom    -> the stored array's real extent, as a cross-check on (1).

If a dbinfo query fails, the run does not lose the coverage: it still emits a
complete row built from DescribeCoverage plus the declared tiling from your
ingest recipes.

WHEN A dbinfo QUERY FAILS
-------------------------
Every failure records the server's OWN error text -- the OWS exception code and
message, not just a status code -- and the coverage still gets a complete row
from DescribeCoverage plus its declared recipe tiling. Nothing drops out.

OUTPUTS (in --outdir, created if absent)
---------------------------------------
  _summary.csv       one row per coverage -- the audit deliverable
  _compact/<id>.json per-coverage raw dbinfo + describe facts
  _coverage_ids.txt  every coverage ID found
  _errors.log        every failure, with the server's own message
  _run.json          run metadata

KEY COLUMNS
-----------
  storage_overhead_factor   persisted bytes / real data bytes. 1.0 = no waste.
  per_axis_divisibility     per-axis 'ok' or 'PAD'. Catches an axis-order swap
                            that the tile-size arithmetic alone cannot see.
  point_query_amplification bytes read to answer a full time series at ONE x/y
                            divided by bytes actually wanted. The metric that
                            matters for point and small-AOI access.
  map_query_amplification   same idea for a full XY slice at one index on the
                            other axes. The metric that matters for WMS.

Python 3.6+, standard library only.

    export RASDAMAN_USER=rasadmin
    export RASDAMAN_PASS='...'

    python3 rasdaman_tiling_audit.py \
        --url 'https://zeus.snap.uaf.edu/rasdaman/ows?&SERVICE=WCS&ACCEPTVERSIONS=2.1.0&REQUEST=GetCapabilities' \
        --outdir ./tiling_audit \
        --recipes /path/to/rasdaman-ingest
"""

import argparse
import base64
import csv
import itertools
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

TYPE_BYTES = {
    "bool": 1, "char": 1, "octet": 1,
    "short": 2, "ushort": 2, "unsigned short": 2,
    "long": 4, "ulong": 4, "unsigned long": 4,
    "float": 4, "double": 8,
    "complex": 8, "complexd": 16,
}

# Axis names that mean "space". Used to decide which axes a point query pins
# and which it sweeps -- the basis of the amplification metrics.
SPATIAL_NAMES = {"x", "y", "lat", "lon", "long", "latitude", "longitude",
                 "e", "n", "easting", "northing"}

RANGE_RE = re.compile(r"(-?\d+)\s*:\s*(-?\d+)")


# ---------------------------------------------------------------- HTTP

class RasqlError(Exception):
    """Carries the server's own explanation. rasdaman puts its real diagnostic
    in the response BODY of a 4xx/5xx; discarding it turns every distinct
    failure into an indistinguishable 'HTTP Error 500'."""

    def __init__(self, status, body):
        self.status = status
        self.body = (body or "").strip()
        # An OWS ExceptionReport buries the actual message a long way past the
        # XML preamble, so truncating the raw body throws away the only part
        # that says anything. Pull the message out first.
        msg = ""
        hit = re.search(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>", self.body)
        if hit:
            msg = re.sub(r"\s+", " ", hit.group(1)).strip()
            code = re.search(r'exceptionCode="([^"]+)"', self.body)
            if code:
                msg = "[{}] {}".format(code.group(1), msg)
        if not msg:
            msg = re.sub(r"\s+", " ", self.body)
        short = msg[:600] or "(empty response body)"
        super(RasqlError, self).__init__("HTTP {} -- {}".format(status, short))


def make_ssl_context(insecure):
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(url, timeout, ctx, auth=None):
    req = urllib.request.Request(url, method="GET")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RasqlError(exc.code, body)


def rasql(url, query, timeout, ctx, auth, max_bytes=None):
    """max_bytes reads only the head of the response. That is what makes
    --verify-tiles affordable: a printtiles listing for a million-tile coverage
    is hundreds of MB, but the first couple of MB already show how the
    partition is laid out."""
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(max_bytes) if max_bytes else resp.read()
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RasqlError(exc.code, body)


def basic_auth_header(user, password):
    raw = "{}:{}".format(user, password).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------- WCS

def scrape_coverage_ids(xml_text):
    root = ET.fromstring(xml_text)
    ids, seen = [], set()
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "CoverageId" and elem.text:
            cid = elem.text.strip()
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
    return ids


def describe_coverage(ows_url, coverage_id, timeout, ctx, auth):
    """Grid-order axis labels + real grid extent + band names, straight from
    petascope. Needs no rasdaman credentials, so this still works for coverages
    whose rasql path fails."""
    sep = "&" if "?" in ows_url else "?"
    url = ("{}{}SERVICE=WCS&VERSION=2.0.1&REQUEST=DescribeCoverage"
           "&COVERAGEID={}".format(ows_url, sep, urllib.parse.quote(coverage_id)))
    text = http_get(url, timeout, ctx, auth)

    info = {"describe_ok": False}

    exc = re.search(r"<ows:ExceptionText>([\s\S]{0,400}?)</ows:ExceptionText>", text)
    if exc:
        info["describe_error"] = re.sub(r"\s+", " ", exc.group(1)).strip()[:300]
        return info

    # gml:axisLabels inside the domainSet limits is in GRID order -- the same
    # order the tiling bracket uses. The envelope's axisLabels is CRS order and
    # is deliberately NOT what we use here.
    # Anchor to the limits block: a document can carry more than one
    # GridEnvelope, and grabbing the first <gml:low> in the file can pick up an
    # unrelated one -- which silently yields a 1x1x1 grid.
    lim = re.search(r"<gml:limits>([\s\S]*?)</gml:limits>", text)
    scope = lim.group(1) if lim else text
    labels = re.search(r"<gml:axisLabels>([^<]+)</gml:axisLabels>", text)
    low = re.search(r"<gml:low>([^<]+)</gml:low>", scope)
    high = re.search(r"<gml:high>([^<]+)</gml:high>", scope)
    if labels and low and high:
        try:
            lo = [int(v) for v in low.group(1).split()]
            hi = [int(v) for v in high.group(1).split()]
            info["grid_axes"] = labels.group(1).split()
            info["grid_extents"] = [h - l + 1 for l, h in zip(lo, hi)]
            info["describe_ok"] = True
        except ValueError:
            pass

    bands = re.findall(r"<ras:bands>([\s\S]*?)</ras:bands>", text)
    if bands:
        names = re.findall(r"<ras:([A-Za-z0-9_]+)>", bands[0])
        info["band_names_hint"] = [n for n in names if n not in ("_FillValue",)]

    env = re.search(r'axisLabels="([^"]+)"', text)
    if env:
        info["crs_order_axes"] = env.group(1).split()
    return info


# ---------------------------------------------------------- dbinfo parsing

def extract_json(text):
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except ValueError:
        return None


def parse_base_type(base_type):
    """(band_count, bytes_per_cell). A tile holds EVERY band of the struct cell,
    not one band -- this is the accounting that is most often underestimated."""
    if not base_type:
        return (None, None)
    bt = base_type.strip()
    inner = bt[bt.find("{") + 1: bt.rfind("}")] if ("{" in bt and "}" in bt) else bt
    names = sorted(TYPE_BYTES, key=len, reverse=True)
    found = re.findall(r"\b(" + "|".join(re.escape(t) for t in names) + r")\b", inner.lower())
    if not found:
        return (None, None)
    return (len(found), sum(TYPE_BYTES[t] for t in found))


def parse_ranges(s):
    if not s:
        return None
    pairs = RANGE_RE.findall(s)
    return [int(h) - int(l) + 1 for l, h in pairs] if pairs else None


def parse_tile_config(s, extents=None, bytes_per_cell=None, budget=None):
    """Parse a tileConfiguration into a per-axis chunk list.

    rasdaman may report an unbounded axis as `0:*` or `*:*`. dbinfo echoes
    this literal wildcard even when rasdaman has ALREADY resolved it
    internally to a concrete chunk size to fit the tiling's byte budget --
    it does NOT mean "this axis is unbounded in storage." An earlier version
    of this function resolved a wildcard to that axis's FULL extent, on the
    theory that "unbounded means the tile spans it wholly." That is wrong
    for exactly the coverages this matters most for: any `ALIGNED [0:*, ...]`
    declaration where rasdaman shrank an axis to hit a ~1-4 MB tile budget
    (which is the entire point of the wildcard). For crrel_gipl_outputs_nc
    this inflated computed tile_extents from the real, tile-dump-confirmed
    (1, 1, 1, 42, 2471) up to the full (100, 3, 2, 1941, 2471) array --
    overstating point_query_amplification by about 3,000x (see
    CRREL_GIPL_tiling.md and rasdaman-tiling-audit.md's Finding 2 for the
    real, hand-verified numbers this was checked against).

    When a byte budget and bytes-per-cell are available, solve each
    wildcard axis from the budget instead, using the SAME isotropic-split
    math as estimate_aligned_shape() (exact for a single wildcard axis;
    approximate, and known to be so, when several axes are wildcards at
    once -- rasdaman actually fills wildcard axes in gridOrder sequence
    rather than splitting the budget evenly across them, so for a
    multi-wildcard coverage this is a much closer estimate than "full
    extent" but still not exact; sample real tile domains with
    --verify-tiles-all for the exact shape). Without a budget, a wildcard
    position is left unresolved (None) -- unresolved is safer than
    silently wrong, and downstream code already treats a None-containing
    shape as "could not compute."
    """
    if not s:
        return None
    m = re.search(r"\[([^\]]*)\]", s)
    parts = [p.strip() for p in (m.group(1) if m else s).split(",") if p.strip()]
    if not parts:
        return None
    out, wild = [], []
    for i, p in enumerate(parts):
        rng = RANGE_RE.findall(p)
        if rng:
            lo, hi = rng[0]
            out.append(int(hi) - int(lo) + 1)
        else:
            out.append(None)
            wild.append(i)  # '*' or anything else unparseable as a range

    if not wild:
        return out
    if not (bytes_per_cell and budget):
        return None  # wildcard present but no budget to solve it from

    fixed = product([c for c in out if c]) or 1
    remaining = max(1, budget // (bytes_per_cell * fixed))
    per = remaining if len(wild) == 1 else max(1, int(round(remaining ** (1.0 / len(wild)))))
    for i in wild:
        cap = extents[i] if extents and i < len(extents) else None
        out[i] = max(1, min(per, cap)) if cap else max(1, per)
    return out


def product(vals):
    out = 1
    for v in vals:
        out *= v
    return out


def find_first(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = find_first(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_first(v, key)
            if hit is not None:
                return hit
    return None


def as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------- declared tiling (recipes)

def _parse_recipe(txt):
    """Extract what the audit needs from one ingredients file.

    Uses json.loads(strict=False) because several of these files carry embedded
    WMS style definitions with literal newlines inside a JSON string. Strict
    parsing rejects those, and a parser that then skips the file silently turns
    a recoverable formatting quirk into a false "no recipe exists" finding.
    Falls back to regex so a file that cannot be parsed at all still yields its
    coverage id and tiling.
    """
    out = {"parse": "json"}
    doc = None
    try:
        doc = json.loads(txt, strict=False)
    except Exception:
        try:
            doc = json.loads(txt)
        except Exception:
            doc = None
            out["parse"] = "regex-fallback"

    def D(x):
        """These files are hand-edited; a key that is normally an object is
        occasionally a list or a string. Treat anything unexpected as empty
        rather than raising, so one odd file cannot abort the whole scan."""
        return x if isinstance(x, dict) else {}

    if doc is not None:
        out["coverage_id"] = D(D(doc).get("input")).get("coverage_id")
        opts = D(D(doc).get("recipe")).get("options")
        opts = D(opts)
        out["tiling"] = opts.get("tiling", "") or ""
        cov = D(opts.get("coverage"))
        slicer = D(cov.get("slicer"))
        out["crs"] = cov.get("crs", "") or ""
        bands = slicer.get("bands")
        out["bands_declared"] = len(bands) if isinstance(bands, list) else None
        axes = D(slicer.get("axes"))
        order = {n: c.get("gridOrder") for n, c in axes.items()
                 if isinstance(c, dict) and c.get("gridOrder") is not None}
        # gridOrder is the AUTHORITATIVE storage axis order -- the one the
        # tiling bracket follows, and the one DescribeCoverage does NOT report.
        out["grid_axes_by_order"] = [n for n, _ in sorted(order.items(), key=lambda kv: kv[1])]
        out["irregular_axes"] = [n for n, c in axes.items()
                                 if isinstance(c, dict) and c.get("irregular")]
        p = D(D(doc).get("input")).get("paths")
        out["paths"] = list(p) if isinstance(p, list) else []

    if not isinstance(out.get("coverage_id"), str) or not out.get("coverage_id"):
        m = re.search(r'"coverage_id"\s*:\s*"([^"]+)"', txt)
        out["coverage_id"] = m.group(1) if m else None
    if not out.get("tiling"):
        m = re.search(r'"tiling"\s*:\s*"([^"]*)"', txt)
        out["tiling"] = m.group(1) if m else ""
    if not out.get("paths"):
        blk = re.search(r'"paths"\s*:\s*\[([^\]]*)\]', txt)
        out["paths"] = re.findall(r'"([^"]+\.(?:nc|nc4|tif|tiff|zarr))"', blk.group(1)) if blk else []
    return out


def scan_recipes(root, ref=None, report=None):
    """Map coverage_id -> declared tiling and gridOrder from a rasdaman-ingest
    checkout.

    With `ref` it reads from a git ref (e.g. origin/main) instead of the working
    tree. That matters: a checkout sitting on a feature branch shows no trace of
    coverages that are live in production, which reads exactly like "the recipe
    does not exist".
    """
    found, problems = {}, []

    def take(rel, txt):
        info = _parse_recipe(txt)
        if not info.get("coverage_id"):
            problems.append((rel, "no coverage_id found"))
            return
        if info["parse"] != "json":
            problems.append((rel, "JSON unparseable; fell back to regex"))
        found[info["coverage_id"]] = {
            "tiling": info.get("tiling", ""),
            "recipe_path": rel,
            "paths": info.get("paths", []),
            "grid_axes_by_order": info.get("grid_axes_by_order", []),
            "irregular_axes": info.get("irregular_axes", []),
            "crs": info.get("crs", ""),
            "bands_declared": info.get("bands_declared"),
        }

    if ref:
        import subprocess
        try:
            names = subprocess.run(["git", "-C", root, "ls-tree", "-r", "--name-only", ref],
                                   capture_output=True, text=True, check=True).stdout.split("\n")
        except Exception as exc:
            raise SystemExit("ERROR: could not read git ref {!r} in {}: {}".format(ref, root, exc))
        for rel in names:
            if not rel.endswith((".json", ".json.tpl", ".template")):
                continue
            txt = subprocess.run(["git", "-C", root, "show", "{}:{}".format(ref, rel)],
                                 capture_output=True, text=True).stdout
            if txt:
                take(rel, txt)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                if not fn.endswith((".json", ".json.tpl", ".template")):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    txt = open(path, encoding="utf-8", errors="replace").read()
                except Exception as exc:
                    problems.append((path, "unreadable: {}".format(exc)))
                    continue
                take(os.path.relpath(path, root), txt)

    if report is not None:
        report.extend(problems)
    return found


def estimate_aligned_shape(tiling_str, extents, bytes_per_cell):
    """Work out the tile shape rasdaman derives from an ALIGNED declaration,
    for coverages whose dbinfo we could not read.

    ALIGNED pins the axes given an explicit range and expands the wildcard
    axes to consume the tile-size budget. Exact for the common single-wildcard
    case; approximate (and flagged as such) when several axes are wildcards.
    """
    if not tiling_str or not extents or not bytes_per_cell:
        return (None, None)
    m = re.search(r"\[([^\]]*)\]", tiling_str)
    budget = re.search(r"tile\s+size\s+(\d+)", tiling_str, re.IGNORECASE)
    if not m or not budget:
        return (None, None)
    budget = int(budget.group(1))
    parts = [p.strip() for p in m.group(1).split(",")]
    if len(parts) != len(extents):
        return (None, None)

    shape, wild = [], []
    for i, p in enumerate(parts):
        rng = RANGE_RE.findall(p)
        if rng:
            lo, hi = rng[0]
            shape.append(min(int(hi) - int(lo) + 1, extents[i]))
        else:
            shape.append(None)
            wild.append(i)

    fixed = product([s for s in shape if s]) or 1
    if not wild:
        return (shape, "estimated")
    remaining = max(1, budget // (bytes_per_cell * fixed))
    if len(wild) == 1:
        shape[wild[0]] = max(1, min(remaining, extents[wild[0]]))
        return (shape, "estimated")
    per = max(1, int(round(remaining ** (1.0 / len(wild)))))
    for i in wild:
        shape[i] = max(1, min(per, extents[i]))
    return (shape, "estimated (multi-wildcard)")


# ---------------------------------------------------------------- analysis

def find_clean_permutation(extents, chunks, authored=None):
    """If some reordering of the declared chunk sizes divides every axis
    evenly while the order as written does not, the bracket positions are
    almost certainly transposed.

    `authored` is the set of positions a human actually wrote. Only those may
    move: a chunk rasdaman derived for a `0:*` position carries no intent, so
    "reordering" it is meaningless, and permuting derived values finds clean
    arrangements by coincidence -- which is pure noise, not a finding.

    Returns the permutation needing the FEWEST moved positions, so a two-axis
    swap is reported as a two-axis swap rather than as some equivalent
    rearrangement of the size-1 chunks that happens to sort first.
    """
    n = len(extents)
    if n < 2 or n > 7 or len(chunks) != n:
        return None
    if any(not c for c in chunks):
        return None
    if all(e % c == 0 for e, c in zip(extents, chunks)):
        return None  # already clean as written -- nothing to suggest
    if authored is not None and len(authored) < 2:
        return None  # nothing hand-written to rearrange

    best = None
    for perm in itertools.permutations(range(n)):
        cand = [chunks[p] for p in perm]
        if any(c > e for c, e in zip(cand, extents)):
            continue
        if not all(e % c == 0 for e, c in zip(extents, cand)):
            continue
        moved = [i for i in range(n) if cand[i] != chunks[i]]
        if not moved:
            continue
        if authored is not None and any(
                i not in authored or perm[i] not in authored for i in moved):
            continue  # would shuffle a value rasdaman chose, not one a human did
        if best is None or len(moved) < best[0]:
            best = (len(moved), perm, cand)
    return best


def wildcard_positions(tiling_str):
    """Which bracket positions were written as `0:*`. A chunk rasdaman derived
    for a wildcard carries no authorial intent, so it cannot be evidence that
    the author misjudged that position."""
    if not tiling_str:
        return None
    m = re.search(r"\[([^\]]*)\]", tiling_str)
    if not m:
        return None
    return {i for i, p in enumerate(m.group(1).split(",")) if "*" in p}


def detect_axis_mismatch(extents, tile_shape, axes, wildcards=None,
                         trust_explicit=True):
    """Flag bracket positions whose chunk size looks like it was meant for a
    different axis.

    Intent is unknowable, so this reports evidence and a confidence tier
    rather than a verdict:
      high   - reordering the chunks removes all padding
      medium - a chunk is larger than the axis it sits on
      low    - a chunk exactly equals some other axis's extent
    """
    out = {"suspected_axis_mismatch": "", "mismatch_confidence": "",
           "mismatch_evidence": "", "suggested_reorder": ""}
    if not extents or not tile_shape or len(extents) != len(tile_shape):
        return out
    if any(not c for c in tile_shape):
        return out

    n = len(extents)
    names = [axes[i] if i < len(axes) else "axis{}".format(i) for i in range(n)]
    evidence, tiers = [], []

    # Chunks below this are excluded from the cross-axis rules: 1 and 2 divide
    # or equal so many extents by chance that they produce noise, not signal.
    MIN_MEANINGFUL_CHUNK = 4

    def candidates(i, chunk, exact_only, dedup_same_chunk):
        """Other axes this chunk would have suited.

        dedup_same_chunk applies only when we are implying the value belongs
        somewhere else: relocating a chunk onto an axis that already carries
        the identical value changes nothing, so it cannot be the mistake. An
        exact extent match is different -- it is a coincidence worth reporting
        even when the other axis legitimately holds that same chunk.
        """
        out = []
        for j in range(n):
            if j == i:
                continue
            if wildcards is not None and j in wildcards:
                continue  # rasdaman derived that axis's chunk; not a home for this one
            if dedup_same_chunk and tile_shape[j] == chunk:
                continue
            if extents[j] == chunk or (not exact_only and extents[j] % chunk == 0):
                out.append(names[j])
        return out

    for i, (ext, chunk) in enumerate(zip(extents, tile_shape)):
        derived = (wildcards is not None and i in wildcards) or not trust_explicit
        if derived:
            continue  # rasdaman chose this value, so it reflects no intent
        small = chunk < MIN_MEANINGFUL_CHUNK
        others = [] if small else candidates(i, chunk, True, False)
        if chunk > ext:
            # Only meaningful for a chunk the author wrote by hand. A derived
            # one simply overshot a short axis, which costs nothing.
            if not derived:
                msg = "chunk {} exceeds '{}' (extent {})".format(chunk, names[i], ext)
                if others:
                    msg += ", equals extent of " + "/".join(others)
                evidence.append(msg)
                tiers.append("medium")
        elif chunk == ext:
            continue  # axis deliberately kept whole
        elif ext % chunk:
            # Bare non-divisibility is NOT evidence of a misorder -- it is
            # ordinary padding, already reported in per_axis_divisibility, and
            # near-universal under ALIGNED. Only a chunk that fits some OTHER
            # axis suggests it was written for that axis instead.
            if small:
                continue
            exact = candidates(i, chunk, True, False)
            divides = [nm for nm in candidates(i, chunk, False, True) if nm not in exact]
            if exact:
                evidence.append("chunk {} does not divide '{}' ({}), but equals extent of {}".format(
                    chunk, names[i], ext, "/".join(exact)))
                tiers.append("medium")
            elif divides:
                evidence.append("chunk {} does not divide '{}' ({}), but divides {} exactly".format(
                    chunk, names[i], ext, "/".join(divides)))
                tiers.append("low")
        elif others:
            evidence.append("chunk {} on '{}' equals extent of {}".format(
                chunk, names[i], "/".join(others)))
            tiers.append("low")

    # With no declared tiling to consult we cannot tell an authored chunk from
    # a derived one, so a reorder suggestion would be speculation. Stay silent.
    authored = set() if wildcards is None else {i for i in range(n) if i not in wildcards}
    best = find_clean_permutation(extents, tile_shape, authored)
    if best:
        moved, perm, cand = best
        swaps = ["{}<-{}".format(names[i], names[perm[i]])
                 for i in range(n) if cand[i] != tile_shape[i]]
        out["suggested_reorder"] = "reorder chunks to [{}] ({})".format(
            ",".join(str(c) for c in cand), " ".join(swaps))
        tiers.append("high")
        evidence.append("reordering chunks removes all padding")

    if not evidence:
        out["suspected_axis_mismatch"] = "no"
        return out
    rank = {"high": 3, "medium": 2, "low": 1}
    top = max(tiers, key=lambda t: rank[t]) if tiers else "low"
    out["suspected_axis_mismatch"] = "yes"
    out["mismatch_confidence"] = top
    out["mismatch_evidence"] = "; ".join(evidence)[:400]
    return out


DOMAIN_RE = re.compile(r"\[-?\d+:-?\d+(?:,-?\d+:-?\d+)*\]")


def sample_tile_domains(text, limit=20000):
    """Pull tile domains out of a printtiles response (possibly only its head).

    This is what catches partition drift that tileConfiguration cannot show:
    a coverage grown by repeated updates can hold several tile shapes at once
    while still reporting one nominal shape.
    """
    idx = text.find("tileDomains")
    seg = text[idx:] if idx != -1 else text
    return DOMAIN_RE.findall(seg)[:limit]


def analyze_tile_domains(domains, axes, tile_shape, extents):
    """Summarise a sample of tile domains: how many distinct shapes, and where
    each axis's blocks actually start."""
    out = {}
    if not domains:
        return out
    shapes, lows = [], None
    for d in domains:
        pairs = RANGE_RE.findall(d)
        if not pairs:
            continue
        lo = [int(a) for a, b in pairs]
        shape = tuple(int(b) - int(a) + 1 for a, b in pairs)
        shapes.append(shape)
        if lows is None:
            lows = [set() for _ in lo]
        if len(lo) == len(lows):
            for i, v in enumerate(lo):
                lows[i].add(v)
    if not shapes:
        return out

    distinct = sorted(set(shapes))
    modal = max(set(shapes), key=shapes.count)
    out["tiles_sampled"] = len(shapes)
    out["distinct_tile_shapes"] = len(distinct)
    out["modal_tile_shape"] = ",".join(str(v) for v in modal)
    out["partition_homogeneous"] = "yes" if len(distinct) == 1 else "no"

    if lows:
        names = [axes[i] if i < len(axes) else "axis{}".format(i) for i in range(len(lows))]
        parts = []
        for i, s in enumerate(lows):
            vals = sorted(s)[:4]
            parts.append("{}:{}{}".format(names[i], ",".join(str(v) for v in vals),
                                          "..." if len(s) > 4 else ""))
        out["observed_block_starts"] = " | ".join(parts)

    # A position the author believed was a whole small axis, but which the
    # server is actually cutting into blocks, is the classic order mix-up.
    if tile_shape and extents and len(tile_shape) == len(lows or []):
        chopped = []
        for i, s in enumerate(lows):
            if len(s) > 1 and tile_shape[i] < (extents[i] if i < len(extents) else 0):
                nm = axes[i] if i < len(axes) else "axis{}".format(i)
                chopped.append("{}(chunk {})".format(nm, tile_shape[i]))
        if chopped:
            out["axes_split_into_blocks"] = ", ".join(chopped)
    return out


def amplification(extents, tile_shape, axes, bytes_per_cell, mode):
    """Bytes read / bytes wanted for a representative query.

    mode 'point': pin both spatial axes, sweep everything else (a time series
                  at one x/y -- the point and small-AOI access pattern).
    mode 'map'  : sweep both spatial axes, pin everything else (one map frame
                  -- the WMS access pattern).
    """
    if not (extents and tile_shape and bytes_per_cell) or len(extents) != len(tile_shape):
        return (None, None, None)
    if any(not c for c in tile_shape):
        return (None, None, None)

    spatial = [i for i, a in enumerate(axes or []) if a.lower() in SPATIAL_NAMES]
    if len(spatial) != 2:
        return (None, None, None)

    tiles, wanted_cells = 1, 1
    for i, ext in enumerate(extents):
        is_spatial = i in spatial
        sweep = (not is_spatial) if mode == "point" else is_spatial
        if sweep:
            tiles *= int(math.ceil(float(ext) / tile_shape[i]))
            wanted_cells *= ext
        else:
            tiles *= 1
    tile_bytes = product(tile_shape) * bytes_per_cell
    read = tiles * tile_bytes
    wanted = wanted_cells * bytes_per_cell
    if wanted <= 0:
        return (None, None, None)
    return (tiles, read, round(float(read) / wanted, 1))


def analyze(cid, dbinfo_obj, sdom_text, describe, declared):
    lower = cid.lower()
    if re.search(r"_wms(_test)?$", lower):
        suffix = "wms"
    elif lower.endswith("_wcs"):
        suffix = "wcs"
    else:
        suffix = "none"
    row = {
        "coverage_id": cid,
        "name_suffix": suffix,
        "is_test_named": "yes" if "test" in lower else "no",
        "declared_tiling": (declared or {}).get("tiling", ""),
        "recipe_path": (declared or {}).get("recipe_path", ""),
    }

    axes = describe.get("grid_axes") or []
    extents = describe.get("grid_extents") or []
    row["grid_axes"] = " ".join(axes)
    row["grid_extents"] = ",".join(str(e) for e in extents)

    bands = bytes_per_cell = None
    tile_shape = None
    tiling_source = ""

    if dbinfo_obj is not None:
        base_type = find_first(dbinfo_obj, "baseType")
        base_type = base_type if isinstance(base_type, str) else ""
        row["base_type"] = base_type
        bands, bytes_per_cell = parse_base_type(base_type)

        row["tile_no"] = as_int(find_first(dbinfo_obj, "tileNo"))
        row["total_size_bytes"] = as_int(find_first(dbinfo_obj, "totalSize"))
        scheme = find_first(dbinfo_obj, "tilingScheme")
        row["tiling_scheme"] = scheme if isinstance(scheme, str) else ""
        conf = find_first(dbinfo_obj, "tileConfiguration")
        row["tile_configuration"] = conf if isinstance(conf, str) else ""
        row["declared_tile_size_bytes"] = as_int(find_first(dbinfo_obj, "tileSize"))
        tile_shape = parse_tile_config(row["tile_configuration"], extents,
                                        bytes_per_cell, row["declared_tile_size_bytes"])
        if tile_shape:
            n_wild = row["tile_configuration"].count("*")
            if n_wild == 0:
                tiling_source = "dbinfo (measured)"
            elif n_wild == 1:
                tiling_source = "dbinfo (measured pinned axes, budget-derived wildcard)"
            else:
                tiling_source = "dbinfo (measured pinned axes, approximate multi-wildcard)"
        elif "*" in row["tile_configuration"]:
            row["note"] = ((row.get("note", "") + "; ") if row.get("note") else "") + \
                "wildcard tileConfiguration could not be resolved (missing bytes/cell or tile-size budget)"

        # sdom is the stored array's own extent; prefer it over petascope's view
        if sdom_text:
            m = re.search(r"\[[^\]]*\]", sdom_text)
            if m:
                row["sdom"] = m.group(0)
                sd = parse_ranges(m.group(0))
                if sd:
                    if extents and sd != extents:
                        row["note"] = "sdom disagrees with DescribeCoverage extent"
                    extents = sd
                    row["grid_extents"] = ",".join(str(e) for e in extents)

    # Fall back to the recipe when dbinfo was unavailable.
    if tile_shape is None and declared and declared.get("tiling") and extents:
        if bytes_per_cell is None:
            hint = describe.get("band_names_hint") or []
            bands = len(hint) or 1
            bytes_per_cell = bands * 4  # assume float32; flagged below
            row["note"] = ((row.get("note", "") + "; ") if row.get("note") else "") + \
                          "bytes/cell assumed {}B ({} band(s) x float32)".format(bytes_per_cell, bands)
        tile_shape, how = estimate_aligned_shape(declared["tiling"], extents, bytes_per_cell)
        if tile_shape:
            tiling_source = "recipe ({})".format(how)
            row["tile_configuration"] = "[" + ",".join("0:{}".format(c - 1) for c in tile_shape) + "]"

    row["tiling_source"] = tiling_source
    row["band_count"] = bands
    row["bytes_per_cell"] = bytes_per_cell
    row["tile_extents"] = ",".join(str(c) for c in tile_shape) if tile_shape else ""

    if tile_shape and bytes_per_cell:
        cells = product(tile_shape)
        row["cells_per_tile"] = cells
        row["computed_bytes_per_tile"] = cells * bytes_per_cell

    tn, ts = row.get("tile_no"), row.get("total_size_bytes")
    if isinstance(tn, int) and isinstance(ts, int) and tn:
        row["actual_avg_bytes_per_tile"] = ts / tn

    real_bytes = product(extents) * bytes_per_cell if (extents and bytes_per_cell) else None
    row["real_data_bytes"] = real_bytes
    if real_bytes and isinstance(ts, int) and real_bytes > 0:
        row["storage_overhead_factor"] = round(ts / float(real_bytes), 4)

    # Per-axis divisibility: the only check that catches an axis-order swap,
    # because tile volume is identical whichever spatial axis got which chunk.
    if extents and tile_shape and len(extents) == len(tile_shape):
        flags, blocks = [], []
        for real, chunk in zip(extents, tile_shape):
            if not chunk:
                flags.append("?"); blocks.append("?"); continue
            flags.append("ok" if real % chunk == 0 else "PAD")
            blocks.append(str(int(math.ceil(float(real) / chunk))))
        row["per_axis_divisibility"] = ",".join(flags)
        row["blocks_per_axis"] = ",".join(blocks)
        row["axes_with_padding"] = sum(1 for f in flags if f == "PAD")

    for mode, prefix in (("point", "point_query"), ("map", "map_query")):
        tiles, read, amp = amplification(extents, tile_shape, axes, bytes_per_cell, mode)
        row[prefix + "_tiles"] = tiles
        row[prefix + "_bytes_read"] = read
        row[prefix + "_amplification"] = amp

    # A coverage whose recipe declares no tiling got rasdaman's default scheme.
    # Nothing was authored, so there is no authorial mistake to detect and the
    # whole check is meaningless -- run it only where a bracket was written.
    has_recipe = bool(row.get("recipe_path"))
    declared_t = row.get("declared_tiling", "")
    if has_recipe and not declared_t:
        row["suspected_axis_mismatch"] = "n/a (default tiling)"
    else:
        wilds = wildcard_positions(declared_t)
        # Without a recipe we cannot tell which positions were wildcards, so for
        # an ALIGNED coverage we withhold intent-based signals rather than
        # assert authorship we cannot verify.
        trust = True
        if wilds is None and str(row.get("tiling_scheme", "")).lower().startswith("align"):
            trust = False
        row.update(detect_axis_mismatch(extents, tile_shape, axes, wilds, trust))

    # Declared (recipe) vs actual (dbinfo) tile shape. ALIGNED derives its
    # wildcard axes, so a difference there is expected and not a finding.
    # REGULAR states the shape outright, so any difference is a real one.
    declared_tiling = row.get("declared_tiling", "")
    if declared_tiling and tiling_source.startswith("dbinfo") and tile_shape:
        m = re.search(r"\[([^\]]*)\]", declared_tiling)
        scheme = "REGULAR" if re.match(r"\s*REGULAR", declared_tiling, re.I) else (
            "ALIGNED" if re.match(r"\s*ALIGNED", declared_tiling, re.I) else "")
        if m and "*" in m.group(1):
            row["declared_vs_actual"] = "n/a (wildcards derived)"
        elif m:
            dec = parse_ranges(m.group(1))
            if dec and len(dec) == len(tile_shape):
                if dec == list(tile_shape):
                    row["declared_vs_actual"] = "match"
                else:
                    row["declared_vs_actual"] = "DIFFERS: declared {} vs actual {}".format(
                        ",".join(map(str, dec)), ",".join(map(str, tile_shape)))
                    if scheme == "REGULAR":
                        row["declared_vs_actual"] += " (REGULAR should match)"
    return row


SUMMARY_FIELDS = [
    "coverage_id", "name_suffix", "is_test_named", "status",
    "grid_axes", "grid_extents", "band_count", "bytes_per_cell",
    "tiling_source", "tiling_scheme", "declared_tiling", "tile_configuration",
    "tile_extents", "cells_per_tile", "declared_tile_size_bytes",
    "computed_bytes_per_tile", "actual_avg_bytes_per_tile",
    "tile_no", "total_size_bytes", "real_data_bytes", "storage_overhead_factor",
    "per_axis_divisibility", "blocks_per_axis", "axes_with_padding",
    "point_query_tiles", "point_query_bytes_read", "point_query_amplification",
    "map_query_tiles", "map_query_bytes_read", "map_query_amplification",
    "suspected_axis_mismatch", "mismatch_confidence", "mismatch_evidence",
    "suggested_reorder", "declared_vs_actual",
    "tiles_sampled", "distinct_tile_shapes", "partition_homogeneous",
    "modal_tile_shape", "observed_block_starts", "axes_split_into_blocks",
    "collection_used", "collection_resolution", "other_collections_found",
    "wcps_domain", "wcps_cell_type", "wcps_null_values", "wcps_vs_describe",
    "dbinfo_array_matches",
    "sdom", "base_type", "recipe_path", "raw_file", "note",
]


# ------------------------------------------------------- WCPS domain recovery

ARRAY_RE = re.compile(r"ARRAY\s*\(([^)]*)\)\s*\[([^\]]*)\]", re.I)
DIM_RE = re.compile(r"D\d+\s*\(\s*(-?\d+)\s*:\s*(-?\d+)\s*\)")


def parse_array_descriptor(text):
    """Pull the real rasdaman array out of a WCPS type error.

    rasdaman answers a bad binary operation with its own description of the
    operand, e.g.

        ARRAY (float) [D0(0:18249),D1(0:442),D2(0:459)] null values [-9999.000000]

    which is the stored domain in STORAGE order, the cell type, and the null
    value -- everything sdom would have given us, for a coverage rasql cannot
    reach by name.
    """
    if not text:
        return None
    m = ARRAY_RE.search(text)
    if not m:
        return None
    cell_type = re.sub(r"\s+", " ", m.group(1)).strip()
    dims = DIM_RE.findall(m.group(2))
    if not dims:
        return None
    out = {"cell_type": cell_type,
           "extents": [int(hi) - int(lo) + 1 for lo, hi in dims],
           "domain": "[" + ",".join("{}:{}".format(a, b) for a, b in dims) + "]"}
    nv = re.search(r"null values\s*\[([^\]]*)\]", text, re.I)
    if nv:
        out["null_values"] = nv.group(1).strip()
    bands, bpc = parse_base_type(cell_type)
    out["band_count"] = bands
    out["bytes_per_cell"] = bpc
    return out


def wcps_domain(ows_url, coverage_id, timeout, ctx, auth):
    """Recover a coverage's true stored array via a deliberate WCPS type error.

    Needs no rasdaman credentials and no collection name, so it works for the
    coverages whose rasql path fails outright. Read-only: the query is rejected
    before anything executes.
    """
    q = 'for $c in ({}) return encode($c * "{}", "csv")'.format(coverage_id, "__typeprobe__")
    body = urllib.parse.urlencode({
        "service": "WCS", "version": "2.0.1",
        "request": "ProcessCoverages", "query": q}).encode("utf-8")
    req = urllib.request.Request(ows_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            return None
    except Exception:
        return None
    m = re.search(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>", text)
    return parse_array_descriptor(m.group(1) if m else text)


# ---------------------------------------------------- collection resolution

def collection_candidates(coverage_id, recipe=None):
    """Plausible rasdaman collection names for a coverage whose own name fails.

    Ordered cheapest-first. Guessing is a fallback, not a substitute for the
    mapping in petascope's database -- if none of these hit, the name is not
    derivable from what we can see.
    """
    c = []
    base = coverage_id
    c.append(base)
    for suf in ("_wcs", "_wms"):
        if base.endswith(suf):
            c.append(base[: -len(suf)])
    for tok in ("_v2", "_V2"):
        if tok in base:
            c.append(base.replace(tok, ""))
            stripped = base.replace(tok, "")
            for suf in ("_wcs", "_wms"):
                if stripped.endswith(suf):
                    c.append(stripped[: -len(suf)])
    for n in range(1, 6):
        c.append("{}_{}".format(base, n))
        c.append("{}{}".format(base, n))
    # the source file's stem: wcst_import sometimes names the collection for it
    for p in ((recipe or {}).get("paths") or []):
        stem = os.path.basename(p)
        for ext in (".nc", ".nc4", ".tif", ".tiff", ".zarr"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        stem = re.sub(r"[^A-Za-z0-9_]", "_", stem)
        if stem:
            c.extend([stem, stem + "_wcs", stem + "_wms"])
    seen, out = set(), []
    for x in c:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def resolve_collection(rasql_url, coverage_id, want_extents, candidates,
                       timeout, ctx, auth, log=None, published=None):
    """Find the collection holding this coverage, and PROVE it is the right one.

    A name that merely exists is not enough: this server carries stub and stale
    collections squatting on coverage names (one returned a single 4-byte cell
    while WCS served the real array). A candidate is only accepted when its own
    sdom matches the domain we independently know the coverage to have.
    """
    found = []
    for name in candidates:
        # A candidate that is itself a published coverage belongs to THAT
        # coverage. Two coverages over the same grid have identical domains, so
        # the domain check cannot tell them apart -- without this guard the
        # resolver happily reports one coverage's storage as another's.
        if published and name != coverage_id and name in published:
            if log:
                log("{}: candidate '{}' skipped -- it is another published coverage".format(
                    coverage_id, name))
            continue
        try:
            txt = rasql(rasql_url, "select sdom(c) from {} as c".format(name),
                        timeout, ctx, auth)
        except RasqlError as exc:
            if "object unknown" not in (exc.body or "").lower() and \
               "object unknown" not in str(exc).lower():
                if log:
                    log("{}: candidate '{}' -> {}".format(coverage_id, name, str(exc)[:160]))
            continue
        except Exception:
            continue
        m = re.search(r"\[[^\]]*\]", txt or "")
        if not m:
            continue
        ext = parse_ranges(m.group(0))
        found.append((name, ext))
        if want_extents and ext and sorted(ext) == sorted(want_extents):
            return {"collection": name, "extents": ext, "match": "exact",
                    "others": [f[0] for f in found if f[0] != name]}
    if found:
        return {"collection": None, "match": "exists but domain differs",
                "others": ["{}{}".format(n, e) for n, e in found]}
    return None

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="One-pass rasdaman tiling audit.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="WCS GetCapabilities URL")
    ap.add_argument("--outdir", required=True, help="Output directory (created if missing)")
    ap.add_argument("--rasql-url", default=None,
                    help="rasql endpoint (default: derived from --url)")
    ap.add_argument("--recipes", default=None,
                    help="Path to a rasdaman-ingest checkout. Supplies each coverage's DECLARED "
                         "tiling, and lets the audit still evaluate coverages whose dbinfo fails. "
                         "Reads the working tree -- check out the branch matching production.")
    ap.add_argument("--dbinfo-args", default="",
                    help="Argument string for dbinfo(). Default '' (no per-tile listing). "
                         "Pass 'printtiles=embedded' only for forensics on one coverage: on large "
                         "coverages that response can arrive truncated.")
    ap.add_argument("--recipes-ref", default=None,
                    help="Read recipes from this git ref (e.g. origin/main) instead of the working "
                         "tree. A checkout on a feature branch shows no trace of coverages that are "
                         "live in production, which looks identical to 'no recipe exists'.")
    ap.add_argument("--wcps-domains", action="store_true",
                    help="For coverages rasql cannot reach, recover the TRUE stored array domain, "
                         "cell type and null value with a WCPS type-error probe. Needs no rasdaman "
                         "credentials and no collection name, so it closes the integrity gap for "
                         "coverages that would otherwise have no measured facts at all.")
    ap.add_argument("--mapping-file", default=None,
                    help="coverage_id -> collection_name pairs exported from petascopedb "
                         "(SELECT c.coverage_id, r.collection_name FROM coverage c JOIN "
                         "rasdaman_range_set r USING (rasdaman_range_set_id)). When given, "
                         "dbinfo targets the real collection instead of the coverage id, "
                         "which is what makes the v2 coverages measurable. Supersedes "
                         "--resolve-collections.")
    ap.add_argument("--resolve-collections", action="store_true",
                    help="When dbinfo fails, probe likely rasdaman collection names and accept one "
                         "only if its sdom matches the coverage's known domain. Recovers persisted "
                         "size and real tiling for coverages whose collection name diverged from "
                         "their coverage ID. Implies --wcps-domains for the validation target.")
    ap.add_argument("--verify-tiles", action="store_true",
                    help="Second query, on FLAGGED coverages only, fetching actual tile domains "
                         "(printtiles=embedded). Confirms an axis-order mix-up by showing where "
                         "blocks really start, and reveals a partition grown heterogeneous by "
                         "repeated updates -- which tileConfiguration alone cannot show.")
    ap.add_argument("--verify-tiles-all", action="store_true",
                    help="Run the tile-domain check on every coverage, not just flagged ones.")
    ap.add_argument("--verify-max-mb", type=float, default=2.0,
                    help="Read only this much of each printtiles response (default 2MB). The head "
                         "is enough to characterise the layout; the trailing boundary tiles are "
                         "not seen, so homogeneity is reported over the sample.")
    ap.add_argument("--save-raw", action="store_true",
                    help="Also write each coverage's raw dbinfo response to <id>.txt")
    ap.add_argument("--limit", type=int, default=0, help="Only the first N coverages (trial run)")
    ap.add_argument("--coverage", action="append", default=[], help="Only this coverage ID (repeatable)")
    ap.add_argument("--sleep", type=float, default=0.0, help="Pause between coverages")
    ap.add_argument("--timeout", type=int, default=600, help="Per-request timeout (s)")
    ap.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    args = ap.parse_args()

    user = os.environ.get("RASDAMAN_USER")
    password = os.environ.get("RASDAMAN_PASS")
    if not user or not password:
        sys.exit("ERROR: set credentials first:\n"
                 "    export RASDAMAN_USER=rasadmin\n"
                 "    export RASDAMAN_PASS='...'\n")
    auth = basic_auth_header(user, password)
    ctx = make_ssl_context(args.insecure)

    ows_url = args.url.split("?")[0]
    if args.rasql_url:
        rasql_url = args.rasql_url
    else:
        parts = urllib.parse.urlsplit(args.url)
        path = parts.path
        path = path.rsplit("/ows", 1)[0] + "/rasql" if "/ows" in path \
            else path.rstrip("/").rsplit("/", 1)[0] + "/rasql"
        rasql_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    outdir = os.path.abspath(args.outdir)
    compact_dir = os.path.join(outdir, "_compact")
    os.makedirs(compact_dir, exist_ok=True)

    print("GetCapabilities : {}".format(args.url))
    print("rasql endpoint  : {}".format(rasql_url))
    print("output directory: {}\n".format(outdir))

    declared_map = {}
    recipe_problems = []
    if args.recipes:
        print("Scanning recipes under {}{} ...".format(
            args.recipes, " @ " + args.recipes_ref if args.recipes_ref else " (working tree)"))
        recipe_problems = []
        declared_map = scan_recipes(args.recipes, args.recipes_ref, recipe_problems)
        print("  parsed {} recipes".format(len(declared_map)))
        if recipe_problems:
            # Never let a parse failure masquerade as an absent recipe.
            print("  {} file(s) needed attention:".format(len(recipe_problems)))
            for pth, why in recipe_problems[:8]:
                print("     {} -- {}".format(pth, why))
            if len(recipe_problems) > 8:
                print("     ... and {} more (see _recipe_problems.log)".format(len(recipe_problems) - 8))
        print()

    print("Fetching GetCapabilities ...")
    try:
        caps = http_get(args.url, args.timeout, ctx, auth)
        coverage_ids = scrape_coverage_ids(caps)
    except Exception as exc:
        sys.exit("ERROR: could not read GetCapabilities: {}".format(exc))
    if not coverage_ids:
        sys.exit("ERROR: no <CoverageId> elements found.")
    print("  {} coverages\n".format(len(coverage_ids)))
    if args.recipes and recipe_problems:
        with open(os.path.join(outdir, "_recipe_problems.log"), "w") as fh:
            for pth, why in recipe_problems:
                fh.write("{}\t{}\n".format(pth, why))

    published_ids = set(coverage_ids)

    # petascopedb's own coverage -> collection pointer. When supplied there is
    # nothing left to guess: dbinfo goes straight at the real array.
    coll_map = {}
    if args.mapping_file:
        with open(args.mapping_file) as fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip().strip('"') for p in
                         re.split(r"[,\t|]|\s{2,}|\s", line) if p.strip()]
                if len(parts) < 2:
                    continue
                if n == 0 and parts[0].lower() in ("coverage_id", "coverageid", "coverage"):
                    continue
                coll_map[parts[0]] = parts[1]
        print("mapping file    : {} ({} coverage -> collection pairs)".format(
            args.mapping_file, len(coll_map)))

    with open(os.path.join(outdir, "_coverage_ids.txt"), "w") as fh:
        fh.write("\n".join(coverage_ids) + "\n")

    if args.coverage:
        wanted = set(args.coverage)
        coverage_ids = [c for c in coverage_ids if c in wanted]
    if args.limit:
        coverage_ids = coverage_ids[:args.limit]

    rows = []
    counts = {"full": 0, "describe_only": 0, "failed": 0}
    started = datetime.now(timezone.utc)
    error_fh = open(os.path.join(outdir, "_errors.log"), "w")

    for index, cid in enumerate(coverage_ids, start=1):
        prefix = "[{}/{}] {}".format(index, len(coverage_ids), cid)
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", cid)

        # 1. DescribeCoverage -- no credentials, so this is the reliable backbone
        describe = {}
        try:
            describe = describe_coverage(ows_url, cid, args.timeout, ctx, auth)
            if not describe.get("describe_ok"):
                error_fh.write("{}: DescribeCoverage: {}\n".format(
                    cid, describe.get("describe_error", "no grid envelope found")))
        except Exception as exc:
            error_fh.write("{}: DescribeCoverage failed: {}\n".format(cid, exc))

        # 2. dbinfo, retrying against the resolved collection name if needed
        call = 'dbinfo(c,"{}")'.format(args.dbinfo_args) if args.dbinfo_args else "dbinfo(c)"
        dbinfo_obj, dbinfo_text, used, db_err = None, "", "", ""
        mapped = coll_map.get(cid)
        for name in ([mapped] if mapped else [cid]):
            try:
                dbinfo_text = rasql(rasql_url, "select {} from {} as c".format(call, name),
                                    args.timeout, ctx, auth)
            except Exception as exc:
                db_err = str(exc)
                continue
            obj = extract_json(dbinfo_text)
            if obj is None:
                truncated = ("\x00" in dbinfo_text) or not dbinfo_text.rstrip().endswith("}")
                db_err = ("response truncated ({:.1f}MB) -- rerun this coverage with "
                          "--dbinfo-args ''".format(len(dbinfo_text) / 1048576.0)) if truncated \
                    else "unparseable response: " + re.sub(r"\s+", " ", dbinfo_text)[:200]
                continue
            dbinfo_obj, used, db_err = obj, name, ""
            break

        # 2b. WCPS domain recovery -- the only measured fact available for a
        # coverage rasql cannot reach. Also a cross-check when it can.
        wcps = None
        if (args.wcps_domains or args.resolve_collections) and (db_err or args.wcps_domains):
            wcps = wcps_domain(ows_url, cid, args.timeout, ctx, auth)
            if wcps is None and db_err:
                error_fh.write("{}: wcps domain probe returned nothing\n".format(cid))

        # 2c. collection resolution -- only accepted when the candidate's own
        # domain matches what we independently know this coverage to be.
        resolved = None
        if args.resolve_collections and db_err:
            want = (wcps or {}).get("extents") or describe.get("grid_extents")
            cands = collection_candidates(cid, declared_map.get(cid))
            resolved = resolve_collection(rasql_url, cid, want, cands,
                                          args.timeout, ctx, auth,
                                          log=lambda m: error_fh.write(m + "\n"),
                                          published=published_ids)
            if resolved and resolved.get("collection"):
                try:
                    dbinfo_text = rasql(rasql_url, "select {} from {} as c".format(
                        call, resolved["collection"]), args.timeout, ctx, auth)
                    obj = extract_json(dbinfo_text)
                    if obj is not None:
                        dbinfo_obj, used, db_err = obj, resolved["collection"], ""
                except Exception as exc:
                    error_fh.write("{}: resolved to '{}' but dbinfo failed: {}\n".format(
                        cid, resolved["collection"], str(exc)[:200]))

        # 3. sdom cross-check (only meaningful once dbinfo resolved a collection)
        sdom_text = ""
        if used:
            try:
                sdom_text = rasql(rasql_url, "select sdom(c) from {} as c".format(used),
                                  args.timeout, ctx, auth)
            except Exception as exc:
                error_fh.write("{}: sdom failed (non-fatal): {}\n".format(cid, exc))
        # WCPS reads the coverage's OWN array, so it outranks both sdom (which
        # describes whatever collection answered, possibly a stub) and
        # DescribeCoverage (petascope metadata, which can disagree with storage).
        sdom_from_collection = sdom_text
        if wcps and wcps.get("domain"):
            sdom_text = wcps["domain"]

        if db_err:
            error_fh.write("{}: dbinfo: {}\n".format(cid, db_err))
            error_fh.flush()

        row = analyze(cid, dbinfo_obj, sdom_text, describe, declared_map.get(cid))
        row["collection_used"] = used
        if mapped:
            row["collection_resolution"] = (
                "petascopedb pointer" if used == mapped
                else "petascopedb pointer '{}' but dbinfo failed".format(mapped))
        if wcps:
            # Does the collection dbinfo answered for actually hold this
            # coverage? If not, its tiling and size belong to something else.
            if used and sdom_from_collection:
                m1 = re.search(r"\[[^\]]*\]", sdom_from_collection)
                c_ext = parse_ranges(m1.group(0)) if m1 else None
                w_ext = wcps.get("extents")
                if c_ext and w_ext:
                    row["dbinfo_array_matches"] = ("yes" if sorted(c_ext) == sorted(w_ext)
                                                   else "NO -- dbinfo describes a different array")
            row["wcps_domain"] = wcps.get("domain", "")
            row["wcps_cell_type"] = wcps.get("cell_type", "")
            row["wcps_null_values"] = wcps.get("null_values", "")
            if not row.get("bytes_per_cell") and wcps.get("bytes_per_cell"):
                row["bytes_per_cell"] = wcps["bytes_per_cell"]
                row["band_count"] = wcps.get("band_count")
            de = describe.get("grid_extents")
            if de and wcps.get("extents"):
                row["wcps_vs_describe"] = ("match" if sorted(de) == sorted(wcps["extents"])
                                           else "DIFFERS")
        if resolved:
            row["collection_resolution"] = (
                "resolved: " + resolved["collection"] if resolved.get("collection")
                else resolved.get("match", ""))
            if resolved.get("others"):
                row["other_collections_found"] = "; ".join(map(str, resolved["others"]))[:300]
        if used and used != cid:
            row["note"] = ((row.get("note", "") + "; ") if row.get("note") else "") + \
                          "collection name differs from coverage id"

        if dbinfo_obj is not None:
            row["status"] = "ok"
            counts["full"] += 1
            mark = "ok"
        elif row.get("tile_extents"):
            row["status"] = "describe+recipe (no dbinfo)"
            row["note"] = ((row.get("note", "") + "; ") if row.get("note") else "") + \
                          "dbinfo unavailable: " + db_err[:200]
            counts["describe_only"] += 1
            mark = "partial (no dbinfo)"
        else:
            row["status"] = "failed"
            row["note"] = db_err[:300]
            counts["failed"] += 1
            mark = "FAILED"

        # Opt-in: confirm the nominal tile shape against real tile domains.
        if (args.verify_tiles or args.verify_tiles_all) and used:
            flagged = row.get("suspected_axis_mismatch") == "yes"
            if args.verify_tiles_all or flagged:
                try:
                    vt = rasql(rasql_url,
                               'select dbinfo(c,"printtiles=embedded") from {} as c'.format(used),
                               args.timeout, ctx, auth,
                               max_bytes=int(args.verify_max_mb * 1048576))
                    doms = sample_tile_domains(vt)
                    ext_now = [int(v) for v in row.get("grid_extents", "").split(",") if v.strip()]
                    tile_shape_now = parse_tile_config(row.get("tile_configuration", ""), ext_now,
                                                        row.get("bytes_per_cell"),
                                                        row.get("declared_tile_size_bytes"))
                    stats = analyze_tile_domains(doms, describe.get("grid_axes") or [],
                                                 tile_shape_now, ext_now)
                    row.update(stats)
                    if stats.get("partition_homogeneous") == "no":
                        row["note"] = ((row.get("note", "") + "; ") if row.get("note") else "") + \
                            "{} distinct tile shapes in sample".format(stats.get("distinct_tile_shapes"))

                    # A sampled modal shape is ground truth, not an estimate --
                    # prefer it over parse_tile_config's isotropic-split guess
                    # for tile_extents/amplification. This matters most for a
                    # multi-wildcard ALIGNED declaration (every axis `0:*`),
                    # where the isotropic split is only approximate because
                    # rasdaman actually fills wildcard axes in gridOrder
                    # sequence, not evenly (see parse_tile_config's docstring;
                    # crrel_gipl_outputs_nc is the confirmed worked example in
                    # CRREL_GIPL_tiling.md).
                    modal = stats.get("modal_tile_shape")
                    bpc = row.get("bytes_per_cell")
                    if modal and bpc:
                        sampled_shape = [int(v) for v in modal.split(",")]
                        if len(sampled_shape) == len(ext_now):
                            row["tile_extents"] = modal
                            row["tiling_source"] = "dbinfo (measured, confirmed by tile-domain sample)"
                            cells = product(sampled_shape)
                            row["cells_per_tile"] = cells
                            row["computed_bytes_per_tile"] = cells * bpc
                            for mode, prefix in (("point", "point_query"), ("map", "map_query")):
                                tiles, read, amp = amplification(
                                    ext_now, sampled_shape, describe.get("grid_axes") or [], bpc, mode)
                                row[prefix + "_tiles"] = tiles
                                row[prefix + "_bytes_read"] = read
                                row[prefix + "_amplification"] = amp
                except Exception as exc:
                    error_fh.write("{}: verify-tiles failed: {}\n".format(cid, exc))

        if args.save_raw and dbinfo_text:
            with open(os.path.join(outdir, fname + ".txt"), "w") as fh:
                fh.write(dbinfo_text)
            row["raw_file"] = fname + ".txt"

        with open(os.path.join(compact_dir, fname + ".json"), "w") as fh:
            payload = {"coverage_id": cid, "collection_used": used,
                       "describe": describe, "sdom": row.get("sdom", ""),
                       "dbinfo_error": db_err, "dbinfo": dbinfo_obj}
            if isinstance(payload["dbinfo"], dict):
                t = payload["dbinfo"].get("tiling")
                if isinstance(t, dict) and "tileDomains" in t:
                    t["tileDomains"] = "<{} entries omitted>".format(len(t["tileDomains"]))
            json.dump(payload, fh, indent=2)

        amp = row.get("point_query_amplification")
        print("{}  {} -- {} bands, pointQ x{}".format(
            prefix, mark, row.get("band_count") or "?", amp if amp else "?"))
        rows.append(row)
        if args.sleep:
            time.sleep(args.sleep)

    error_fh.close()

    with open(os.path.join(outdir, "_summary.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    finished = datetime.now(timezone.utc)
    meta = {"capabilities_url": args.url, "rasql_url": rasql_url,
            "dbinfo_args": args.dbinfo_args, "recipes_dir": args.recipes,
            "wcps_domains": bool(args.wcps_domains),
            "resolve_collections": bool(args.resolve_collections),
            "started_utc": started.isoformat(), "finished_utc": finished.isoformat(),
            "elapsed_seconds": round((finished - started).total_seconds(), 1),
            "coverages": len(coverage_ids), **counts}
    with open(os.path.join(outdir, "_run.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    print("\nDone in {}s".format(meta["elapsed_seconds"]))
    print("  {} full (dbinfo + describe)".format(counts["full"]))
    print("  {} partial (describe + recipe, dbinfo unavailable)".format(counts["describe_only"]))
    print("  {} failed entirely".format(counts["failed"]))
    print("\nSummary: {}".format(os.path.join(outdir, "_summary.csv")))


if __name__ == "__main__":
    main()
