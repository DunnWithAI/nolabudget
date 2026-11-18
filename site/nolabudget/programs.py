"""Inside a department: its programs in plain groups, followed across years, and its
budgeted jobs by rank.

Programs come from the `by_program` rows of department_detail.csv. Each year uses only
that year's own book and its own column (adopted; the proposal year uses the proposal),
because later books repeat earlier columns and some of those repeats double count.
Programs are matched across years by their four-digit code, with a name check from
data/crosswalk/program_groups.csv, since a few codes were reused for unrelated programs.
A program that fails the check, or has no mapping, counts as "Other programs".

Ranks come from site/content/position_ranks.yml, applied to positions.csv titles.
"""
import collections
import csv
import os
import re

from .data import COMBINED, CROSSWALK

OTHER = "Other programs"


def _code_name(dim1):
    d = (dim1 or "").strip()
    m = re.match(r"^(\d{4})\s+(.*)$", d)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.match(r"^(.*?)\s*-\s*(\d{4})$", d)
    if m:
        return m.group(2), m.group(1).strip()
    return None, d


def _crosswalk():
    out = collections.defaultdict(dict)
    with open(os.path.join(CROSSWALK, "program_groups.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["dept_id"]][r["code"]] = r
    return out


_CW = None


def _cw():
    global _CW
    if _CW is None:
        _CW = _crosswalk()
    return _CW


def mapped(dept):
    """{code: crosswalk row} for one department."""
    return _cw().get(dept, {})


def mapped_depts():
    """Departments that have rows in program_groups.csv."""
    return sorted(_cw())


def _column(year, py):
    basis = "proposed" if year == py else "adopted"
    return "%d-operating-%s" % (year, basis), basis


def programs_by_year(b, dept, years, py):
    """{year: [{code, printed, plain, group, amount, book, page}]} for years whose own book
    prints program rows. Rows without a code take the code their printed name carries in
    other years."""
    cw = mapped(dept)
    if not cw:
        return {}
    rows = [r for r in b.detail.get(dept, []) if r["table"] == "by_program"]
    learned = {}
    for r in rows:
        code, name = _code_name(r["dim1"])
        if code:
            learned.setdefault(name.lower(), code)
    out = {}
    for y in years:
        book, basis = _column(y, py)
        rs = [r for r in rows if r["book_id"] == book and r["fiscal_year"] == y and r["basis"] == basis]
        if not rs:
            continue
        progs = collections.OrderedDict()
        for r in rs:
            code, name = _code_name(r["dim1"])
            code = code or learned.get(name.lower())
            m = cw.get(code)
            ok = m is not None and re.search(m["name_pattern"], name, re.I)
            key = code if ok else "other:" + name.lower()
            p = progs.setdefault(key, {
                "code": code, "printed": name, "plain": m["plain_name"] if ok else name.title(),
                "group": m["group"] if ok else OTHER, "amount": 0.0, "book": book, "page": r["page"]})
            p["amount"] += r["amount"]
        out[y] = list(progs.values())
    return out


def inside(b, dept, years, py, fy):
    """Everything the "Inside the department" section shows, or None."""
    by_year = programs_by_year(b, dept, years, py)
    if py not in by_year:
        return None
    latest = [p for p in by_year[py] if abs(p["amount"]) >= 500]
    total = sum(p["amount"] for p in latest)
    if total <= 0:
        return None
    groups = collections.OrderedDict()
    for p in sorted(latest, key=lambda p: -p["amount"]):
        g = groups.setdefault(p["group"], {"name": p["group"], "amount": 0.0, "programs": []})
        g["amount"] += p["amount"]
        g["programs"].append(p)
    gl = sorted(groups.values(), key=lambda g: (g["name"] == OTHER, -g["amount"]))
    # Each group's total in every year with a book, for its small trend line.
    series_years = sorted(by_year)
    for g in gl:
        g["share"] = g["amount"] / total
        g["series"] = {y: sum(p["amount"] for p in by_year[y] if p["group"] == g["name"]) for y in series_years}
        g["first_year"] = series_years[0]
    # Years where the programs do not add up to the department's own total in the citywide tables.
    gaps = []
    for y in series_years:
        basis = "proposed" if y == py else "adopted"
        t = b.amount(dept, y, basis, "all_funds")
        s = sum(p["amount"] for p in by_year[y])
        if t and abs(s - t) > max(0.005 * t, 1000):
            gaps.append({"year": y, "programs": s, "statement": t})
    book, _ = _column(py, py)
    page = latest[0]["page"]
    return {
        "year": py, "total": total, "groups": gl, "years": series_years,
        "missing_years": [y for y in years if y not in by_year and y >= series_years[0]],
        "gaps": gaps, "cite": b.cite(book, page),
    }


# ---------------------------------------------------------------------------- ranks
_POS = None


def _positions():
    global _POS
    if _POS is None:
        d = collections.defaultdict(list)
        with open(os.path.join(COMBINED, "positions.csv"), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d[r["dept_id"]].append(r)
        _POS = d
    return _POS


def ranks(b, dept, rules, years, py):
    """{year: {"ranks": [(name, fte, sworn)], "total": fte, "sworn": fte, "book": id}} using
    each year's own book and column, as for programs."""
    if not rules:
        return {}
    compiled = [(r["name"], re.compile(r["pattern"], re.I), bool(r.get("sworn"))) for r in rules["ranks"]]
    rows = _positions().get(dept, [])
    out = {}
    for y in years:
        book, basis = _column(y, py)
        rs = [r for r in rows if r["book_id"] == book and int(r["fiscal_year"]) == y and r["basis"] == basis]
        if not rs:
            continue
        tally = collections.OrderedDict((name, 0.0) for name, _, _ in compiled)
        tally[rules["other"]] = 0.0
        for r in rs:
            fte = float(r["fte"] or 0)
            title = r["position_title"] or ""
            for name, rx, _ in compiled:
                if title and rx.search(title):
                    tally[name] += fte
                    break
            else:
                tally[rules["other"]] += fte
        sworn_names = {n for n, _, s in compiled if s}
        out[y] = {
            "ranks": [(n, v, n in sworn_names) for n, v in tally.items()],
            "total": sum(tally.values()),
            "sworn": sum(v for n, v in tally.items() if n in sworn_names),
            "untitled": sum(float(r["fte"] or 0) for r in rs if not (r["position_title"] or "").strip()),
            "book": book, "cite": b.cite(book, rs[0]["page"]),
        }
    return out
