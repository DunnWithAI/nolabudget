#!/usr/bin/env python3
"""Extract tables from the City of New Orleans 2012 Adopted Operating Budget book.

Run from anywhere:   .venv/bin/python scripts/extract/extract_2012.py

Reads   data/raw/nola-gov/2012/City_of_New_Orleans_2012_Adopted_Budget_Book_Final.pdf  (read-only)
Writes  data/extracted/2012-operating-adopted/{department_expenditures,revenues,
        department_detail,positions,printed_totals}.csv, workyears_citywide.csv (extra), checks.json

Method: PyMuPDF word boxes -> lines (cluster on y) -> label / numeric cells.  Numbers are
assigned to columns by x-position against the header row of each table (nearest header
centre), never by their order in the text stream.  No amount is typed in by hand; the only
hand-written data are page ranges, department-name aliases used for cross-checks and a few
label spelling repairs for PDF kerning artefacts (see LABEL_FIXES).

Output shape follows data/extracted/SCHEMA.md.  Deviations / extensions (also in checks.json):
  * department_detail.table also takes `by_program_type` (the "Program Detail" table: 2012
    adopted dollars per program split by Personal Services / Other Operating / Debt Service;
    dim2 = "<fund heading> | <type>").  `by_program` is the "Expenditure Summary" table
    (4 year columns, dim2 = fund heading).
  * `positions.csv` is the per-department Personnel Summary (titles, 2011-2012).  The citywide
    "Work Years" table (programs, 2004-2012) goes to the extra file workyears_citywide.csv.
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
BOOK_ID = "2012-operating-adopted"
BOOK_FY = 2012
PDF = ROOT / "data/raw/nola-gov/2012/City_of_New_Orleans_2012_Adopted_Budget_Book_Final.pdf"
OUT = ROOT / "data/extracted" / BOOK_ID

# ---- book-specific page map (1-based PDF pages; verified by sanity asserts in main) -------
P_CITYWIDE_SUMMARY = 44
P_SUMMARY_TABLES = 50
REV_PAGES = range(63, 68)
EXP_PAGES = range(68, 71)
FTE_PAGES = range(71, 79)
DEPT_PAGES = range(100, 563)        # department sections run to the Enterprise Funds, then glossary

# Dept-section names -> citywide-statement names (cross-check only; output keeps printed names)
DEPT_ALIASES = {
    "CHIEF ADMINISTRATIVE OFFICE": "CAO",
    "PARKWAY": "Parks & Parkways",
    "HISTORIC DIST LANDMARKS COMM.": "HDLC",
    "VIEUX CARRE COMMISION": "VCC",
    "ALCOHOLIC BEVERAGE CONTR BRD.": "Alcoholic Bvg Ctrl. Bd.",
    "CITY PLANNING COMMISSION": "City Planning Comm.",
    "N O MOSQUITO CONTROL BRD.": "Mosquito Control Bd.",
    "N O MUSEUM OF ART": "Museum of Art",
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
                    and ((len(a) == 1 and a.upper() in "VYAWT") or a.endswith(("w", "W"))) \
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
# Tier 1a: statement of revenues (pp.63-67)
# ======================================================================================
def parse_revenues(doc, out):
    state = dict(group="general_fund", fund=None, cat=None)
    cols = None
    totals = []   # (label, page, rule, {col: val}, cols)
    for pno in REV_PAGES:
        lines = get_lines(doc[pno - 1])
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            ys = [t for t in ln if re.fullmatch(r"20\d\d", t.text)]
            if len(ys) >= 5:                          # header: years line, then Actual/Adopted line
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, 9)
                if cols is None:
                    out.unp(pno, "revenue header", "could not pair years with basis words")
                i = j
                continue
            i += 1
            if re.match(r"STATEMENT OF REVENUES|Discretionary and Other", text):
                continue
            if text.upper().startswith("OTHER FUNDS"):
                state.update(group="other_funds", fund=None, cat=None)
                continue
            if text.startswith("GENERAL FUND"):      # printed "GENERAL FUND - 001", above the column header
                state.update(group="general_fund", fund="General Fund", cat=None)
                continue
            if cols is None:
                continue
            label, data, probs = split_row(ln, [c[0] for c in cols])
            lab = clean_label(ltext(label))
            if not data:
                if state["group"] == "general_fund":
                    state["cat"] = lab
                else:
                    state.update(fund=lab, cat=None)
                continue
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if re.search(r"\bTOTAL\b|SUB-TOTAL", lab.upper()):
                totals.append(dict(label=lab, page=pno, ctx=dict(state), data=data, cols=cols))
                for c, v in data.items():
                    if v != "DASH":
                        out.total(cols[c][1], cols[c][2], None, lab, v, pno)
                        out.tot[-1]["_rev_total_idx"] = len(totals) - 1
                continue
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.rev.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                    fund_group=state["group"], fund_name=state["fund"] or "",
                                    category=state["cat"] or "", line_name=lab, amount=v, page=pno))
    # -- scopes + checks
    def rule(t):
        U, ctx = t["label"].upper(), t["ctx"]
        if U.startswith("TOTAL ALL FUNDS"):
            return "revenues/all_funds", lambda r: True
        if U.startswith("TOTAL OTHER FUNDS"):
            return "revenues/other_funds", lambda r: r["fund_group"] == "other_funds"
        if U.startswith("TOTAL DISCRETIONARY"):
            return "revenues/discretionary", lambda r: r["fund_group"] == "general_fund" or r["fund_name"] == "SELF-GENERATED"
        if U.startswith("TOTAL GENERAL FUND"):
            return "revenues/General Fund", lambda r: r["fund_group"] == "general_fund"
        if U == "TOTAL TAXES":
            return "revenues/General Fund/TAXES", lambda r: r["fund_group"] == "general_fund" and r["category"] in ("PROPERTY TAXES", "Other Taxes")
        f, c = ctx["fund"] or "", ctx["cat"] or ""
        return "revenues/%s/%s" % (f, c), lambda r: r["fund_name"] == f and r["category"] == c
    scopes = {}
    for k, t in enumerate(totals):
        scope, pred = rule(t)
        scopes[k] = scope
        sums = defaultdict(int)
        for r in out.rev:
            if pred(r):
                sums[(r["fiscal_year"], r["basis"])] += r["amount"]
        for c, (cx, fy, basis) in enumerate(t["cols"]):
            v = t["data"].get(c)
            if v is None:
                continue
            printed = 0 if v == "DASH" else v
            out.check(1, "revenue_total", scope, fy, basis, sums.get((fy, basis), 0), printed, t["page"],
                      "printed as dash" if v == "DASH" else "")
    for r in out.tot:
        if "_rev_total_idx" in r:
            r["scope"] = scopes[r.pop("_rev_total_idx")]


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
                cols = year_basis_columns(htoks, 9)
                if cols is None:
                    out.unp(pno, "expenditure header", "could not pair years with basis words")
                i = j
                continue
            i += 1
            m = re.match(r"(General Fund|Other Fund|Total) Expenditures", text)
            if m:
                group = {"General Fund": "general_fund", "Other Fund": "other_funds", "Total": "all_funds"}[m.group(1)]
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
# Tier 1c: printed summary tables p.50 / p.44 (cross-check material)
# ======================================================================================
def parse_p50_totals(doc, out):
    lines = get_lines(doc[P_SUMMARY_TABLES - 1])
    tot_lines = [l for l in lines if l[0].text == "Total"]
    out.p50 = {}
    if len(tot_lines) != 2:
        out.unp(P_SUMMARY_TABLES, "General/Non-General summary totals", "expected two 'Total' lines")
        return
    for name, ln in zip(("revenue", "expenditure"), tot_lines):
        nums = [parse_num(t.text) for t in ln[1:] if parse_num(t.text) not in (None, "DASH")]
        if len(nums) != 4:
            out.unp(P_SUMMARY_TABLES, "%s totals" % name, "expected four numbers")
            continue
        for lab, fy, v, grp in (("General Fund", 2011, nums[0], "general_fund"), ("General Fund", 2012, nums[1], "general_fund"),
                                ("Non-General Fund", 2011, nums[2], "other_funds"), ("Non-General Fund", 2012, nums[3], "other_funds")):
            out.total(fy, "adopted", "summary_p50/%s/%s" % (name, grp), "Total (%s)" % lab, v, P_SUMMARY_TABLES)
            out.p50[(name, grp, fy)] = v


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
        if section is None:             # citywide p.44 prints no "EXPENDITURES" / "SOURCE OF FUNDING" captions
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


FUND_HEAD = re.compile(r"^[A-Z0-9]{3} \S")
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
    if yb is None or not grade:
        out.unp(pno, "%s personnel summary" % dept, "header columns not recognised")
        return
    gcx = grade[0].cx
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
    for pno in DEPT_PAGES:
        segs = segments(get_lines(doc[pno - 1]))
        if not segs:
            out.narr_pages.append(pno)
            continue
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

    # (d) citywide statement vs department sections
    stm = defaultdict(dict)   # (name_norm, fy, basis) -> {fund_group: amount}
    for r in out.dept_exp:
        stm[(norm(r["dept_name"]), r["fiscal_year"], r["basis"])][r["fund_group"]] = r["amount"]
    stm_pages = {}
    for r in out.dept_exp:
        stm_pages[(norm(r["dept_name"]), r["fund_group"])] = r["page"]
    stm_names = {k[0] for k in stm}
    matched = set()
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
            g = stm.get((alias, fy, b))
            if not g:
                continue            # 2012 proposed has no citywide statement column
            out.check(2, "dept_section_vs_citywide_all_funds", "department_detail/%s" % d, fy, b, t["amount"], g.get("all_funds", 0), t["page"],
                      "statement name: %s" % DEPT_ALIASES.get(d, d))
            out.check(2, "dept_section_GF_vs_citywide_general_fund", "department_detail/%s" % d, fy, b, gf.get((fy, b), 0), g.get("general_fund", 0), t["page"])
            out.check(2, "dept_section_nonGF_vs_citywide_other_funds", "department_detail/%s" % d, fy, b, t["amount"] - gf.get((fy, b), 0), g.get("other_funds", 0), t["page"])
    for n in sorted(stm_names - matched):
        if any(abs(v) > 0 for (k, fy, b), g in stm.items() if k == n for v in g.values()):
            out.notes.append("citywide statement department %r has non-zero amounts but no department section" % n)

    # (e) citywide summary p44: sum of departments vs printed citywide.  Only the departments that appear in
    # the citywide expenditure statement are summed (Enterprise-fund entities and the Mayor's sub-office
    # sections, which are already inside "MAYOR", are not part of p.44).
    in_stmt = {d for d in depts if norm(DEPT_ALIASES.get(d, d)) in stm_names and d != "CITYWIDE"}
    cw = defaultdict(list)
    for r in out.cw_rows:
        cw[(r["table"], r["dim1"])].append(r)
    for (tbl, dim1), rs in sorted(cw.items()):
        for r in rs:
            s = 0
            n = 0
            for d in depts:
                if d not in in_stmt:
                    continue
                for x in D.get((d, tbl), []):
                    if x["dim1"] == dim1 and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]:
                        s += x["amount"]
                        n += 1
            out.check(2, "sum_of_departments_vs_citywide_summary_p44", "citywide/%s/%s" % (tbl, dim1), r["fiscal_year"], r["basis"], s, r["amount"], r["page"])
    for t in [t for t in out.tot if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund")]:
        s = 0
        tbl = t["scope"].rsplit("/", 1)[1]
        for d in depts:
            if d not in in_stmt:
                continue
            for x in D.get((d, tbl), []):
                if x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]:
                    s += x["amount"]
        out.check(2, "sum_of_departments_vs_citywide_summary_p44", t["scope"] + " TOTAL", t["fiscal_year"], t["basis"], s, t["amount"], t["page"])
    # (f) citywide statement totals vs citywide summary / p50
    for t in out.tot:
        if t["scope"] == "department_detail/CITYWIDE/by_type" and t["basis"] != "proposed":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]]
            for x in ref:
                out.check(1, "citywide_summary_p44_vs_statement_total", "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
        if t["scope"] == "department_detail/CITYWIDE/by_fund" and t["basis"] != "proposed":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]]
            for x in ref:
                out.check(1, "citywide_funding_p44_vs_statement_total", "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
    gfrows = [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND"]
    for r in gfrows:
        ref = [x for x in out.tot if x["scope"] == "department_expenditures/general_fund" and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]]
        for x in ref:
            out.check(1, "citywide_summary_p44_GF_vs_statement_GF_total", "department_expenditures/general_fund", r["fiscal_year"], r["basis"], r["amount"], x["amount"], r["page"])
    for (name, grp, fy), v in out.p50.items():
        if name == "expenditure":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/" + grp and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p50_vs_statement_total", "department_expenditures/" + grp, fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)
        else:
            ref = [x for x in out.tot if x["scope"] == ("revenues/General Fund" if grp == "general_fund" else "revenues/other_funds") and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p50_vs_statement_total", x["scope"], fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)

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
    for pno in FTE_PAGES:
        if done:
            break
        lines = get_lines(doc[pno - 1])
        cols = None
        for i, ln in enumerate(lines):
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, 9)
                lines = lines[j:]
                break
        if cols is None:
            out.unp(pno, "citywide work years table", "header columns not recognised")
            continue
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
    assert has(63, "GENERAL FUND - 001") and has(66, "OTHER FUNDS")
    assert has(68, "General Fund Expenditures") and has(70, "Total Expenditures")
    assert has(71, "Council") and "Citywide" not in doc[100].get_text()[:0]
    assert has(105, "COUNCIL") and has(561, "PIAZZA")


def main():
    doc = fitz.open(str(PDF))
    assert doc.page_count == 598, doc.page_count
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
    out.unp("45-49, 51-62", "narrative, charts, millage-rate table (p.60-61), fund-balance (p.61-62)", "not attempted: outside the requested tiers (p.50 summary totals only are captured, as cross-checks)")
    out.unp("79-94", "capital budget summary, statement of debt (amounts in thousands)", "not attempted: outside the requested tiers")
    out.unp("566-578", "appendix: professional services contract obligations by department", "not attempted: vendor lists, not budget tables")
    out.unp("579-598", "result-level outcome measures", "not attempted: charts")
    out.unp("sub-offices of the Mayor (pp.111-160)", "no DEPARTMENTAL BUDGET SUMMARY per sub-office", "the book prints one summary for the whole MAYOR section (p.161) and program-level tables per sub-office; by_type / by_fund exist only for MAYOR as a whole")
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
    PRINTED_TOTAL_KINDS = {
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
        "tier_definitions": {"tier1": "citywide statements of revenues and expenditures (+ p.44/p.50 summary cross-checks)",
                              "tier2": "department sections: by_type, by_fund, by_program(_type) and cross-checks to the citywide statements",
                              "tier3": "positions / FTE"},
        "mismatches": [c for c in out.checks if c["status"] == "mismatch"],
        "within_1": [c for c in out.checks if c["status"] == "within_1"],
        "checks": out.checks,
        "unparsed": out.unparsed,
        "printed_totals_rows_not_covered_by_any_check": dict(unchecked),
        "narrative_or_chart_pages_skipped": {"range": "%d-%d" % (DEPT_PAGES.start, DEPT_PAGES.stop - 1), "pages": out.narr_pages,
                                              "content": "mission statements, performance measures, funding-summary charts, 'Description of Funded Programs' offer tables"},
        "pages_not_attempted": ["4-43 (narrative)", "45-49,51-62 (narrative/charts)", "79-99 (capital budget, debt, policies)",
                                 "563-598 (glossary, appendix of professional-services contracts, outcome measures)"],
        "row_level_problems": out.problems,
        "notes": out.notes + ["wrapped position titles joined: %d" % out.wrapped_titles],
        "label_repairs": sorted(REPAIRS),
        "label_suspects_not_repaired": sorted(sus),
        "section_pages": {k: len(v) for k, v in kinds_pages.items()},
        "deviations_from_schema": [
            "department_detail.table has an extra value by_program_type (Program Detail table; dim2 = '<fund> | <type>'); by_program = Expenditure Summary (dim2 = fund)",
            "workyears_citywide.csv is an extra file (citywide FTE by department/program, 2004-2012)",
            "dept_code is blank everywhere: the 2012 book prints no department numbers; program numbers are in dim1/program",
            "department_expenditures.csv: variance % column ignored (derived)",
            "revenues.fund_name: 'General Fund' for the GF statement, other funds as printed (upper case)",
        ],
    }
    (OUT / "checks.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("row_counts", "check_summary_by_tier", "check_summary_by_kind")}, indent=1))
    print("mismatches:", len(report["mismatches"]), " unparsed:", len(out.unparsed), " problems:", len(out.problems))


if __name__ == "__main__":
    main()
