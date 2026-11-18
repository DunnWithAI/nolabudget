"""Computations shared by several page types."""
from . import fmt


def moment(site):
    """The two budgets the site leads with: the proposal and the budget in force."""
    c = site.config
    return {"py": c["proposal_year"], "fy": c["in_force_year"]}


SPLIT_PARTS = 5   # named parts in an "of every $100" bar; the rest fold into one grey part


def split(items, total, other_label="Everything else", n=SPLIT_PARTS):
    """Parts for a split bar (macro split_bar): the n largest (name, amount) items, then the
    rest folded into one grey part. Each part: name, amount, share of `total`, per100
    (whole dollars out of every $100, adding to exactly 100) and colour slot.
    Returns [] when the picture would mislead: no total, or a negative amount."""
    items = sorted([(k, a) for k, a in items if a], key=lambda kv: -kv[1])
    if not total or total <= 0 or any(a < 0 for k, a in items) or not items:
        return []
    head, rest = items[:n], items[n:]
    parts = [{"name": k, "amount": a, "slot": "c%d" % (i + 1)} for i, (k, a) in enumerate(head)]
    if rest:
        parts.append({"name": other_label, "amount": sum(a for k, a in rest), "slot": "c-other"})
    for part, h in zip(parts, fmt.per_hundred([p["amount"] for p in parts])):
        part["share"] = part["amount"] / total
        part["per100"] = h
    return parts


def dept_amounts(budget, year, basis, fund="general_fund"):
    """[(dept, amount)] for departments printed in that column, largest first."""
    rows = []
    for d in budget.depts.values():
        a = budget.amount(d["id"], year, basis, fund)
        if a is not None:
            rows.append((d, a))
    rows.sort(key=lambda r: -r[1])
    return rows


def changes(budget, new=(2027, "proposed"), old=(2026, "adopted"), fund="general_fund"):
    """Per-department change between two columns. A department printed in only one of
    the two columns gets `status` 'new' or 'gone' instead of a numeric change."""
    out = []
    for d in budget.depts.values():
        a = budget.amount(d["id"], new[0], new[1], fund)
        b = budget.amount(d["id"], old[0], old[1], fund)
        if a is None and b is None:
            continue
        row = {"dept": d, "new": a, "old": b, "diff": None, "pct": None, "status": "both"}
        if a is None:
            row["status"] = "gone"
        elif b is None:
            row["status"] = "new"
        else:
            row["diff"] = a - b
            row["pct"] = (a - b) / b if b else None
        out.append(row)
    return out


def change_sentence(r):
    d = r["diff"]
    if d is None:
        if r["status"] == "gone":
            return "not printed as its own line in the newer budget"
        return "printed as its own line for the first time, at %s" % fmt.money(r["new"])
    if abs(d) < 50000:
        return "about the same, at %s" % fmt.money(r["new"])
    return "%s %s, to %s" % ("up" if d > 0 else "down", fmt.money(abs(d)), fmt.money(r["new"]))


def year_label(site, y, basis):
    c = site.config
    if y == c["proposal_year"] and basis == "proposed":
        return "%d, proposed" % y
    if y == c["in_force_year"]:
        return "%d, in force" % y
    return str(y)
