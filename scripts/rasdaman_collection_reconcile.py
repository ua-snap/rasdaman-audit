#!/usr/bin/env python3
"""
rasdaman_collection_reconcile.py

Reconcile petascope's COVERAGE catalogue against rasdaman's COLLECTION
catalogue, and report where the assumed 1:1 relationship does not hold.

WHY THIS IS AWKWARD
-------------------
The two layers share no key.

  petascope  knows coverages: names, CRS, axis labels, metadata -- plus a
             private pointer to the rasdaman array that holds the pixels.
  rasdaman   knows collections: anonymous arrays (D0, D1, D2 ...) and nothing
             about geography.

wcst_import usually names the collection after the coverage, so the two
normally coincide. Nothing enforces it. On zeus they already diverge.

There are three ways to line the layers up. This script does all three and
tells you which one actually answered.

  A. THE POINTER (authoritative).  petascope stores the collection name in
     petascopedb. Hand it over with --mapping-file and every verdict below
     becomes a fact rather than an inference. See "GETTING THE POINTER".

  B. THE NAME (no privileges, always available).  Ask rasdaman for
     sdom(<coverage_id>) and compare it with the coverage's true array shape,
     recovered through petascope with a WCPS type-error probe. This proves
     whether the same-named collection holds this coverage's data. It cannot
     tell you where the data went when it does not.

  C. THE SHAPE (last resort, often useless).  Match unclaimed collections to
     unmatched coverages by array shape. Only works where the shape is
     distinctive. On zeus it is not: 171 of 177 unmatched coverages share just
     two shapes, so C resolves almost nothing here. The script measures this
     and warns you rather than guessing.

VERDICTS
--------
  matched              the same-named collection holds this coverage's array
  axis order differs   same array, storage order differs from catalogue order
  RANK MISMATCH        the collection has a different NUMBER of dimensions, so
                       it certainly is not this coverage's array
  EXTENT DIVERGENCE    same rank, different sizes: either a different array or
                       petascope's cached grid domain has drifted. Not
                       resolvable from here -- see the printed follow-up check
  EMPTY COLLECTION     a 1-cell placeholder sits at this name; the real data,
                       if any, is elsewhere
  no collection of this name
                       rasdaman has nothing at the coverage's own name. This is
                       a state the script FOUND, not an action it took -- it
                       never writes. Usually wcst_import stored the array under
                       a timestamped name; --mapping-file resolves it.
  unknown              the probe could not establish the shape

  SHADOW               with --mapping-file: the coverage's array lives elsewhere,
                       yet something still sits at the coverage's own name.
                       Bytes on disk that petascope does not serve. Same shape
                       means a full duplicate, usually a re-ingest that left the
                       previous copy behind; different shape means an older,
                       smaller version. --sizes prices them.

  ORPHAN               a collection no coverage claims -- candidate dead disk
  name-squatter        a collection whose name matches a coverage that points
                       somewhere else (the dangerous case: rasql answers
                       confidently about the wrong array)

GETTING THE POINTER (route A)
-----------------------------
petascopedb is ordinary PostgreSQL. Reading it is read-only and needs no sudo,
only database read access. The table holding the pointer has moved between
petascope versions, so discover it rather than assuming:

    psql -d petascopedb -Atc "
      SELECT table_name, column_name
        FROM information_schema.columns
       WHERE table_schema='public'
         AND (column_name ILIKE '%collection%'
              OR column_name ILIKE '%oid%'
              OR column_name ILIKE '%rasdaman%')
       ORDER BY table_name, column_name;"

That prints the table and column that carry the collection name (commonly a
rasdaman range-set table joined to the coverage table by coverage id). Then
export the mapping as two columns:

    psql -d petascopedb -Atc "
      SELECT c.coverage_id, r.<collection_column>
        FROM coverage c JOIN <rangeset_table> r ON r.<fk> = c.id;" \
      > mapping.txt

and pass it with --mapping-file mapping.txt (comma, tab or whitespace
separated; a header line is skipped automatically).

GETTING THE COLLECTION LIST (needed only for ORPHAN detection)
--------------------------------------------------------------
  --collections-query 'select r from RAS_COLLECTIONNAMES as r'
      Through the rasql servlet. Worth one attempt; it aborts on some builds.
  --collections-file names.txt
      One name per line, from whatever route works on your box:
        rasql -q 'select r from RAS_COLLECTIONNAMES as r' --out string
        psql -d RASBASE -Atc 'SELECT collname FROM ras_collectionnames'
  neither
      Everything else still runs. You simply cannot find a collection nobody
      pointed you at without enumerating them.

Everything here is READ-ONLY: GetCapabilities, a WCPS query rejected before it
executes, sdom() and dbinfo().

    export RASDAMAN_USER=rasadmin
    export RASDAMAN_PASS='...'

    python3 rasdaman_collection_reconcile.py \
        --url 'https://zeus.snap.uaf.edu/rasdaman/ows?&SERVICE=WCS&ACCEPTVERSIONS=2.1.0&REQUEST=GetCapabilities' \
        --outdir ./reconcile \
        --mapping-file mapping.txt \
        --collections-file names.txt --sizes

    python3 rasdaman_collection_reconcile.py --self-test     # offline, no server
"""

