"""Year pages: /budgets/ and /budgets/<year>/

The index lists every year in one table with a chart. Each year page gives a plain
summary computed from the data, what the Council changed from the Mayor's proposal,
where actual spending ended up against the budget, the full department table,
revenue, budgeted positions, the comparability notes that apply, and links to every
original document we have for the year.
"""
from .. import budgetdata as bd, common, fmt, views
from ..data import BASES

TOP_N = 8


def build(site):
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    years = [y for y in b.years if any(b.total(y, bs, "general_fund") or b.total(y, bs, "all_funds") for bs in BASES)]
    docs = bd.load_documents()
    build_index(site, b, years, py, fy)
    for y in years:
        build_year(site, b, y, years, docs, py, fy)


# ------------------------------------------------------------------- index
def lead_basis(b, y, py):
    if y == py and b.total(y, "proposed", "general_fund") is not None:
        return "proposed"
    for bs in ("adopted", "proposed", "actual"):
        if b.total(y, bs, "general_fund") is not None:
            return bs
    return "actual"


def has_changes_page(b, y):
    return b.total(y, "adopted", "general_fund") is not None and b.total(y - 1, "adopted", "general_fund") is not None


def build_index(site, b, years, py, fy):
    rows, numbered = [], []
    for y in years:
        pos_basis = "adopted" if bd.positions_total(b, y, "adopted") is not None else "proposed"
        notes = site.notes_for([y], ["general_fund", "all_funds"], ["proposed", "adopted", "actual"])
        rows.append({
            "year": y, "label": common.year_label(site, y, "proposed" if y == py else "adopted"),
            "gf_proposed": b.total(y, "proposed", "general_fund"),
            "gf_adopted": b.total(y, "adopted", "general_fund"),
            "gf_actual": b.total(y, "actual", "general_fund"),
            "all_adopted": b.total(y, "adopted", "all_funds"),
            "pos": bd.positions_total(b, y, pos_basis), "pos_basis": pos_basis, "notes": notes,
            "only_actual": b.total(y, "adopted", "general_fund") is None and b.total(y, "proposed", "general_fund") is None,
        })
    for r in reversed(rows):  # newest first, as shown
        for n in r["notes"]:
            if n["id"] not in numbered:
                numbered.append(n["id"])
    page_notes = sorted([n for n in site.notes if n["id"] in numbered], key=lambda n: numbered.index(n["id"]))
    fig = views.money_figure(
        b, "budgets-over-time", "The budget over time", list(range(site.config["first_year"], py + 1)),
        lambda fund, basis, y: b.total(y, basis, fund), subject="The city budget")
    first_budget = min(r["year"] for r in rows if not r["only_actual"])
    last_actual = max(r["year"] for r in rows if r["gf_actual"] is not None)
    cites = [b.total_source(py, "proposed", "general_fund"), b.total_source(fy, "adopted", "general_fund")]
    site.page("/budgets/", "budgets_index.html", "Budgets by year", rows=rows, py=py, fy=fy, fig=fig,
              page_notes=page_notes, numbered=numbered, first_budget=first_budget, last_actual=last_actual,
              first_year=years[0], cites=cites, changes_years=[y for y in years if has_changes_page(b, y)])


