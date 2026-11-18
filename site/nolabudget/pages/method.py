"""/method/ : how the numbers on this site were made.

The prose is in site/templates/method.html. Every statistic on the page is computed here from
the extraction evidence and the combined data, so it cannot drift from them.
"""
import collections
import csv
import json
import os
import re

from .. import fmt
from ..data import COMBINED, CROSSWALK, RAW, read_csv
from ..evidence import Evidence, book_order, book_stage, book_year


def agreement(observations, bases):
    """For each basis: figures printed in more than one book, and how many are identical to the dollar.

    A "figure" is one department (or income line), one fiscal year and one fund group. Several
    printed lines for the same figure in one book are added together first."""
    stats = {b: [0, 0, 0] for b in bases}      # all, in more than one book, identical
    for (fy, basis, fund, ident), books in observations.items():
        if basis not in stats:
            continue
        s = stats[basis]
        s[0] += 1
        if len(books) > 1:
            s[1] += 1
            if len({round(v) for v in books.values()}) == 1:
                s[2] += 1
    return stats


def build(site):
    b = site.budget
    ev = Evidence()
    books = ev.books
    adopted = [x for x in books if book_stage(x) == "adopted"]
    proposed = [x for x in books if book_stage(x) == "proposed"]

    # where the books came from
    sources = collections.Counter()
    for x in books:
        url = b.book_urls.get(x) or ""
        if "web.archive.org" in url:
            sources["archive"] += 1
        elif "nola.gov" in url:
            sources["nola"] += 1
        elif "questica" in url or "openbook" in url or "blob.core.windows.net" in url:
            sources["openbook"] += 1
        else:
            sources["other"] += 1
    by_source = {"archive": sorted(book_year(x) for x in books if "web.archive.org" in (b.book_urls.get(x) or ""))}
    first_open = min(book_year(x) for x in books if book_year(x) >= 2025)
    adopted_years = sorted(book_year(x) for x in adopted)
    fallbacks = []
    for y in range(adopted_years[0], adopted_years[-1] + 1):
        if y not in adopted_years:
            fallbacks.append({"year": y, "later": min(a for a in adopted_years if a > y)})
    py = sorted(book_year(x) for x in proposed)
    runs = []
    for y in py:
        if runs and y == runs[-1][-1] + 1:
            runs[-1].append(y)
        else:
            runs.append([y])
    proposed_years = " and ".join(str(r[0]) if len(r) == 1 else "%d to %d" % (r[0], r[-1]) for r in runs)
    ocr_pages = len(ev.checks("2008-operating-adopted").get("ocr_only_pages", {}).get("pages", []))

    # the three checks
    printed = collections.Counter()
    cross = collections.Counter()
    for x in books:
        cc = ev.check_counts(x)
        printed.update(cc["printed"])
        cross.update(cc["cross"])
    sampled = found = 0
    for x in books:
        s = ev.checks(x).get("spot_checks_summary")
        if s:
            sampled += s.get("sampled", 0)
            found += s.get("found", 0)

    sp = agreement(ev.spending_observations(), ("actual", "adopted", "proposed"))
    rv = agreement(ev.revenue_observations(), ("actual", "adopted", "proposed"))

    def stat_rows(st):
        out = []
        for basis in ("actual", "adopted", "proposed"):
            n_all, n_multi, n_same = st[basis]
            out.append({"basis": basis, "multi": n_multi, "same": n_same, "diff": n_multi - n_same,
                        "share": fmt.pct(n_same / n_multi, 0) if n_multi else ""})
        return out

    # matching departments across years: three examples from the crosswalk
    al = ev.alias_rows("statement")
    names = collections.defaultdict(lambda: collections.defaultdict(set))
    for r in al:
        names[r["dept_id"]][r["printed_name"]].add(book_year(r["book_id"]))
    examples = []
    for dept_id in ("inspector-general", "community-development", "municipal-traffic-court"):
        rows = []
        for printed_name, years in names[dept_id].items():
            if len(years) >= 2:
                rows.append((min(years), max(years), printed_name))
        rows.sort()
        d = b.depts.get(dept_id)
        if rows and d:
            examples.append({"dept": d["name"], "url": d["url"], "names": [{"name": n, "from": a, "to": z} for a, z, n in rows]})

    corr = ev.corrections()
    problems = collections.OrderedDict()
    for c in corr:
        problems.setdefault((c["book_id"], "positions" if c["file"] == "positions" else "statements"), []).append(c)
    spend_corrected = sum(1 for r in read_csv(os.path.join(COMBINED, "spending_observations.csv")) if r["corrected"])
    rev_corrected = sum(1 for r in read_csv(os.path.join(COMBINED, "revenue_observations.csv")) if r["corrected"])

    # bodies with their own budgets: do any citywide spending tables name them?
    words = ("sewerage", "water board", "school", "transit")
    named = sorted({r["dept_name"] for x in books for r in ev.dept_rows(x) if any(w in r["dept_name"].lower() for w in words)})

    report = json.load(open(os.path.join(COMBINED, "build_report.json")))

    ctx = {
        "n_books": len(books), "n_adopted": len(adopted), "n_proposed": len(proposed),
        "fallbacks": fallbacks, "proposed_years": proposed_years, "gap_years": " or ".join(str(f["year"]) for f in fallbacks),
        "first": book_year(books[0]), "last": book_year(books[-1]), "n_archive": sources["archive"], "n_nola": sources["nola"],
        "n_openbook": sources["openbook"], "archive_years": by_source["archive"], "first_open": first_open,
        "ocr_pages": ocr_pages,
        "printed": {k: fmt.number(printed[k]) for k in ("total", "exact", "within_1", "mismatch")},
        "printed_other": printed["total"] - printed["exact"] - printed["within_1"] - printed["mismatch"],
        "cross": {k: fmt.number(cross[k]) for k in ("total", "exact", "within_1", "mismatch")},
        "cross_other": cross["total"] - cross["exact"] - cross["within_1"] - cross["mismatch"],
        "sampled": fmt.number(sampled), "found": fmt.number(found),
        "spend_stats": stat_rows(sp), "rev_stats": stat_rows(rv),
        "n_names": len({r["printed_name"] for r in al}), "n_depts": len({r["dept_id"] for r in al}),
        "n_alias_rows": len(al),
        "examples": examples, "n_corr_rows": len(corr), "n_problems": len(problems), "spend_corrected": fmt.number(spend_corrected),
        "rev_corrected": fmt.number(rev_corrected),
        "own_budget_named": named,
        "n_spending_rows": fmt.number(report["rows"]["spending_observations.csv"]),
        "n_detail_rows": fmt.number(report["rows"]["department_detail.csv"]),
        "n_position_rows": fmt.number(report["rows"]["positions.csv"]),
        "n_revenue_rows": fmt.number(report["rows"]["revenue_observations.csv"]),
    }
    site.page("/method/", "method.html", "How the numbers were made", **ctx)