import argparse
import base64
import csv
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

RANGE_RE = re.compile(r"(-?\d+)\s*:\s*(-?\d+)")
ARRAY_RE = re.compile(r"ARRAY\s*\(([^)]*)\)\s*\[([^\]]*)\]", re.I)
DIM_RE = re.compile(r"D\d+\s*\(\s*(-?\d+)\s*:\s*(-?\d+)\s*\)")
EXC_RE = re.compile(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>")

# rasdaman says one of these when the name resolves to nothing
ABSENT_MARKERS = ("object unknown", "collection name unknown",
                  "collection does not exist", "unknown collection")

MATCHED = "matched"
AXIS_ORDER = "matched (axis order differs)"
RANK = "RANK MISMATCH"
DIVERGENCE = "EXTENT DIVERGENCE"
EMPTY = "EMPTY COLLECTION"
RENAMED = "no collection of this name"
UNKNOWN = "unknown"

RESOLVED = (MATCHED, AXIS_ORDER)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, (body or "").strip()
        hit = EXC_RE.search(self.body)
        msg = re.sub(r"\s+", " ", hit.group(1)).strip() if hit else ""
        super(HttpError, self).__init__(
            "HTTP {} -- {}".format(status, (msg or self.body)[:300]))


def _open(req, timeout, ctx):
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise HttpError(exc.code, body)


def http_get(url, timeout, ctx, auth=None):
    req = urllib.request.Request(url, method="GET")
    if auth:
        req.add_header("Authorization", auth)
    return _open(req, timeout, ctx)


def post_form(url, fields, timeout, ctx, auth=None):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", auth)
    return _open(req, timeout, ctx)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def extents(text):
    """'[0:99,0:2]' -> [100, 3]. None when nothing parses."""
    if not text:
        return None
    m = re.search(r"\[[^\]]*\]", text)
    pairs = RANGE_RE.findall(m.group(0) if m else text)
    return [int(b) - int(a) + 1 for a, b in pairs] if pairs else None


def coverage_ids(xml_text):
    root = ET.fromstring(xml_text)
    out, seen = [], set()
    for el in root.iter():
        if el.tag.split("}")[-1] == "CoverageId" and el.text:
            c = el.text.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def looks_absent(text):
    low = (text or "").lower()
    return any(m in low for m in ABSENT_MARKERS)


def read_mapping(path):
    """coverage_id -> collection_name, from comma/tab/whitespace columns."""
    out = {}
    with open(path) as fh:
        for n, line in enumerate(fh):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip().strip('"') for p in re.split(r"[,\t|]|\s{2,}|\s", line)
                     if p.strip()]
            if len(parts) < 2:
                continue
            if n == 0 and parts[0].lower() in ("coverage_id", "coverageid", "coverage"):
                continue
            out[parts[0]] = parts[1]
    return out


# --------------------------------------------------------------------------
# the classification -- pure, so it can be tested without a server
# --------------------------------------------------------------------------

def classify(cov_extents, coll_exists, coll_extents, error=""):
    """Compare a coverage's true array with a named collection's array.

    cov_extents  list[int] from the WCPS probe (petascope's own view), or None
    coll_exists  True / False / None (None = could not tell)
    coll_extents list[int] from sdom(), or None
    """
    if not cov_extents:
        return UNKNOWN, "WCPS probe returned no array descriptor"
    if coll_exists is False:
        return RENAMED, "rasdaman has no collection of this name"
    if coll_exists is not True:
        return UNKNOWN, (error or "sdom() gave no usable answer")[:200]

    ce = coll_extents or []
    if not ce:
        return UNKNOWN, "sdom() returned nothing parseable"
    if ce == list(cov_extents):
        return MATCHED, ""
    if len(ce) != len(cov_extents):
        return RANK, "collection is {}-D {}, coverage is {}-D {}".format(
            len(ce), ce, len(cov_extents), list(cov_extents))
    if sorted(ce) == sorted(cov_extents):
        return AXIS_ORDER, "collection {} vs coverage {}".format(ce, list(cov_extents))
    if set(ce) == {1}:
        return EMPTY, "collection holds a single cell; coverage claims {}".format(
            list(cov_extents))
    return DIVERGENCE, "collection {} vs coverage {}".format(ce, list(cov_extents))


