#!/usr/bin/env python3
"""
rasdaman_metadata_check.py

Find coverages whose metadata contains the word "into", and confirm whether
each one is actually broken.

WHY THIS EXISTS
---------------
If a coverage's metadata contains the word "into", every request that ENCODES
the coverage (GetCoverage as JSON / GeoTIFF / netCDF, WCPS encode()) comes back
as HTTP 200 with an EMPTY body. DescribeCoverage, WMS GetMap, GML GetCoverage
and unencoded WCPS scalars still work, so the coverage looks healthy until a
front end tries to parse nothing. See docs/rasdaman-metadata-keywords.md.

The exact trigger, measured on Zeus 2026-11-11, is the four letters "into"
with an ordinary ASCII space on BOTH sides, in any case (" into ", " INTO ").
"into b", "b into", "a\tinto b", "a.into b" and "take-into" all encode fine.
No other rasql keyword tested (insert, update, delete, drop, create, alter,
values, commit, select, from, where) has any effect.

Every whole-word "into" is reported. Hits that carry the exact trigger are
marked TRIGGER; the rest are marked "word" -- harmless as measured, but
rewording them costs nothing and doesn't depend on how wcst_import or petascope
reformats whitespace.

WHAT IT DOES
------------
  server mode (default)
      1. DescribeCoverage for each coverage (all of them from GetCapabilities,
         or the ones named with --coverage).
      2. Searches every text value inside <gmlcov:metadata> for "into",
         whole word, case-insensitive, and marks the exact-trigger hits.
      3. Probes: a WCPS encode() of ONE cell, as JSON, POSTed to
         ProcessCoverages. HTTP 200 with an empty body = AFFECTED.
         By default only coverages with a hit are probed (--probe hits);
         --probe all probes every coverage, --probe none skips it.

  text mode (--text FILE, or --text - for stdin)
      Searches any text before ingest: a recipe JSON, or `ncdump -h` output,
      which carries every netCDF attribute wcst_import may copy into the
      coverage's band, axis and global metadata.

          ncdump -h file.nc | python3 scripts/rasdaman_metadata_check.py --text -

      Exits 1 if anything is found, so it can gate an ingest script.

Every request is a read. Nothing is written to the server. No credentials are
needed: DescribeCoverage and ProcessCoverages are public on /rasdaman/ows.

OUTPUT
------
  stdout     one line per hit, with context, and a probe verdict per coverage
  --out CSV  coverage_id, n_into, n_trigger, probe, probe_http, probe_bytes,
             probe_detail, contexts

USAGE
-----
    python3 scripts/rasdaman_metadata_check.py --out ~/metadata_check.csv
    python3 scripts/rasdaman_metadata_check.py --coverage cmip6_fwi --probe all
    python3 scripts/rasdaman_metadata_check.py --self-test

Python 3.6+, standard library only.
"""

import argparse
import csv
import html
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://zeus.snap.uaf.edu/rasdaman/ows"

# Any whole-word "into" is reported; " into " (ASCII space both sides, any
# case) is the measured trigger. Tab/newline neighbours do not trigger it.
WORD_RE = re.compile(r"\binto\b", re.I)
TRIGGER_RE = re.compile(r" into ", re.I)

