"""Department pages: /departments/ and /departments/<dept_id>/"""
from .. import charts, common, compare, detail, fmt, programs, views
from ..data import BASES


# Departments with a matching spending category in the Census comparison data.
COMPARE_KEYS = {"police": "police", "fire": "fire", "public-works": "streets", "sanitation": "trash",
                "parks-parkways": "parks", "library": "libraries", "health": "health"}


def build(site):
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    build_index(site, b, py, fy)
    for d in b.depts.values():
        build_dept(site, b, d, py, fy)


def build_index(site, b, py, fy):
    ch = {r["dept"]["id"]: r for r in common.changes(b, (py, "proposed"), (fy, "adopted"))}
    groups = []
    for g in b.groups:
        rows = []
        for d in b.depts.values():
            if d["group"] != g:
                continue
            r = ch.get(d["id"], {"new": None, "old": None, "diff": None})
            rows.append({"dept": d, "new": r["new"], "old": r["old"], "diff": r["diff"]})
        rows.sort(key=lambda r: (-(r["new"] or 0), -(r["old"] or 0), r["dept"]["name"]))
        tot_new = sum(r["new"] or 0 for r in rows)
        tot_old = sum(r["old"] or 0 for r in rows)
        groups.append({"name": g, "rows": rows, "new": tot_new, "old": tot_old, "diff": tot_new - tot_old})
    groups.sort(key=lambda g: -g["new"])
    totals = {"new": b.total(py, "proposed", "general_fund"), "old": b.total(fy, "adopted", "general_fund")}
    for g in groups:
        g["share"] = g["new"] / totals["new"] if totals["new"] else None
    group_split = common.split([(g["name"], g["new"]) for g in groups], totals["new"], "Everything else")
    cites = [b.total_source(py, "proposed", "general_fund"), b.total_source(fy, "adopted", "general_fund")]
    site.publish("/departments/", gf_new=totals["new"], gf_now=totals["old"])
    site.page("/departments/", "departments_index.html", "Departments", groups=groups, totals=totals,
              group_split=group_split,
              cites=cites, py=py, fy=fy)


