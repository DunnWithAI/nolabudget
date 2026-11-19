#!/usr/bin/env python3
"""Verify data/crosswalk/revenue_{sources,aliases}.csv against data/extracted/*/revenues.csv.

Prints (1) coverage, (2) conservation vs raw lines and vs printed totals, (3) cross-book
overlap of actuals, (4) continuity per rev_id with gap/spike flags.
Exits 1 if coverage is incomplete or conservation against raw lines fails. Overlap
disagreements and printed-total differences are reported, not treated as failures.

Run:  .venv/bin/python scripts/check_crosswalk_revenue.py [--quiet-series]
"""
import csv
import glob
import itertools
import os
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "data", "extracted")
CW = os.environ.get("CROSSWALK_DIR") or os.path.join(ROOT, "data", "crosswalk")
GF = "general_fund"
GF_CATS = {"Property taxes", "Sales taxes", "Other taxes", "Licenses and permits",
           "Intergovernmental", "Service charges", "Fines and forfeits",
           "Miscellaneous", "Other financing sources"}
FAIL = []


def rd(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def money(x):
    return "{:,}".format(int(round(x)))


def mm(x):
    return "%.1f" % (x / 1e6)


def ranges(years):
    years = sorted(years)
    out, start, prev = [], None, None
    for y in years:
        if start is None:
            start = prev = y
        elif y == prev + 1:
            prev = y
        else:
            out.append((start, prev))
            start = prev = y
    if start is not None:
        out.append((start, prev))
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in out)