def follow_up(verdict, cid, coll):
    """The one manual check that settles a verdict the script cannot."""
    if verdict == DIVERGENCE:
        return ("fetch one cell from the far end of the differing axis; if the "
                "coverage serves it, petascope points at a bigger array than "
                "'{}' -- if it errors, petascope's grid domain has drifted"
                .format(coll))
    if verdict == EMPTY:
        return ("request a small subset of '{}' over WCS; real values mean the "
                "data is under another collection name, nulls or an error mean "
                "the coverage is empty".format(cid))
    if verdict == RANK:
        return ("'{}' cannot be this coverage's array; find the real one via "
                "petascopedb (--mapping-file)".format(coll))
    if verdict == RENAMED:
        return "resolve through petascopedb (--mapping-file); shape alone will not find it"
    return ""


# --------------------------------------------------------------------------
# server probes
# --------------------------------------------------------------------------

def wcps_shape(ows_url, cid, timeout, ctx, auth):
    """The coverage's true stored array, via a deliberate WCPS type error.

    This goes through petascope, so it follows the coverage's own pointer --
    which is exactly why it works when the collection name is unknown.
    """
    q = 'for $c in ({}) return encode($c * "__probe__", "csv")'.format(cid)
    try:
        body = post_form(ows_url, {"service": "WCS", "version": "2.0.1",
                                   "request": "ProcessCoverages", "query": q},
                         timeout, ctx, auth)
    except HttpError as exc:
        body = exc.body
    except Exception:
        return None
    hit = EXC_RE.search(body)
    hit = ARRAY_RE.search(hit.group(1) if hit else body)
    if not hit:
        return None
    dims = DIM_RE.findall(hit.group(2))
    if not dims:
        return None
    return {"cell_type": re.sub(r"\s+", " ", hit.group(1)).strip(),
            "extents": [int(b) - int(a) + 1 for a, b in dims]}


def collection_shape(rasql_url, name, timeout, ctx, auth):
    """sdom() on a collection name. exists=False means rasdaman has no such object."""
    try:
        txt = post_form(rasql_url, {"query": "select sdom(c) from {} as c".format(name)},
                        timeout, ctx, auth)
    except HttpError as exc:
        if looks_absent(exc.body) or looks_absent(str(exc)):
            return {"exists": False}
        return {"exists": None, "error": str(exc)[:200]}
    except Exception as exc:
        return {"exists": None, "error": str(exc)[:200]}
    return {"exists": True, "extents": extents(txt)}


def collection_size(rasql_url, name, timeout, ctx, auth):
    try:
        txt = post_form(rasql_url, {"query": "select dbinfo(c) from {} as c".format(name)},
                        timeout, ctx, auth)
    except Exception:
        return {}
    start = txt.find("{")
    if start < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(txt[start:])
    except ValueError:
        return {}

    def dig(o, k):
        if isinstance(o, dict):
            if k in o:
                return o[k]
            for v in o.values():
                r = dig(v, k)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = dig(v, k)
                if r is not None:
                    return r
        return None

    def as_int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    return {"tiles": as_int(dig(obj, "tileNo")),
            "bytes": as_int(dig(obj, "totalSize")),
            "base_type": dig(obj, "baseType")}


def probe_describe_pointer(ows_url, cid, timeout, ctx, auth):
    """Does DescribeCoverage leak the collection name? Cheap to ask once.

    Some petascope builds emit a rangeSet fileReference or a rasdaman-namespaced
    element naming the collection. Most do not. Asked once, not per coverage.
    """
    url = ("{}?SERVICE=WCS&VERSION=2.0.1&REQUEST=DescribeCoverage&COVERAGEID={}"
           .format(ows_url, urllib.parse.quote(cid)))
    try:
        xml = http_get(url, timeout, ctx, auth)
    except Exception as exc:
        return {"available": False, "note": "DescribeCoverage failed: {}".format(str(exc)[:120])}
    hits = re.findall(r"<[^>]*(?:fileReference|collection|rasdaman)[^>]*>[^<]*",
                      xml, re.I)
    if hits:
        return {"available": True,
                "note": "DescribeCoverage exposes: " + " | ".join(h.strip()[:120] for h in hits[:3])}
    return {"available": False, "note": "DescribeCoverage carries no collection reference"}


