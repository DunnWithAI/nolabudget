#!/usr/bin/env python3
"""Extract tables from the City of New Orleans 2017 Adopted Operating Budget book.

Run from anywhere:   .venv/bin/python scripts/extract/extract_2017.py

Reads   data/raw/nola-gov/2017/2017-adopted-budget.pdf  (read-only)
Writes  data/extracted/2017-operating-adopted/{department_expenditures,revenues,
        department_detail,positions,printed_totals}.csv, workyears_citywide.csv (extra), checks.json

Method: PyMuPDF word boxes -> lines (cluster on y) -> label / numeric cells.  Numbers are
assigned to columns by x-position against the header row of each table (nearest header
centre), never by their order in the text stream.  No amount is typed in by hand; the only
hand-written data are page ranges, department-name aliases used for cross-checks and a few
label spelling repairs for PDF kerning artefacts.

Layout: the citywide statements (revenues, expenditures by department, work years) are 2009-2017 (2010-2017 for the expenditure statements)-year
tables; the department sections have one DEPARTMENTAL BUDGET SUMMARY (by type, by fund), PROGRAM DETAIL,
EXPENDITURE SUMMARY and PERSONNEL SUMMARY per department (the same family as the 2012 pilot).  Code started
from extract_2012.py, with the statement-of-revenues parser rebuilt around a running stack of rows and
sub-totals (as in extract_2019.py).

Output shape follows data/extracted/SCHEMA.md.  Deviations / extensions (also in checks.json):
  * department_detail.table also takes `by_program_type` (the "Program Detail" table: 2017
    adopted dollars per program split by Personal Services / Other Operating / Debt Service;
    dim2 = "<fund heading> | <type>").  `by_program` is the "Expenditure Summary" table
    (4 year columns, dim2 = fund heading).
  * `positions.csv` is the per-department Personnel Summary (titles; 3 columns).  The citywide
    "Work Years" table goes to the extra file workyears_citywide.csv.
"""
import csv
import json
import re
import sys
from collections import defaultdict, namedtuple
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parents[2]
BOOK_ID = "2017-operating-adopted"
BOOK_FY = 2017
Y0, Y1 = BOOK_FY - 1, BOOK_FY      # the two fiscal years of the p.50 / p.51 / p.78 summary tables
PDF = ROOT / "data/raw/nola-gov/2017/2017-adopted-budget.pdf"
OUT = ROOT / "data/extracted" / BOOK_ID

# ---- book-specific page map (1-based PDF pages; verified by sanity asserts in main) -------
P_CITYWIDE_SUMMARY = 45            # CITYWIDE departmental budget summary (4 year columns)
P_SUMMARY_TABLES = 50              # General / Non-General revenue and expenditure summary (2016, 2017 adopted)
P_SUMMARY_MAYOR_ETC = 51           # sub-office split of Mayor, CAO, Health, Miscellaneous, OCD (2016, 2017 adopted)
REV_PAGES = range(69, 75)
EXP_PAGES = range(75, 78)
P_FUND_BY_DEPT = 78                # department x 14 funds, 2017 adopted (cross-check only)
N_REV_COLS, N_EXP_COLS, N_FTE_COLS = 9, 8, 9      # year columns of the statements (the 2017 book's expenditure statements start at 2010)
FTE_PAGES = range(79, 90)
DEPT_PAGES = range(120, 626)       # department sections run to the Enterprise Funds, then glossary

# FY2016: the statement and the Mayor tables print it as Adopted, p.45 / the department summaries as Actual (values agree with p.45)
MAYOR_SKIP_FYS = set()

# Dept-section names -> citywide-statement names (cross-check only; output keeps printed names)
DEPT_ALIASES = {
    "CHIEF ADMINISTRATIVE OFFICE": "CAO",
    "PARKWAY": "Parks & Parkways",
    "HISTORIC DIST LANDMARKS COMM.": "HDLC",
    "VIEUX CARRE COMMISSION": "VCC",
    "ALCOHOLIC BEVERAGE CONTR BRD.": "Alcoholic Bvg Ctrl. Bd.",
    "CITY PLANNING COMMISSION": "City Planning Comm.",
    "N O MOSQUITO CONTROL BRD.": "Mosquito Control Bd.",
    "N O MUSEUM OF ART": "Museum of Art",
    "OFFICE OF INSPECTOR GENERAL": "General Services",
    "OFFICE OF INDEP POLICE MONITOR": "Office of Ind. Police Mon.",
    "OFFICE OF COMM DEVELOPMENT": "Office of Comm. Dev.",
    "WORKFORCE INVESTMENT": "Workforce Invst. Act",
    "ECONOMIC DEVELOPMENT FUND": "Economic Dev. Fund",
    "NEIGHBORHOOD HOUSING IMPROVMNT": "N’hood Hsg Imprv. Fnd",
    "SHERIFF": "Criminal Sheriff",
    "CLERK OF CRIMINAL DIST COURT": "Clrk of Crim. Dist. Crt",
}

# PDF kerning splits words ("Parkw ays", "V EHICLE"); the gap is indistinguishable from a real space, so
# adjacent alphabetic tokens are re-joined when (a) the first token is a single letter V/Y/A/W/T or ends in
# 'w' and (b) the joined word occurs >= 2 times elsewhere in the book's text (VOCAB, built in main()).
# Every repair is logged to checks.json ("label_repairs").
VOCAB = {}
REPAIRS = set()


Tok = namedtuple("Tok", "text x0 x1 y0 y1")
Tok.cx = property(lambda t: (t.x0 + t.x1) / 2)


# ======================================================================================
# generic helpers
# ======================================================================================
def get_lines(page, tol=4.0):
    """Word boxes -> list of lines (list of Tok, x-sorted).  '|' cell borders dropped."""
    ws = [Tok(w[4], w[0], w[2], w[1], w[3]) for w in page.get_text("words") if w[4].strip() not in ("|", "")]
    # drop the "Page NNN" footer tokens (they can share a y-band with the last table row)
    foot = [k for k, t in enumerate(ws) if t.text == "Page" and t.y0 > page.rect.height - 60]
    drop = set(foot) | {k2 for k in foot for k2, u in enumerate(ws)
                        if re.fullmatch(r"\d+", u.text) and u.y0 > page.rect.height - 60 and abs(u.y0 - ws[k].y0) < 3 and 0 < u.x0 - ws[k].x1 < 12}
    ws = [t for k, t in enumerate(ws) if k not in drop]
    ws.sort(key=lambda t: (t.y0 + t.y1) / 2)
    lines, cur, ys = [], [], []
    for t in ws:
        yc = (t.y0 + t.y1) / 2
        if cur and yc - sum(ys) / len(ys) > tol:
            lines.append(sorted(cur, key=lambda a: a.x0))
            cur, ys = [], []
        cur.append(t)
        ys.append(yc)
    if cur:
        lines.append(sorted(cur, key=lambda a: a.x0))
    return [l for l in lines if not re.fullmatch(r"Page \d+", " ".join(t.text for t in l))]


def ltext(toks):
    return " ".join(t.text for t in toks)


def clean_label(s):
    toks = re.sub(r"\s+", " ", s).strip().split(" ")
    out, i = [], 0
    while i < len(toks):
        if i + 1 < len(toks):
            a, b = toks[i], toks[i + 1]
            if a.isalpha() and b.isalpha() and (len(b) >= 2 or a.endswith(("w", "W"))) and (a.isupper() == b.isupper() or len(a) == 1) \
                    and ((len(a) == 1 and a.upper() in "VYAWT") or (a.endswith(("w", "W")) and len(b) <= 4)) \
                    and VOCAB.get((a + b).lower(), 0) >= 2:
                REPAIRS.add("%s %s -> %s%s" % (a, b, a, b))
                out.append(a + b)
                i += 2
                continue
        out.append(toks[i])
        i += 1
    return " ".join(out)


DASHES = {"-", "–", "—", "--"}


def parse_num(t, decimal=False):
    """int (money) / Decimal (decimal=True, FTE), 'DASH', or None when not a number."""
    s = t.replace("$", "")
    if s in DASHES:
        return "DASH"
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("-") and len(s) > 1:
        neg, s = True, s[1:]
    if decimal:
        if re.fullmatch(r"\d{1,3}(,\d{3})*\.\d+|\d+\.\d+|\d+", s):
            v = Decimal(s.replace(",", ""))
            return -v if neg else v
        return None
    if re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", s):
        v = int(s.replace(",", ""))
        return -v if neg else v
    return None


def nearest(x, anchors):
    return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x))


