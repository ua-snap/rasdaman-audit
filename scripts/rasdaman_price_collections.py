#!/usr/bin/env python3
"""
rasdaman_price_collections.py

Run dbinfo on a list of rasdaman collections and report what each one costs on
disk. Built to price the unreferenced collections found by comparing RASBASE's
RAS_MDDCOLLNAMES against petascope's coverage -> collection mapping.

Read-only: dbinfo() and sdom(), nothing else. It prints a drop statement per row
for review, and never executes one.

    export RASDAMAN_USER=rasadmin
    export RASDAMAN_PASS='...'

    python3 rasdaman_price_collections.py \
        --rasql-url https://zeus.snap.uaf.edu/rasdaman/rasql \
        --names rasdaman_unreferenced_names.txt \
        --out unreferenced_sizes.csv

Optionally pass the categorised CSV instead, and the categories are carried
through to the output:

    python3 rasdaman_price_collections.py ... \
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
    ap = argparse.ArgumentParser(description="Price rasdaman collections with dbinfo.")
    ap.add_argument("--rasql-url", required=True)
    ap.add_argument("--names", help="One collection name per line")
    ap.add_argument("--csv", help="CSV with a 'collection' column (categories carried through)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    if not args.names and not args.csv:
        ap.error("pass --names or --csv")
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
        try:
            txt = post(args.rasql_url, "select dbinfo(c) from {} as c".format(name),
                       args.timeout, ctx, auth)
            start = txt.find("{")
            obj, _ = json.JSONDecoder().raw_decode(txt[start:]) if start >= 0 else (None, 0)
            if obj is None:
                row["error"] = "unparseable dbinfo response"
            else:
                b = as_int(dig(obj, "totalSize"))
                row["bytes"] = b
                row["gb"] = round((b or 0) / 1e9, 3)
                row["tiles"] = as_int(dig(obj, "tileNo"))
                row["base_type"] = dig(obj, "baseType")
                total += b or 0
        except Exception as exc:
            msg = str(exc)
            row["error"] = ("collection no longer exists" if
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
