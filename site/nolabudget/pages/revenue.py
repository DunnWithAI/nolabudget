"""Revenue pages.

    /revenue/                     where the General Fund's money comes from; other funds
    /revenue/<category slug>/     one of the nine categories (slugs in content/revenue_categories.yml)
    /revenue/<rev_id>/            one General Fund line (rev_id from data/crosswalk/revenue_sources.csv)

Source pages sit directly under /revenue/ (not under their category) so a line's URL does
not change if the books ever file it under another category. Checks make sure no rev_id
equals a category slug.

Over-time charts of a single line follow its `comparable_id` (see revenue_rollups.csv):
where books printed a line inside another line, the chart adds the lines together so every
year means the same thing. One-off events come from content/revenue_events.yml.
"""
import re

from markupsafe import Markup, escape

from .. import common, fmt, views
from ..data import BASES

# Years for which the books' actual figures for other funds are incomplete.
OTHER_ACTUALS_INCOMPLETE = set(range(2010, 2015)) | set(range(2017, 2023))


def _spec(py):
    return [("s1", "adopted", "Adopted budget", "Adopted", "line", None),
            ("s2", "actual", "Actual income", "Actual", "line", None),
            ("s3", "proposed", "%d proposal" % py, "%d proposed" % py, "point", [py])]


def plain(b, rev_id):
    """The crosswalk's description, with analyst notes taken out and line ids swapped for names."""
    d = b.rev_lines[rev_id]["plain_description"]
    d = re.sub(r"\s*CAUTION:.*$", "", d)
    d = re.sub(r"'([a-z0-9]+(?:-[a-z0-9]+)+)'", lambda m: "\u201c%s\u201d" % (
        b.rev_lines[m.group(1)]["name"] if m.group(1) in b.rev_lines else m.group(1)), d)
    d = d.replace("so they are one rev_id", "so this site treats them as one line")
    return d.strip()


def build(site):
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    cats = site.content["revenue_categories"]
    ctx = {"events": _events(site), "py": py, "fy": fy}
    build_index(site, b, cats, ctx)
    for c in cats:
        build_category(site, b, c, cats, ctx)
    for rev_id in b.rev_ids():
        build_source(site, b, rev_id, cats, ctx)


# ---------------------------------------------------------------------------- events
def _events(site):
    """Events from content/revenue_events.yml with their citations resolved."""
    b = site.budget
    out = []
    for e in site.content["revenue_events"]["events"]:
        e = dict(e)
        cites = []
        for c in e.get("cites") or []:
            cell = b.rev_cell(c["rev_id"], c["year"], c["basis"])
            if cell is None:
                raise ValueError("revenue_events.yml: %s cites a figure that is not in revenue.csv: %r" % (e["id"], c))
            cites.append(b.cite(cell.book, cell.page, cell.derived))
        e["cite_list"] = cites
        e["rev_ids"] = e.get("rev_ids") or []
        e["chart"] = e.get("chart", True)
        out.append(e)
    return out


def _note(e, cite_fn):
    """A footnote dict {id, html} for an event."""
    html = "<strong>%d.</strong> %s" % (e["year"], escape(" ".join(e["text"].split())))
    if e.get("source_note"):
        html += " " + str(escape(e["source_note"]))
    if e["cite_list"]:
        html += " <span class=\"note-src\">Source: %s.</span>" % "; ".join(str(cite_fn(c)) for c in e["cite_list"])
    return {"id": "event-" + e["id"], "year": e["year"], "html": Markup(html)}


def _cite_html(c):
    if not c:
        return Markup("")
    t = escape(c["text"])
    if c.get("href"):
        return Markup('<a class="cite" href="%s">%s</a>' % (escape(c["href"]), t))
    return Markup('<span class="cite">%s</span>' % t)


def _event_notes(events, applies):
    return [_note(e, _cite_html) for e in sorted(events, key=lambda e: e["year"]) if applies(e)]