def split_row(line, anchors, tol_left=30, decimal=False):
    """Split a line into (label_tokens, {col_idx: value|'DASH'}, problems).

    Data = the trailing run of numeric/dash tokens whose centre lies within tol_left of the
    leftmost column; each is assigned to the nearest column anchor by x.  Lone '$' tokens and
    trailing variance percentages are ignored.
    """
    toks = [t for t in line if t.text != "$"]
    while toks and "%" in toks[-1].text:
        toks.pop()
    i = len(toks)
    while i > 0 and parse_num(toks[i - 1].text, decimal) is not None:
        i -= 1
    lo = min(anchors) - tol_left
    while i < len(toks) and toks[i].cx < lo:
        i += 1
    label, run = toks[:i], toks[i:]
    data, problems, seen = {}, [], {}
    ref = sorted((t.y0 + t.y1) / 2 for t in (label or line))[len(label or line) // 2]   # row's typical y
    for t in run:
        c = nearest(t.cx, anchors)
        if c in seen:
            # two cells fell into one column (a stray cell from the line below/above): keep the one on the row's y
            problems.append("two numbers in column %d (kept the one nearer the row baseline)" % c)
            if abs((t.y0 + t.y1) / 2 - ref) >= abs((seen[c].y0 + seen[c].y1) / 2 - ref):
                continue
        seen[c] = t
        data[c] = parse_num(t.text, decimal)
    return label, data, problems


def header_cols(lines, start=0, extra_ok=()):
    """Read a multi-line column header (words from a closed vocabulary) beginning at lines[start].
    Returns (header_tokens, index_of_first_non_header_line)."""
    vocab = {"Program", "Actual", "Adopted", "Proposed", "No.", "Pay", "Grade", "Personal", "Services",
             "Other", "Operating", "Debt", "Service", "Total", "Source", "Department", "Variance",
             "-", "Estimated", "/"} | set(extra_ok)
    toks, i = [], start
    while i < len(lines):
        if all(t.text in vocab or re.fullmatch(r"20\d\d", t.text) for t in lines[i]):
            toks += lines[i]
            i += 1
        else:
            break
    return toks, i


def year_basis_columns(htoks, expect=None):
    """Pair year tokens with basis words (both left->right) -> [(cx, fiscal_year, basis)]."""
    years = sorted([t for t in htoks if re.fullmatch(r"20\d\d", t.text)], key=lambda t: t.cx)
    bases = sorted([t for t in htoks if t.text in ("Actual", "Adopted", "Proposed", "Estimated")], key=lambda t: t.cx)
    # statement headers also carry the "2011 - 2012" variance label: keep the first len(bases) years
    # that sit left of the Variance word if present
    var = [t for t in htoks if t.text == "Variance"]
    if var:
        years = [t for t in years if t.cx < var[0].x0 + 5]
        years = years[: len(bases)]
    if len(years) != len(bases) or (expect and len(years) != expect):
        return None
    return [(y.cx, int(y.text), b.text.lower()) for y, b in zip(years, bases)]


class Out:
    """Collects all outputs and the check results."""

    def __init__(self):
        self.dept_exp, self.rev, self.detail, self.pos, self.tot, self.fte = [], [], [], [], [], []
        self.checks = []
        self.unparsed = []   # {page, what, why}
        self.notes = []
        self.problems = []   # row-level parse problems
        self.fallbacks = defaultdict(int)

    def unp(self, page, what, why):
        self.unparsed.append({"page": page, "what": what, "why": why})

    def total(self, fy, basis, scope, label, amount, page, **private):
        # private keys (leading underscore) are used by the checks only and never written
        self.tot.append(dict(book_id=BOOK_ID, fiscal_year=fy, basis=basis, scope=scope, label=label,
                             amount=amount, page=page, **private))

    def check(self, tier, kind, scope, fy, basis, extracted, printed, page, note=""):
        diff = extracted - printed
        if isinstance(diff, Decimal):
            status = "exact" if abs(diff) < Decimal("0.005") else "mismatch"
        else:
            status = "exact" if diff == 0 else ("within_1" if abs(diff) <= 1 else "mismatch")
        if status == "mismatch" and not isinstance(diff, Decimal) and abs(diff) <= 5:
            note = (note + "; " if note else "") + "|diff| <= $5: consistent with the book rounding cents in actual-year cells"
        self.checks.append(dict(tier=tier, kind=kind, scope=scope, fiscal_year=fy, basis=basis,
                                extracted=str(extracted) if isinstance(extracted, Decimal) else extracted,
                                printed=str(printed) if isinstance(printed, Decimal) else printed,
                                diff=str(diff) if isinstance(diff, Decimal) else diff,
                                status=status, page=page, note=note))


# ======================================================================================
# Tier 1a: statement of revenues (pp.65-70)
# ======================================================================================
WRAP_END = {"&", "OF", "THE", "AND", "-", "TRUST", "FUND"}     # a label line ending in one of these continues on the next line
WRAP_GAP = 11.5                                                   # max y distance (pt) between wrapped label lines


def line_y(ln):
    return sum((t.y0 + t.y1) / 2 for t in ln) / len(ln)


def is_total_label(lab):
    return bool(re.search(r"\bTOTAL\b|SUB-TOTAL", lab.upper()))


def stack_total(stack, printed):
    """printed: {col_idx: value}.  Smallest suffix of the running stack (rows and already-rolled totals) whose
    column sums reproduce the most printed cells.  Returns (k, sums, n_matching)."""
    cols = sorted(printed)
    best, run = None, defaultdict(int)
    for k in range(1, len(stack) + 1):
        for c in cols:
            run[c] += stack[-k]["vals"].get(c, 0)
        m = sum(1 for c in cols if run[c] == printed[c])
        if best is None or m > best[0]:
            best = (m, k, dict(run))
        if m == len(cols):
            break
    return best[1], best[2], best[0]


def parse_revenues(doc, out):
    group, fund, cat = "general_fund", "General Fund", None
    stacks = {"general_fund": [], "other_funds": []}
    cols = None
    blocks = []
    prev_heading = False            # previous logical line was a heading with no data
    prefix = ""                     # wrapped label prefix carried to the next line
    for pno in REV_PAGES:
        lines = get_lines(doc[pno - 1])
        ys = [line_y(l) for l in lines]
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            nyr = [t for t in ln if re.fullmatch(r"20\d\d", t.text)]
            if len(nyr) >= 5:                          # header: years line, then Source Actual/Adopted line
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, N_REV_COLS)
                if cols is None:
                    out.unp(pno, "revenue header", "could not pair years with basis words")
                i = j
                prev_heading, prefix = False, ""
                continue
            y = ys[i]
            i += 1
            if re.match(r"STATEMENT OF REVENUES|Discretionary and Other|\*", text, re.I):
                continue
            if text.upper().strip() == "OTHER FUNDS":
                group, fund, cat = "other_funds", None, None
                prev_heading, prefix = False, ""
                continue
            if text.upper().strip() == "GENERAL FUND":
                group, fund, cat = "general_fund", "General Fund", None
                continue
            if cols is None:
                continue
            label, data, probs = split_row(ln, [c[0] for c in cols])
            lab = clean_label(ltext(label))
            if not data:
                full = clean_label((prefix + " " + lab).strip())
                nxt_gap = (ys[i] - y) if i < len(lines) else 99
                if lab.split()[-1:] and (lab.split()[-1] in WRAP_END or lab.upper().startswith(("TOTAL ", "SUB-TOTAL"))) and nxt_gap < WRAP_GAP:
                    prefix = full                      # wrapped label: continues on the next line
                    continue
                prefix = ""
                if group == "general_fund":
                    cat = full
                elif prev_heading and fund:
                    cat = full                         # second heading in a row: category inside the fund
                else:
                    fund, cat = full, None
                prev_heading = True
                continue
            lab = clean_label((prefix + " " + lab).strip())
            prefix = ""
            prev_heading = False
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if is_total_label(lab):
                vals = {c: (0 if v == "DASH" else v) for c, v in data.items()}
                memo = bool(re.match(r"TOTAL (DISCRETIONARY|ALL FUNDS)", lab.upper()))
                blocks.append(dict(label=lab, page=pno, group=group, fund=fund, cat=cat, vals=vals, cols=cols, memo=memo,
                                   stack_before=[dict(s) for s in stacks[group]]))
                if not memo:
                    k, sums, m = stack_total(stacks[group], vals)
                    blocks[-1].update(k=k, sums=sums, covers=[s["label"] for s in stacks[group][-k:]])
                    aggregate = any(s["total"] for s in stacks[group][-k:])
                    del stacks[group][-k:]
                    stacks[group].append(dict(vals=vals, label=lab, total=True))
                    blocks[-1]["aggregate"] = aggregate
                for c, v in data.items():
                    if v != "DASH":
                        out.total(cols[c][1], cols[c][2], None, lab, v, pno)
                        out.tot[-1]["_rev_block"] = len(blocks) - 1
                continue
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.rev.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                    fund_group=group, fund_name=fund or "",
                                    category=(cat or "") if group == "general_fund" else (cat or ""), line_name=lab, amount=v, page=pno))
            stacks[group].append(dict(vals={c: (0 if v == "DASH" else v) for c, v in data.items()}, label=lab, total=False))
    # -- scopes + checks
    gf_total = other_total = selfgen = None
    for b in blocks:
        U = b["label"].upper()
        if U.startswith("TOTAL GENERAL FUND"):
            gf_total = b
        elif U.startswith("TOTAL OTHER FUNDS"):
            other_total = b
        elif U.startswith("TOTAL SELF-GENERATED"):
            selfgen = b
    for k, b in enumerate(blocks):
        U = b["label"].upper()
        if U.startswith("TOTAL ALL FUNDS"):
            b["scope"] = "revenues/all_funds"
        elif U.startswith("TOTAL DISCRETIONARY"):
            b["scope"] = "revenues/discretionary"
        elif b["group"] == "general_fund":
            if U.startswith("TOTAL GENERAL FUND"):
                b["scope"] = "revenues/General Fund"
            elif b.get("aggregate"):
                b["scope"] = "revenues/General Fund/" + b["label"]
            else:
                b["scope"] = "revenues/General Fund" + ("/" + b["cat"] if b["cat"] else "")
        else:
            b["scope"] = "revenues/%s" % (b["fund"] or b["label"]) if not U.startswith("TOTAL OTHER FUNDS") else "revenues/other_funds"
        for c, printed in b["vals"].items():
            cx, fy, basis = b["cols"][c]
            if b["memo"]:
                if U.startswith("TOTAL ALL FUNDS"):
                    s = (gf_total["vals"].get(c, 0) if gf_total else 0) + (other_total["vals"].get(c, 0) if other_total else 0)
                    note = "TOTAL GENERAL FUND + TOTAL OTHER FUNDS (as printed)"
                else:
                    s = (gf_total["vals"].get(c, 0) if gf_total else 0) + (selfgen["vals"].get(c, 0) if selfgen else 0)
                    note = "TOTAL GENERAL FUND + printed TOTAL SELF-GENERATED (memo total outside the Other Funds stack)"
            else:
                s = b["sums"].get(c, 0)
                note = "sum of the last %d item(s) of the running statement stack (rows / sub-totals not yet rolled into a total): %s" % (
                    b["k"], "; ".join(b["covers"]) if len(b["covers"]) <= 8 else "%d items from %r to %r" % (len(b["covers"]), b["covers"][0], b["covers"][-1]))
            out.check(1, "revenue_total", b["scope"], fy, basis, s, printed, b["page"], note)
    for r in out.tot:
        if "_rev_block" in r:
            r["scope"] = blocks[r.pop("_rev_block")]["scope"]
    out.rev_blocks = blocks


