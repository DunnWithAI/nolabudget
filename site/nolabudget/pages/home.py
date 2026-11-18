"""The home page: /"""
from .. import common, fmt, views

TOP_N = 12
RECEIPT_LINES = 8  # departments named on the "where every $100 goes" receipt


def build(site):
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    T = b.total
    head = {
        "gf_new": T(py, "proposed", "general_fund"), "all_new": T(py, "proposed", "all_funds"),
        "gf_now": T(fy, "adopted", "general_fund"), "all_now": T(fy, "adopted", "all_funds"),
    }
    head["other_new"] = T(py, "proposed", "other_funds")
    head["gf_diff"] = head["gf_new"] - head["gf_now"]
    head["all_diff"] = head["all_new"] - head["all_now"]

    # The once-only explanation of General Fund vs all funds, using 2022 as the example.
    ex_year = 2022
    explain = {
        "year": ex_year,
        "other_budgeted": T(ex_year, "adopted", "other_funds"),
        "other_spent": T(ex_year, "actual", "other_funds"),
        "cites": [b.total_source(ex_year, "adopted", "other_funds"), b.total_source(ex_year, "actual", "other_funds")],
    }

    # Where the money comes from: General Fund revenue by category, proposal year.
    cats = {c["category"]: c for c in site.content["revenue_categories"]}
    rev = b.revenue_by_category(py, "proposed")
    rev_total = sum(rev.values())
    income = []
    for name, amt in sorted(rev.items(), key=lambda kv: -kv[1]):
        c = cats[name]
        income.append({"name": name, "url": "/revenue/%s/" % c["slug"], "amount": amt,
                       "share": amt / rev_total, "text": c["short"]})
    rev_rows = b.revenue_rows(py, "proposed")
    rev_cite = b.cite(rev_rows[0]["source_book"], rev_rows[0]["source_page"]) if rev_rows else None

    # Where the money goes: departments by General Fund amount, proposal year.
    spend = common.dept_amounts(b, py, "proposed")
    spend = [(d, a) for d, a in spend if a > 0]
    top = [{"dept": d, "amount": a, "share": a / head["gf_new"]} for d, a in spend[:TOP_N]]
    rest = spend[TOP_N:]
    rest_total = sum(a for d, a in rest)

    # What is different: largest increases and decreases in the General Fund.
    ch = [r for r in common.changes(b, (py, "proposed"), (fy, "adopted")) if r["diff"] is not None]
    ups = sorted([r for r in ch if r["diff"] > 0], key=lambda r: -r["diff"])[:5]
    downs = sorted([r for r in ch if r["diff"] < 0], key=lambda r: r["diff"])[:5]
    for r in ups + downs:
        r["sentence"] = common.change_sentence(r)

    # Pictures: the two parts of the budget, and "of every $100" in and out.
    fund_split = [
        {"name": "General Fund", "amount": head["gf_new"], "slot": "c1", "share": head["gf_new"] / head["all_new"]},
        {"name": "Other funds", "amount": head["other_new"], "slot": "c-other", "share": head["other_new"] / head["all_new"]},
    ]
    for part, h in zip(fund_split, fmt.per_hundred([p["amount"] for p in fund_split])):
        part["per100"] = h
    fund_cites = [b.total_source(py, "proposed", "all_funds"), b.total_source(py, "proposed", "general_fund"),
                  b.total_source(py, "proposed", "other_funds")]
    income_split = common.split([(i["name"], i["amount"]) for i in income], rev_total, "Everything else")
    spend_split = common.split([(d["name"], a) for d, a in spend], head["gf_new"], "All other departments")
    receipt = common.split([(d["name"], a) for d, a in spend], head["gf_new"], "Everything else", n=RECEIPT_LINES)
    urls = {d["name"]: d["url"] for d, a in spend}
    for line in receipt:
        line["url"] = urls.get(line["name"], "/departments/")

    # The ten largest changes as one diverging bar chart, increases first.
    biggest = max(abs(r["diff"]) for r in ups + downs) if ups + downs else 1
    change_bars = [{"dept": r["dept"], "diff": r["diff"], "new": r["new"], "width": abs(r["diff"]) / biggest}
                   for r in ups + list(reversed(downs))]

    # Where the budget is in its yearly cycle. site.yml `stage` says which step is current.
    steps = [
        {"id": "proposed", "when": "Fall %d" % fy, "title": "The Mayor proposes",
         "text": "The Mayor sends the City Council a budget for the next year."},
        {"id": "council", "when": "By %s" % site.config["adoption_deadline"], "title": "The City Council decides",
         "text": "The Council reviews the proposal, can change it, and votes to adopt it."},
        {"id": "spending", "when": "January to December %d" % py, "title": "The city spends",
         "text": "Departments spend the money. The Council can amend the budget during the year."},
        {"id": "actual", "when": "About %d" % (py + 2), "title": "The real totals come out",
         "text": "What was actually spent is printed in the budget book two years later."},
    ]
    stage = site.config.get("stage", "council")
    reached = [s_["id"] for s_ in steps].index(stage)
    for i, s_ in enumerate(steps):
        s_["state"] = "done" if i < reached else ("now" if i == reached else "later")

    # Budgets since 2007.
    years = list(range(site.config["table_first_year"], py + 1))
    rows, numbered = [], []
    prev_gf = None
    for y in years:
        basis = "proposed" if y == py else "adopted"
        gf = T(y, basis, "general_fund")
        al = T(y, basis, "all_funds")
        act = T(y, "actual", "general_fund")
        notes = site.notes_for([y], ["general_fund", "all_funds"], [basis, "actual"])
        rows.append({
            "year": y, "label": common.year_label(site, y, basis), "basis": basis,
            "gf": gf, "all": al, "actual": act, "notes": notes,
            "change": (gf - prev_gf) / prev_gf if (gf is not None and prev_gf) else None,
        })
        prev_gf = gf
    for r in reversed(rows):  # number notes in the order the table shows them (newest first)
        for n in r["notes"]:
            if n["id"] not in numbered:
                numbered.append(n["id"])
    page_notes = [n for n in site.notes if n["id"] in numbered]
    page_notes.sort(key=lambda n: numbered.index(n["id"]))
    table_cites = [b.total_source(py, "proposed", "general_fund"), b.total_source(fy, "adopted", "general_fund")]

    # Citywide chart over time.
    all_years = list(range(site.config["first_year"], py + 1))
    fig = views.money_figure(
        b, "city-over-time", "The city budget over time", all_years,
        lambda fund, basis, y: T(y, basis, fund), subject="The city budget")

    # Find a department: every department, grouped.
    groups = []
    for g in b.groups:
        ds = [d for d in b.depts.values() if d["group"] == g]
        groups.append({"name": g, "depts": ds})

    # Common questions, rendered from content with computed values.
    qctx = dict(head=head, py=py, fy=fy, income=income, rev_total=rev_total, money=fmt.money,
                pct=fmt.pct, deadline=site.config["adoption_deadline"], explain=explain)

    spend_cites = [b.total_source(py, "proposed", "general_fund")]

    site.publish("/", gf_new=head["gf_new"], all_new=head["all_new"], gf_now=head["gf_now"], all_now=head["all_now"])
    site.page("/", "home.html", site.config["tagline"], head=head, explain=explain, income=income,
              rev_total=rev_total, rev_cite=rev_cite, top=top, rest=rest, rest_total=rest_total,
              ups=ups, downs=downs, rows=rows, page_notes=page_notes, numbered=numbered,
              table_cites=table_cites, fig=fig, groups=groups, glossary=site.content["glossary"],
              questions=site.content.get("questions", []), qctx=qctx, spend_cites=spend_cites,
              py=py, fy=fy, fund_split=fund_split, fund_cites=fund_cites, income_split=income_split,
              spend_split=spend_split, change_bars=change_bars, steps=steps,
              stage_num=reached + 1, receipt=receipt)