# ---------------------------------------------------------------------------- index
def build_index(site, b, cats, ctx):
    py, fy = ctx["py"], ctx["fy"]
    years = list(range(site.config["first_year"], py + 1))
    new = {c["category"]: b.rev_category_amount(c["category"], py, "proposed") for c in cats}
    old = {c["category"]: b.rev_category_amount(c["category"], fy, "adopted") for c in cats}
    tot_new = b.rev_total(py, "proposed")
    tot_old = b.rev_total(fy, "adopted")
    rows = []
    for c in sorted(cats, key=lambda c: -(new[c["category"]] or 0)):
        n, o = new[c["category"]], old[c["category"]]
        rows.append({
            "cat": c, "url": "/revenue/%s/" % c["slug"], "new": n, "old": o,
            "diff": (n or 0) - (o or 0) if (n is not None or o is not None) else None,
            "pct": ((n - o) / o) if n is not None and o else None,
            "share": n / tot_new if n is not None and tot_new else None})
    for r, label in zip(rows, fmt.whole_shares([r["new"] or 0 for r in rows])):
        r["share_label"] = label
    cites = [x for x in (b.rev_cite(b.revenue_rows(fy, "adopted")), b.rev_cite(b.revenue_rows(py, "proposed"))) if x]

    # One small chart per category.
    figs = []
    for c in cats:
        cat = c["category"]
        fig = views.money_figure(
            b, "cat-" + c["slug"], cat, years,
            lambda fund, basis, y, cat=cat: b.rev_category_amount(cat, y, basis, "general_fund"),
            funds=("general_fund",), series_spec=_spec(py), subject=cat)
        figs.append({"cat": c, "fig": fig, "url": "/revenue/%s/" % c["slug"]})

    ev_notes = _event_notes([e for e in ctx["events"] if e["chart"]], lambda e: True)
    gap_ev = [e for e in ctx["events"] if e["id"] == "printed-total-gap"]
    gap_note = gap_ev[0]["text"] if gap_ev else ""
    gap_cites = [b.cite(*_book_page(b, fy, "adopted")), b.cite(*_book_page(b, py, "proposed"))]

    other = _other_funds(site, b, ctx, years)

    site.publish("/revenue/", gf_new=tot_new, gf_now=tot_old, of_new=other["tot_new"], of_now=other["tot_old"],
                 **dict([("new:" + c["slug"], new[c["category"]]) for c in cats] +
                        [("now:" + c["slug"], old[c["category"]]) for c in cats]))
    top = rows[0]
    second = rows[1]
    cat_split = common.split([(r["cat"]["category"], r["new"]) for r in rows if r["new"]], tot_new)
    other["split"] = common.split([(L["name"], L["new"]) for L in other["lines"] if L["new"]], other["tot_new"])
    site.page("/revenue/", "revenue_index.html", "Where the money comes from", rows=rows, totals={"new": tot_new, "old": tot_old},
              cat_split=cat_split,
              cites=cites, figs=figs, ev_notes=ev_notes, gap_note=gap_note, gap_cites=gap_cites, other=other,
              top=top, second=second, py=py, fy=fy)


def _book_page(b, fy, basis):
    r = b.revenue_rows(fy, basis)[0]
    return r["source_book"], r["source_page"]


def _other_funds(site, b, ctx, years):
    py, fy = ctx["py"], ctx["fy"]
    lines = {}
    for basis, y, key in (("proposed", py, "new"), ("adopted", fy, "old")):
        for r in b.revenue_rows(y, basis, "other_funds"):
            L = lines.setdefault(r["rev_id"], {"name": b.rev_lines[r["rev_id"]]["name"], "new": None, "old": None,
                                               "text": plain(b, r["rev_id"])})
            L[key] = (L[key] or 0) + r["amount"]
    ls = sorted(lines.values(), key=lambda L: -((L["new"] or 0) + (L["old"] or 0) / 1e3))
    tot_new = b.rev_total(py, "proposed", "other_funds")
    tot_old = b.rev_total(fy, "adopted", "other_funds")

    def getter(fund, basis, y):
        if basis == "actual" and y in OTHER_ACTUALS_INCOMPLETE:
            return None
        return b.rev_total(y, basis, "other_funds")

    spec = [("s1", "adopted", "Budgeted (adopted)", "Adopted", "line", None),
            ("s2", "actual", "Actual receipts (years the books are complete)", "Actual", "line", None),
            ("s3", "proposed", "%d proposal" % py, "%d proposed" % py, "point", [py])]
    fig = views.money_figure(b, "other-funds", "Other funds income", years, getter, funds=("other_funds",),
                             series_spec=spec, subject="Other funds income")
    note_inc = {"id": "other-incomplete", "html": Markup(
        "In the books, actual receipts for other funds are incomplete for 2010 to 2014 and 2017 to 2022. "
        "The figure printed for those years is lower than what the city received, so the chart leaves it out.")}
    table, numbered = [], []
    for y in reversed(years):
        cells, cites, present = [], [], []
        for basis in BASES:
            rows = b.revenue_rows(y, basis, "other_funds")
            cells.append(sum(r["amount"] for r in rows) if rows else None)
            if rows:
                present.append(basis)
                cites.append((basis, b.rev_cite(rows)))
        if not present:
            continue
        notes = [note_inc] if ("actual" in present and y in OTHER_ACTUALS_INCOMPLETE) else []
        if notes and note_inc["id"] not in numbered:
            numbered.append(note_inc["id"])
        table.append({"year": y, "cells": cells, "cites": cites, "notes": notes})
    cites = [x for x in (b.rev_cite(b.revenue_rows(fy, "adopted", "other_funds")),
                         b.rev_cite(b.revenue_rows(py, "proposed", "other_funds"))) if x]
    return {"lines": ls, "tot_new": tot_new, "tot_old": tot_old, "fig": fig, "table": table, "numbered": numbered,
            "notes": [note_inc], "cites": cites,
            "explain_year": fy}