# ======================================================================================
# Tier 1b: statements of expenditures by department (pp.68-70)
# ======================================================================================
def parse_dept_expenditures(doc, out):
    group = None
    cols = None
    for pno in EXP_PAGES:
        lines = get_lines(doc[pno - 1])
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            if re.fullmatch(r"20\d\d 20\d\d.*", text) and sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, N_EXP_COLS)
                if cols is None:
                    out.unp(pno, "expenditure header", "could not pair years with basis words")
                i = j
                continue
            i += 1
            m = re.match(r"(General Fund|Other Fund|Total) Expenditures", text, re.I)
            if m:
                group = {"general fund": "general_fund", "other fund": "other_funds", "total": "all_funds"}[m.group(1).lower()]
                continue
            if cols is None or group is None or text.startswith("STATEMENT OF"):
                continue
            label, data, probs = split_row(ln, [c[0] for c in cols])
            lab = clean_label(ltext(label))
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if lab == "Total":
                for c, v in data.items():
                    if v != "DASH":
                        out.total(cols[c][1], cols[c][2], "department_expenditures/" + group, lab, v, pno)
                continue
            if not data and not lab:
                continue
            if not data:
                out.unp(pno, "line %r" % text, "no numbers in expenditure statement row")
                continue
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                         fund_group=group, dept_code="", dept_name=lab, amount=v, page=pno))
    # checks: department rows vs printed 'Total'; general + other = all
    for group in ("general_fund", "other_funds", "all_funds"):
        sums = defaultdict(int)
        for r in out.dept_exp:
            if r["fund_group"] == group:
                sums[(r["fiscal_year"], r["basis"])] += r["amount"]
        for t in out.tot:
            if t["scope"] == "department_expenditures/" + group:
                out.check(1, "dept_statement_total", t["scope"], t["fiscal_year"], t["basis"],
                          sums[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
    by = defaultdict(dict)
    for r in out.dept_exp:
        by[(r["dept_name"], r["fiscal_year"], r["basis"])][r["fund_group"]] = (r["amount"], r["page"])
    names = {k[0] for k in by}
    keys = {(k[1], k[2]) for k in by}
    for (n, fy, b), g in sorted(by.items()):
        gf, of, af = (g.get(x, (0, 0)) for x in ("general_fund", "other_funds", "all_funds"))
        out.check(1, "general+other=all", "department_expenditures/" + n, fy, b, gf[0] + of[0], af[0],
                  af[1] or gf[1] or of[1])
    # grand totals: GF total + other total = all-funds total (printed)
    pt = {(t["scope"], t["fiscal_year"], t["basis"]): t for t in out.tot if t["scope"].startswith("department_expenditures/")}
    for (fy, b) in sorted(keys):
        g, o, a = (pt.get(("department_expenditures/" + x, fy, b)) for x in ("general_fund", "other_funds", "all_funds"))
        if g and o and a:
            out.check(1, "general+other=all (printed totals)", "department_expenditures/Total", fy, b,
                      g["amount"] + o["amount"], a["amount"], a["page"])


# ======================================================================================
# Tier 1c: printed summary tables p.50 / p.51 / p.78 (cross-check material)
# ======================================================================================
def split_two_sided(lines):
    """Two side-by-side tables (General Fund | Non-General Fund) with two value columns each (Y0, Y1).  Returns rows
    (section, side, label, [vY0, vY1], title) where side is 'general_fund' / 'other_funds', section is 'revenue' /
    'expenditure' and title is the last caption line.  The x split between the halves is the second year-header
    token's x0 + 50 (the right-hand labels start ~55 pt right of the left table's last year column).  A label that wraps
    onto two lines (label-only line above the numbers) is joined per side."""
    rows, section, split, title = [], None, None, None
    pend = None            # (y, left_tokens, right_tokens) of a label-only line awaiting its numbers
    for ln in lines:
        text = ltext(ln)
        heads = [t for t in ln if t.text in ("Source", "Department")]
        yrs = sorted([t for t in ln if re.fullmatch(r"20\d\d", t.text)], key=lambda t: t.x0)
        if len(heads) == 2:
            section = "revenue" if heads[0].text == "Source" else "expenditure"
        if len(yrs) == 4:
            split = yrs[1].x0 + 50
        if len(heads) == 2 or len(yrs) == 4 or text.startswith("General Fund Revenue") or text.startswith("General Fund Expenditures"):
            pend = None
            continue
        if not any(parse_num(t.text) is not None for t in ln):
            y = (ln[0].y0 + ln[0].y1) / 2
            L, R = ([t for t in ln if t.x0 < split], [t for t in ln if t.x0 >= split]) if split is not None else ([], [])
            if L and R and ltext(L) == ltext(R):       # the same wrapped label printed on both halves
                pend = (y, L, R)
            else:
                pend = None
                title = text.rstrip("*").strip() if not text.startswith("Page") else title
            continue
        if split is None:
            continue
        y = (ln[0].y0 + ln[0].y1) / 2
        sides = (("general_fund", [t for t in ln if t.x0 < split]), ("other_funds", [t for t in ln if t.x0 >= split]))
        for k, (side, toks) in enumerate(sides):
            toks = [t for t in toks if t.text != "$"]
            nums = toks[-2:]
            if len(nums) < 2 or any(parse_num(t.text) is None for t in nums):
                continue
            lab_toks = toks[:-2]
            if pend and abs(y - pend[0]) <= 13:
                lab_toks = (pend[1] if k == 0 else pend[2]) + lab_toks
            lab = clean_label(ltext(lab_toks))
            lab = re.sub(r"\*+$", "", lab).strip()
            if not lab:
                continue
            vals = [0 if parse_num(t.text) == "DASH" else parse_num(t.text) for t in nums]
            rows.append((section, side, lab, vals, title))
        pend = None
    return rows


def parse_p50_totals(doc, out):
    """p.50: totals of the General / Non-General revenue and expenditure summary (2016 and 2017 adopted), plus the
    department rows of the expenditure half (cross-checked in dept_checks)."""
    rows = split_two_sided(get_lines(doc[P_SUMMARY_TABLES - 1]))
    out.p50, out.p50_dept = {}, []
    for section, side, lab, vals, _ in rows:
        if lab == "Total":
            for fy, v in zip((Y0, Y1), vals):
                name = "revenue" if section == "revenue" else "expenditure"
                out.total(fy, "adopted", "summary_p50/%s/%s" % (name, side), "Total (%s)" % ("General Fund" if side == "general_fund" else "Non-General Fund"), v, P_SUMMARY_TABLES)
                out.p50[(name, side, fy)] = v
        elif section == "expenditure":
            out.p50_dept.append((side, lab, vals))
    if len(out.p50) != 8:
        out.unp(P_SUMMARY_TABLES, "General/Non-General summary totals", "expected eight totals, got %d" % len(out.p50))
    # p.51: sub-office split of Mayor, CAO, Health, Miscellaneous, OCD (cross-check for sub-office program pages)
    out.p51 = []
    for section, side, lab, vals, title in split_two_sided(get_lines(doc[P_SUMMARY_MAYOR_ETC - 1])):
        out.p51.append((title, side, lab, vals))
    # p.78: department x 14 funds (2017 adopted)
    out.p78 = []
    prefix, prefix_y = "", None
    for ln in get_lines(doc[P_FUND_BY_DEPT - 1]):
        nums = [t for t in ln if parse_num(t.text) is not None]
        y = line_y(ln)
        if len(nums) == 14:
            lab = clean_label(ltext([t for t in ln if t not in nums]))
            if prefix and prefix_y is not None and y - prefix_y <= 13:
                lab = clean_label(prefix + " " + lab)
            out.p78.append((lab, [0 if parse_num(t.text) == "DASH" else parse_num(t.text) for t in nums]))
            prefix = ""
        elif len(nums) >= 3:
            out.unp(P_FUND_BY_DEPT, "line %r" % ltext(ln), "expected 14 numeric cells, got %d" % len(nums))
        elif not nums and ln[0].x0 < 40 and not ltext(ln).startswith(("This table", "departmental", "Department", "General", "SELF", "Downtown", "STATE", "Wisner")):
            prefix, prefix_y = ltext(ln), y            # a department name wrapped onto two lines
        else:
            prefix = ""


# ======================================================================================
# Tier 2/3: department sections
# ======================================================================================
TITLE_RE = re.compile(r"^(.*?)\s*(PROGRAM DETAIL|EXPENDITURE SUMMARY|PERSONNEL SUMMARY)$")


def classify_line(ln):
    """Table-title line -> ('summary'|'detail'|'expsum'|'personnel', dept_name or None) else None."""
    t = re.sub(r"\s+", " ", ltext(ln)).strip()
    if t == "DEPARTMENTAL BUDGET SUMMARY":
        return "summary", None
    m = TITLE_RE.match(t)
    if m and m.group(1):
        kind = {"PROGRAM DETAIL": "detail", "EXPENDITURE SUMMARY": "expsum", "PERSONNEL SUMMARY": "personnel"}[m.group(2)]
        return kind, m.group(1)
    return None


def segments(lines):
    """Split a page into table segments at title lines (several tables can share a page).
    -> [(kind, dept, lines_after_title)]; a 'summary' title takes its name from the following line(s)."""
    marks = [(i, classify_line(ln)) for i, ln in enumerate(lines)]
    marks = [(i, c) for i, c in marks if c]
    segs = []
    for n, (i, (kind, dept)) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body = lines[i + 1:end]
        if kind == "summary":
            j = 0
            name = []
            while j < len(body) and not any(x.text in ("Actual", "Adopted") for x in body[j]):
                name.append(ltext(body[j]))
                j += 1
            dept, body = clean_label(" ".join(name)), body[j:]
        segs.append((kind, dept, body))
    return segs


def parse_summary_page(lines, start, pno, dept, out, rows_by_type, rows_by_fund):
    htoks, j = header_cols(lines, start)
    cols = year_basis_columns(htoks, 4)
    if cols is None:
        out.unp(pno, "%s budget summary" % dept, "header columns not recognised")
        return
    anchors = [c[0] for c in cols]
    section = None
    for ln in lines[j:]:
        text = clean_label(ltext(ln))
        if text == "EXPENDITURES":
            section = "by_type"
            continue
        if text == "SOURCE OF FUNDING":
            section = "by_fund"
            continue
        label, data, probs = split_row(ln, anchors, tol_left=40)
        lab = clean_label(ltext(label))
        if not data:
            out.unp(pno, "%s: %r" % (dept, text), "unrecognised line in budget summary")
            continue
        for p in probs:
            out.problems.append(dict(page=pno, line=text, problem=p))
        if section is None:             # citywide p.50 prints no "EXPENDITURES" / "SOURCE OF FUNDING" captions
            section = "by_type"
        if lab.startswith("TOTAL"):
            for c, v in data.items():
                out.total(cols[c][1], cols[c][2], "department_detail/%s/%s" % (dept, section), lab, v, pno)
            if section == "by_type":
                section = "by_fund"
            continue
        for c, v in data.items():
            if v == "DASH":
                continue
            row = dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2], dept_code="", dept_name=dept,
                       table=section, dim1=lab, dim2="", amount=v, page=pno)
            (rows_by_type if section == "by_type" else rows_by_fund).append(row)


FUND_HEAD = re.compile(r"^[A-Z0-9]{3,4} \S")
PROG_HEAD = re.compile(r"^\d{4}\b")