# --------------------------------------------------------------- year page
def sum_or_none(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def build_year(site, b, y, years, docs, py, fy):
    T = b.total
    lead = lead_basis(b, y, py)
    cols = [(bs, f) for bs in ("proposed", "adopted", "actual") for f in ("general_fund", "all_funds")
            if T(y, bs, f) is not None]
    lead_fund = "general_fund" if T(y, lead, "general_fund") is not None else "all_funds"
    totals = {k: T(y, *k) for k in cols}

    # Department table, grouped.
    lead_col = (lead, lead_fund)
    groups, dept_vals = [], {}
    for g in b.groups:
        rows = []
        for d in b.depts.values():
            if d["group"] != g:
                continue
            vals = {k: b.amount(d["id"], y, k[0], k[1]) for k in cols}
            if all(v is None for v in vals.values()):
                continue
            for k, v in vals.items():
                if v is not None:
                    dept_vals["%s|%s|%s" % (d["id"], k[0], k[1])] = v
            rows.append({"dept": d, "vals": vals})
        if not rows:
            continue
        rows.sort(key=lambda r: (-(r["vals"].get(lead_col) or 0), r["dept"]["name"]))
        sub = {k: sum_or_none(r["vals"][k] for r in rows) for k in cols}
        groups.append({"name": g, "rows": rows, "sub": sub})
    groups.sort(key=lambda g: -(g["sub"].get(lead_col) or 0))

    notes = site.notes_for([y], ["general_fund", "all_funds"], [k[0] for k in cols])
    col_notes = {}
    for k in cols:
        f = {k[1]} | ({"other_funds"} if k[1] == "all_funds" else set())
        col_notes[k] = [n for n in notes if k[0] in n["bases"] and f & set(n["funds"]) and y in n["years"]]
    numbered = [n["id"] for n in notes]
    cites = [(bs, f, b.total_source(y, bs, f)) for bs, f in cols]

    # Proposal to adoption.
    prop = None
    if T(y, "proposed", lead_fund) is not None and T(y, "adopted", lead_fund) is not None:
        prop = compare(b, y, ("adopted", y), ("proposed", y), lead_fund)
        prop["cites"] = [("Proposed", b.total_source(y, "proposed", lead_fund)), ("Adopted", b.total_source(y, "adopted", lead_fund))]
    # Budget vs actual.
    act = None
    if T(y, "actual", lead_fund) is not None and T(y, "adopted", lead_fund) is not None:
        act = compare(b, y, ("actual", y), ("adopted", y), lead_fund)
        act["cites"] = [("Adopted", b.total_source(y, "adopted", lead_fund)), ("Actual", b.total_source(y, "actual", lead_fund))]

    # Revenue.
    rcols = [bs for bs in bd.REVENUE_BASES if b.revenue_rows(y, bs)]
    cat_order = [c["category"] for c in site.content["revenue_categories"]]
    rev_rows = []
    for c in cat_order:
        vals = {bs: b.revenue_by_category(y, bs).get(c) for bs in rcols}
        if any(v is not None for v in vals.values()):
            rev_rows.append({"name": c, "vals": vals})
    rev_totals = {bs: sum(b.revenue_by_category(y, bs).values()) for bs in rcols}
    rev_cites = [(bs, bd.revenue_cite(b, y, bs)) for bs in rcols]

    # Positions.
    pos = {}
    for bs in ("proposed", "adopted", "revised"):
        pos[bs] = bd.positions_total(b, y, bs)
    pos_basis = "adopted" if pos["adopted"] is not None else ("proposed" if pos["proposed"] is not None else None)
    prev_pos = bd.positions_total(b, y - 1, "adopted") if pos_basis == "adopted" else (
        bd.positions_total(b, y - 1, "proposed") if pos_basis == "proposed" else None)
    pos_prev_basis = "adopted" if pos_basis == "adopted" else "proposed"
    pos_cite = bd.positions_cite(b, y, pos_basis) if pos_basis else None

    summary = summarise(site, b, y, py, fy, lead, totals, notes)
    ydocs = docs.get(y, [])
    book_note = site.content["year_docs"]["books"].get(y)
    pos_note = site.content["year_docs"]["positions"].get(y)
    prev = y - 1 if y - 1 in years else None
    nxt = y + 1 if y + 1 in years else None
    status = "proposed" if y == py else ("in force" if y == fy else "")
    url = "/budgets/%d/" % y
    site.publish(url, dept_vals=dept_vals, **{"%s_%s" % (bs, f): v for (bs, f), v in totals.items()})
    site.publish(url, docs=[d["url"] for d in ydocs])
    site.page(url, "year.html", "The %d budget" % y, year=y, cols=cols, groups=groups, totals=totals, cites=cites,
              notes=notes, numbered=numbered, col_notes=col_notes, lead=lead, lead_fund=lead_fund, status=status,
              py=py, fy=fy, summary=summary, prop=prop, act=act, rcols=rcols, rev_rows=rev_rows,
              rev_totals=rev_totals, rev_cites=rev_cites, pos=pos, pos_basis=pos_basis, prev_pos=prev_pos,
              pos_prev_basis=pos_prev_basis, pos_cite=pos_cite, pos_note=pos_note, docs=ydocs,
              book_note=book_note, prev=prev, next=nxt, only_actual=lead == "actual",
              has_changes=has_changes_page(b, y), is_proposal=(y == py and lead == "proposed"))


def compare(b, y, new, old, fund):
    """Departments' change between two columns of the same year; largest first."""
    rows = [r for r in common.changes(b, (new[1], new[0]), (old[1], old[0]), fund)
            if r["status"] == "both" and r["diff"] is not None and abs(r["diff"]) >= 0.5]
    rows.sort(key=lambda r: -abs(r["diff"]))
    t_new, t_old = b.total(new[1], new[0], fund), b.total(old[1], old[0], fund)
    return {"fund": fund, "rows": rows[:TOP_N], "n_changed": len(rows), "new": t_new, "old": t_old,
            "diff": t_new - t_old, "new_basis": new[0], "old_basis": old[0]}


def summarise(site, b, y, py, fy, lead, totals, notes):
    """The year's one-paragraph plain summary, as a list of sentences computed from the data."""
    T = b.total
    s = []
    gf = lambda bs: T(y, bs, "general_fund")
    af = lambda bs: T(y, bs, "all_funds")
    if lead == "proposed":
        s.append("The Mayor has proposed spending %s from the General Fund in %d, or %s counting all funds. "
                 "The City Council has not yet adopted a budget." % (fmt.money(gf("proposed")), y, fmt.money(af("proposed"))))
    elif lead == "adopted":
        s.append("The City Council adopted a %d budget that spends %s from the General Fund, or %s counting all funds."
                 % (y, fmt.money(gf("adopted")), fmt.money(af("adopted"))))
    else:
        s.append("The books we have print only what the city actually spent in %d, not a budget: %s from the General "
                 "Fund, or %s counting all funds." % (y, fmt.money(gf("actual")), fmt.money(af("actual"))))
    # Change from the year before, like with like where possible.
    prev_adopted = T(y - 1, "adopted", "general_fund")
    prev_actual = T(y - 1, "actual", "general_fund")
    if lead in ("proposed", "adopted") and prev_adopted is not None:
        word = "the %d budget now in force" % (y - 1) if y - 1 == fy and lead == "proposed" else "the %d adopted budget" % (y - 1)
        s.append("The General Fund is %s on %s." % (fmt.change(gf(lead), prev_adopted), word))
    elif lead in ("proposed", "adopted") and prev_actual is not None:
        s.append("The General Fund is %s on the %s the city actually spent in %d. (No %d budget is printed to compare with.)"
                 % (fmt.change(gf(lead), prev_actual), fmt.money(prev_actual), y - 1, y - 1))
    elif lead == "actual" and prev_actual is not None:
        s.append("That is %s on the %d actual spending." % (fmt.change(gf("actual"), prev_actual), y - 1))
    # Proposed vs adopted.
    for f, fl in (("general_fund", "General Fund"), ("all_funds", "all-funds total")):
        if lead == "adopted" and T(y, "proposed", f) is not None and T(y, "adopted", f) is not None:
            d = T(y, "adopted", f) - T(y, "proposed", f)
            if abs(d) < 0.5:
                s.append("The Mayor proposed %s for the %s, and the adopted budget matches it." % (fmt.money(T(y, "proposed", f)), fl))
            else:
                s.append("The Mayor had proposed %s for the %s; the adopted figure is %s on that." % (
                    fmt.money(T(y, "proposed", f)), fl, fmt.change(T(y, "adopted", f), T(y, "proposed", f))))
            break
    # Actual vs adopted.
    if gf("actual") is not None and gf("adopted") is not None:
        t = "The city actually spent %s from the General Fund, %s on the adopted budget." % (
            fmt.money(gf("actual")), fmt.change(gf("actual"), gf("adopted")))
        if any(n["id"] == "pension-2012" for n in notes):
            f = site.note_facts
            t += (" That includes %s of one-time pension bond refinancing; without it, spending was about %s."
                  % (fmt.money(f["pension_2012"]), fmt.money(f["gf_actual_2012_ex"])))
        s.append(t)
        if af("actual") is not None and af("adopted") is not None:
            s.append("Counting all funds, it spent %s of %s budgeted." % (fmt.money(af("actual")), fmt.money(af("adopted"))))
    elif lead != "actual" and y < py and gf("actual") is None:
        s.append("Actual spending for %d has not been printed yet; books print it about two years later." % y)
    if gf("revised") is not None and gf("adopted") is not None and abs(gf("revised") - gf("adopted")) > 0.005 * gf("adopted"):
        s.append("The General Fund budget was later revised to %s." % fmt.money(gf("revised")))
    return s