# ---------------------------------------------------------------------------- category
def build_category(site, b, c, cats, ctx):
    py, fy = ctx["py"], ctx["fy"]
    cat = c["category"]
    years = list(range(site.config["first_year"], py + 1))
    ids = [i for i in b.rev_ids() if b.rev_lines[i]["category"] == cat]
    tot_new = b.rev_category_amount(cat, py, "proposed")
    tot_old = b.rev_category_amount(cat, fy, "adopted")
    gf_new = b.rev_total(py, "proposed")

    lines = []
    for i in ids:
        n, o = b.rev_amount(i, py, "proposed"), b.rev_amount(i, fy, "adopted")
        lines.append({"id": i, "name": b.rev_lines[i]["name"], "text": plain(b, i),
                      "url": "/revenue/%s/" % i, "new": n, "old": o,
                      "diff": (n or 0) - (o or 0) if (n is not None or o is not None) else None,
                      "last": _last_year(b, i)})
    now = [L for L in lines if L["new"] or L["old"]]
    gone = [L for L in lines if not (L["new"] or L["old"])]
    now.sort(key=lambda L: -((L["new"] or 0) + (L["old"] or 0) / 1e3))
    gone.sort(key=lambda L: -(L["last"] or 0))

    fig = views.money_figure(
        b, "category", cat, years, lambda fund, basis, y: b.rev_category_amount(cat, y, basis, "general_fund"),
        funds=("general_fund",), series_spec=_spec(py), subject=cat)

    idset = set(ids)

    def applies(e):
        if e.get("index_only"):
            return False
        if e.get("all_lines"):
            return b.rev_category_amount(cat, e["year"], e["basis"]) is not None
        return bool(idset & set(e["rev_ids"]))
    events = [e for e in ctx["events"] if applies(e)]
    notes_all = {n["id"]: n for n in _event_notes(events, lambda e: True)}

    # Category rollup notes: lines of this category that are chart-combined with lines elsewhere.
    cat_notes = []
    if cat in ("Sales taxes", "Other taxes"):
        cat_notes.append(
            "Before 2008 the books printed the hotel tax (an Other taxes line) and the motor-vehicle sales tax inside "
            "the sales tax line, and some books did the same for 2008 to 2011. So the Sales taxes total here is larger, "
            "and the Other taxes total smaller, in those years. The page for each tax line shows the three taxes together, "
            "which means the same thing in every year.")
    if cat in ("Service charges", "Intergovernmental"):
        cat_notes.append("Harrah's casino payments are counted as service charges in some years and as money from other "
                         "governments in others. See the notes below the chart.")

    table, numbered = [], []
    for y in reversed(years):
        cells, cites, present = [], [], []
        for basis in BASES:
            rows = [r for r in b.revenue_rows(y, basis) if r["category"] == cat]
            cells.append(sum(r["amount"] for r in rows) if rows else None)
            if rows:
                present.append(basis)
                cites.append((basis, b.rev_cite(rows)))
        if not present:
            continue
        rn = [notes_all["event-" + e["id"]] for e in events if e["year"] == y and "event-" + e["id"] in notes_all]
        for n in rn:
            if n["id"] not in numbered:
                numbered.append(n["id"])
        table.append({"year": y, "cells": cells, "cites": cites, "notes": rn})
    for n in notes_all:
        if n not in numbered:
            numbered.append(n)
    page_notes = [notes_all[i] for i in numbered if i in notes_all]
    cites = [x for x in (b.rev_cite([r for r in b.revenue_rows(fy, "adopted") if r["category"] == cat]),
                         b.rev_cite([r for r in b.revenue_rows(py, "proposed") if r["category"] == cat])) if x]

    site.publish("/revenue/%s/" % c["slug"], new=tot_new, now=tot_old)
    line_split = common.split([(L["name"], L["new"]) for L in now if L["new"]], tot_new)
    site.page("/revenue/%s/" % c["slug"], "revenue_category.html", "Revenue: " + cat, cat=c, now=now, gone=gone, new=tot_new, old=tot_old,
              line_split=line_split,
              share=(tot_new / gf_new) if tot_new is not None and gf_new else None, fig=fig, table=table,
              numbered=numbered, page_notes=page_notes, cat_notes=cat_notes, cites=cites, py=py, fy=fy)


