"""Changes pages: /changes/ and /changes/<year>/

/changes/ sets the Mayor's proposal for the proposal year against the budget in force.
/changes/<year>/ does the same for each earlier year that has an adopted General Fund
figure for both that year and the year before (adopted against adopted).
Each page has three parts: spending by department, income by category, and
budgeted positions by department.
"""
from .. import budgetdata as bd, common, fmt
from ..data import BASIS_LABEL
from .budgets import has_changes_page


def build(site):
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    build_page(site, b, "/changes/", (py, "proposed"), (fy, "adopted"), py, fy, main=True)
    for y in b.years:
        if y != py and has_changes_page(b, y):
            build_page(site, b, "/changes/%d/" % y, (y, "adopted"), (y - 1, "adopted"), py, fy, main=False)


def split(rows):
    both = [r for r in rows if r["status"] == "both"]
    ups = sorted([r for r in both if r["diff"] >= 0.5], key=lambda r: -r["diff"])
    downs = sorted([r for r in both if r["diff"] <= -0.5], key=lambda r: r["diff"])
    same = [r for r in both if abs(r["diff"]) < 0.5]
    one = sorted([r for r in rows if r["status"] != "both"], key=lambda r: -(r["new"] or r["old"] or 0))
    return ups, downs, same, one


def build_page(site, b, url, new, old, py, fy, main):
    ny, nb = new
    oy, ob = old
    if main:
        new_name = "the Mayor's %d proposal" % ny
        old_name = "the %d budget now in force" % oy
        new_short, old_short = "%d proposed" % ny, "%d adopted" % oy
    else:
        new_name = "the %d adopted budget" % ny
        old_name = "the %d adopted budget" % oy
        new_short, old_short = "%d adopted" % ny, "%d adopted" % oy

    moves = [m for m in site.content["changes"]["moves"] if m["year"] == ny]
    moved = {d for m in moves for d in m["depts"]}

    sections, facts = [], {}
    for fund in ("general_fund", "all_funds"):
        rows = [r for r in common.changes(b, new, old, fund) if (r["new"] or r["old"])]
        ups, downs, same, one = split(rows)
        tn, to = b.total(ny, nb, fund), b.total(oy, ob, fund)
        sections.append({
            "fund": fund, "ups": ups, "downs": downs, "same": same, "one": one, "new": tn, "old": to,
            "n_both": len(ups) + len(downs) + len(same),
            "cites": [(BASIS_LABEL[ob] + " " + str(oy), b.total_source(oy, ob, fund)),
                      (BASIS_LABEL[nb] + " " + str(ny), b.total_source(ny, nb, fund))],
        })
        facts[fund] = {"total_new": tn, "total_old": to,
                       "rows": {r["dept"]["id"]: (r["old"], r["new"], r["diff"]) for r in rows}}
    gf, af = sections

    # What the page says up front.
    lead = []
    d_gf = gf["new"] - gf["old"]
    lead.append("%s spends %s from the General Fund. That is %s on %s (%s)." % (
        new_name[0].upper() + new_name[1:], fmt.money(gf["new"]), fmt.change(gf["new"], gf["old"]), old_name, fmt.money(gf["old"])))
    nu, nd, ns = len(gf["ups"]), len(gf["downs"]), len(gf["same"])
    lead.append("Of the %d departments with a General Fund figure in both, %d %s up, %d %s down and %d %s the same." % (
        gf["n_both"], nu, "goes" if nu == 1 else "go", nd, "goes" if nd == 1 else "go", ns, "stays" if ns == 1 else "stay"))
    if gf["ups"]:
        r = gf["ups"][0]
        lead.append("The largest increase is %s, %s." % (r["dept"]["name"], common.change_sentence(r)))
    if gf["downs"]:
        r = gf["downs"][0]
        lead.append("The largest decrease is %s, %s." % (r["dept"]["name"], common.change_sentence(r)))
    all_line = "Counting all funds, the total is %s, %s on %s (%s)." % (
        fmt.money(af["new"]), fmt.change(af["new"], af["old"]), old_name, fmt.money(af["old"]))

    # Known moves, with the figures the books print for the departments involved.
    move_ctx = []
    rowmap = {s["fund"]: {r["dept"]["id"]: r for r in s["ups"] + s["downs"] + s["same"] + s["one"]} for s in sections}
    for m in moves:
        ds = []
        for d in m["depts"]:
            r = rowmap["general_fund"].get(d)
            ds.append({"dept": b.depts[d], "sentence": common.change_sentence(r) if r else None})
        move_ctx.append({"id": m["id"], "text": m["text"], "depts": ds})

    # Revenue by category.
    cats = [c["category"] for c in site.content["revenue_categories"]]
    rn, ro = b.revenue_by_category(ny, nb), b.revenue_by_category(oy, ob)
    rev_rows = []
    for c in cats:
        a, o = rn.get(c), ro.get(c)
        if a is None and o is None:
            continue
        rev_rows.append({"name": c, "new": a, "old": o,
                         "diff": (a or 0) - (o or 0), "pct": ((a or 0) - o) / o if o else None})
    rev_rows.sort(key=lambda r: -abs(r["diff"]))
    rev_tn, rev_to = sum(rn.values()), sum(ro.values())
    rev_cites = [(BASIS_LABEL[ob] + " " + str(oy), bd.revenue_cite(b, oy, ob)),
                 (BASIS_LABEL[nb] + " " + str(ny), bd.revenue_cite(b, ny, nb))]

    # Positions: the same kind of book for both years.
    pos_new, pos_old = (ny, nb), (oy, ob)
    ref = None
    if main:
        pos_old = (oy, "proposed")      # compare proposal with proposal; 2026 adopted is shown for reference
        ref = (oy, "adopted")
    pn, po = bd.positions_by_dept(b, *pos_new), bd.positions_by_dept(b, *pos_old)
    pref = bd.positions_by_dept(b, *ref) if ref else {}
    pos_rows = []
    if pn and po:
        for d in b.depts.values():
            a, o = pn.get(d["id"]), po.get(d["id"])
            if a is None and o is None:
                continue
            pos_rows.append({"dept": d, "new": a, "old": o, "ref": pref.get(d["id"]),
                             "diff": (a - o) if a is not None and o is not None else None})
        pos_rows.sort(key=lambda r: (r["diff"] is None, -abs(r["diff"] or 0), r["dept"]["name"]))
    def pf(v, signed=False):
        if v is None:
            return None
        dig = 2 if abs(v - round(v)) > 0.004 else 0
        t = fmt.number(abs(v), dig)
        return ("+" if v > 0 else fmt.MINUS if v < 0 else "") + t if signed else fmt.number(v, dig)
    for r in pos_rows:
        r.update(f_new=pf(r["new"]), f_old=pf(r["old"]), f_ref=pf(r["ref"]), f_diff=pf(r["diff"], True))
    pos_tn, pos_to = (sum(pn.values()) if pn else None), (sum(po.values()) if po else None)
    pos_tref = sum(pref.values()) if pref else None
    pos_f = {"new": pf(pos_tn), "old": pf(pos_to), "ref": pf(pos_tref),
             "diff": pf(pos_tn - pos_to, True) if pos_tn is not None and pos_to is not None else None}
    pos_cites = [(BASIS_LABEL[k[1]] + " " + str(k[0]), bd.positions_cite(b, *k)) for k in (pos_old, pos_new)]
    pos_cites = [(l, c) for l, c in pos_cites if c]

    notes = site.notes_for([ny, oy], ["general_fund", "all_funds"], [nb, ob])
    title = ("Changes from %d to %d" % (oy, ny)) if main else ("Changes from %d to %d" % (oy, ny))
    site.publish(url, new_key=(ny, nb), old_key=(oy, ob), funds=facts,
                 rev={"new": rev_tn, "old": rev_to, "rows": {r["name"]: (r["old"], r["new"]) for r in rev_rows}},
                 pos={"new_key": pos_new, "old_key": pos_old, "total_new": pos_tn, "total_old": pos_to,
                      "rows": {r["dept"]["id"]: (r["old"], r["new"]) for r in pos_rows}})
    site.page(url, "changes.html", title, main=main, ny=ny, oy=oy, py=py, fy=fy, new_name=new_name,
              old_name=old_name, new_short=new_short, old_short=old_short, sections=sections, lead=lead,
              all_line=all_line, moves=move_ctx, moved=moved, rev_rows=rev_rows, rev_tn=rev_tn, rev_to=rev_to,
              rev_cites=rev_cites, pos_rows=pos_rows, pos_f=pos_f, pos_tn=pos_tn, pos_to=pos_to, pos_tref=pos_tref,
              pos_new_label="%d %s" % (pos_new[0], pos_new[1]), pos_old_label="%d %s" % (pos_old[0], pos_old[1]),
              pos_ref_label="%d adopted" % ref[0] if ref else "", pos_cites=pos_cites, notes=notes,
              numbered=[n["id"] for n in notes], has_prev_page=has_changes_page(b, ny - 1) if not main else False,
              years_list=[y for y in b.years if y != py and has_changes_page(b, y)])
