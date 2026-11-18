"""Build the combined, cross-year dataset from the per-book extractions.

    .venv/bin/python scripts/build_combined.py

Reads   data/extracted/<book_id>/*.csv      (names as printed, one folder per book)
        data/crosswalk/*.csv                (canonical names, aliases, corrections)
        data/raw/context/*.csv              (population, CPI)
Writes  data/combined/*.csv and build_report.json

Every fiscal year is printed in several books (its own book, then reprinted as a
prior-year column in later ones). `*_observations.csv` keeps every printing;
the series files pick one per cell by the rules in `pick()` below.
"""
import collections
import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "data", "extracted")
CW = os.path.join(ROOT, "data", "crosswalk")
OUT = os.path.join(ROOT, "data", "combined")
os.makedirs(OUT, exist_ok=True)

report = {"unmapped": {}, "notes": []}


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(name, rows, cols):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    report.setdefault("rows", {})[name] = len(rows)


def book_year(book_id):
    return int(book_id[:4])


def book_stage(book_id):
    return book_id.rsplit("-", 1)[1]


BOOKS = sorted(os.path.basename(p) for p in glob.glob(os.path.join(EX, "*-operating-*")))

# ---------------------------------------------------------------- corrections
CORR = read(os.path.join(CW, "corrections.csv"))
corr_used = collections.Counter()


def correct(book_id, file, row):
    for i, c in enumerate(CORR):
        if c["book_id"] == book_id and c["file"] == file and row["fiscal_year"] == c["match_fiscal_year"] and row["basis"] == c["match_basis"]:
            if c["match_depts"] and row.get("dept_name") not in c["match_depts"].split("|"):
                continue
            row = dict(row, fiscal_year=c["set_fiscal_year"], basis=c["set_basis"], corrected="1")
            corr_used[i] += 1
            return row
    return row


# ------------------------------------------------------------------ crosswalks
DEPTS = {r["dept_id"]: r for r in read(os.path.join(CW, "departments.csv"))}
ST_ALIAS = {(r["book_id"], r["printed_name"]): r["dept_id"] for r in read(os.path.join(CW, "department_aliases_statement.csv"))}
DT_ALIAS = {(r["book_id"], r["source_file"], r["printed_name"]): (r["dept_id"], r.get("sub_unit", "")) for r in read(os.path.join(CW, "department_aliases_detail.csv"))}
REV_ALIAS = {(r["book_id"], r["fund_group"], r["fund_name"], r["category_printed"], r["line_name"]): r["rev_id"] for r in read(os.path.join(CW, "revenue_aliases.csv"))}
REVS = {r["rev_id"]: r for r in read(os.path.join(CW, "revenue_sources.csv"))}
ROLLUP = {r["rev_id"]: r["rollup_id"] for r in read(os.path.join(CW, "revenue_rollups.csv"))}


def miss(kind, key):
    report["unmapped"].setdefault(kind, set()).add(" | ".join(key))


# ------------------------------------------------------------------- selection
def pick(obs, fy, basis):
    """Choose one printing of a cell. `obs` is a list of dicts with book_id.

    actual   -> the latest book that prints it (latest restatement)
    adopted  -> the fiscal year's own adopted book; failing that, the earliest
                later book that reprints it (2010 from the 2011 book, 2020 from 2021)
    proposed -> the fiscal year's own proposed book, else the adopted book's proposed column
    revised  -> (derived basis) the adopted figure as reprinted in the next book, if that book differs
    """
    fy = int(fy)
    if basis == "actual":
        return max(obs, key=lambda o: (book_year(o["book_id"]), book_stage(o["book_id"]) == "adopted"))
    own = [o for o in obs if book_year(o["book_id"]) == fy]
    if basis == "adopted":
        own_adopted = [o for o in own if book_stage(o["book_id"]) == "adopted"]
        if own_adopted:
            return own_adopted[0]
        later = [o for o in obs if book_year(o["book_id"]) > fy]
        return min(later, key=lambda o: (book_year(o["book_id"]), book_stage(o["book_id"]) != "adopted")) if later else None
    if basis == "proposed":
        if not own:
            return None
        # the proposed book itself where we have it (2020, 2025-2027); the 2026 adopted book's
        # "proposed" column differs from what was actually proposed
        return sorted(own, key=lambda o: book_stage(o["book_id"]) != "proposed")[0]
    return None


