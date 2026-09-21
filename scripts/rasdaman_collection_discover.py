#!/usr/bin/env python3
"""
rasdaman_collection_discover.py

Find the naming rule that maps a petascope coverage to its rasdaman collection,
by generating candidate names and asking rasdaman whether each one exists.

WHY
---
On zeus, 174 coverages have no collection of the same name. The data is there --
the coverages serve it -- so the collections live under some other name. If
wcst_import applied a consistent transformation, a small set of candidate rules
will find it. If it generated opaque names (a serial number, a hash), no rule
will hit, and that is itself the answer: only petascopedb or the full collection
list can resolve the mapping.

Either way you learn something definite in a few minutes.

HOW IT AVOIDS FOOLING ITSELF
----------------------------
A candidate is only accepted when all three hold:

  1. rasdaman says a collection of that name exists;
  2. its array shape matches the coverage's true shape;
  3. the name is NOT itself a published coverage id.

Rule 3 matters. Stripping "_v2" off cmip6_downscaled_tasmax_v2_wms yields
cmip6_downscaled_tasmax_wms -- which exists, and has the right shape, and
belongs to a completely different coverage. Without that guard this script would
cheerfully report that two coverages share one array.

INPUT
-----
The _coverages.csv written by rasdaman_collection_reconcile.py. Rows whose
verdict is a clean match are skipped; the rest are probed.

    export RASDAMAN_USER=rasadmin
    export RASDAMAN_PASS='...'

    # quick look: 25 coverages, a few hundred probes, a couple of minutes
    python3 rasdaman_collection_discover.py \
        --coverages ~/reconcile/_coverages.csv \
        --rasql-url https://zeus.snap.uaf.edu/rasdaman/rasql \
        --outdir ~/reconcile

    # the whole unresolved population
    python3 rasdaman_collection_discover.py ... --all

Read-only throughout: sdom() and nothing else.

    python3 rasdaman_collection_discover.py --self-test    # offline
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
from datetime import datetime, timezone

RANGE_RE = re.compile(r"(-?\d+)\s*:\s*(-?\d+)")
EXC_RE = re.compile(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>")
ABSENT_MARKERS = ("object unknown", "collection name unknown",
                  "collection does not exist", "unknown collection")

RESOLVED_VERDICTS = ("matched", "matched (axis order differs)")
SERVICE_SUFFIXES = ("_wcs", "_wms")
APPEND_TOKENS = ("_data", "_coll", "_collection", "_array", "_c",
                 "_1", "_2", "_new", "_tmp")


# --------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, (body or "").strip()
        hit = EXC_RE.search(self.body)
        msg = re.sub(r"\s+", " ", hit.group(1)).strip() if hit else ""
        super(HttpError, self).__init__(
            "HTTP {} -- {}".format(status, (msg or self.body)[:200]))


def post_form(url, fields, timeout, ctx, auth=None):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise HttpError(exc.code, body)


def looks_absent(text):
    low = (text or "").lower()
    return any(m in low for m in ABSENT_MARKERS)


def extents(text):
    if not text:
        return None
    m = re.search(r"\[[^\]]*\]", text)
    pairs = RANGE_RE.findall(m.group(0) if m else text)
    return [int(b) - int(a) + 1 for a, b in pairs] if pairs else None


# --------------------------------------------------------------------------
# the rules -- pure, testable, and easy to extend
# --------------------------------------------------------------------------

def candidates(cid):
    """(rule_name, candidate_name) pairs to try for one coverage id.

    Ordered roughly by prior plausibility. Duplicates and no-ops are dropped.
    """
    out, seen = [], {cid}

    def add(rule, name):
        if name and name not in seen:
            seen.add(name)
            out.append((rule, name))

    base = cid
    service = ""
    for suf in SERVICE_SUFFIXES:
        if cid.endswith(suf):
            base, service = cid[:-len(suf)], suf
            break

    if "_v2" in cid:
        novers = cid.replace("_v2", "", 1)
        add("strip_v2", novers)
        add("v2_moved_to_end", novers + "_v2")
        add("v2_as_2", cid.replace("_v2", "_2", 1))
        add("v2_joined", cid.replace("_v2", "v2", 1))
        add("v2_as_version2", cid.replace("_v2", "_version2", 1))

    if service:
        add("strip_service_suffix", base)
        if "_v2" in base:
            add("strip_v2_and_service", base.replace("_v2", "", 1))
            add("service_before_v2", base.replace("_v2", "", 1) + service + "_v2")

    for tok in APPEND_TOKENS:
        add("append" + tok, cid + tok)

    add("lowercased", cid.lower())
    add("ras_prefix", "ras_" + cid)

    return out


def accept(cov_extents, coll, name, published):
    """Is this candidate a real hit, or a trap?"""
    if name in published:
        return False, "name belongs to another published coverage"
    if coll.get("exists") is not True:
        return False, "no such collection"
    ce = coll.get("extents") or []
    if not ce:
        return False, "sdom unreadable"
    if not cov_extents:
        return True, "exists (coverage shape unknown, unverified)"
    if sorted(ce) == sorted(cov_extents):
        return True, "exists and shape matches"
    return False, "exists but shape is {} not {}".format(ce, list(cov_extents))


# --------------------------------------------------------------------------

def collection_shape(rasql_url, name, timeout, ctx, auth, cache):
    if name in cache:
        return cache[name]
    try:
        txt = post_form(rasql_url, {"query": "select sdom(c) from {} as c".format(name)},
                        timeout, ctx, auth)
        res = {"exists": True, "extents": extents(txt)}
    except HttpError as exc:
        res = ({"exists": False} if looks_absent(exc.body) or looks_absent(str(exc))
               else {"exists": None, "error": str(exc)[:160]})
    except Exception as exc:
        res = {"exists": None, "error": str(exc)[:160]}
    cache[name] = res
    return res


def read_coverages(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            ext = [int(x) for x in (r.get("coverage_extents") or "").split(",") if x.strip()]
            rows.append({"coverage_id": r["coverage_id"],
                         "verdict": r.get("verdict", ""),
                         "extents": ext})
    return rows


# --------------------------------------------------------------------------

def self_test():
    bad = 0
    print("candidates() for a v2 coverage\n")
    cs = candidates("cmip6_downscaled_pr_7ModelAvg_historical_v2_wcs")
    for rule, name in cs:
        print("  {:24s} {}".format(rule, name))
    names = [n for _, n in cs]
    for want in ["cmip6_downscaled_pr_7ModelAvg_historical_wcs",
                 "cmip6_downscaled_pr_7ModelAvg_historical_wcs_v2",
                 "cmip6_downscaled_pr_7ModelAvg_historical_v2",
                 "cmip6_downscaled_pr_7ModelAvg_historical_v2_wcs_data"]:
        ok = want in names
        bad += 0 if ok else 1
        print("  {} generates {}".format("ok  " if ok else "FAIL", want))
    ok = len(names) == len(set(names))
    bad += 0 if ok else 1
    print("  {} no duplicate candidates ({} total)".format("ok  " if ok else "FAIL", len(names)))

    print("\naccept() guards")
    pub = {"cmip6_downscaled_tasmax_wms"}
    checks = [
        ([2, 3], {"exists": True, "extents": [2, 3]}, "some_collection", True),
        ([2, 3], {"exists": True, "extents": [2, 3]}, "cmip6_downscaled_tasmax_wms", False),
        ([2, 3], {"exists": True, "extents": [9, 9]}, "some_collection", False),
        ([2, 3], {"exists": False}, "some_collection", False),
        ([3, 2], {"exists": True, "extents": [2, 3]}, "some_collection", True),
    ]
    for ext, coll, name, want in checks:
        got, why = accept(ext, coll, name, pub)
        okk = got == want
        bad += 0 if okk else 1
        print("  {} {:32s} -> {} ({})".format("ok  " if okk else "FAIL", name, got, why))

    print("\nextents()")
    for text, want in [("{[0:1,0:2]}", [2, 3]), ("", None)]:
        got = extents(text)
        okk = got == want
        bad += 0 if okk else 1
        print("  {} {!r:16s} -> {}".format("ok  " if okk else "FAIL", text, got))

    print("\n{}".format("all checks passed" if not bad else "{} FAILURES".format(bad)))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(
        description="Discover the coverage -> collection naming rule by probing.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coverages", help="_coverages.csv from the reconcile run")
    ap.add_argument("--rasql-url", help="e.g. https://host/rasdaman/rasql")
    ap.add_argument("--outdir", help="where to write _pattern.csv")
    ap.add_argument("--sample", type=int, default=25,
                    help="probe this many unresolved coverages (default 25)")
    ap.add_argument("--all", action="store_true", help="probe every unresolved coverage")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())
    for need in ("coverages", "rasql_url", "outdir"):
        if not getattr(args, need):
            ap.error("--coverages, --rasql-url and --outdir are required (or --self-test)")

    user, pw = os.environ.get("RASDAMAN_USER"), os.environ.get("RASDAMAN_PASS")
    if not user or not pw:
        sys.exit("ERROR: set RASDAMAN_USER and RASDAMAN_PASS first.")
    auth = "Basic " + base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    ctx = None
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    allrows = read_coverages(args.coverages)
    published = {r["coverage_id"] for r in allrows}
    todo = [r for r in allrows if r["verdict"] not in RESOLVED_VERDICTS]
    if not args.all:
        todo = todo[:args.sample]

    print("unresolved coverages : {} of {}".format(
        len([r for r in allrows if r["verdict"] not in RESOLVED_VERDICTS]), len(allrows)))
    print("probing              : {}".format(len(todo)))
    print("rules per coverage   : {}\n".format(len(candidates(todo[0]["coverage_id"])) if todo else 0))

    cache, hits, rule_tally, rule_tried = {}, [], {}, {}
    for i, r in enumerate(todo, 1):
        cid = r["coverage_id"]
        found = False
        for rule, name in candidates(cid):
            rule_tried[rule] = rule_tried.get(rule, 0) + 1
            coll = collection_shape(args.rasql_url, name, args.timeout, ctx, auth, cache)
            ok, why = accept(r["extents"], coll, name, published)
            if ok:
                rule_tally[rule] = rule_tally.get(rule, 0) + 1
                hits.append({"coverage_id": cid, "rule": rule, "collection": name,
                             "why": why,
                             "coverage_extents": ",".join(map(str, r["extents"])),
                             "collection_extents": ",".join(map(str, coll.get("extents") or []))})
                found = True
                break
        if not found:
            hits.append({"coverage_id": cid, "rule": "", "collection": "",
                         "why": "no candidate rule matched",
                         "coverage_extents": ",".join(map(str, r["extents"])),
                         "collection_extents": ""})
        if i % 10 == 0 or i == len(todo):
            print("  [{}/{}] {} resolved, {} distinct names probed".format(
                i, len(todo), sum(1 for h in hits if h["rule"]), len(cache)))

    fields = ["coverage_id", "rule", "collection", "why",
              "coverage_extents", "collection_extents"]
    with open(os.path.join(outdir, "_pattern.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for h in hits:
            w.writerow(h)

    resolved = sum(1 for h in hits if h["rule"])
    existing = sum(1 for v in cache.values() if v.get("exists") is True)
    meta = {"generated_utc": datetime.now(timezone.utc).isoformat(),
            "probed_coverages": len(todo), "resolved": resolved,
            "distinct_names_probed": len(cache),
            "names_that_exist": existing,
            "rule_hits": rule_tally, "rule_attempts": rule_tried}
    with open(os.path.join(outdir, "_pattern.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    print("\n=== which rule found it ===")
    if rule_tally:
        for k, v in sorted(rule_tally.items(), key=lambda kv: -kv[1]):
            print("  {:24s} {} of {} coverages".format(k, v, len(todo)))
    else:
        print("  none. No candidate name existed with the right shape.")
        print("  {} distinct names probed, {} existed at all."
              .format(len(cache), existing))
        print("  -> the collection names are not derived from the coverage id.")
        print("     petascopedb or the full collection list is the only route.")
    print("\nwrote {}/_pattern.csv".format(outdir))


if __name__ == "__main__":
    main()