METADATA_RE = re.compile(r"<gmlcov:metadata>(.*?)</gmlcov:metadata>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
COVID_RE = re.compile(r"<wcs:CoverageId>\s*([^<\s]+)\s*</wcs:CoverageId>")
GRID_LABELS_RE = re.compile(r"<gml:axisLabels>([^<]+)</gml:axisLabels>")
GRID_LOW_RE = re.compile(r"<gml:low>([^<]+)</gml:low>")
EXC_RE = re.compile(r"<ows:ExceptionText>([\s\S]*?)</ows:ExceptionText>")


# --------------------------------------------------------------------------
# text scanning (pure; exercised by --self-test)
# --------------------------------------------------------------------------

def metadata_values(describe_xml):
    """Each text value inside <gmlcov:metadata>, unescaped, whitespace kept.

    Values are kept separate, never joined, so a tag boundary can't fake a
    " into " that isn't in any one value. Returns None if the coverage has no
    metadata block at all.
    """
    m = METADATA_RE.search(describe_xml)
    if not m:
        return None
    body = CDATA_RE.sub(lambda c: "<cdata>%s<cdata>" % html.escape(c.group(1)),
                        m.group(1))
    return [html.unescape(v) for v in TAG_RE.split(body) if v.strip()]


def find_into(values, width=60):
    """[(is_trigger, context), ...] for every whole-word "into".

    is_trigger is True when that occurrence has an ASCII space on both sides.
    """
    hits = []
    for text in values or ():
        for m in WORD_RE.finditer(text):
            trig = bool(TRIGGER_RE.match(text, m.start() - 1)) \
                if m.start() > 0 else False
            a = max(0, m.start() - width)
            b = min(len(text), m.end() + width // 2)
            ctx = re.sub(r"\s+", " ", text[a:b]).strip()
            hits.append((trig, ctx))
    return hits


def grid_origin(describe_xml):
    """[(axis_label, low_index), ...] in grid order, or None if unparseable."""
    lab = GRID_LABELS_RE.search(describe_xml)
    low = GRID_LOW_RE.search(describe_xml)
    if not lab or not low:
        return None
    labels, lows = lab.group(1).split(), low.group(1).split()
    if len(labels) != len(lows):
        return None
    return list(zip(labels, lows))


def probe_query(coverage_id, origin):
    """WCPS that encodes exactly one cell (the grid origin) as JSON.

    Index-CRS subsets ("CRS:1") so time and irregular axes need no
    coordinate formatting.
    """
    subs = ", ".join('%s:"CRS:1"(%s)' % (lab, lo) for lab, lo in origin)
    return ('for $c in (%s) return encode($c[%s], "application/json")'
            % (coverage_id, subs))


def classify_probe(status, body):
    """'ok' | 'AFFECTED' | 'error' from one probe response."""
    if status == 200 and len(body) == 0:
        return "AFFECTED"
    if status == 200:
        return "ok"
    return "error"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _ctx(insecure):
    if not insecure:
        return None
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def http(url, params=None, data=None, timeout=120, insecure=False):
    """(status, body_bytes). POST when data is given."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ctx(insecure)) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def list_coverages(url, insecure):
    st, body = http(url, params=dict(service="WCS", version="2.0.1",
                                     request="GetCapabilities"),
                    insecure=insecure)
    if st != 200:
        sys.exit("GetCapabilities failed: HTTP %s" % st)
    return sorted(set(COVID_RE.findall(body.decode("utf-8", "replace"))))


def describe(url, cov, insecure):
    st, body = http(url, params=dict(service="WCS", version="2.0.1",
                                     request="DescribeCoverage",
                                     coverageId=cov), insecure=insecure)
    return st, body.decode("utf-8", "replace")


def probe(url, cov, origin, insecure):
    st, body = http(url, data=dict(service="WCS", version="2.0.1",
                                   request="ProcessCoverages",
                                   query=probe_query(cov, origin)),
                    insecure=insecure)
    detail = ""
    if st != 200:
        m = EXC_RE.search(body.decode("utf-8", "replace"))
        detail = (m.group(1).strip() if m else body[:200].decode(
            "utf-8", "replace")).replace("\n", " ")
    return classify_probe(st, body), st, len(body), detail


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------

def run_text(path):
    text = sys.stdin.read() if path == "-" else open(path).read()
    hits = find_into(text.splitlines())
    for trig, ctx in hits:
        print("%-7s ... %s ..." % ("TRIGGER" if trig else "word", ctx))
    if not hits:
        print('clean: no "into"')
    return 1 if hits else 0


def run_server(args):
    covs = args.coverage or list_coverages(args.url, args.insecure)
    if args.limit:
        covs = covs[:args.limit]
    print("%d coverage(s) on %s" % (len(covs), args.url), file=sys.stderr)

    rows, n_hit, n_bad = [], 0, 0
    for i, cov in enumerate(covs, 1):
        st, xml = describe(args.url, cov, args.insecure)
        row = dict(coverage_id=cov, n_into=0, n_trigger=0, probe="",
                   probe_http="", probe_bytes="", probe_detail="", contexts="")
        if st != 200:
            row["probe"] = "describe_failed"
            row["probe_detail"] = "DescribeCoverage HTTP %s" % st
            rows.append(row)
            print("[%d/%d] %s  DescribeCoverage HTTP %s"
                  % (i, len(covs), cov, st))
            continue

        hits = find_into(metadata_values(xml))
        row["n_into"] = len(hits)
        row["n_trigger"] = sum(1 for t, _ in hits if t)
        row["contexts"] = " | ".join(c for _, c in hits)
        if hits:
            n_hit += 1

        if args.probe == "all" or (args.probe == "hits" and hits):
            origin = grid_origin(xml)
            if origin is None:
                row["probe"] = "no_grid"
            else:
                v, pst, nbytes, detail = probe(args.url, cov, origin,
                                               args.insecure)
                row.update(probe=v, probe_http=pst, probe_bytes=nbytes,
                           probe_detail=detail)
                if v == "AFFECTED":
                    n_bad += 1

        if hits or row["probe"] not in ("", "ok"):
            print("[%d/%d] %s  into=%d trigger=%d  probe=%s %s"
                  % (i, len(covs), cov, row["n_into"], row["n_trigger"],
                     row["probe"] or "skipped", row["probe_detail"]))
            for trig, ctx in hits:
                print("        %-7s ... %s ..."
                      % ("TRIGGER" if trig else "word", ctx))
        rows.append(row)

    print('\n%d coverage(s) scanned; %d with "into" in metadata; %d AFFECTED '
          "(empty encode)." % (len(covs), n_hit, n_bad))
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows
                               else ["coverage_id"])
            w.writeheader()
            w.writerows(rows)
        print("wrote %s" % args.out)
    return 1 if n_bad else 0


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def self_test():
    xml = """<wcs:CoverageDescriptions>
      <gml:domainSet><gml:RectifiedGrid>
        <gml:limits><gml:GridEnvelope>
          <gml:low>0 5 0</gml:low><gml:high>9 20 99</gml:high>
        </gml:GridEnvelope></gml:limits>
        <gml:axisLabels>time Y X</gml:axisLabels>
      </gml:RectifiedGrid></gml:domainSet>
      <gmlcov:metadata><gmlcov:Extension><ras:covMetadata>
        <ras:Abstract>the rule turns three classes INTO one</ras:Abstract>
        <ras:bands><ras:isi>
          <ras:description>does not take fuel type into account</ras:description>
          <ras:a>into</ras:a><ras:b>split here</ras:b>
          <ras:c>into b</ras:c><ras:d>b into</ras:d><ras:e>a	into b</ras:e>
          <ras:f>intoxicated; onto; insert values; drop</ras:f>
          <ras:g>&quot;water&quot; into &amp; out</ras:g>
        </ras:isi></ras:bands>
        <ras:x><![CDATA[{"comment": "goes into <air>"}]]></ras:x>
      </ras:covMetadata></gmlcov:Extension></gmlcov:metadata>
    </wcs:CoverageDescriptions>"""
    values = metadata_values(xml)
    assert '"water" into & out' in values, values
    assert all("<" not in v or "<air>" in v for v in values), values
    hits = find_into(values)
    trig = [t for t, _ in hits]
    # Abstract, isi description, a, c, d, e, g, CDATA -- in document order.
    # Only space-on-both-sides occurrences are TRIGGER; a tag boundary next to
    # a lone "into" (a) must not be counted as one.
    assert trig == [True, True, False, False, False, False, True, True], hits
    assert metadata_values("<wcs:CoverageDescriptions/>") is None
    assert find_into(["Evaporation at surface (also known as "
                      "evapotranspiration)."]) == []

    origin = grid_origin(xml)
    assert origin == [("time", "0"), ("Y", "5"), ("X", "0")], origin
    q = probe_query("cov_a", origin)
    assert q == ('for $c in (cov_a) return encode($c[time:"CRS:1"(0), '
                 'Y:"CRS:1"(5), X:"CRS:1"(0)], "application/json")'), q

    assert classify_probe(200, b"") == "AFFECTED"
    assert classify_probe(200, b'"1 0 0 0"') == "ok"
    assert classify_probe(400, b"<ows:ExceptionReport/>") == "error"
    print("self-test passed")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="OWS endpoint (default: %(default)s)")
    ap.add_argument("--coverage", action="append",
                    help="coverage ID to check (repeatable); default: all")
    ap.add_argument("--probe", choices=("hits", "all", "none"),
                    default="hits",
                    help="which coverages get the one-cell encode probe "
                         "(default: %(default)s)")
    ap.add_argument("--limit", type=int, help="first N coverages only")
    ap.add_argument("--out", help="write a per-coverage CSV here")
    ap.add_argument("--text", metavar="FILE",
                    help="scan a text file (or - for stdin) instead of the "
                         "server, e.g. a recipe or `ncdump -h` output")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate verification")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline checks and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.text:
        return run_text(args.text)
    return run_server(args)


if __name__ == "__main__":
    sys.exit(main())