def series(observations, keycols):
    """Collapse observations to one row per key + (fiscal_year, basis), adding a `revised` basis.

    The source book is chosen per statement COLUMN (fund_group, fiscal_year, basis), never per
    cell, so one column is never a mixture of books. `keycols` is (id column, "fund_group").
    """
    idcol = keycols[0]
    cols = collections.defaultdict(lambda: collections.defaultdict(dict))  # column -> book -> id -> cell
    for o in observations:
        cell = cols[(o["fund_group"], o["fiscal_year"], o["basis"])][o["book_id"]].setdefault(
            o[idcol], {"amount": 0, "pages": set()})
        cell["amount"] += o["amount"]  # several printed lines can map to one id inside a book
        cell["pages"].add(str(o["page"]))
    out = []
    for (fund, fy, basis), by_book in cols.items():
        most = max(len(c) for c in by_book.values())
        # a later book sometimes reprints only part of a column: ignore printings with <90% of the lines
        # (actuals only; for budgets the year's own book wins however many lines it prints)
        full = [{"book_id": b} for b, c in by_book.items() if basis != "actual" or len(c) >= 0.9 * most]
        chosen = pick(full, fy, basis)
        if chosen is None:
            continue

        def emit(book, as_basis):
            for key, cell in by_book[book].items():
                others = [by_book[b][key]["amount"] for b in by_book if key in by_book[b]]
                out.append({idcol: key, "fund_group": fund, "fiscal_year": fy, "basis": as_basis, "amount": cell["amount"],
                            "source_book": book, "source_page": ";".join(sorted(cell["pages"], key=lambda p: (len(p), p))[:4]),
                            "n_books": len(others), "books_agree": int(len(set(others)) == 1), "derived": ""})

        emit(chosen["book_id"], basis)
        if basis == "adopted":
            later = [f for f in full if book_year(f["book_id"]) > int(fy) and f["book_id"] != chosen["book_id"]]
            if later:
                nxt = min(later, key=lambda f: (book_year(f["book_id"]), book_stage(f["book_id"]) != "adopted"))
                emit(nxt["book_id"], "revised")
    return out


# -------------------------------------------------------------------- spending
sp_obs = []
for b in BOOKS:
    for r in read(os.path.join(EX, b, "department_expenditures.csv")):
        r = correct(b, "department_expenditures", r)
        dept = ST_ALIAS.get((b, r["dept_name"]))
        if dept is None:
            miss("statement_department", (b, r["dept_name"]))
            continue
        sp_obs.append({"fiscal_year": r["fiscal_year"], "basis": r["basis"], "fund_group": r["fund_group"], "dept_id": dept,
                       "amount": int(float(r["amount"])), "book_id": b, "page": r["page"], "printed_name": r["dept_name"],
                       "corrected": r.get("corrected", "")})
write("spending_observations.csv", sp_obs, ["fiscal_year", "basis", "fund_group", "dept_id", "amount", "book_id", "page", "printed_name", "corrected"])

spending = series(sp_obs, ["dept_id", "fund_group"])

# Fill a missing fund group from the other two where the source book prints only two of the
# three statements for that year (e.g. the 2011 book has no all-funds statement; the 2008 and
# 2009 books have no other-funds statement). A department absent from one printed statement
# counts as zero there.
idx = {(r["dept_id"], r["fiscal_year"], r["basis"], r["fund_group"]): r for r in spending}
have = collections.defaultdict(dict)  # (fy, basis) -> fund_group -> source_book
for r in spending:
    have[(r["fiscal_year"], r["basis"])][r["fund_group"]] = r["source_book"]