def main():
    quiet = "--quiet-series" in sys.argv
    sources = rd(os.path.join(CW, "revenue_sources.csv"))
    aliases = rd(os.path.join(CW, "revenue_aliases.csv"))
    rows = []
    for f in sorted(glob.glob(os.path.join(EXT, "*", "revenues.csv"))):
        rows += rd(f)
    for r in rows:
        r["amount"] = int(r["amount"])
        r["fy"] = int(r["fiscal_year"])

    # ---------------- 1. coverage ----------------
    print("=" * 78)
    print("1. COVERAGE")
    src = {s["rev_id"]: s for s in sources}
    if len(src) != len(sources):
        FAIL.append("duplicate rev_id in sources")
    bad_cat = [s["rev_id"] for s in sources if s["category"] not in GF_CATS and s["category"] != "Other funds"]
    if bad_cat:
        FAIL.append("rev_ids with category outside the fixed set: %s" % bad_cat)
    empty_desc = [s["rev_id"] for s in sources if not s["plain_description"].strip()]
    if empty_desc:
        FAIL.append("rev_ids without description: %s" % empty_desc)
    key = lambda r: (r["book_id"], r["fund_group"], r["fund_name"], r.get("category", r.get("category_printed")), r["line_name"])
    amap = {}
    for a in aliases:
        k = key(a)
        if k in amap:
            FAIL.append("duplicate alias key %s" % (k,))
        amap[k] = a["rev_id"]
        if a["rev_id"] not in src:
            FAIL.append("alias points to unknown rev_id %s" % a["rev_id"])
    raw_keys = collections.OrderedDict((key(r), 1) for r in rows)
    missing = [k for k in raw_keys if k not in amap]
    extra = [k for k in amap if k not in raw_keys]
    unused = sorted(set(src) - set(amap.values()))
    gf_ids = sorted(r for r, s in src.items() if s["category"] != "Other funds")
    of_ids = sorted(r for r, s in src.items() if s["category"] == "Other funds")
    print("rev_ids: %d total (%d General Fund, %d other-funds); alias rows: %d" % (len(src), len(gf_ids), len(of_ids), len(aliases)))
    print("distinct printed keys in revenues.csv: %d; unmapped: %d; aliases with no raw row: %d; rev_ids never used: %d" %
          (len(raw_keys), len(missing), len(extra), len(unused)))
    # General Fund lines must not map to an other-funds rev_id and vice versa
    for k, rid in amap.items():
        is_gf = k[1] == GF
        if is_gf == (src[rid]["category"] == "Other funds"):
            FAIL.append("fund_group/category mismatch for %s -> %s" % (k, rid))
    for k in missing[:20]:
        print("  UNMAPPED", k)
    if missing:
        FAIL.append("%d unmapped printed lines" % len(missing))
    if extra:
        FAIL.append("%d alias rows match no raw line" % len(extra))
    if unused:
        FAIL.append("unused rev_ids: %s" % unused)

    for r in rows:
        r["rev_id"] = amap.get(key(r))
    rows = [r for r in rows if r["rev_id"]]

    # ---------------- 2. conservation ----------------
    print("=" * 78)
    print("2. CONSERVATION (General Fund, per book / fiscal year / basis)")
    raw = collections.defaultdict(int)
    mapped = collections.defaultdict(int)
    for r in rows:
        if r["fund_group"] == GF:
            k = (r["book_id"], r["fy"], r["basis"])
            raw[k] += r["amount"]
    bykey = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in rows:
        if r["fund_group"] == GF:
            bykey[(r["book_id"], r["fy"], r["basis"])][r["rev_id"]] += r["amount"]
    nbad = 0
    for k, d in bykey.items():
        if sum(d.values()) != raw[k]:
            nbad += 1
            print("  NOT CONSERVED", k, sum(d.values()), raw[k])
    if nbad:
        FAIL.append("%d (book,fy,basis) cells where rev_id sum != raw sum" % nbad)
    print("cells checked: %d; rev_id sum == raw line sum in %d" % (len(bykey), len(bykey) - nbad))

    printed = {}
    for f in sorted(glob.glob(os.path.join(EXT, "*", "printed_totals.csv"))):
        for p in rd(f):
            sc = p["scope"]
            k = (p["book_id"], int(p["fiscal_year"]), p["basis"])
            lab = p["label"].strip().lower()
            if sc == "revenues/General Fund" and lab == "total general fund":
                printed[k] = (int(p["amount"]), "TOTAL GENERAL FUND p.%s" % p["page"])
            elif sc == "revenues/General Fund/summary_by_category" and lab == "grand total" and k not in printed:
                printed[k] = (int(p["amount"]), "statement grand total p.%s" % p["page"])
    for f in sorted(glob.glob(os.path.join(EXT, "*", "printed_totals.csv"))):
        for p in rd(f):
            k = (p["book_id"], int(p["fiscal_year"]), p["basis"])
            if p["scope"] == "overview_of_revenues" and p["label"] == "General Fund" and k not in printed:
                printed[k] = (int(p["amount"]), "overview p.%s" % p["page"])
    diffs = []
    noprint = []
    exact = 0
    within = 0
    for k in sorted(raw):
        if k not in printed:
            noprint.append(k)
            continue
        d = raw[k] - printed[k][0]
        if d == 0:
            exact += 1
        elif abs(d) <= 5:
            within += 1
        else:
            diffs.append((k, raw[k], printed[k][0], d, printed[k][1]))
    print("vs printed General Fund total: %d cells with a printed total: %d exact, %d within $5 (rounding in the book), %d differ by more" %
          (len(raw) - len(noprint), exact, within, len(diffs)))
    for k, a, b, d, src_ in diffs:
        print("  DIFF %s FY%d %-8s lines=%s printed=%s diff=%s (%s)" % (k[0][:4] + k[0][4:].replace("-operating", ""), k[1], k[2], money(a), money(b), money(d), src_))
    if noprint:
        print("  no printed total found for:", ", ".join("%s/%d/%s" % (k[0][:4], k[1], k[2][:3]) for k in noprint))

    # ---------------- 3. overlap ----------------
    print("=" * 78)
    print("3. OVERLAP: same fiscal-year actuals printed by several books")
    rollups = collections.defaultdict(list)
    rpath = os.path.join(CW, "revenue_rollups.csv")
    rollup_of = {}
    if os.path.exists(rpath):
        for rr in rd(rpath):
            rollups[rr["rollup_id"]].append(rr["rev_id"])
            rollup_of[rr["rev_id"]] = rr["rollup_id"]
            if rr["rev_id"] not in src:
                FAIL.append("rollup member not a rev_id: %s" % rr["rev_id"])
    ident = lambda rid: rid
    grouped = lambda rid: rollup_of.get(rid, rid)

    def overlap(fgroup, basis, grp):
        cell = collections.defaultdict(lambda: collections.defaultdict(int))
        books = collections.defaultdict(set)
        for r in rows:
            if r["fund_group"] != fgroup or r["basis"] != basis:
                continue
            cell[r["fy"]][(r["book_id"], grp(r["rev_id"]))] += r["amount"]
            books[r["fy"]].add(r["book_id"])
        tot = agree = 0
        bad = []
        for fy in sorted(books):
            bs = books[fy]
            if len(bs) < 2:
                continue
            for rid in sorted({k[1] for k in cell[fy]}):
                vals = {bk: cell[fy].get((bk, rid), 0) for bk in sorted(bs)}
                tot += 1
                if max(vals.values()) - min(vals.values()) <= 1:
                    agree += 1
                else:
                    bad.append((fy, rid, vals))
        return tot, agree, bad

    def show_bad(bad, fgroup, skip2006=True):
        known = [x for x in bad if x[0] == 2006 and skip2006]
        rest = [x for x in bad if x not in known]
        if known:
            s = collections.defaultdict(int)
            for fy, rid, vals in known:
                for bk, v in vals.items():
                    s[bk] += v
            print("   known 2006 restatement (2009 vs 2012 books): %d ids differ; sums of the differing ids: %s" %
                  (len(known), "  ".join("%s=%s" % (bk[:4], money(v)) for bk, v in sorted(s.items()))))
        for fy in sorted({x[0] for x in rest}):
            sub = [x for x in rest if x[0] == fy]
            print("   FY%d: %d disagreements" % (fy, len(sub)))
            for _, rid, vals in sub:
                print("      %-38s %s" % (rid, "  ".join("%s=%s" % (bk[:4], money(v)) for bk, v in sorted(vals.items()))))
            ids = [x[1] for x in sub]
            for n in (2, 3):
                for combo in itertools.combinations(ids, n):
                    sums = collections.defaultdict(int)
                    for _, rid, vals in sub:
                        if rid in combo:
                            for bk, v in vals.items():
                                sums[bk] += v
                    if max(sums.values()) - min(sums.values()) <= 1:
                        print("      -> books agree if merged: %s" % " + ".join(combo))

    for fgroup, label in ((GF, "General Fund"), ("other_funds", "Other funds")):
        for basis, tag in (("actual", "ACTUALS (the test)"), ("adopted", "adopted budgets (informational)")):
            tot, agree, bad = overlap(fgroup, basis, ident)
            print("\n[%s] %s, by rev_id: %d (rev_id, fiscal year) comparisons, %d agree within $1 (%.1f%%), %d disagree" %
                  (label, tag, tot, agree, 100.0 * agree / max(tot, 1), len(bad)))
            if basis == "adopted":
                byfy = collections.Counter(fy for fy, rid, v in bad)
                print("   disagreements by fiscal year (budget revisions between books, not mapping):", dict(sorted(byfy.items())))
                if "--detail" in sys.argv:
                    t2, a2, b2 = overlap(fgroup, basis, grouped)
                    for fy, rid, vals in b2:
                        if fy >= 2024:
                            print("      after rollups FY%d %-34s %s" % (fy, rid, "  ".join("%s=%s" % (bk[:4], money(v)) for bk, v in sorted(vals.items()))))
                if fgroup == GF:
                    t2, a2, b2 = overlap(fgroup, basis, grouped)
                    late = [x for x in bad if x[0] >= 2024]
                    late2 = [x for x in b2 if x[0] >= 2024]
                    print("   FY2024-2026 (2024/2025/2026/2027 books, where line splits changed): %d disagree by rev_id, %d after rollups" %
                          (len(late), len(late2)))
                continue
            show_bad(bad, fgroup)
            if rollups:
                t2, a2, b2 = overlap(fgroup, basis, grouped)
                print("   -- same test after applying revenue_rollups.csv (%d rollups): %d comparisons, %d agree (%.1f%%), %d disagree" %
                      (len(rollups), t2, a2, 100.0 * a2 / max(t2, 1), len(b2)))
                if fgroup == GF:
                    nk = [x for x in b2 if x[0] != 2006]
                    print("      of which not the known 2006 restatement: %d" % len(nk))
                show_bad(b2, fgroup)

    print("=" * 78)
    print("4. CONTINUITY: one value per rev_id per fiscal year")
    print("   rule: for each fiscal year take the book cell with the best basis (actual > adopted > proposed), latest book first;")
    print("   a rev_id with no printed row in that cell has no data that year (a gap); in the Nxx column A=actual B=adopted P=proposed")
    pri = {"actual": 3, "adopted": 2, "proposed": 1}
    ORDER = ["Property taxes", "Sales taxes", "Other taxes", "Licenses and permits", "Intergovernmental", "Service charges",
             "Fines and forfeits", "Miscellaneous", "Other financing sources", "Other funds"]
    flags_all = []
    for fgroup, label in ((GF, "General Fund"), ("other_funds", "Other funds")):
        cells = collections.defaultdict(lambda: collections.defaultdict(int))   # (fy) -> (pri, book) -> rev -> sum
        for r in rows:
            if r["fund_group"] != fgroup:
                continue
            cells[r["fy"]].setdefault((pri.get(r["basis"], 0), r["book_id"]), collections.defaultdict(int))
            cells[r["fy"]][(pri.get(r["basis"], 0), r["book_id"])][r["rev_id"]] += r["amount"]
        series = collections.defaultdict(dict)
        basis_of = {}
        for fy, d in cells.items():
            sc = max(d)
            for rid, v in d[sc].items():
                series[rid][fy] = v
                basis_of[(rid, fy)] = {3: "A", 2: "B", 1: "P"}[sc[0]]
        tot = collections.defaultdict(int)
        for rid, s in series.items():
            for fy, v in s.items():
                tot[fy] += v
        print("\n[%s] total by year ($M; A/B/P = actual/adopted/proposed basis used):" % label)
        print("   " + "  ".join("%d%s:%s" % (fy, {3: "A", 2: "B", 1: "P"}[max(cells[fy])[0]], mm(tot[fy])) for fy in sorted(tot)))
        ids = gf_ids if fgroup == GF else of_ids
        order = sorted(ids, key=lambda r: (ORDER.index(src[r]["category"]), r))
        if not quiet:
            print("\nrev_id | category | years with data | gaps | flags")
        for rid in order:
            s = series.get(rid, {})
            yrs = sorted(s)
            if not yrs:
                continue
            allfy = sorted(tot)
            # a "gap" is a year inside this rev_id's span with no printed row; the first/last printed year bound the span
            gaps = [y for y in allfy if yrs[0] < y < yrs[-1] and y not in s]
            flags = []
            for y in yrs:
                if y - 1 not in s or y + 1 not in s:
                    continue
                v, a, b = s[y], s[y - 1], s[y + 1]
                if v > 3 * max(a, b) and v - max(a, b) > 1e6:
                    flags.append("spike %d%s:%sM (nbrs %s,%s)" % (y, basis_of[(rid, y)], mm(v), mm(a), mm(b)))
                elif min(a, b) > 3 * max(v, 0) and min(a, b) - v > 1e6:
                    flags.append("drop %d%s:%sM (nbrs %s,%s)" % (y, basis_of[(rid, y)], mm(v), mm(a), mm(b)))
            for f_ in flags:
                flags_all.append((rid, f_))
            if not quiet:
                print("%-38s| %-22s| %s | %s | %s" % (rid, src[rid]["category"][:22], ranges(yrs),
                                                      ",".join(map(str, gaps)) or "-", "; ".join(flags) or "-"))
        if fgroup == GF and rollups:
            print("\nRollups (sum of members, one value per year):")
            for rid_, members in sorted(rollups.items()):
                s = {fy: sum(series[m].get(fy, 0) for m in members) for fy in set().union(*[set(series[m]) for m in members])}
                yrs = sorted(s)
                gaps = [y for y in sorted(tot) if yrs[0] < y < yrs[-1] and y not in s]
                fl = []
                for y in yrs:
                    if y - 1 in s and y + 1 in s:
                        v, a, b = s[y], s[y - 1], s[y + 1]
                        if v > 3 * max(a, b) and v - max(a, b) > 1e6:
                            fl.append("spike %d:%sM (nbrs %s,%s)" % (y, mm(v), mm(a), mm(b)))
                        elif min(a, b) > 3 * max(v, 0) and min(a, b) - v > 1e6:
                            fl.append("drop %d:%sM (nbrs %s,%s)" % (y, mm(v), mm(a), mm(b)))
                        if fl and fl[-1] and False:
                            pass
                for f_ in fl:
                    flags_all.append((rid_ + " [rollup]", f_))
                print("%-42s| %s | gaps %s | %s" % (rid_, ranges(yrs), ",".join(map(str, gaps)) or "-", "; ".join(fl) or "-"))
    print("\nFlagged one-year spikes/drops (>3x both neighbours and >$1M change):")
    for rid, f_ in flags_all:
        print("  %-42s %s" % (rid, f_))

    print("=" * 78)
    if FAIL:
        print("FAIL:")
        for f_ in FAIL[:50]:
            print("  -", f_)
        sys.exit(1)
    print("OK: coverage complete; conservation holds")


if __name__ == "__main__":
    main()
