#!/usr/bin/env python3
"""
rasdaman_price_collections.py

Report what each unreferenced rasdaman collection costs on disk, priced from
RAS_MDDOBJECTS.PhysicalSize in RASBASE -- not from dbinfo()'s totalSize.

Why not dbinfo() for the price
-------------------------------
totalSize is a sum over the tile *index*: cells-per-tile x bytes-per-cell x
the number of INDEXED entries. If a coverage's index has duplicate entries
(rasdaman-tiling-audit.md, Finding 1 -- happens when wcst_import is re-run
against a collection that already exists), totalSize is inflated by exactly
that duplication, sometimes several-fold. PhysicalSize, read directly off
RAS_MDDOBJECTS, is computed per stored array object and can't be inflated by
a duplicate index entry, because it isn't summed from index entries at all.
Earlier versions of this script priced off totalSize and overstated the
unreferenced-collections total by about 218 GB (3,522.8 GB vs. the real
3,304.8 GB) -- see rasdaman_physical_size.py's module docstring for the full
join this script now reuses.

dbinfo() is still used here, best-effort, for `sdom`, `base_type` and `tiles`
(a raw index-entry count, not a byte figure -- duplicate-index inflation
doesn't bias it the way it biases totalSize) -- fields RASBASE doesn't carry
and that are useful for a human reviewing the drop list. If a dbinfo() call
fails or a collection is missing from RASBASE, the row still gets whatever
price and review fields are available, with an `error` noting what's missing.

Read-only: RASBASE opened `mode=ro`, and dbinfo()/sdom() over rasql, nothing
else. It prints a drop statement per row for review, and never executes one.

    export RASDAMAN_USER=rasadmin
    export RASDAMAN_PASS='...'

    python3 rasdaman_price_collections.py \
        --rasql-url https://zeus.snap.uaf.edu/rasdaman/rasql \
        --rasbase /opt/rasdaman/data/RASBASE \
        --names rasdaman_unreferenced_names.txt \
        --out unreferenced_sizes.csv

Optionally pass the categorised CSV instead, and the categories are carried
through to the output:

    python3 rasdaman_price_collections.py ... \
        --rasbase /opt/rasdaman/data/RASBASE \
        --csv rasdaman_unreferenced_collections.csv --out unreferenced_sizes.csv
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

from rasdaman_physical_size import connect_ro, physical_sizes

EXC_RE = re.compile(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>")
ABSENT = ("object unknown", "collection name unknown", "unknown collection")


def post(url, query, timeout, ctx, auth):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode({"query": query}).encode("utf-8"),
        method="POST")
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
        hit = EXC_RE.search(body)
        msg = re.sub(r"\s+", " ", hit.group(1)).strip() if hit else body[:200]
        raise RuntimeError(msg)


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


def main():
    ap = argparse.ArgumentParser(description="Price rasdaman collections from RASBASE's PhysicalSize.")
    ap.add_argument("--rasql-url", required=True, help="Used for sdom/base_type/tiles only, not the price")
    ap.add_argument("--rasbase", required=True,
                     help="Path to the RASBASE sqlite file (read-only) -- source of the real price")
    ap.add_argument("--names", help="One collection name per line")
    ap.add_argument("--csv", help="CSV with a 'collection' column (categories carried through)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if not args.names and not args.csv:
        ap.error("pass --names or --csv")

    phys_by_name = {r["collection"]: r["physical_bytes"] for r in physical_sizes(connect_ro(args.rasbase))}
    user, pw = os.environ.get("RASDAMAN_USER"), os.environ.get("RASDAMAN_PASS")
    if not user or not pw:
        sys.exit("ERROR: set RASDAMAN_USER and RASDAMAN_PASS first.")
    auth = "Basic " + base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    ctx = None
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    extra, items = {}, []
    if args.csv:
        for r in csv.DictReader(open(args.csv)):
            items.append(r["collection"])
            extra[r["collection"]] = r
    else:
        items = [l.strip() for l in open(args.names) if l.strip() and not l.startswith("#")]

    rows, total = [], 0
    for i, name in enumerate(items, 1):
        row = {"collection": name}
        row.update({k: v for k, v in (extra.get(name) or {}).items() if k != "collection"})

        b = phys_by_name.get(name)
        row["bytes"] = b
        row["gb"] = round((b or 0) / 1e9, 3)
        total += b or 0
        if b is None:
            row["error"] = "not found in RASBASE (no RAS_MDDOBJECTS row, or name mismatch)"

        try:
            txt = post(args.rasql_url, "select dbinfo(c) from {} as c".format(name),
                       args.timeout, ctx, auth)
            start = txt.find("{")
            obj, _ = json.JSONDecoder().raw_decode(txt[start:]) if start >= 0 else (None, 0)
            if obj is None:
                row.setdefault("error", "unparseable dbinfo response")
            else:
                row["tiles"] = as_int(dig(obj, "tileNo"))
                row["base_type"] = dig(obj, "baseType")
        except Exception as exc:
            msg = str(exc)
            row.setdefault("error", "collection no longer exists" if
                            any(a in msg.lower() for a in ABSENT) else msg[:200])
        try:
            row["sdom"] = re.search(r"\[[^\]]*\]", post(
                args.rasql_url, "select sdom(c) from {} as c".format(name),
                args.timeout, ctx, auth)).group(0)
        except Exception:
            pass
        row["review_statement"] = "drop collection {}".format(name)
        rows.append(row)
        if i % 10 == 0 or i == len(items):
            print("  [{}/{}] {:,.1f} GB so far".format(i, len(items), total / 1e9))

    fields = ["collection", "category", "why", "gb", "bytes", "tiles", "sdom",
              "base_type", "mdd_coll_id", "set_type_id", "error", "review_statement"]
    seen = [f for f in fields if any(f in r for r in rows)]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=seen, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print("\ntotal across {} collections: {:,.1f} GB".format(len(rows), total / 1e9))
    if any(r.get("category") for r in rows):
        by = {}
        for r in rows:
            by[r.get("category", "")] = by.get(r.get("category", ""), 0) + (r.get("bytes") or 0)
        print("\nby category")
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print("  {:20s} {:,.1f} GB".format(k, v / 1e9))
    print("\nwrote {}".format(args.out))
    print("Nothing was dropped. The review_statement column is for a human to check.")


if __name__ == "__main__":
    main()