def parse_program_tables(kind, lines, start, pno, dept, out, state):
    """Program Detail (kind=detail: PS/OO/DS/Total columns) and Expenditure Summary (4 year columns)."""
    htoks, j = header_cols(lines, start)
    if kind == "detail":
        names = {"Services": "Personal Services", "Operating": "Other Operating", "Service": "Debt Service", "Total": "Total"}
        hdr = sorted([t for t in htoks if t.text in names], key=lambda t: t.cx)
        if [names[t.text] for t in hdr] != ["Personal Services", "Other Operating", "Debt Service", "Total"]:
            out.unp(pno, "%s program detail" % dept, "header columns not recognised")
            return
        cols = [(t.cx, BOOK_FY, "adopted", names[t.text]) for t in hdr]
    else:
        yb = year_basis_columns(htoks, 4)
        if yb is None:
            out.unp(pno, "%s expenditure summary" % dept, "header columns not recognised")
            return
        cols = [(a, fy, b, "") for a, fy, b in yb]
    anchors = [c[0] for c in cols]
    table = "by_program_type" if kind == "detail" else "by_program"
    for ln in lines[j:]:
        text = clean_label(ltext(ln))
        label, data, probs = split_row(ln, anchors, tol_left=40)
        lab = clean_label(ltext(label))
        if not data:
            if FUND_HEAD.match(text) and not PROG_HEAD.match(text):
                state["fund"] = text
            else:
                out.unp(pno, "%s: %r" % (dept, text), "unrecognised heading line in %s" % table)
            continue
        for p in probs:
            out.problems.append(dict(page=pno, line=text, problem=p))
        if lab.endswith("TOTAL") or (FUND_HEAD.match(lab) and not PROG_HEAD.match(lab)):
            # fund total (Program Detail prints it as the bare fund heading with numbers)
            fund = "" if lab == "DEPARTMENT TOTAL" else (lab[: -len(" TOTAL")] if lab.endswith(" TOTAL") else lab)
            for c, v in data.items():
                scope = "department_detail/%s/%s" % (dept, table) + ("/" + fund if fund else "")
                if kind == "detail":
                    scope += "/" + cols[c][3]
                out.total(cols[c][1], cols[c][2], scope, lab, v, pno)
            continue
        if not PROG_HEAD.match(lab):
            out.unp(pno, "%s: %r" % (dept, text), "row without program number")
            continue
        for c, v in data.items():
            if v == "DASH":
                continue
            dim2 = state["fund"] if kind == "expsum" else "%s | %s" % (state["fund"], cols[c][3])
            if kind == "detail" and cols[c][3] == "Total":
                continue        # the Total column is a derived sum: kept as a check, not as rows
            out.detail.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2], dept_code="",
                                   dept_name=dept, table=table, dim1=lab, dim2=dim2, amount=v, page=pno))
        if kind == "detail":
            tv = data.get(3)
            if tv is not None and tv != "DASH":
                out.detail_totals.append(dict(dept=dept, fund=state["fund"], prog=lab, page=pno, total=tv))


def merge_orphans(lines, anchors, maxdist=8.0):
    """The Enterprise-fund personnel tables print one FTE cell (or a whole TOTAL row's numbers) a few
    points above/below its label line.  Attach numeric-only lines to the nearest label line that has
    free columns, within maxdist points."""
    def isnum(t):
        return parse_num(t.text, True) not in (None, "DASH")

    def yc(l):
        return sum((t.y0 + t.y1) / 2 for t in l) / len(l)

    def cols(l):
        return {nearest(t.cx, anchors) for t in l if isnum(t) and t.cx >= min(anchors) - 35}

    out = [list(l) for l in lines]
    keep = [True] * len(out)
    for i, l in enumerate(out):
        if not keep[i] or not all(isnum(t) for t in l):
            continue
        for k in sorted((i - 1, i + 1), key=lambda k: abs(yc(out[k]) - yc(l)) if 0 <= k < len(out) else 1e9):
            if 0 <= k < len(out) and keep[k] and not all(isnum(t) for t in out[k]) and abs(yc(out[k]) - yc(l)) <= maxdist \
                    and cols(out[k]).isdisjoint(cols(l)):
                out[k] = sorted(out[k] + l, key=lambda t: t.x0)
                keep[i] = False
                break
    return [l for l, kp in zip(out, keep) if kp]


def parse_personnel_page(lines, start, pno, dept, out, state):
    htoks, j = header_cols(lines, start)
    yb = year_basis_columns(htoks, 3)
    grade = [t for t in htoks if t.text == "Grade"]
    if (yb is None or not grade) and state.get("hdr"):
        yb, gcx = state["hdr"]            # continuation page without its own header
    elif yb is None or not grade:
        out.unp(pno, "%s personnel summary" % dept, "header columns not recognised")
        return
    else:
        gcx = grade[0].cx
        state["hdr"] = (yb, gcx)
    anchors = [c[0] for c in yb]
    first_data = min(anchors) - 35
    for ln in merge_orphans(lines[j:], anchors):
        text = clean_label(ltext(ln))
        data, grade_tok, left = {}, [], []
        for t in ln:
            v = parse_num(t.text, decimal=True)
            if t.cx >= first_data and v is not None and v != "DASH":
                c = nearest(t.cx, anchors)
                if c in data:
                    out.problems.append(dict(page=pno, line=text, problem="two numbers in column %d" % c))
                data[c] = v
            elif abs(t.cx - gcx) <= 22 and t.x0 >= gcx - 40 and data == {} and t.text not in DASHES:
                grade_tok.append(t.text)
            else:
                left.append(t)
        lab = clean_label(ltext(left))
        if not data:
            first_x = ln[0].x0
            if first_x < 140 and FUND_HEAD.match(text) and not PROG_HEAD.match(text):
                state["fund"], state["prog"] = text, ""
                state["pend_rows"], state["pend_tots"] = [], []
            elif first_x < 140 and PROG_HEAD.match(text):
                state["prog"] = text
            elif state.get("last_pos") is not None and first_x >= 140:
                state["last_pos"]["position_title"] = clean_label(state["last_pos"]["position_title"] + " " + text)
                out.wrapped_titles += 1
            else:
                out.unp(pno, "%s: %r" % (dept, text), "unrecognised line in personnel summary")
            continue
        # total rows: "... TOTAL", or (Mayor sub-offices etc.) a bare program / fund heading carrying numbers
        heading_col = ln[0].x0 < 140 and not grade_tok
        if lab.endswith("TOTAL") or (heading_col and (PROG_HEAD.match(lab) or FUND_HEAD.match(lab))):
            base = lab[: -len(" TOTAL")] if lab.endswith(" TOTAL") else lab
            if lab == "DEPARTMENT TOTAL":
                scope, pk = "positions/%s" % dept, dict(_dept=dept, _fund=None, _prog=None)
            elif PROG_HEAD.match(lab):
                scope = "positions/%s/%s/%s" % (dept, state["fund"], base)
                pk = dict(_dept=dept, _fund=state["fund"], _prog=base)
            else:
                scope = "positions/%s/%s" % (dept, base)
                pk = dict(_dept=dept, _fund=base, _prog=None)
            n0 = len(out.tot)
            for c, v in data.items():
                out.total(yb[c][1], yb[c][2], scope, lab, v, pno, **pk)
            new_tots = out.tot[n0:]
            if pk["_prog"] is not None:
                state.setdefault("pend_tots", []).extend(new_tots)
            elif pk["_fund"] is not None:
                # fund total: if the fund heading line was lost at a page break, the rows since the last
                # fund total belong to this fund (the book prints the fund only in its total row)
                if pk["_fund"] != state["fund"]:
                    for r in state.get("pend_rows", []):
                        r["fund"] = pk["_fund"]
                    for t in state.get("pend_tots", []):
                        t["_fund"] = pk["_fund"]
                        t["scope"] = "positions/%s/%s/%s" % (dept, pk["_fund"], t["_prog"])
                    out.notes.append("p.%d %s: fund heading missing before program rows; fund taken from the printed total %r (was %r)"
                                     % (pno, dept, pk["_fund"], state["fund"]))
                    state["fund"] = pk["_fund"]
                state["pend_rows"], state["pend_tots"] = [], []
            state["last_pos"] = None
            continue
        if not state.get("prog"):
            out.unp(pno, "%s: %r" % (dept, text), "position row outside a program")
            continue
        for c, v in data.items():
            pos = dict(book_id=BOOK_ID, fiscal_year=yb[c][1], basis=yb[c][2], dept_code="", dept_name=dept,
                       program=state["prog"], fund=state["fund"], position_title=lab, pay_grade=" ".join(grade_tok),
                       fte=v, page=pno)
            out.pos.append(pos)
            state.setdefault("pend_rows", []).append(pos)
        state["last_pos"] = {"position_title": lab}
        # keep a handle to the rows just written so wrapped titles can be appended
        rows = out.pos[-len(data):]
        state["last_pos"] = _TitleProxy(rows)


class _TitleProxy(dict):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def __getitem__(self, k):
        return self.rows[0][k]

    def __setitem__(self, k, v):
        for r in self.rows:
            r[k] = v


def parse_departments(doc, out):
    out.detail_totals = []
    out.wrapped_titles = 0
    state_by = {}
    sections = []
    prev_last = None
    ent_start = next((p for p in DEPT_PAGES if get_lines(doc[p - 1]) and ltext(get_lines(doc[p - 1])[0]).strip() == "Enterprise Funds"), None)
    before_ent, ent_cur = set(), None      # department names seen before the Enterprise Funds section; current enterprise entity
    for pno in DEPT_PAGES:
        lines = get_lines(doc[pno - 1])
        segs = segments(lines)
        if not segs:
            # a Personnel Summary can run on to pages that carry neither title nor column header (OCD, pp.171-172)
            n_fte = sum(1 for l in lines if re.search(r"\d+\.\d\d \d+\.\d\d( \d+\.\d\d)?$", ltext(l)))
            if prev_last and prev_last[0] == "personnel" and n_fte >= 5:
                kind, dept = prev_last
                out.notes.append("p.%d: headerless continuation of the %s personnel summary (column header taken from the previous page)" % (pno, dept))
                sections.append((pno, kind, dept))
                parse_personnel_page(lines, 0, pno, dept, out, state_by[(kind, dept)])
                continue
            out.narr_pages.append(pno)
            prev_last = None
            continue
        in_ent = ent_start is not None and pno > ent_start
        fixed = []
        for kind, dept, body in segs:
            if in_ent and kind == "summary":
                ent_cur = dept
            elif in_ent and kind != "summary" and dept in before_ent and ent_cur:
                # a page inside the Enterprise Funds section whose printed title names an ordinary department (book misprint):
                # it carries the enterprise entity's rows, so it is filed under the entity of the preceding summary page
                out.notes.append("p.%d: page printed with the title %r sits in the Enterprise Funds section and continues the %s tables; filed under %r"
                                 % (pno, dept + " " + {"detail": "PROGRAM DETAIL", "expsum": "EXPENDITURE SUMMARY", "personnel": "PERSONNEL SUMMARY"}[kind], ent_cur, ent_cur))
                dept = ent_cur
            if not in_ent:
                before_ent.add(dept)
            fixed.append((kind, dept, body))
        segs = fixed
        prev_last = (segs[-1][0], segs[-1][1])
        for kind, dept, body in segs:
            sections.append((pno, kind, dept))
            if kind == "summary":
                parse_summary_page(body, 0, pno, dept, out, out.detail, out.detail)
            elif kind in ("detail", "expsum"):
                st = state_by.setdefault((kind, dept), {"fund": ""})
                parse_program_tables(kind, body, 0, pno, dept, out, st)
            else:
                st = state_by.setdefault((kind, dept), {"fund": "", "prog": "", "last_pos": None})
                parse_personnel_page(body, 0, pno, dept, out, st)
    out.sections = sections