for (fy, basis), funds in sorted(have.items()):
    depts = sorted({k[0] for k in idx if k[1] == fy and k[2] == basis})
    g, o, a = funds.get("general_fund"), funds.get("other_funds"), funds.get("all_funds")

    def val(dept, fund):
        r = idx.get((dept, fy, basis, fund))
        return r["amount"] if r else 0

    def add(dept, fund, amount, how, book):
        spending.append({"dept_id": dept, "fund_group": fund, "fiscal_year": fy, "basis": basis, "amount": amount,
                         "source_book": book, "source_page": "", "n_books": "", "books_agree": "", "derived": how})

    if a is None and g and o and g == o:
        for d in depts:
            add(d, "all_funds", val(d, "general_fund") + val(d, "other_funds"), "general_fund + other_funds", g)
    elif o is None and g and a and g == a:
        for d in depts:
            add(d, "other_funds", val(d, "all_funds") - val(d, "general_fund"), "all_funds - general_fund", g)

# A department with no general_fund row in a year where the statement has one for others is zero, not unknown:
# left as absent on purpose; the site should treat absent as "not printed".

for r in spending:
    d = DEPTS.get(r["dept_id"], {})
    r["dept_name"] = d.get("name", r["dept_id"])
    r["group"] = d.get("group", "")
spending.sort(key=lambda r: (r["fiscal_year"], r["basis"], r["fund_group"], r["dept_id"]))
write("spending.csv", spending, ["fiscal_year", "basis", "fund_group", "dept_id", "dept_name", "group", "amount", "source_book", "source_page", "n_books", "books_agree", "derived"])

# --------------------------------------------------------------------- revenue
rv_obs = []
for b in BOOKS:
    for r in read(os.path.join(EX, b, "revenues.csv")):
        r = correct(b, "revenues", r)
        rev = REV_ALIAS.get((b, r["fund_group"], r["fund_name"], r["category"], r["line_name"]))
        if rev is None:
            miss("revenue_line", (b, r["fund_group"], r["fund_name"], r["category"], r["line_name"]))
            continue
        rv_obs.append({"fiscal_year": r["fiscal_year"], "basis": r["basis"], "fund_group": r["fund_group"], "rev_id": rev,
                       "amount": int(float(r["amount"])), "book_id": b, "page": r["page"], "printed_name": r["line_name"],
                       "corrected": r.get("corrected", "")})
write("revenue_observations.csv", rv_obs, ["fiscal_year", "basis", "fund_group", "rev_id", "amount", "book_id", "page", "printed_name", "corrected"])
revenue = series(rv_obs, ["rev_id", "fund_group"])
for r in revenue:
    d = REVS.get(r["rev_id"], {})
    r["rev_name"] = d.get("name", r["rev_id"])
    r["category"] = d.get("category", "")
    # some books print as one line what others split (e.g. hotel tax inside sales tax before
    # 2008): `comparable_id` is the series that is the same thing in every year
    r["comparable_id"] = ROLLUP.get(r["rev_id"], r["rev_id"])
revenue.sort(key=lambda r: (r["fiscal_year"], r["basis"], r["fund_group"], r["category"], r["rev_id"]))
write("revenue.csv", revenue, ["fiscal_year", "basis", "fund_group", "rev_id", "rev_name", "category", "comparable_id", "amount", "source_book", "source_page", "n_books", "books_agree"])

# ------------------------------------------------- department detail and positions
det_obs, pos_obs = [], []
for b in BOOKS:
    for r in read(os.path.join(EX, b, "department_detail.csv")):
        m = DT_ALIAS.get((b, "department_detail", r["dept_name"]))
        if m is None:
            miss("detail_department", (b, r["dept_name"]))
            continue
        if not m[0]:  # the 2019 book's CITYWIDE summary page: not a department
            continue
        det_obs.append({"fiscal_year": r["fiscal_year"], "basis": r["basis"], "dept_id": m[0], "sub_unit": m[1], "table": r["table"],
                        "dim1": r["dim1"], "dim2": r["dim2"], "amount": int(float(r["amount"])), "book_id": b, "page": r["page"],
                        "printed_dept": r["dept_name"]})
    for r in read(os.path.join(EX, b, "positions.csv")):
        r = correct(b, "positions", r)
        m = DT_ALIAS.get((b, "positions", r["dept_name"]))
        if m is None:
            miss("positions_department", (b, r["dept_name"]))
            continue
        try:
            fte = float(r["fte"])
        except ValueError:
            continue
        pos_obs.append({"fiscal_year": r["fiscal_year"], "basis": r["basis"], "dept_id": m[0], "sub_unit": m[1], "program": r["program"],
                        "fund": r["fund"], "position_title": r["position_title"], "pay_grade": r["pay_grade"], "fte": fte,
                        "book_id": b, "page": r["page"], "printed_dept": r["dept_name"], "corrected": r.get("corrected", "")})