# --------------------------------------------------------------------------
# self-test: the five divergent coverages found on zeus, replayed offline
# --------------------------------------------------------------------------

CASES = [
    # cid, coverage extents, coll exists, coll extents, expected verdict
    ("air_freezing_index_Fdays", [80, 30, 20], True, [80, 30, 20], MATCHED),
    ("cmip6_downscaled_pr_wms", [2, 2, 2, 443, 460], True, [2, 2, 443, 460], RANK),
    ("cmip6_fwi", [5, 25680, 178, 569], True, [5, 44165, 178, 569], DIVERGENCE),
    ("era5_4km_daily_rh2_max", [23376, 460, 442], True, [21915, 460, 442], DIVERGENCE),
    ("temperature_anomaly_anomalies", [13, 5, 251, 1440, 158], True, [1, 1, 1, 1, 1], EMPTY),
    ("temperature_anomaly_baselines", [13, 1440, 158], True, [1, 1, 1], EMPTY),
    ("cmip6_downscaled_pr_v2_wms", [2, 2, 2, 443, 460], False, None, RENAMED),
    ("permuted_example", [10, 20, 30], True, [30, 10, 20], AXIS_ORDER),
    ("no_probe", None, True, [1, 2], UNKNOWN),
    ("sdom_unreadable", [1, 2], None, None, UNKNOWN),
]