# ---- department-level checks -------------------------------------------------------------
def dept_checks(out):
    D = defaultdict(list)
    for r in out.detail:
        D[(r["dept_name"], r["table"])].append(r)
    T = defaultdict(list)
    for t in out.tot:
        T[t["scope"]].append(t)

    def sum_rows(rows, **kw):
        s = defaultdict(int)
        for r in rows:
            if all(r[k] == v for k, v in kw.items()):
                s[(r["fiscal_year"], r["basis"])] += r["amount"]
        return s

    depts = sorted({d for d, _ in D})
    # (a) summary: rows vs printed totals
    for d in depts:
        for tbl in ("by_type", "by_fund"):
            if (d, tbl) not in D:
                continue
            s = sum_rows(D[(d, tbl)])
            for t in T.get("department_detail/%s/%s" % (d, tbl), []):
                out.check(2, "summary_total", t["scope"], t["fiscal_year"], t["basis"], s[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
        # by_type total == by_fund total (printed)
        a = {(t["fiscal_year"], t["basis"]): t for t in T.get("department_detail/%s/by_type" % d, [])}
        b = {(t["fiscal_year"], t["basis"]): t for t in T.get("department_detail/%s/by_fund" % d, [])}
        for k in a:
            if k in b:
                out.check(2, "total_expenditures=total_funding", "department_detail/%s" % d, k[0], k[1], a[k]["amount"], b[k]["amount"], a[k]["page"])
    # (b) expenditure summary (by_program): programs per fund vs fund total, funds vs dept total, dept total vs by_type
    for d in depts:
        rows = D.get((d, "by_program"), [])
        if not rows:
            continue
        funds = sorted({r["dim2"] for r in rows})
        for f in funds:
            s = sum_rows(rows, dim2=f)
            for t in T.get("department_detail/%s/by_program/%s" % (d, f), []):
                out.check(2, "program_sum_vs_fund_total", t["scope"], t["fiscal_year"], t["basis"], s[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
        s = sum_rows(rows)
        dt = T.get("department_detail/%s/by_program" % d, [])
        for t in dt:
            out.check(2, "program_sum_vs_dept_total", t["scope"], t["fiscal_year"], t["basis"], s[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
            bt = {(x["fiscal_year"], x["basis"]): x for x in T.get("department_detail/%s/by_type" % d, [])}
            x = bt.get((t["fiscal_year"], t["basis"]))
            if x:
                out.check(2, "expenditure_summary_vs_budget_summary", "department_detail/%s" % d, t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
        # dept total = sum of fund totals (printed)
        ft = defaultdict(int)
        for k, lst in T.items():
            pre = "department_detail/%s/by_program/" % d
            if k.startswith(pre):
                for t in lst:
                    ft[(t["fiscal_year"], t["basis"])] += t["amount"]
        for t in dt:
            if (t["fiscal_year"], t["basis"]) in ft:
                out.check(2, "fund_totals_vs_dept_total", t["scope"], t["fiscal_year"], t["basis"], ft[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
    # (c) program detail: per-type sums vs fund/dept totals; PS+OO+DS=Total; vs by_type 2012 adopted
    for d in depts:
        rows = D.get((d, "by_program_type"), [])
        if not rows:
            continue
        # fund | type sums
        keys = sorted({r["dim2"] for r in rows})
        for k in keys:
            fund, typ = k.rsplit(" | ", 1)
            s = sum_rows(rows, dim2=k)
            for t in T.get("department_detail/%s/by_program_type/%s/%s" % (d, fund, typ), []):
                out.check(2, "program_detail_sum_vs_fund_total", t["scope"], t["fiscal_year"], t["basis"], s[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
        # fund-level printed "Total" column = sum of that fund's three type columns over all programs
        for fund in sorted({k.rsplit(" | ", 1)[0] for k in keys}):
            s = sum(r["amount"] for r in rows if r["dim2"].startswith(fund + " | "))
            for t in T.get("department_detail/%s/by_program_type/%s/Total" % (d, fund), []):
                out.check(2, "program_detail_sum_vs_fund_total", t["scope"], t["fiscal_year"], t["basis"], s, t["amount"], t["page"])
        # dept totals by type vs sum of rows and vs by_type
        for typ in ("Personal Services", "Other Operating", "Debt Service", "Total"):
            tts = T.get("department_detail/%s/by_program_type/%s" % (d, typ), [])
            if not tts:
                continue
            s = sum(r["amount"] for r in rows if r["dim2"].endswith(" | " + typ)) if typ != "Total" else sum(r["amount"] for r in rows)
            for t in tts:
                out.check(2, "program_detail_sum_vs_dept_total", t["scope"], t["fiscal_year"], t["basis"], s, t["amount"], t["page"])
                lbl = {"Personal Services": "PERSONAL SERVICES", "Other Operating": "OTHER OPERATING", "Debt Service": "DEBT SERVICE"}.get(typ)
                for r in D.get((d, "by_type"), []):
                    if (lbl and r["dim1"] == lbl and r["fiscal_year"] == BOOK_FY and r["basis"] == "adopted"):
                        out.check(2, "program_detail_vs_budget_summary", "department_detail/%s/%s" % (d, typ), BOOK_FY, "adopted", t["amount"], r["amount"], t["page"])
                if typ == "Other Operating" and D.get((d, "by_type")):
                    # the Program Detail table has no Reserves / Grants columns: those amounts are folded into 'Other Operating'
                    # there.  Derived check, reported next to (not instead of) the plain comparison above.
                    extra = sum(r["amount"] for r in D.get((d, "by_type"), []) if r["fiscal_year"] == BOOK_FY and r["basis"] == "adopted"
                                and r["dim1"] in ("OTHER OPERATING", "RESERVES", "GRANTS, CONTRIB. & FUND TRAN."))
                    for t in tts:
                        out.check(2, "program_detail_OO_vs_budget_summary_OO+reserves+grants", "department_detail/%s/%s" % (d, typ), BOOK_FY, "adopted", t["amount"], extra, t["page"],
                                  "Program Detail 'Other Operating' compared with the summary's OTHER OPERATING + RESERVES + GRANTS, CONTRIB. & FUND TRAN.")
        # (row arithmetic PS+OO+DS = printed Total is checked below)
    # row arithmetic: PS+OO+DS == printed Total (per program row)
    prog_rows = defaultdict(dict)
    for r in out.detail:
        if r["table"] == "by_program_type":
            prog_rows[(r["dept_name"], r["dim2"].rsplit(" | ", 1)[0], r["dim1"], r["page"])][r["dim2"].rsplit(" | ", 1)[1]] = r["amount"]
    for pt in out.detail_totals:
        parts = prog_rows.get((pt["dept"], pt["fund"], pt["prog"], pt["page"]), {})
        out.check(2, "program_row_PS+OO+DS=Total", "department_detail/%s/%s/%s" % (pt["dept"], pt["fund"], pt["prog"]),
                  BOOK_FY, "adopted", sum(parts.values()), pt["total"], pt["page"])

    # (d) citywide statement vs department sections.  The statement prints FY2016 as 'Adopted' while the sections
    # and p.45 print it as 'Actual': compared on fiscal year (basis difference recorded in the note).
    stm = defaultdict(dict)   # (name_norm, fy) -> {fund_group: amount}
    stm_basis = {}
    for r in out.dept_exp:
        stm[(norm(r["dept_name"]), r["fiscal_year"])][r["fund_group"]] = r["amount"]
        stm_basis[r["fiscal_year"]] = r["basis"]
    stm_names = {k[0] for k in stm}
    matched = set()
    sec_total = defaultdict(lambda: defaultdict(int))     # (fy, basis) -> dept sums over sections matched to the statement
    sec_gf = defaultdict(lambda: defaultdict(int))
    for d in depts:
        if (d, "by_type") not in D:
            continue
        alias = norm(DEPT_ALIASES.get(d, d))
        if alias not in stm_names:
            out.notes.append("department section %r has no counterpart in the citywide expenditure statement (cross-check skipped)" % d)
            continue
        matched.add(alias)
        bt = {(t["fiscal_year"], t["basis"]): t for t in T.get("department_detail/%s/by_type" % d, [])}
        gf = defaultdict(int)
        for r in D.get((d, "by_fund"), []):
            if r["dim1"] == "GENERAL FUND":
                gf[(r["fiscal_year"], r["basis"])] += r["amount"]
        for (fy, b), t in sorted(bt.items()):
            sec_total[(fy, b)]["sum"] += t["amount"]
            sec_gf[(fy, b)]["sum"] += gf.get((fy, b), 0)
            g = stm.get((alias, fy))
            if not g or b == "proposed":
                continue            # the statements have no 'proposed' column
            note = "statement name: %s" % DEPT_ALIASES.get(d, d)
            if stm_basis.get(fy) != b:
                note += "; statement prints FY%d as %s, section prints %s" % (fy, stm_basis.get(fy), b)
            out.check(2, "dept_section_vs_citywide_all_funds", "department_detail/%s" % d, fy, b, t["amount"], g.get("all_funds", 0), t["page"], note)
            out.check(2, "dept_section_GF_vs_citywide_general_fund", "department_detail/%s" % d, fy, b, gf.get((fy, b), 0), g.get("general_fund", 0), t["page"], note)
            out.check(2, "dept_section_nonGF_vs_citywide_other_funds", "department_detail/%s" % d, fy, b, t["amount"] - gf.get((fy, b), 0), g.get("other_funds", 0), t["page"], note)
    for n in sorted(stm_names - matched):
        if any(abs(v) > 0 for (k, fy), g in stm.items() if k == n for v in g.values()):
            out.notes.append("citywide statement department %r has non-zero amounts but no department section" % n)

    # (d2) Mayor: no DEPARTMENTAL BUDGET SUMMARY; the five sub-office program tables are rolled up (printed
    # DEPARTMENT TOTAL / '001 GENERAL FUND' totals of the Expenditure Summary tables) and compared to the statement.
    mayor_subs = sorted({d for d in depts if d.startswith("MAYOR - ")})
    mayor_tot, mayor_gf = defaultdict(int), defaultdict(int)
    mayor_sub_gf = defaultdict(int)
    for sname in mayor_subs:
        for t in T.get("department_detail/%s/by_program" % sname, []):
            mayor_tot[(t["fiscal_year"], t["basis"])] += t["amount"]
        for fk in ("001 GENERAL FUND", "GENERAL FUND"):        # the Mayor's Office table prints 'GENERAL FUND TOTAL' without the code
            for t in T.get("department_detail/%s/by_program/%s" % (sname, fk), []):
                mayor_gf[(t["fiscal_year"], t["basis"])] += t["amount"]
                mayor_sub_gf[(sname, t["fiscal_year"], t["basis"])] += t["amount"]
    out.mayor_tot, out.mayor_gf = mayor_tot, mayor_gf
    if mayor_subs:
        g_of = lambda fy: stm.get((norm("Mayor"), fy), {})
        for (fy, b), v in sorted(mayor_tot.items()):
            if b == "proposed" or not g_of(fy):
                continue
            if fy in MAYOR_SKIP_FYS and b == "adopted":
                out.notes.append("Mayor sub-office tables print FY%d as 'adopted' (%s) while the statement / p.45 figure for FY%d is an actual (%s): "
                                 "roll-up not compared" % (fy, format(v, ","), fy, format(g_of(fy).get("all_funds", 0), ",")))
                continue
            nt = "sum of the %d Mayor sub-offices' printed DEPARTMENT TOTAL (Expenditure Summary tables)" % len(mayor_subs)
            if stm_basis.get(fy) != b:
                nt += "; statement prints FY%d as %s, section prints %s" % (fy, stm_basis.get(fy), b)
            out.check(2, "mayor_suboffices_vs_citywide_all_funds", "department_detail/MAYOR sub-offices", fy, b, v, g_of(fy).get("all_funds", 0), 135, nt)
            out.check(2, "mayor_suboffices_GF_vs_citywide_general_fund", "department_detail/MAYOR sub-offices", fy, b, mayor_gf.get((fy, b), 0), g_of(fy).get("general_fund", 0), 135, nt)
        # p.51 sub-office split (2016, 2017 adopted): GF / non-GF per sub-office
        p51map = {"Mayor - Core": "MAYOR - MAYOR'S OFFICE", "Criminal Justice Coordination": "MAYOR - OFFICE OF CRIMINAL JUSTICE COORDINATION",
                  "Homeland Security": "MAYOR - HOMELAND SECURITY", "Office of Community Development": "MAYOR - COMMUNITY DEVELOPMENT",
                  "Office of Resilience & Sustainability": "MAYOR - OFFICE OF RESILIENCE & SUSTAINABILITY"}
        for title, side, lab, vals in out.p51:
            if title != "Mayor" or lab not in p51map:
                continue
            sname = p51map[lab]
            for fy, v in zip((Y0, Y1), vals):
                tot = [t for t in T.get("department_detail/%s/by_program" % sname, []) if t["fiscal_year"] == fy and t["basis"] == "adopted"]
                gfsum = mayor_sub_gf.get((sname, fy, "adopted"), 0)
                if not tot:
                    continue
                ext = gfsum if side == "general_fund" else tot[0]["amount"] - gfsum
                out.check(2, "p51_mayor_suboffice_vs_expenditure_summary", "summary_p51/Mayor/%s/%s" % (lab, side), fy, "adopted", ext, v, P_SUMMARY_MAYOR_ETC,
                          "p.51 '%s' <-> section %s (GF = printed '001 GENERAL FUND TOTAL', non-GF = DEPARTMENT TOTAL - GF)" % (lab, sname))

    # (d3) p.51 sub-tables: sub-rows sum to the printed 'Total <dept>' row, and that total equals the citywide statement
    p51stm = {"Mayor": "Mayor", "Chief Administrative Office": "CAO", "Health": "Health", "Miscellaneous": "Miscellaneous",
              "Office of Community Development": "Office of Comm. Dev."}
    for title in sorted({x[0] for x in out.p51}):
        for side in ("general_fund", "other_funds"):
            rows = [(lab, vals) for t, s_, lab, vals in out.p51 if t == title and s_ == side]
            tot = [(lab, vals) for lab, vals in rows if lab.startswith("Total ")]
            body = [(lab, vals) for lab, vals in rows if not lab.startswith("Total ")]
            if len(tot) != 1:
                out.unp(P_SUMMARY_MAYOR_ETC, "p.51 %s %s" % (title, side), "expected one Total row")
                continue
            for k, fy in enumerate((Y0, Y1)):
                out.total(fy, "adopted", "summary_p51/%s/%s" % (title, side), tot[0][0], tot[0][1][k], P_SUMMARY_MAYOR_ETC)
                out.check(2, "p51_subtable_sum_vs_total", "summary_p51/%s/%s" % (title, side), fy, "adopted", sum(v[k] for _, v in body), tot[0][1][k], P_SUMMARY_MAYOR_ETC)
                g = stm.get((norm(p51stm.get(title, title)), fy), {})
                if g:
                    out.check(1, "p51_total_vs_citywide_statement", "summary_p51/%s/%s" % (title, side), fy, "adopted", tot[0][1][k],
                              g.get(side, 0), P_SUMMARY_MAYOR_ETC, "statement name: %s" % p51stm.get(title, title))

    # (e) citywide summary (p.45): TOTAL EXPENDITURES and the GENERAL FUND line = sum over department sections
    # + Mayor sub-office roll-up + statement-only rows (departments that have no section of their own).
    unmatched = sorted(stm_names - matched - {norm("Mayor")})
    for t in [t for t in out.tot if t["scope"] == "department_detail/CITYWIDE/by_type"]:
        if t["basis"] == "proposed":
            continue
        fy, b = t["fiscal_year"], t["basis"]
        extra = sum(stm.get((n, fy), {}).get("all_funds", 0) for n in unmatched)
        s = sec_total[(fy, b)]["sum"] + (mayor_tot[(fy, b)] if (fy, b) in mayor_tot else stm.get((norm("Mayor"), fy), {}).get("all_funds", 0)) + extra
        out.check(2, "citywide_p45_total_vs_sum_of_sections", t["scope"], fy, b, s, t["amount"], t["page"],
                  "sum of department-section by_type totals (%d depts) + Mayor sub-office roll-up (statement Mayor row where the sub-office tables print the year on another basis) + statement rows with no section (%s)" % (
                      len(matched), ", ".join(unmatched)))
    for r in [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND"]:
        if r["basis"] == "proposed":
            continue
        fy, b = r["fiscal_year"], r["basis"]
        extra = sum(stm.get((n, fy), {}).get("general_fund", 0) for n in unmatched)
        s = sec_gf[(fy, b)]["sum"] + (mayor_gf[(fy, b)] if (fy, b) in mayor_gf else stm.get((norm("Mayor"), fy), {}).get("general_fund", 0)) + extra
        out.check(2, "citywide_p45_GF_vs_sum_of_sections", "citywide/by_fund/GENERAL FUND", fy, b, s, r["amount"], r["page"],
                  "sum of department-section by_fund GENERAL FUND + Mayor sub-office '001 GENERAL FUND TOTAL' + statement GF rows with no section")

    # (f) citywide statement totals vs citywide summary p.45 / p.50 (fiscal-year match; basis difference noted)
    for t in out.tot:
        if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund") and t["basis"] != "proposed":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"]]
            for x in ref:
                kind = "citywide_summary_p45_vs_statement_total" if t["scope"].endswith("by_type") else "citywide_funding_p45_vs_statement_total"
                nt = "" if x["basis"] == t["basis"] else "statement prints FY%d as %s, p.45 prints %s" % (t["fiscal_year"], x["basis"], t["basis"])
                out.check(1, kind, "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"], nt)
    for r in [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND" and r["basis"] != "proposed"]:
        for x in [x for x in out.tot if x["scope"] == "department_expenditures/general_fund" and x["fiscal_year"] == r["fiscal_year"]]:
            nt = "" if x["basis"] == r["basis"] else "statement prints FY%d as %s, p.45 prints %s" % (r["fiscal_year"], x["basis"], r["basis"])
            out.check(1, "citywide_summary_p45_GF_vs_statement_GF_total", "department_expenditures/general_fund", r["fiscal_year"], r["basis"], r["amount"], x["amount"], r["page"], nt)
    for (name, grp, fy), v in out.p50.items():
        if name == "expenditure":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/" + grp and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p50_vs_statement_total", "department_expenditures/" + grp, fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)
        else:
            ref = [x for x in out.tot if x["scope"] == ("revenues/General Fund" if grp == "general_fund" else "revenues/other_funds") and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p50_vs_statement_total", x["scope"], fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)

    # (h) p.50 department rows (2016, 2017 adopted; General / Non-General halves) vs the statements, aligned by value
    stm_order = []
    for r in out.dept_exp:
        if r["fund_group"] == "all_funds" and r["dept_name"] not in stm_order:
            stm_order.append(r["dept_name"])        # the all-funds statement lists every department with any amount
    sv = lambda n, fy, grp: stm.get((norm(n), fy), {}).get(grp, 0)

    def take(used, key_of):
        """First not-yet-used statement row whose values equal this p.50 / p.78 row (the book orders departments differently
        in the two tables, so rows are matched by value, each statement row at most once)."""
        for n in stm_order:
            if n not in used and key_of(n):
                used.add(n)
                return n
        return None

    for side in ("general_fund", "other_funds"):
        used = set()
        for s_, lab, vals in out.p50_dept:
            if s_ != side or lab == "Total":
                continue
            n = take(used, lambda n: sv(n, Y0, side) == vals[0] and sv(n, Y1, side) == vals[1])
            for k, fy in enumerate((Y0, Y1)):
                out.check(1, "p50_dept_row_vs_statement", "summary_p50/expenditure/%s/%s" % (side, lab), fy, "adopted", vals[k],
                          sv(n, fy, side) if n else None if False else (sv(n, fy, side) if n else 0), P_SUMMARY_TABLES,
                          ("matched by value to statement row %r" % n) if n else "no unused statement row has both years' values")
    # (i) p.78 department x 14 funds (2017 adopted): GF column = GF statement; all columns = all-funds; columns 2-14 = other funds
    used = set()
    for lab, vals in out.p78:
        if lab.upper() == "TOTAL":
            tt = {grp: next((x["amount"] for x in out.tot if x["scope"] == "department_expenditures/" + grp and x["fiscal_year"] == Y1 and x["basis"] == "adopted"), None)
                  for grp in ("general_fund", "other_funds", "all_funds")}
            out.check(1, "p78_fund_columns_vs_statement_total", "p78/Total/general_fund", Y1, "adopted", vals[0], tt["general_fund"] or 0, P_FUND_BY_DEPT)
            out.check(1, "p78_fund_columns_vs_statement_total", "p78/Total/other_funds", Y1, "adopted", sum(vals[1:]), tt["other_funds"] or 0, P_FUND_BY_DEPT)
            out.check(1, "p78_fund_columns_vs_statement_total", "p78/Total/all_funds", Y1, "adopted", sum(vals), tt["all_funds"] or 0, P_FUND_BY_DEPT)
            continue
        n = take(used, lambda n: sv(n, Y1, "all_funds") == sum(vals) and sv(n, Y1, "general_fund") == vals[0])
        for grp, ext in (("general_fund", vals[0]), ("other_funds", sum(vals[1:])), ("all_funds", sum(vals))):
            out.check(1, "p78_fund_columns_vs_statement", "p78/%s/%s" % (lab, grp), Y1, "adopted", ext, sv(n, Y1, grp) if n else 0, P_FUND_BY_DEPT,
                      ("matched by value to statement row %r" % n) if n else "no unused statement row has this GF and all-funds value")

    # (g) personnel
    P = defaultdict(list)
    for r in out.pos:
        P[r["dept_name"]].append(r)
    for d, rows in sorted(P.items()):
        progs = defaultdict(lambda: defaultdict(Decimal))
        funds = defaultdict(lambda: defaultdict(Decimal))
        alls = defaultdict(Decimal)
        for r in rows:
            k = (r["fiscal_year"], r["basis"])
            progs[(r["fund"], r["program"])][k] += r["fte"]
            funds[r["fund"]][k] += r["fte"]
            alls[k] += r["fte"]
        for t in out.tot:
            if t.get("_dept") != d:
                continue
            k = (t["fiscal_year"], t["basis"])
            if t["_fund"] is None:
                out.check(3, "fte_sum_vs_dept_total", t["scope"], k[0], k[1], alls[k], t["amount"], t["page"])
            elif t["_prog"] is None:
                out.check(3, "fte_sum_vs_fund_total", t["scope"], k[0], k[1], funds[t["_fund"]][k] if t["_fund"] in funds else Decimal(0), t["amount"], t["page"])
            else:
                out.check(3, "fte_sum_vs_program_total", t["scope"], k[0], k[1], progs[(t["_fund"], t["_prog"])][k] if (t["_fund"], t["_prog"]) in progs else Decimal(0), t["amount"], t["page"])


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower().replace("’", "'").replace("'", ""))


# ======================================================================================
# citywide FTE ("Work Years") tables, pp.71-78 -> workyears_citywide.csv
# ======================================================================================
def parse_workyears(doc, out):
    """Citywide Work Years (FTE) 2004-2012, pp.71-78: Department / Program rows + 'Department Total' rows,
    ending with the 'City-Wide Total' row (the charts after it are ignored)."""
    dept = None
    seq = defaultdict(int)
    done = False
    prev_cols = None
    for pno in FTE_PAGES:
        if done:
            break
        lines = get_lines(doc[pno - 1])
        cols = None
        for i, ln in enumerate(lines):
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, N_FTE_COLS)
                lines = lines[j:]
                break
        if cols is None and prev_cols is not None:
            cols = prev_cols            # a continuation page without its own header row (p.82): same table, same columns
            out.notes.append("p.%d: work-years continuation page has no header row; columns taken from the previous page" % pno)
        if cols is None:
            out.unp(pno, "citywide work years table", "header columns not recognised")
            continue
        prev_cols = cols
        anchors = [c[0] for c in cols]
        bottom = False      # the closing by-fund block (General Fund / Other Funds / City-Wide Total)
        for ln in lines:
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                bottom = True
                continue
            label, data, probs = split_row(ln, anchors, tol_left=25, decimal=True)
            lab = clean_label(ltext(label))
            if not data:
                if lab and not lab.startswith("Page") and not done and not bottom:
                    dept = lab
                continue
            if not lab:
                out.unp(pno, "unlabelled number(s) %r" % ltext(ln), "stray cell between work-year rows; ignored")
                continue
            for pr in probs:
                out.problems.append(dict(page=pno, line=ltext(ln), problem=pr))
            if bottom:
                for c, v in data.items():
                    kind = "citywide" if lab.lower().startswith("city-wide total") else "fund"
                    out.total(cols[c][1], cols[c][2], "workyears_citywide/by_fund", lab, v, pno, _wy=kind)
                if lab.lower().startswith("city-wide total"):
                    done = True
                    break
                continue
            if re.search(r"(?i)\btotal$", lab) and not (lab != "Department Total" and not any(r["dept_name"] == dept and r["_seq"] == seq[dept] for r in out.fte)):
                for c, v in data.items():
                    out.total(cols[c][1], cols[c][2], "workyears_citywide/%s" % dept, lab, v, pno, _wy=("dept" if lab == "Department Total" else "sub"),
                              _dept=dept, _seq=seq[dept])
                seq[dept] += 1
                continue
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.fte.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2], dept_name=dept or "",
                                    program=lab, fte=v, page=pno, _seq=seq[dept]))


def workyears_checks(out):
    rows = defaultdict(lambda: defaultdict(Decimal))      # (dept, seq) -> (fy,basis) -> sum
    for r in out.fte:
        rows[(r["dept_name"], r["_seq"])][(r["fiscal_year"], r["basis"])] += r["fte"]
    dept_total = defaultdict(Decimal)
    for t in out.tot:
        kind = t.get("_wy")
        if kind in ("dept", "sub"):
            k = (t["fiscal_year"], t["basis"])
            if kind == "dept":
                s = sum((v.get(k, Decimal(0)) for (d, q), v in rows.items() if d == t["_dept"]), Decimal(0))
                dept_total[k] += t["amount"]
                out.check(3, "workyears_programs_sum_vs_department_total", t["scope"], k[0], k[1], s, t["amount"], t["page"])
            else:
                s = rows.get((t["_dept"], t["_seq"]), {}).get(k, Decimal(0))
                out.check(3, "workyears_programs_sum_vs_subtotal", t["scope"], k[0], k[1], s, t["amount"], t["page"])
    for t in out.tot:
        if t.get("_wy") == "citywide":
            k = (t["fiscal_year"], t["basis"])
            out.check(3, "workyears_department_totals_vs_citywide_total", t["scope"], k[0], k[1], dept_total[k], t["amount"], t["page"])
            gf = sum((x["amount"] for x in out.tot if x.get("_wy") == "fund" and (x["fiscal_year"], x["basis"]) == k), Decimal(0))
            out.check(3, "workyears_general+other_funds=citywide_total", t["scope"], k[0], k[1], gf, t["amount"], t["page"])
    # personnel summary (positions.csv) vs the citywide work-years department totals, where names line up
    wy = {}
    for t in out.tot:
        if t.get("_wy") == "dept":
            wy[(norm(t["_dept"]), t["fiscal_year"], t["basis"])] = t
    pos = defaultdict(Decimal)
    for r in out.pos:
        pos[(norm(r["dept_name"]), r["fiscal_year"], r["basis"])] += r["fte"]
    names = {k[0] for k in pos}
    alias = {"chiefadministrativeoffice": "chiefadministrativeoffice", "parkway": "parksparkways"}
    for (n, fy, b), v in sorted(pos.items()):
        key = (alias.get(n, n), fy, b)
        if key in wy and b == "adopted":
            out.check(3, "personnel_summary_fte_vs_citywide_workyears", "positions/%s" % n, fy, b, v, wy[key]["amount"], wy[key]["page"],
                      "department names matched by normalised text only")


# ======================================================================================
# main
# ======================================================================================
def fmt_for_search(v, decimal=False):
    if decimal:
        return "%.2f" % v
    return "{:,}".format(abs(int(v)))


def spot_checks(out, n_each=20, seed=2017):
    """Independent re-read: seeded-random output cells are looked up in pdfplumber's text of the cited page (a different
    extraction engine and line builder from the PyMuPDF word clustering used above): the amount must appear on a text
    line that also contains the row's label (compared on letters/digits only)."""
    import random
    import pdfplumber
    rng = random.Random(seed)
    sets = [("department_expenditures", out.dept_exp, lambda r: r["dept_name"], "amount", False),
            ("revenues", out.rev, lambda r: r["line_name"], "amount", False),
            ("department_detail", [r for r in out.detail if r["dept_name"] != "CITYWIDE"], lambda r: r["dim1"], "amount", False),
            ("positions", out.pos, lambda r: r["position_title"] or r["program"], "fte", True),
            ("workyears_citywide", out.fte, lambda r: r["program"], "fte", True)]
    res = []
    with pdfplumber.open(str(PDF)) as pdf:
        cache = {}
        for name, rows, labf, key, dec in sets:
            for r in rng.sample(rows, min(n_each, len(rows))):
                pg = r["page"]
                if pg not in cache:
                    cache[pg] = [re.sub(r"\s+", " ", x).strip() for x in (pdf.pages[pg - 1].extract_text() or "").split("\n")]
                needle = fmt_for_search(r[key], dec)
                lab = labf(r)
                nl = re.sub(r"[^a-z0-9]", "", lab.lower())
                hits = [ln for ln in cache[pg] if needle in ln and nl and nl in re.sub(r"[^a-z0-9]", "", ln.lower())]
                res.append(dict(file=name, page=pg, fiscal_year=r["fiscal_year"], basis=r["basis"], label=lab, amount=str(r[key]),
                                found=bool(hits), matched_line=hits[0] if hits else None))
    return res


def write_csv(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(r[k]) if isinstance(r[k], Decimal) else r[k]) for k in header})


def sanity(doc):
    def has(p, s):
        return s in doc[p - 1].get_text()
    assert has(P_CITYWIDE_SUMMARY, "CITYWIDE") and has(P_CITYWIDE_SUMMARY, "TOTAL EXPENDITURES")
    assert has(69, "Statement of Revenues") and has(74, "TOTAL ALL FUNDS")
    assert has(75, "GENERAL FUND EXPENDITURES") and has(76, "OTHER FUND EXPENDITURES") and has(77, "TOTAL EXPENDITURES")
    assert has(P_SUMMARY_TABLES, "Non-General Fund Expenditures") and has(P_SUMMARY_MAYOR_ETC, "Department Section")
    assert has(P_FUND_BY_DEPT, "major fund categories") and has(79, "CITYWIDE WORK YEARS")
    assert has(123, "DEPARTMENTAL BUDGET SUMMARY") and has(625, "DEPARTMENTAL BUDGET SUMMARY") and has(626, "GLOSSARY")


def main():
    doc = fitz.open(str(PDF))
    assert doc.page_count == 646, doc.page_count
    sanity(doc)
    for pg in doc:
        for w in pg.get_text("words"):
            k = re.sub(r"[^a-z0-9]", "", w[4].lower())
            VOCAB[k] = VOCAB.get(k, 0) + 1
    out = Out()
    out.narr_pages = []
    out.cw_rows = []
    parse_revenues(doc, out)
    parse_dept_expenditures(doc, out)
    parse_p50_totals(doc, out)
    # citywide summary page 44 (parsed with the department-summary parser, kept for checks only)
    lines = get_lines(doc[P_CITYWIDE_SUMMARY - 1])
    seg = segments(lines)[0]
    tmp_type, tmp_fund = [], []
    parse_summary_page(seg[2], 0, P_CITYWIDE_SUMMARY, seg[1], out, tmp_type, tmp_fund)
    out.cw_rows = tmp_type + tmp_fund
    parse_departments(doc, out)
    parse_workyears(doc, out)
    dept_checks(out)
    workyears_checks(out)

    # ---- tables that exist but are deliberately not extracted (recorded, not guessed)
    def pages_with(rng, key):
        return [p for p in rng if key in doc[p - 1].get_text()[:600]]
    for key, why in (
            ("Description of Funded Programs", "per-offer funding tables (result team, rank, source of funds, GF / other funds recommended): free-form multi-line offer text interleaved with numbers; not parsed"),
            ("Funding Summary", "pie-chart labels + a small GF/total/FTE box per department; chart text, not a table"),
            ("Performance Measures", "KPI actual-vs-target tables with mixed units (%, counts, $); not parsed")):
        pgs = pages_with(out.narr_pages, key)
        if pgs:
            out.unp("%d pages: %s" % (len(pgs), ",".join(map(str, pgs))), key, why)
    out.unp("pp.1-44, 46-49, 52-68", "narrative, budget process, General Fund summary charts, revenue descriptions, fund list, millage rates, fund-balance / projection tables", "not attempted: outside the requested tiers (p.45, p.50, p.51 and p.78 summary tables are captured as cross-checks only)")
    out.unp("pp.90-119", "capital budget summary, statement of debt, pension notes, financial policies", "not attempted: outside the requested tiers")
    out.unp("pp.626-646", "glossary, appendix of strategic alignment / performance measures", "not attempted: narrative")
    out.unp("pp.484-490 (OCD)", "Funding Summary grant tables (agency / org / 2017 / prior years / total by grant)", "not parsed: multi-year grant allocation tables outside the schema; narrative page range")
    out.unp("Mayor (pp.130-181)", "no DEPARTMENTAL BUDGET SUMMARY for the Mayor or for any of its five sub-offices",
            "the book prints program-level tables per sub-office (Program Detail, Expenditure Summary, Personnel Summary) but no by-type / by-fund summary: department_detail has no by_type / by_fund rows for 'MAYOR - ...'; the citywide statement carries Mayor as one line")
    out.unp("Health, CAO, Miscellaneous, OCD sub-units", "no separate summary per sub-unit (Health Core/EMS, CAO Core/ITI/OEP, Miscellaneous Core/NORD/..., OCD Core/Code Enforcement)",
            "one DEPARTMENTAL BUDGET SUMMARY per group; only p.51 splits GF / non-GF 2016-2017 by sub-unit (cross-check only)")
    printed_checked = {(c["scope"], c["fiscal_year"], c["basis"]) for c in out.checks}
    unchecked = defaultdict(int)
    for t in out.tot:
        if (t["scope"], t["fiscal_year"], t["basis"]) not in printed_checked:
            unchecked[t["scope"].split("/")[0]] += 1

    # ---- label suspects: adjacent alphabetic label tokens whose concatenation is a word used elsewhere in the
    # book but which the repair rule did not join.  Reported for review, never auto-fixed.
    labels = set()
    for r in out.rev:
        labels.add(r["line_name"])
    for r in out.dept_exp:
        labels.add(r["dept_name"])
    for r in out.detail:
        labels.add(r["dim1"])
    for r in out.pos:
        labels.add(r["position_title"])
    labels |= {r["program"] for r in out.fte}
    sus = set()
    for lab in labels:
        toks = lab.split()
        for a, b in zip(toks, toks[1:]):
            ja = (a + b).lower()
            if a.isalpha() and b.isalpha() and len(ja) >= 5 and VOCAB.get(ja, 0) >= 2 and (len(a) <= 2 or len(b) <= 2):
                sus.add("%s  [%s + %s -> %s]" % (lab, a, b, ja))

    # ---- write
    write_csv("department_expenditures.csv", ["book_id", "fiscal_year", "basis", "fund_group", "dept_code", "dept_name", "amount", "page"],
              [r for r in out.dept_exp])
    write_csv("revenues.csv", ["book_id", "fiscal_year", "basis", "fund_group", "fund_name", "category", "line_name", "amount", "page"], out.rev)
    detail_rows = [r for r in out.detail if r["dept_name"] != "CITYWIDE"]
    write_csv("department_detail.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "table", "dim1", "dim2", "amount", "page"], detail_rows)
    write_csv("positions.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "program", "fund", "position_title", "pay_grade", "fte", "page"], out.pos)
    write_csv("printed_totals.csv", ["book_id", "fiscal_year", "basis", "scope", "label", "amount", "page"], out.tot)
    write_csv("workyears_citywide.csv", ["book_id", "fiscal_year", "basis", "dept_name", "program", "fte", "page"], out.fte)

    # ---- checks.json
    PRINTED_TOTAL_KINDS = {"p51_subtable_sum_vs_total", 
        "revenue_total", "dept_statement_total", "summary_total", "program_sum_vs_fund_total", "program_sum_vs_dept_total",
        "fund_totals_vs_dept_total", "program_detail_sum_vs_fund_total", "program_detail_sum_vs_dept_total",
        "program_row_PS+OO+DS=Total", "fte_sum_vs_program_total", "fte_sum_vs_fund_total", "fte_sum_vs_dept_total",
        "workyears_programs_sum_vs_department_total", "workyears_programs_sum_vs_subtotal"}
    for c in out.checks:
        c["against"] = "printed_total" if c["kind"] in PRINTED_TOTAL_KINDS else "cross_check"
        d = c["diff"]
        c["small_mismatch_le_5"] = bool(c["status"] == "mismatch" and re.fullmatch(r"-?\d+", str(d)) and abs(int(d)) <= 5)
    summ = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for c in out.checks:
        for key in ("all", c["against"]):
            sm = summ["tier%d" % c["tier"]][key]
            sm[c["status"]] += 1
            sm["total"] += 1
            if c["small_mismatch_le_5"]:
                sm["of_which_mismatch_le_$5"] += 1
    by_kind = defaultdict(lambda: defaultdict(int))
    for c in out.checks:
        by_kind[c["kind"]][c["status"]] += 1
        by_kind[c["kind"]]["total"] += 1
    kinds_pages = defaultdict(list)
    for p, k, d in out.sections:
        kinds_pages[k].append(p)
    spot = spot_checks(out)
    report = {
        "book_id": BOOK_ID,
        "row_counts": {
            "department_expenditures.csv": len(out.dept_exp), "revenues.csv": len(out.rev),
            "department_detail.csv": len(detail_rows), "positions.csv": len(out.pos),
            "printed_totals.csv": len(out.tot), "workyears_citywide.csv": len(out.fte),
            "department_detail.csv by table": {t: sum(1 for r in detail_rows if r["table"] == t) for t in ("by_type", "by_fund", "by_program", "by_program_type")},
        },
        "check_summary_by_tier": {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in sorted(summ.items())},
        "check_summary_by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "tier_definitions": {"tier1": "citywide statements of revenues and expenditures (+ p.45 / p.50 / p.51 / p.78 summary cross-checks)",
                              "tier2": "department sections: by_type, by_fund, by_program(_type) and cross-checks to the citywide statements",
                              "tier3": "positions / FTE (personnel summaries; citywide work years)"},
        "tier_numbering_note": "the check records use tier 1 = citywide statements, tier 2 = department sections (by_type, by_fund, by_program, by_program_type, cross-checks to the statements), tier 3 = positions / FTE; in the task's own numbering these are tiers 1, 2+3 and 4",
        "mismatches": [c for c in out.checks if c["status"] == "mismatch"],
        "within_1": [c for c in out.checks if c["status"] == "within_1"],
        "checks": out.checks,
        "unparsed": out.unparsed,
        "printed_totals_rows_not_covered_by_any_check": dict(unchecked),
        "narrative_or_chart_pages_skipped": {"range": "%d-%d" % (DEPT_PAGES.start, DEPT_PAGES.stop - 1), "pages": out.narr_pages,
                                              "content": "mission statements, performance measures, funding-summary charts, 'Description of Funded Programs' offer tables"},
        "pages_not_attempted": ["1-44 (narrative, budget process, strategic framework)", "46-49 (General Fund summary charts and narrative)",
                                 "52-68 (revenue descriptions, fund list, millage rates, fund balance)", "90-119 (capital budget summary, statement of debt, financial policies)",
                                 "626-646 (glossary, appendix of strategic alignment / performance measures)"],
        "row_level_problems": out.problems,
        "notes": out.notes + ["wrapped position titles joined: %d" % out.wrapped_titles],
        "label_repairs": sorted(REPAIRS),
        "label_suspects_not_repaired": sorted(sus),
        "section_pages": {k: len(v) for k, v in kinds_pages.items()},
        "deviations_from_schema": [
            "department_detail.table has an extra value by_program_type (Program Detail table; dim2 = '<fund> | <type>'); by_program = Expenditure Summary (dim2 = fund)",
            "workyears_citywide.csv is an extra file (citywide FTE by department/program, 2009-2017)",
            "dept_code is blank everywhere: the 2017 book prints no department numbers; program numbers are in dim1/program",
            "department_expenditures.csv: variance columns, if any, ignored (derived)",
            "revenues.fund_name: 'General Fund' for the GF statement, other funds as printed (upper case); revenues.category for other funds is blank except where the fund block has a second heading line (SELF-GENERATED / CONTRIBUTIONS & OTHER)",
            "Mayor: no by_type / by_fund rows (the book prints no department summary for the Mayor or its sub-offices); sub-offices appear as 'MAYOR - <sub-office>' in by_program / by_program_type / positions",
            "basis labels are as printed (FY2016 is 'Adopted' throughout this book; the expenditure statements start at FY2010, the revenue and work-years statements at FY2009)",
            "printed_totals.csv also holds p.50 / p.51 summary-table totals (scopes summary_p50/..., summary_p51/...)",
        ],
        "spot_checks_summary": {"sampled": len(spot), "found": sum(1 for x in spot if x["found"])},
        "spot_checks": spot,
    }
    (OUT / "checks.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("row_counts", "check_summary_by_tier", "check_summary_by_kind")}, indent=1))
    print("mismatches:", len(report["mismatches"]), " unparsed:", len(out.unparsed), " problems:", len(out.problems))


if __name__ == "__main__":
    main()