# Detail and positions are only published from a fiscal year's own book (its budget-year
# columns), plus the prior-year columns as context; no cross-book selection is attempted.
write("department_detail.csv", det_obs, ["fiscal_year", "basis", "dept_id", "sub_unit", "table", "dim1", "dim2", "amount", "book_id", "page", "printed_dept"])
write("positions.csv", pos_obs, ["fiscal_year", "basis", "dept_id", "sub_unit", "program", "fund", "position_title", "pay_grade", "fte", "book_id", "page", "printed_dept", "corrected"])

fte_obs = collections.defaultdict(float)
for p in pos_obs:
    fte_obs[(p["dept_id"], p["fiscal_year"], p["basis"], p["book_id"])] += p["fte"]
fte_rows = [{"dept_id": k[0], "fund_group": "all_funds", "fiscal_year": k[1], "basis": k[2], "amount": round(v, 2), "book_id": k[3], "page": ""}
            for k, v in fte_obs.items()]
fte = series(fte_rows, ["dept_id", "fund_group"])
for r in fte:
    r["fte"] = round(r.pop("amount"), 2)
    r["dept_name"] = DEPTS.get(r["dept_id"], {}).get("name", r["dept_id"])
fte.sort(key=lambda r: (r["fiscal_year"], r["basis"], r["dept_id"]))
write("positions_by_department.csv", fte, ["fiscal_year", "basis", "dept_id", "dept_name", "fte", "source_book", "n_books", "books_agree"])

# --------------------------------------------------------------------- context
CTX = os.path.join(ROOT, "data", "raw", "context")
pop = {r["year"]: r["population_july1"] for r in read(os.path.join(CTX, "orleans_parish_population_2000_2025.csv"))}


def cpi(fn):
    rows = read(os.path.join(CTX, fn))
    if not rows:
        return {}
    val = [c for c in rows[0] if c.startswith("annual_avg")][0]
    return {r["year"]: (r[val], r.get("months_available", "")) for r in rows}


us, south = cpi("cpi_u_us_city_avg_annual_avg_CUUR0000SA0.csv"), cpi("cpi_u_south_region_annual_avg_CUUR0300SA0.csv")
ctx = [{"year": y, "population": pop.get(y, ""), "cpi_us": us.get(y, ("", ""))[0], "cpi_south": south.get(y, ("", ""))[0],
        "cpi_months": us.get(y, ("", ""))[1]} for y in sorted(set(pop) | {y for y in us if int(y) >= 2000})]
write("context.csv", ctx, ["year", "population", "cpi_us", "cpi_south", "cpi_months"])

# ---------------------------------------------------------------------- report
report["books"] = BOOKS
report["corrections_applied"] = [{"correction": CORR[i]["book_id"] + "/" + CORR[i]["file"] + " " + CORR[i]["match_fiscal_year"] + " " + CORR[i]["match_basis"], "rows": n} for i, n in sorted(corr_used.items())]
report["corrections_unused"] = [c["book_id"] + "/" + c["file"] + " " + c["match_fiscal_year"] + " " + c["match_basis"] for i, c in enumerate(CORR) if i not in corr_used]
report["unmapped"] = {k: sorted(v) for k, v in report["unmapped"].items()}
tot = collections.defaultdict(int)
for r in spending:
    tot[(r["fiscal_year"], r["basis"], r["fund_group"])] += r["amount"]
report["spending_totals"] = {f"{k[0]} {k[1]} {k[2]}": v for k, v in sorted(tot.items())}
with open(os.path.join(OUT, "build_report.json"), "w") as f:
    json.dump(report, f, indent=1)

for k, v in report["rows"].items():
    print(f"{k:32} {v:>8,}")
for k, v in report["unmapped"].items():
    print(f"UNMAPPED {k}: {len(v)}")
print("corrections applied:", report["corrections_applied"])
if report["corrections_unused"]:
    print("corrections that matched nothing:", report["corrections_unused"])
sys.exit(1 if report["unmapped"] else 0)