def build_dept(site, b, d, py, fy):
    dept = d["id"]
    years = list(range(site.config["first_year"], py + 1))

    # The current numbers: proposal year vs the budget in force.
    current = []
    for fund in ("general_fund", "other_funds", "all_funds"):
        new = b.cell(dept, py, "proposed", fund)
        old = b.cell(dept, fy, "adopted", fund)
        current.append({
            "fund": fund, "new": new.amount if new else None, "old": old.amount if old else None,
            "new_cite": b.cite(new.book, new.page, new.derived) if new else None,
            "old_cite": b.cite(old.book, old.page, old.derived) if old else None,
        })
    has_current = any(r["new"] or r["old"] for r in current)
    gf_new = current[0]["new"]
    gf_old = current[0]["old"]

    active_years = b.dept_years(dept)
    last_year = max(active_years) if active_years else None

    # Over-time chart. Offer a fund only if the department has non-zero figures in it.
    # A chart needs at least three years with money in them; otherwise the table says it all.
    funds = [f for f in ("general_fund", "all_funds")
             if len({y for y in years for bs in BASES if abs(b.amount(dept, y, bs, f) or 0) >= 50000}) >= 3]
    fig = None
    if funds:
        fig = views.money_figure(
            b, "over-time", d["name"], years,
            lambda fund, basis, y: b.amount(dept, y, basis, fund), funds=funds, subject=d["name"])

    # Share of the General Fund.
    share_fig = None
    if "general_fund" in funds:
        s_ad = {y: _share(b, dept, y, "adopted") for y in years}
        s_ac = {y: _share(b, dept, y, "actual") for y in years}
        s_pr = {py: _share(b, dept, py, "proposed")}
        series = [
            {"key": "s1", "label": "Share of the adopted budget", "short": "Adopted", "values": s_ad},
            {"key": "s2", "label": "Share of actual spending", "short": "Actual", "values": s_ac},
            {"key": "s3", "label": "Share of the %d proposal" % py, "short": "%d proposed" % py, "kind": "point", "values": s_pr},
        ]
        series = [s for s in series if any(v is not None for v in s["values"].values())]
        if series:
            mx = max(v for s in series for v in s["values"].values() if v is not None)
            share_fig = views.simple_figure(
                b, "share", d["name"], "Share of all General Fund spending", years, series,
                tick=lambda v, mx=mx: fmt.pct(v, 0 if mx >= 0.05 else 1),
                cellf=lambda v: fmt.pct(v, 2), tipf=lambda v: fmt.pct(v, 1),
                note="Each year's General Fund amount for this department divided by the General Fund total for all "
                     "departments. Actual spending in 2012 includes %s of one-time pension bond refinancing, so most "
                     "departments' share of actual spending looks smaller that year." % fmt.money(site.note_facts["pension_2012"]))

    # Budgeted positions.
    pos_fig = None
    pos_years = [y for y in years if any(b.positions(dept, y, bs) is not None for bs in ("adopted", "proposed"))]
    pos_notes = []
    if pos_years:
        span = list(range(min(pos_years), py + 1))
        series = [
            {"key": "s1", "label": "Adopted positions", "short": "Adopted", "values": {y: b.positions(dept, y, "adopted") for y in span}},
            {"key": "s3", "label": "%d proposal" % py, "short": "%d proposed" % py, "kind": "point",
             "values": {py: b.positions(dept, py, "proposed")}},
        ]
        series = [s for s in series if any(v is not None for v in s["values"].values())]
        pos_fig = views.simple_figure(
            b, "positions", d["name"], "Budgeted positions (full-time equivalents)", span, series,
            tick=lambda v: fmt.number(v), cellf=lambda v: fmt.number(v, 2 if v != int(v) else 0),
            tipf=lambda v: fmt.number(v, 1 if v != int(v) else 0) + " positions")
        pos_notes.append("Position counts are the least reliable figures on this site: books count them differently from year to year, and a budgeted position may be empty.")
        if 2018 in span:
            pos_notes.append("The 2018 book printed the same position counts in every column, so its figures for 2017 and 2018 may not show the change between those years.")
        if d["group"] == "Enterprise bodies":
            pos_notes.append("Position counts for enterprise bodies such as this one are unreliable: some books count their staff and some do not.")

    # What the money is spent on. Each table also gets a split bar of its largest rows.
    bd = detail.breakdowns(b, dept)
    if bd:
        for t in bd["tables"]:
            sums = {}
            for p in t["parts"]:
                for k, v in p["items"]:
                    sums[k] = sums.get(k, 0) + v
            t["split"] = common.split(list(sums.items()), t["total"], "Everything else")

    # Inside the department: programs in plain groups, each with a small trend line.
    inside = programs.inside(b, dept, years, py, fy)
    if inside:
        span = list(range(inside["years"][0], py + 1))
        for g in inside["groups"]:
            ser = {y: v for y, v in g["series"].items()}
            g["spark"] = charts.sparkline(ser, span)
            first = next((y for y in span if ser.get(y)), None)
            g["first"] = (first, ser[first]) if first else None
        inside["split"] = common.split([(g["name"], g["amount"]) for g in inside["groups"]], inside["total"],
                                       "Everything else", n=5)

    # Who works there: budgeted jobs by rank (departments listed in content/position_ranks.yml).
    rank_rules = site.content.get("position_ranks", {}).get(dept)
    staff = None
    if rank_rules:
        rk = programs.ranks(b, dept, rank_rules, years, py)
        if py in rk:
            now = rk[py]
            slots = ["c1", "c2", "c3", "c4", "c5"]
            parts, i = [], 0
            for name, fte, sworn in now["ranks"]:
                if fte <= 0:
                    continue
                other = name == rank_rules["other"]
                parts.append({"name": name, "amount": fte, "slot": "c-other" if other else slots[min(i, 4)],
                              "share": fte / now["total"], "per100": 0, "count": fte})
                if not other:
                    i += 1
            # A year counts toward the sworn trend only if nearly every job has a title.
            clean = {y: v for y, v in rk.items() if v["untitled"] <= 0.02 * v["total"]}
            pop = b.population(py)
            series = [
                {"key": "s1", "label": "Budgeted sworn positions (adopted)", "short": "Adopted",
                 "values": {y: v["sworn"] for y, v in clean.items() if y != py}},
                {"key": "s3", "label": "%d proposal" % py, "short": "%d proposed" % py, "kind": "point",
                 "values": {py: now["sworn"]}},
            ]
            span = list(range(min(rk), py + 1))
            staff = {
                "year": py, "total": now["total"], "sworn": now["sworn"], "parts": parts, "cite": now["cite"],
                "per1000": now["sworn"] / pop * 1000 if pop else None,
                "skipped": sorted(y for y in rk if y not in clean),
                "fig": views.simple_figure(
                    b, "sworn", d["name"], "Budgeted sworn positions", span, series,
                    tick=lambda v: fmt.number(v), cellf=lambda v: fmt.number(v),
                    tipf=lambda v: fmt.number(v) + " positions"),
            }

    # How New Orleans compares (Census figures), for departments that match a Census function.
    cmp = None
    key = COMPARE_KEYS.get(dept)
    if key:
        rows = compare.load()
        peer, la = compare.summary(rows, key, "peer"), compare.summary(rows, key, "louisiana")
        br = next((r for r in la["cities"] if r["name"] == "Baton Rouge"), None)
        cmp = {"key": key, "year": compare.YEAR, "nola": peer["nola"][key + "_per"], "median": peer["median"],
               "n": peer["n"] - 1, "higher_than": peer["higher_than"],
               "baton_rouge": br[key + "_per"] if br else None, "source": compare.SOURCE,
               "fn": compare.BY_KEY[key], "pct_diff": peer["pct_diff"], "ratio": peer["ratio"]}

    # Pictures for the top of the page: this year against last, and the share of the whole.
    gf_total = b.total(py, "proposed", "general_fund")
    gf_share = gf_new / gf_total if gf_new and gf_new > 0 and gf_total else None
    other_new = current[1]["new"]
    fund_split = []
    if gf_new and other_new and gf_new > 0 and other_new > 0:
        fund_split = [
            {"name": "General Fund", "amount": gf_new, "slot": "c1"},
            {"name": "Other funds", "amount": other_new, "slot": "c-other"},
        ]
        tot = gf_new + other_new
        for part, h in zip(fund_split, fmt.per_hundred([gf_new, other_new])):
            part["share"] = part["amount"] / tot
            part["per100"] = h

    # Year-by-year table with sources.
    tables, numbered = [], []
    auto_notes = []
    for fund in ("general_fund", "all_funds"):
        rows = []
        for y in years:
            cells, cites, bases_present = [], [], []
            for basis in BASES:
                c = b.cell(dept, y, basis, fund)
                cells.append(c.amount if c else None)
                if c:
                    bases_present.append(basis)
                    cites.append((basis, b.cite(c.book, c.page, c.derived)))
            if not bases_present:
                continue
            notes = site.notes_for([y], [fund], bases_present, dept)
            if fund == "all_funds":
                for basis in bases_present:
                    an = _parts_note(b, dept, y, basis)
                    if an:
                        notes.append(an)
                        if an["id"] not in [n["id"] for n in auto_notes]:
                            auto_notes.append(an)
            rows.append({"year": y, "cells": cells, "cites": cites, "notes": notes})
        if rows and any(any(c for c in r["cells"] if c) for r in rows):
            tables.append({"fund": fund, "rows": rows})
    for t in tables:  # number notes in display order (tables show newest year first)
        for r in reversed(t["rows"]):
            for n in r["notes"]:
                if n["id"] not in numbered:
                    numbered.append(n["id"])
    all_notes = {n["id"]: n for n in site.notes}
    all_notes.update({n["id"]: n for n in auto_notes})
    page_notes = [all_notes[i] for i in numbered]

    # Bodies with a section in the books but no (or only zero) citywide line.
    sections = []
    recent_statement = any(b.amount(dept, y, bs, f) for y in (py, fy, fy - 1) for bs in BASES for f in ("general_fund", "all_funds"))
    if not recent_statement:
        sections = detail.section_totals(b, dept)

    see = [b.depts[s] for s in d.get("see", []) if s in b.depts]
    nordc = None
    if dept == "recreation":
        mis = detail.breakdowns(b, "miscellaneous")
        if mis:
            for t in mis["tables"]:
                if t["kind"] == "type":
                    for p in t["parts"]:
                        if p["sub"] == "nordc":
                            nordc = {"amount": p["total"], "year": mis["year"], "basis": mis["basis"], "cite": p["cite"],
                                     "name": p["name"]}

    site.publish(d["url"], gf_new=gf_new, gf_now=gf_old,
                 all_new=current[2]["new"], all_now=current[2]["old"])
    site.page(d["url"], "department.html", d["name"], d=d, current=current, has_current=has_current,
              fig=fig, share_fig=share_fig, pos_fig=pos_fig, pos_notes=pos_notes, bd=bd, tables=tables,
              page_notes=page_notes, numbered=numbered, sections=sections, see=see, last_year=last_year,
              nordc=nordc, py=py, fy=fy, gf_new=gf_new, gf_old=gf_old, gf_share=gf_share, fund_split=fund_split,
              inside=inside, staff=staff, cmp=cmp,
              change=fmt.change(gf_new, gf_old) if gf_new is not None and gf_old is not None else "")


def _share(b, dept, y, basis):
    a = b.amount(dept, y, basis, "general_fund")
    t = b.total(y, basis, "general_fund")
    if a is None or not t:
        return None
    return a / t


def _parts_note(b, dept, y, basis):
    """An automatic note when a department's all-funds figure is not its General Fund plus
    other funds in the same column (and no site-wide note already explains it)."""
    a = b.cell(dept, y, basis, "all_funds")
    g = b.cell(dept, y, basis, "general_fund")
    o = b.cell(dept, y, basis, "other_funds")
    if not a or not g or not o or a.derived:
        return None
    gap = a.amount - g.amount - o.amount
    if abs(gap) < 1000:
        return None
    from markupsafe import Markup
    book = b.book(a.book).label
    return {
        "id": "parts-%d-%s" % (y, basis),
        "html": Markup(
            "In the %s, this department's %s %d all-funds figure (%s) is %s %s than its General Fund "
            "and other-funds figures added together (%s). The all-funds figure is shown as printed." % (
                book, basis, y, fmt.money(a.amount), fmt.money(abs(gap)), "more" if gap > 0 else "less",
                fmt.money(g.amount + o.amount))),
    }