def self_test():
    bad = 0
    print("classify() -- replaying the divergent cases observed on zeus\n")
    for cid, cov, exists, coll, want in CASES:
        got, detail = classify(cov, exists, coll)
        ok = got == want
        bad += 0 if ok else 1
        print("  {} {:34s} {:22s} {}".format("ok  " if ok else "FAIL",
                                             cid, got, detail[:60]))
    print("\nextents() parsing")
    for text, want in [("[0:99,0:2]", [100, 3]),
                       ("{[0:1,0:1,0:442,0:459]}", [2, 2, 443, 460]),
                       ("", None), ("no brackets here", None)]:
        got = extents(text)
        ok = got == want
        bad += 0 if ok else 1
        print("  {} {!r:34s} -> {}".format("ok  " if ok else "FAIL", text, got))
    print("\nabsent-marker detection")
    for text, want in [("[RasdamanRequestFailed] Object Unknown: foo", True),
                       ("Collection name unknown", True),
                       ("some other error", False)]:
        got = looks_absent(text)
        ok = got == want
        bad += 0 if ok else 1
        print("  {} {!r:44s} -> {}".format("ok  " if ok else "FAIL", text[:42], got))
    print("\n{}".format("all checks passed" if not bad else "{} FAILURES".format(bad)))
    return 1 if bad else 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Reconcile petascope coverages against rasdaman collections.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="WCS GetCapabilities URL")
    ap.add_argument("--outdir", help="Output directory (created if absent)")
    ap.add_argument("--rasql-url", default=None, help="rasql endpoint (default: derived from --url)")
    ap.add_argument("--mapping-file", default=None,
                    help="coverage_id -> collection_name, exported from petascopedb. "
                         "The authoritative route; see the module docstring.")
    ap.add_argument("--collections-file", default=None,
                    help="Text file of collection names, one per line. Enables ORPHAN detection.")
    ap.add_argument("--collections-query", default=None,
                    help="rasql query returning collection names, e.g. "
                         "'select r from RAS_COLLECTIONNAMES as r'. Tried before the file.")
    ap.add_argument("--sizes", action="store_true",
                    help="Run dbinfo on every unmatched collection, to price orphans in GB. Slower.")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="Replay the known cases offline and exit. No server needed.")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    if not args.url or not args.outdir:
        ap.error("--url and --outdir are required (or use --self-test)")

    user, pw = os.environ.get("RASDAMAN_USER"), os.environ.get("RASDAMAN_PASS")
    if not user or not pw:
        sys.exit("ERROR: set RASDAMAN_USER and RASDAMAN_PASS first.")
    auth = "Basic " + base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    ctx = None
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    ows = args.url.split("?")[0]
    if args.rasql_url:
        rasql_url = args.rasql_url
    else:
        p = urllib.parse.urlsplit(args.url)
        path = (p.path.rsplit("/ows", 1)[0] + "/rasql" if "/ows" in p.path
                else p.path.rstrip("/").rsplit("/", 1)[0] + "/rasql")
        rasql_url = urllib.parse.urlunsplit((p.scheme, p.netloc, path, "", ""))

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    print("GetCapabilities : {}\nrasql           : {}\noutput          : {}\n"
          .format(args.url, rasql_url, outdir))

    covs = coverage_ids(http_get(args.url, args.timeout, ctx, auth))
    print("coverages published : {}".format(len(covs)))

    # ---- route A: the real pointer
    mapping = {}
    if args.mapping_file:
        mapping = read_mapping(args.mapping_file)
        route = "petascopedb mapping ({} entries) -- AUTHORITATIVE".format(len(mapping))
    else:
        route = "no mapping file -- falling back to the name, which is an assumption"
    print("mapping route       : {}".format(route))

    # is DescribeCoverage a free alternative? ask once
    dc = probe_describe_pointer(ows, covs[0], args.timeout, ctx, auth) if covs else {}
    if dc:
        print("DescribeCoverage    : {}".format(dc.get("note", "")))

    # ---- the collection list (only needed to find orphans)
    collections, coll_note = [], "not supplied -- ORPHAN detection disabled"
    if args.collections_query:
        try:
            raw = post_form(rasql_url, {"query": args.collections_query},
                            args.timeout, ctx, auth)
            names = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", raw)))
            drop = {"struct", "marray", "set", "array", "char", "float", "double",
                    "short", "long", "ulong", "ushort", "bool", "octet", "select", "from"}
            collections = [n for n in names if n.lower() not in drop]
            coll_note = "via query ({} names)".format(len(collections))
        except Exception as exc:
            coll_note = "query failed: {}".format(str(exc)[:160])
            print("  collection query failed: {}".format(str(exc)[:140]))
    if not collections and args.collections_file:
        with open(args.collections_file) as fh:
            collections = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        coll_note = "from file {} ({} names)".format(args.collections_file, len(collections))
    print("collection list     : {}\n".format(coll_note))

    # ---- per coverage: true array, then the collection it should live in
    rows, by_shape = [], {}
    for i, cid in enumerate(covs, 1):
        shape = wcps_shape(ows, cid, args.timeout, ctx, auth)
        ext = (shape or {}).get("extents")
        target = mapping.get(cid, cid)
        same = collection_shape(rasql_url, target, args.timeout, ctx, auth)
        verdict, detail = classify(ext, same.get("exists"), same.get("extents"),
                                   same.get("error", ""))

        # ---- shadow: petascope serves from elsewhere, but is anything still
        #      parked at the coverage's own name? Only askable once we have a
        #      real pointer, so this costs nothing without --mapping-file.
        shadow_name = shadow_ext = shadow_kind = ""
        shadow_bytes = shadow_tiles = ""
        if target != cid:
            sh = collection_shape(rasql_url, cid, args.timeout, ctx, auth)
            if sh.get("exists") is True:
                se = sh.get("extents") or []
                shadow_name = cid
                shadow_ext = ",".join(map(str, se))
                if ext and sorted(se) == sorted(ext):
                    shadow_kind = "SHADOW duplicate (same shape as the live array)"
                else:
                    shadow_kind = "SHADOW stale (different shape from the live array)"
                if args.sizes:
                    sz = collection_size(rasql_url, cid, args.timeout, ctx, auth)
                    shadow_bytes = sz.get("bytes") or ""
                    shadow_tiles = sz.get("tiles") or ""

        rows.append({
            "shadow_collection": shadow_name,
            "shadow_kind": shadow_kind,
            "shadow_extents": shadow_ext,
            "shadow_bytes": shadow_bytes,
            "shadow_tiles": shadow_tiles,
            "coverage_id": cid,
            "collection_checked": target,
            "pointer_source": "petascopedb" if cid in mapping else "assumed from name",
            "verdict": verdict,
            "detail": detail,
            "coverage_extents": ",".join(map(str, ext)) if ext else "",
            "collection_extents": ",".join(map(str, same.get("extents") or []))
                                  if same.get("exists") else "",
            "cell_type": (shape or {}).get("cell_type", ""),
            "follow_up": follow_up(verdict, cid, target),
        })
        if ext and verdict not in RESOLVED:
            by_shape.setdefault(tuple(sorted(ext)), []).append(cid)
        if i % 25 == 0 or i == len(covs):
            print("  [{}/{}] {}".format(i, len(covs), verdict))

    # ---- how discriminating is shape here? measure before relying on it
    unresolved = [r for r in rows if r["verdict"] not in RESOLVED]
    uniq = sum(1 for k, v in by_shape.items() if len(v) == 1)
    shape_note = ("{} unresolved coverages occupy {} distinct shapes; only {} are unique, "
                  "so shape identifies at most {} of them"
                  .format(len(unresolved), len(by_shape), uniq, uniq))
    if unresolved:
        print("\nshape as a key      : {}".format(shape_note))
        if uniq < len(unresolved) / 2:
            print("                      -> shape will NOT resolve this server. Use "
                  "--mapping-file.")

    # ---- collections nobody claimed
    claimed = {r["collection_checked"] for r in rows if r["verdict"] in RESOLVED}
    cov_set = set(covs)
    orphans = []
    for k, name in enumerate(collections, 1):
        if name in claimed:
            continue
        sh = collection_shape(rasql_url, name, args.timeout, ctx, auth)
        if sh.get("exists") is not True:
            continue
        ext = sh.get("extents") or []
        cand = by_shape.get(tuple(sorted(ext)), [])
        if name in cov_set:
            kind = "name-squatter (a coverage of this name points elsewhere)"
        elif len(cand) == 1:
            kind = "orphan -> likely the collection for {}".format(cand[0])
        elif cand:
            kind = "orphan -> ambiguous, {} coverages share this shape".format(len(cand))
        else:
            kind = "ORPHAN (no coverage has this shape)"
        entry = {"collection": name, "kind": kind,
                 "extents": ",".join(map(str, ext)),
                 "shape_matches": "; ".join(cand[:6])}
        if args.sizes:
            entry.update({"size_" + k2: v for k2, v in
                          collection_size(rasql_url, name, args.timeout, ctx, auth).items()})
        orphans.append(entry)
        if k % 25 == 0:
            print("  collections scanned: {}/{}".format(k, len(collections)))

    # ---- write
    cfields = ["coverage_id", "collection_checked", "pointer_source", "verdict",
               "detail", "coverage_extents", "collection_extents", "cell_type",
               "shadow_collection", "shadow_kind", "shadow_extents",
               "shadow_bytes", "shadow_tiles", "follow_up"]
    with open(os.path.join(outdir, "_coverages.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cfields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    if orphans:
        base = ["collection", "kind", "extents", "shape_matches"]
        extra = sorted({k for o in orphans for k in o} - set(base))
        with open(os.path.join(outdir, "_collections.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=base + extra, extrasaction="ignore")
            w.writeheader()
            for o in orphans:
                w.writerow(o)

    tally = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    meta = {"generated_utc": datetime.now(timezone.utc).isoformat(),
            "capabilities_url": args.url, "rasql_url": rasql_url,
            "coverages": len(covs), "mapping_route": route,
            "describe_coverage_pointer": dc,
            "collection_list": coll_note, "collections_examined": len(collections),
            "shape_discrimination": shape_note, "verdicts": tally}
    with open(os.path.join(outdir, "_run.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    print("\n=== coverages ===")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print("  {:24s} {}".format(k, v))

    shadows = [r for r in rows if r["shadow_collection"]]
    if shadows:
        dup = sum(1 for r in shadows if "duplicate" in r["shadow_kind"])
        print("\n=== shadow collections ===")
        print("  {} coverages have something parked at their own name that "
              "petascope does not serve".format(len(shadows)))
        print("  {} of those are full duplicates of the live array".format(dup))
        if args.sizes:
            tot = sum(r["shadow_bytes"] for r in shadows
                      if isinstance(r["shadow_bytes"], int))
            print("  unreferenced bytes: {:,.1f} GB".format(tot / 1e9))
        else:
            print("  re-run with --sizes to price them")
    if orphans:
        okinds = {}
        for o in orphans:
            okinds[o["kind"].split(" ->")[0].split(" (")[0]] = \
                okinds.get(o["kind"].split(" ->")[0].split(" (")[0], 0) + 1
        print("\n=== collections not matched 1:1 ===")
        for k, v in sorted(okinds.items(), key=lambda kv: -kv[1]):
            print("  {:24s} {}".format(k, v))
        if args.sizes:
            tot = sum(o.get("size_bytes") or 0 for o in orphans
                      if o["kind"].startswith("ORPHAN"))
            print("\n  disk held by true orphans: {:,.1f} GB".format(tot / 1e9))
    print("\nwrote {}/_coverages.csv{}".format(
        outdir, " and _collections.csv" if orphans else ""))


if __name__ == "__main__":
    main()