def _last_year(b, rev_id):
    return b.rev_last_year(rev_id)


# ---------------------------------------------------------------------------- one line
def build_source(site, b, rev_id, cats, ctx):
    py, fy = ctx["py"], ctx["fy"]
    info = dict(b.rev_lines[rev_id], plain_description=plain(b, rev_id))
    cat = info["category"]
    catc = next(c for c in cats if c["category"] == cat)
    years = list(range(site.config["first_year"], py + 1))
    group = b.rev_group(rev_id)
    comp = b.rev_comparable(rev_id)
    together = len(group) > 1
    roll = site.content["revenue_events"]["rollups"].get(comp) if together else None
    others = [i for i in group if i != rev_id]

    new, old = b.rev_amount(rev_id, py, "proposed"), b.rev_amount(rev_id, fy, "adopted")
    c_new, c_old = b.rev_cell(rev_id, py, "proposed"), b.rev_cell(rev_id, fy, "adopted")
    cur_cites = [b.cite(c.book, c.page, c.derived) for c in (c_old, c_new) if c]
    tot_new, tot_old = b.rev_total(py, "proposed"), b.rev_total(fy, "adopted")
    last = _last_year(b, rev_id)

    # Over-time chart: the comparable series (this line plus any it was printed inside).
    fig = None
    data_years = {y for y in years for bs in ("adopted", "actual", "proposed") if b.rev_series_amount(rev_id, y, bs) is not None}
    if len(data_years) >= 3:
        fig = views.money_figure(
            b, "over-time", info["name"], years, lambda fund, basis, y: b.rev_series_amount(rev_id, y, basis),
            funds=("general_fund",), series_spec=_spec(py), subject=(roll["label"][0].upper() + roll["label"][1:]) if roll else info["name"])

    # Events for this line (or any line charted with it).
    gset = set(group)

    def has_year(e):
        if e.get("all_lines"):
            return any(b.rev_cell(i, e["year"], e["basis"]) for i in group)
        return bool(gset & set(e["rev_ids"]))
    events = [e for e in ctx["events"] if not e.get("index_only") and has_year(e)]
    notes_all = {n["id"]: n for n in _event_notes(events, lambda e: True)}

    table, numbered = [], []
    for y in reversed(years):
        cells, cites, present, tog = [], [], [], []
        for basis in BASES:
            c = b.rev_cell(rev_id, y, basis)
            cells.append(c.amount if c else None)
            if c:
                present.append(basis)
                cites.append((basis, b.cite(c.book, c.page, c.derived)))
        if together:
            for basis in ("adopted", "actual"):
                tog.append(b.rev_series_amount(rev_id, y, basis))
        if not present and not any(v is not None for v in tog):
            continue
        rn = [notes_all["event-" + e["id"]] for e in events if e["year"] == y]
        for n in rn:
            if n["id"] not in numbered:
                numbered.append(n["id"])
        table.append({"year": y, "cells": cells, "tog": tog, "cites": cites, "notes": rn})
    for n in notes_all:
        if n not in numbered:
            numbered.append(n)
    page_notes = [notes_all[i] for i in numbered if i in notes_all]
    if not any(any(c is not None for c in r["cells"]) for r in table):
        table = []

    siblings = [{"id": i, "name": b.rev_lines[i]["name"], "url": "/revenue/%s/" % i}
                for i in b.rev_ids() if b.rev_lines[i]["category"] == cat and i != rev_id]
    others_named = [{"name": b.rev_lines[i]["name"], "url": "/revenue/%s/" % i} for i in others]

    url = "/revenue/%s/" % rev_id
    site.publish(url, new=new, now=old)
    site.page(url, "revenue_source.html", "Revenue: " + info["name"], info=info, rev_id=rev_id, cat=catc, new=new, old=old,
              cur_cites=cur_cites, tot_new=tot_new, tot_old=tot_old, last=last, fig=fig, roll=roll,
              others=others_named, table=table, numbered=numbered, page_notes=page_notes, siblings=siblings,
              share=(new / tot_new) if new is not None and tot_new else None,
              change=fmt.change(new, old) if new is not None and old is not None else "",
              together=together, py=py, fy=fy)
