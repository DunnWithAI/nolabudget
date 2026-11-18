#!/usr/bin/env python3
"""Extract tables from the City of New Orleans 2009 Adopted Operating Budget book.

Run from anywhere:   .venv/bin/python scripts/extract/extract_2009.py

Reads   data/raw/pre-2011/2009/CNO_2009_Operating_Budget-Proposed1.pdf  (read-only)
        (the file name says "Proposed" but the cover (PDF p.2) reads "Adopted 2009 Operating Budget,
        Approved December 1, 2008" and every page footer says "2009 Adopted Budget": it is the ADOPTED book)
Writes  data/extracted/2009-operating-adopted/{department_expenditures,revenues,
        department_detail,positions,printed_totals}.csv, workyears_citywide.csv (extra), checks.json

Method (same as extract_2012.py, which this script was copied from): PyMuPDF word boxes -> lines
(cluster on y) -> label / numeric cells.  Numbers are assigned to columns by x-position against the
header row of each table (nearest header centre), never by their order in the text stream.  No amount is
typed in by hand; the only hand-written data are page ranges, department-name aliases used for cross-checks
and a few label spelling repairs for PDF kerning artefacts (see clean_label / checks.json label_repairs).

Differences from the 2012 book (see also checks.json "layout_notes"):
  * The 2009 book prints ONE citywide expenditure statement (All Funds, p.78-79 = printed p.72-73, columns
    2004-2007 actual, 2008 adopted, 2009 proposed, 2009 adopted).  There are no General Fund / Other Funds
    statements by department.  The only citywide General-Fund-by-department table is on the Budget-in-Brief
    page (PDF p.10, 2008 and 2009 adopted); it is captured as fund_group=general_fund.  No other_funds rows
    exist as a statement (they would be a derived all_funds - general_fund).
  * Revenue statement p.43-47: General Fund, "Other Discretionary Funds", "Other Funds", "Special Revenue
    Fund"; columns 2005-2007 actual, 2008 adopted, 2009 proposed, 2009 adopted.
  * Department sections carry NO department numbers; dept names are in caps in the section tables and
    differ from the citywide statement's names (DEPT_ALIASES).
  * Mayor and CAO sections include their sub-offices in one set of tables (no per-sub-office summaries).
Output shape follows data/extracted/SCHEMA.md; deviations are listed in checks.json.
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
BOOK_ID = "2009-operating-adopted"
BOOK_FY = 2009
PDF = ROOT / "data/raw/pre-2011/2009/CNO_2009_Operating_Budget-Proposed1.pdf"
OUT = ROOT / "data/extracted" / BOOK_ID

# ---- book-specific page map (1-based PDF pages; verified by sanity asserts in main) -------
# printed page number = PDF page - 6 (front matter i-v)
P_CITYWIDE_SUMMARY = 77             # printed 71: CITYWIDE departmental budget summary (by type / by fund)
P_GF_BRIEF = 10                     # printed 4: Budget-in-Brief, General Fund expenditures by department
REV_PAGES = range(43, 48)           # printed 37-41: Statement of Revenues
EXP_PAGES = range(78, 80)           # printed 72-73: City-Wide Expenditures, All Funds
FTE_PAGES = range(81, 90)           # printed 75-83: City-Wide Work Years by Department / Program
DEPT_PAGES = range(90, 485)         # department sections run to the Enterprise Funds, then glossary (p.485+)

# Dept-section names -> citywide-statement names (cross-check only; output keeps printed names)
DEPT_ALIASES = {
    "CHIEF ADMINISTRATIVE OFFICE": "CAO",
    "PARKWAY & PARK COMMISSION": "Parks & Parkways",
    "HISTORIC DIST LANDMARKS COMM.": "HDLC",
    "VIEUX CARRE COMMISION": "VCC",
    "ALCOHOLIC BEVERAGE CONTR BRD.": "Alcoholic Beverage Ctrl. Bd.",
    "N O MOSQUITO CONTROL BRD.": "Mosquito Control Bd.",
    "N O MUSEUM OF ART": "Museum of Art",
    "NEIGHBORHOOD HOUSING IMPROVMNT": "Neighborhood Housing Improvement Fund",
    "CLERK OF CRIMINAL DIST COURT": "Clerk of Criminal District Court",
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
    # drop the page footer "City of New Orleans 2009 Adopted Budget, Page NN" (it can share a y-band with the
    # last table row, so only the footer's own words are removed, never a token outside its vocabulary)
    FOOT = {"City", "of", "New", "Orleans", "2009", "Adopted", "Budget,", "Page"}
    foot = [t for t in ws if t.text == "Page" and t.y0 > page.rect.height - 70]
    drop = set()
    for f in foot:
        for k, u in enumerate(ws):
            if abs(u.y0 - f.y0) < 3 and u.x0 <= f.x1 + 14 and (u.text in FOOT or (re.fullmatch(r"\d+", u.text) and 0 < u.x0 - f.x1 < 12)):
                drop.add(k)
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
    # a dash printed in the variance column (right of the last year column) is not an amount
    while toks and toks[-1].text in DASHES and toks[-1].cx > max(anchors) + 28:
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
# Tier 1a: statement of revenues (pp.43-47)
# ======================================================================================
def attach_label_orphans(lines, anchors, maxdist=14.0):
    """Number-only lines (a wrapped label prints its numbers on the band between its two label lines) are
    attached to the nearest label-only line within maxdist points that has no numbers of its own."""
    def isnum(t):
        return parse_num(t.text, False) is not None

    def yc(l):
        return sum((t.y0 + t.y1) / 2 for t in l) / len(l)

    out = [list(l) for l in lines]
    keep = [True] * len(out)
    for i, l in enumerate(out):
        if not keep[i] or not all(isnum(t) or t.text == "$" for t in l):
            continue
        cands = []
        for k in (i - 1, i + 1):
            if 0 <= k < len(out) and keep[k] and not any(isnum(t) for t in out[k]) and abs(yc(out[k]) - yc(l)) <= maxdist \
                    and not (out[k][0].text.startswith("SOURCES,") and out[k][0].x0 < 60 and i > 0 and out[k] is not out[i - 1]):
                cands.append(k)
        if cands:
            k = min(cands, key=lambda k: abs(yc(out[k]) - yc(l)))
            out[k] = sorted(out[k] + l, key=lambda t: t.x0)
            keep[i] = False
    return [l for l, kp in zip(out, keep) if kp]


REV_HEADER_ANOMALIES = []
HDR_VOCAB = {"Program", "Actual", "Adopted", "Proposed", "No.", "Pay", "Grade", "Personal", "Services", "Other", "Operating",
             "Debt", "Service", "Total", "Source", "Department", "Variance", "-", "Estimated", "/"}


def parse_revenues(doc, out):
    state = dict(group="general_fund", section="GENERAL FUND", fund="General Fund", cat=None)
    cols = None
    totals = []   # dict(label, page, ctx, data, cols)
    prev_label_only = None
    for pno in REV_PAGES:
        lines = get_lines(doc[pno - 1])
        # the header block (years / Actual-Adopted-Proposed / Source) is repeated at the top of each table; a page can
        # carry two of them (p.46: General Fund continuation, then "Other Discretionary Funds").
        body, k = [], 0
        while k < len(lines):
            ln = lines[k]
            if any(re.fullmatch(r"20\d\d", t.text) for t in ln) and all(t.text in HDR_VOCAB or re.fullmatch(r"20\d\d", t.text) for t in ln):
                htoks, j2 = header_cols(lines, k, extra_ok=("Source",))
                c2 = year_basis_columns(htoks, 6)
                if c2 is None:
                    out.unp(pno, "revenue header", "could not pair years with basis words")
                else:
                    # 2007 is headed "Actual" on p.43 but "Adopted" on p.44-47 of the same statement.  The 2007 column holds
                    # the actual figures (they differ from the 2008 book's 2007 *adopted* and agree with the 2012 book's 2007
                    # actuals -- see checks.json cross_book).  basis is therefore normalised to "actual" and logged.
                    cols = []
                    for cx, fy, b in c2:
                        if fy == 2007 and b != "actual":
                            REV_HEADER_ANOMALIES.append(dict(page=pno, printed="2007 %s" % b.capitalize(), used="2007 actual"))
                            b = "actual"
                        cols.append((cx, fy, b))
                k = j2
                continue
            body.append(ln)
            k += 1
        lines = body
        if cols is None:
            continue
        anchors = [c[0] for c in cols]
        lines = attach_label_orphans(lines, anchors)
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            i += 1
            if re.match(r"STATEMENT OF REVENUES|DISCRETIONARY AND OTHER", text):
                continue
            label, data, probs = split_row(ln, anchors)
            lab = clean_label(ltext(label))
            if not data:
                if prev_label_only is not None and re.search(r"FINANCING$", prev_label_only) and lab.startswith("SOURCES,"):
                    # continuation of a wrapped label (handled via last total/row below)
                    out.notes.append("p.%d: label continuation %r after %r" % (pno, lab, prev_label_only))
                    prev_label_only = None
                    continue
                if lab.startswith("GENERAL FUND"):
                    state.update(group="general_fund", section="GENERAL FUND", fund="General Fund", cat=None)
                elif lab.startswith("OTHER DISCRETIONARY FUNDS"):
                    state.update(group="other_funds", section="OTHER DISCRETIONARY FUNDS", fund=None, cat=None)
                elif lab == "OTHER FUNDS":
                    state.update(group="other_funds", section="OTHER FUNDS", fund=None, cat=None)
                elif lab.startswith("SPECIAL REVENUE FUND"):
                    state.update(group="other_funds", section="SPECIAL REVENUE FUND", fund=None, cat=None)
                elif state["group"] == "general_fund":
                    state["cat"] = lab
                else:
                    state["fund"] = lab
                prev_label_only = lab
                continue
            if prev_label_only and prev_label_only.count("(") > prev_label_only.count(")") and lab.count(")") > lab.count("("):
                lab = prev_label_only + " " + lab          # label wrapped over two lines, numbers printed on the second
            prev_label_only = None
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            first_x = ln[0].x0
            U = lab.upper()
            indented = first_x > 55          # other-funds line items are indented (x~61); funds / totals sit at the margin
            is_total = bool(re.search(r"\bTOTAL\b|SUB-TOTAL", U)) and not (state["group"] == "other_funds" and indented)
            if "TOTAL REVENUES AND OTHER FINANCING" in U:
                # printed label wraps ("... FINANCING / SOURCES, GENERAL FUND") with the numbers between the two lines;
                # the wrapped tail is re-attached here.  The next printed line, "SPECIAL REVENUE FUND", is a new heading.
                lab = lab + " SOURCES, GENERAL FUND"
            if is_total:
                totals.append(dict(label=lab, page=pno, ctx=dict(state), data=data, cols=cols))
                for c, v in data.items():
                    if v != "DASH":
                        out.total(cols[c][1], cols[c][2], None, lab, v, pno)
                        out.tot[-1]["_rev_total_idx"] = len(totals) - 1
                continue
            if state["group"] == "other_funds" and not indented and " - FUND" in lab:
                # a fund printed on one line with its only revenue line (HUD, LLE, Grants/Contributions, EDF, ...)
                fund, line = lab, lab
            elif state["group"] == "other_funds" and not indented and state["section"] == "OTHER DISCRETIONARY FUNDS":
                fund, line = lab, lab
            else:
                fund = state["fund"] if state["group"] == "general_fund" or indented else lab
                line = lab
            if state["group"] == "other_funds" and indented and state["fund"] is None:
                out.unp(pno, "line %r" % text, "indented revenue line with no fund heading")
            if state["group"] == "other_funds" and not indented:
                state["fund"] = fund
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.rev.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                    fund_group=state["group"], fund_name=fund or "",
                                    category=(state["cat"] if state["group"] == "general_fund" else state["section"]) or "",
                                    line_name=line, amount=v, page=pno))
    # -- scopes + checks
    def rule(t):
        U, ctx = t["label"].upper(), t["ctx"]
        if U.startswith("CONSOLIDATED TOTAL"):
            return "revenues/all_funds", lambda r: True
        if "TOTAL REVENUES AND OTHER FINANCING" in U:
            return "revenues/general_fund_plus_other_discretionary_plus_other_funds", lambda r: r["category"] != "SPECIAL REVENUE FUND"
        if U.startswith("SPECIAL REVENUE FUND TOTAL"):
            return "revenues/special_revenue_fund", lambda r: r["category"] == "SPECIAL REVENUE FUND"
        if U.startswith("TOTAL OTHER FUNDS"):
            return "revenues/other_funds", lambda r: r["category"] == "OTHER FUNDS"
        if U.startswith("TOTAL DISCRETIONARY"):
            return "revenues/discretionary", lambda r: r["fund_group"] == "general_fund" or r["category"] == "OTHER DISCRETIONARY FUNDS"
        if U.startswith("TOTAL GENERAL FUND"):
            return "revenues/General Fund", lambda r: r["fund_group"] == "general_fund"
        if ctx["group"] == "general_fund":
            c = ctx["cat"] or ""
            return "revenues/General Fund/%s" % c, lambda r: r["fund_group"] == "general_fund" and r["category"] == c
        f = ctx["fund"] or ""
        return "revenues/%s" % f, lambda r: r["fund_group"] == "other_funds" and r["fund_name"] == f
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
    # general fund + discretionary pieces (printed relationships between printed totals)
    tv = defaultdict(dict)
    for r in out.tot:
        if r["scope"].startswith("revenues/"):
            tv[(r["fiscal_year"], r["basis"])][r["scope"]] = (r["amount"], r["page"])
    for k, d in sorted(tv.items()):
        gf, sg = d.get("revenues/General Fund"), None
        # tax categories sum to printed general fund total is covered by revenue_total; here: printed total-vs-total
        sr, of, rg, al = d.get("revenues/special_revenue_fund"), d.get("revenues/other_funds"), d.get("revenues/general_fund_plus_other_discretionary_plus_other_funds"), d.get("revenues/all_funds")
        disc = d.get("revenues/discretionary")
        if rg and al and sr:
            out.check(1, "printed_total_vs_printed_total", "revenues: (GF+discretionary+other funds) + special revenue = consolidated", k[0], k[1], rg[0] + sr[0], al[0], al[1])
        if disc and of and rg:
            out.check(1, "printed_total_vs_printed_total", "revenues: discretionary + other funds = GF & other funds total", k[0], k[1], disc[0] + of[0], rg[0], rg[1])


# ======================================================================================
# Tier 1b: statement of expenditures by department (pp.78-79, All Funds) + GF by department (p.10)
# ======================================================================================
def parse_dept_expenditures(doc, out):
    cols = None
    for pno in EXP_PAGES:
        lines = get_lines(doc[pno - 1])
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, 7)
                if cols is None:
                    out.unp(pno, "expenditure header", "could not pair years with basis words")
                i = j
                continue
            i += 1
            if cols is None or text in ("City-Wide Expenditures", "All Funds"):
                continue
            label, data, probs = split_row(ln, [c[0] for c in cols])
            lab = clean_label(ltext(label))
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if lab == "Total":
                for c, v in data.items():
                    if v != "DASH":
                        out.total(cols[c][1], cols[c][2], "department_expenditures/all_funds", lab, v, pno)
                continue
            if not data:
                out.unp(pno, "line %r" % text, "no numbers in expenditure statement row")
                continue
            for c, v in data.items():
                if v == "DASH":
                    continue
                out.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                         fund_group="all_funds", dept_code="", dept_name=lab, amount=v, page=pno))
    # General Fund by department: Budget-in-Brief p.10, right-hand table (2008 adopted, 2009 adopted)
    lines = get_lines(doc[P_GF_BRIEF - 1])
    right = [[t for t in l if t.x0 >= 300] for l in lines]
    right = [l for l in right if l]
    hdr = next((k for k, l in enumerate(right) if ltext(l) == "2008 2009"), None)     # years line sits just above "Department Adopted Adopted"
    if hdr is None or ltext(right[hdr + 1]) != "Department Adopted Adopted":
        out.unp(P_GF_BRIEF, "general fund expenditures by department", "header not found")
    else:
        ys = sorted([t for t in right[hdr]], key=lambda t: t.cx)
        gcols = [(ys[0].cx, 2008, "adopted"), (ys[1].cx, 2009, "adopted")]
        for ln in right[hdr + 2:]:
            if ltext(ln).startswith("City of New Orleans"):
                continue
            label, data, probs = split_row(ln, [c[0] for c in gcols])
            lab = clean_label(ltext(label))
            if not data:
                out.unp(P_GF_BRIEF, "line %r" % ltext(ln), "no numbers in GF expenditure row")
                continue
            if lab == "Total":
                for c, v in data.items():
                    out.total(gcols[c][1], gcols[c][2], "department_expenditures/general_fund", lab, v, P_GF_BRIEF)
                continue
            for c, v in data.items():
                if v != "DASH":
                    out.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=gcols[c][1], basis=gcols[c][2], fund_group="general_fund",
                                             dept_code="", dept_name=lab, amount=v, page=P_GF_BRIEF))
    # checks: department rows vs printed 'Total'
    for group in ("general_fund", "all_funds"):
        sums = defaultdict(int)
        for r in out.dept_exp:
            if r["fund_group"] == group:
                sums[(r["fiscal_year"], r["basis"])] += r["amount"]
        for t in out.tot:
            if t["scope"] == "department_expenditures/" + group:
                out.check(1, "dept_statement_total", t["scope"], t["fiscal_year"], t["basis"],
                          sums[(t["fiscal_year"], t["basis"])], t["amount"], t["page"])
    # GF-by-department (p.10, 2008/2009 adopted) must be <= all funds for the same department
    by = defaultdict(dict)
    for r in out.dept_exp:
        by[(norm(r["dept_name"]), r["fiscal_year"], r["basis"])][r["fund_group"]] = (r["amount"], r["page"], r["dept_name"])
    for (n, fy, b), g in sorted(by.items()):
        if "general_fund" in g and "all_funds" in g:
            if g["general_fund"][0] > g["all_funds"][0]:
                out.notes.append("p.10 GF %s (%s %s) exceeds the all-funds statement figure %s" % (g["general_fund"][0], fy, b, g["all_funds"][0]))
        elif "general_fund" in g and "all_funds" not in g:
            out.notes.append("p.10 general-fund department %r (%s %s) has no all-funds statement row" % (g["general_fund"][2], fy, b))


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
    if m:
        kind = {"PROGRAM DETAIL": "detail", "EXPENDITURE SUMMARY": "expsum", "PERSONNEL SUMMARY": "personnel"}[m.group(2)]
        return kind, m.group(1)          # name may be "" (printed on the line above, or carried from the previous page)
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
        if kind != "summary" and not dept:
            prev = lines[i - 1] if i > 0 else None
            if prev and prev[0].x0 < 150 and not any(re.search(r"\d", t.text) for t in prev) and not any(t.text in ("Actual", "Adopted") for t in prev):
                dept = clean_label(ltext(prev))
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
    lines = attach_label_orphans(lines[j:], anchors, maxdist=14.0)
    j = 0
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
    if not htoks and state.get("cols"):
        cols = state["cols"]              # untitled continuation page (e.g. only the DEPARTMENT TOTAL row)
    elif kind == "detail":
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
    state["cols"] = cols
    lines = attach_label_orphans(lines[j:], [c[0] for c in cols], maxdist=9.0)
    j = 0
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
    if not htoks and state.get("hdr"):
        yb, gcx = state["hdr"]            # untitled continuation page
    else:
        yb = year_basis_columns(htoks, 3)
        grade = [t for t in htoks if t.text == "Grade"]
        if yb is None or not grade:
            out.unp(pno, "%s personnel summary" % dept, "header columns not recognised")
            return
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
    last = None            # (kind, dept) of the previous titled segment, for untitled continuation pages
    summary_page = {}
    cur_dept = None
    for pno in DEPT_PAGES:
        lines = get_lines(doc[pno - 1])
        segs = segments(lines)
        if not segs:
            nums = sum(1 for l in lines for t in l if parse_num(t.text) not in (None, "DASH") or parse_num(t.text, True) not in (None, "DASH"))
            if doc[pno - 1].rect.width > 700 and nums >= 2 and last is not None and last[0] != "summary":
                # untitled continuation page of the previous table (e.g. only the DEPARTMENT TOTAL row)
                segs = [(last[0], last[1], lines)]
                out.notes.append("p.%d: untitled continuation page parsed as %s of %s" % (pno, last[0], last[1]))
            else:
                out.narr_pages.append(pno)
                continue
        for kind, dept, body in segs:
            if kind == "summary":
                cur_dept = dept
                summary_page[dept] = pno
            elif not dept:
                dept = cur_dept
                out.notes.append("p.%d: %s title without department name; taken from previous page: %r" % (pno, kind, dept))
            elif dept != cur_dept and dept in summary_page and pno - summary_page.get(cur_dept, -99) <= 3:
                # p.474: the Municipal Yacht Harbor personnel table, printed directly after that fund's summary page, is
                # titled "PROPERTY MANAGEMENT PERSONNEL SUMMARY" (a copy of the p.269 title).  Attributed to the section it sits in.
                out.notes.append("p.%d: %s table titled %r sits inside the %r section (summary on p.%d); attributed to %r"
                                 % (pno, kind, dept, cur_dept, summary_page[cur_dept], cur_dept))
                dept = cur_dept
            else:
                cur_dept = dept
            last = (kind, dept)
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
    drop_duplicate_personnel(out)


def drop_duplicate_personnel(out):
    """A personnel table can be printed twice (the Municipal Yacht Harbor table appears inside PROPERTY MANAGEMENT's
    Personnel Summary on p.269 and again, headed 'PROPERTY MANAGEMENT', in the Enterprise Funds section p.474).
    A page whose position rows are all already present on an earlier page of the same department is dropped (rows and
    its printed FTE totals), otherwise FTEs would be counted twice.  Logged in notes."""
    by_page = defaultdict(list)
    for r in out.pos:
        by_page[(r["dept_name"], r["page"])].append(r)
    sig = lambda r: (r["fund"], r["program"], r["position_title"], r["pay_grade"], r["fiscal_year"], r["basis"], str(r["fte"]))
    seen = defaultdict(set)
    drop_pages = set()
    for (d, pg) in sorted(by_page, key=lambda k: (k[0], k[1])):
        rows = by_page[(d, pg)]
        ss = {sig(r) for r in rows}
        if len(ss) >= 4 and ss <= seen[d]:
            drop_pages.add((d, pg))
            out.notes.append("p.%d: personnel table for %s repeats rows already printed on an earlier page; dropped as a duplicate (%d position rows)" % (pg, d, len(rows)))
        else:
            seen[d] |= ss
    if drop_pages:
        out.pos[:] = [r for r in out.pos if (r["dept_name"], r["page"]) not in drop_pages]
        out.tot[:] = [t for t in out.tot if not (t["scope"].startswith("positions/") and (t["scope"].split("/")[1], t["page"]) in drop_pages)]


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
                        extra = sum(x["amount"] for x in D.get((d, "by_type"), []) if x["fiscal_year"] == BOOK_FY and x["basis"] == "adopted"
                                    and x["dim1"] in ("RESERVES", "GRANTS, CONTRIB. & FUND TRAN."))
                        note = ""
                        if typ == "Other Operating" and extra and t["amount"] - r["amount"] == extra:
                            note = "difference equals RESERVES + GRANTS, CONTRIB. & FUND TRAN. (%d) printed in the department summary: Program Detail has no such columns and folds them into Other Operating" % extra
                        out.check(2, "program_detail_vs_budget_summary", "department_detail/%s/%s" % (d, typ), BOOK_FY, "adopted", t["amount"], r["amount"], t["page"], note)
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

    # (d) citywide statement (All Funds, p.78-79; General Fund, p.10) vs department sections
    stm = defaultdict(dict)   # (name_norm, fy, basis) -> {fund_group: amount}
    for r in out.dept_exp:
        stm[(norm(r["dept_name"]), r["fiscal_year"], r["basis"])][r["fund_group"]] = r["amount"]
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
                continue
            if "all_funds" in g:
                out.check(2, "dept_section_vs_citywide_all_funds", "department_detail/%s" % d, fy, b, t["amount"], g["all_funds"], t["page"],
                          "statement name: %s" % DEPT_ALIASES.get(d, d))
            if "general_fund" in g:
                out.check(2, "dept_section_GF_vs_p10_general_fund", "department_detail/%s" % d, fy, b, gf.get((fy, b), 0), g["general_fund"], t["page"])
    for n in sorted(stm_names - matched):
        if any(abs(v) > 0 for (k, fy, b), g in stm.items() if k == n for v in g.values()):
            out.notes.append("citywide statement department %r has non-zero amounts but no department section" % n)

    # (e) citywide summary p77: sum of departments vs printed citywide.  Only the departments that appear in
    # the citywide expenditure statement are summed (the enterprise-fund entities are not part of the statement).
    in_stmt = {d for d in depts if norm(DEPT_ALIASES.get(d, d)) in stm_names and d != "CITYWIDE"}
    cw = defaultdict(list)
    for r in out.cw_rows:
        cw[(r["table"], r["dim1"])].append(r)
    for (tbl, dim1), rs in sorted(cw.items()):
        for r in rs:
            s_ = 0
            for d in depts:
                if d not in in_stmt:
                    continue
                for x in D.get((d, tbl), []):
                    if x["dim1"] == dim1 and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]:
                        s_ += x["amount"]
            out.check(2, "sum_of_departments_vs_citywide_summary", "citywide/%s/%s" % (tbl, dim1), r["fiscal_year"], r["basis"], s_, r["amount"], r["page"])
    for t in [t for t in out.tot if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund")]:
        s_ = 0
        tbl = t["scope"].rsplit("/", 1)[1]
        for d in depts:
            if d not in in_stmt:
                continue
            for x in D.get((d, tbl), []):
                if x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]:
                    s_ += x["amount"]
        out.check(2, "sum_of_departments_vs_citywide_summary", t["scope"] + " TOTAL", t["fiscal_year"], t["basis"], s_, t["amount"], t["page"])
    # (f) citywide summary p77 totals vs the citywide statement totals / p10 GF total
    for t in out.tot:
        if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund"):
            kind = "citywide_summary_p77_%s_vs_statement_total" % t["scope"].rsplit("/", 1)[1]
            for x in out.tot:
                if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]:
                    out.check(1, kind, "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
    for r in [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND"]:
        for x in out.tot:
            if x["scope"] == "department_expenditures/general_fund" and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]:
                out.check(1, "citywide_summary_p77_GF_vs_p10_GF_total", "department_expenditures/general_fund", r["fiscal_year"], r["basis"], r["amount"], x["amount"], r["page"])
    # statement total vs printed revenue total (balanced budget: adopted all-funds expenditure = all-funds revenue)
    rv = {(t["fiscal_year"], t["basis"]): t for t in out.tot if t["scope"] == "revenues/all_funds"}
    for t in out.tot:
        if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) in rv and t["basis"] != "actual":
            out.check(1, "all_funds_expenditure_total_vs_all_funds_revenue_total", "department_expenditures/all_funds", t["fiscal_year"], t["basis"],
                      t["amount"], rv[(t["fiscal_year"], t["basis"])]["amount"], t["page"], "balanced-budget identity; actual years are not expected to match")
    rgf = {(t["fiscal_year"], t["basis"]): t for t in out.tot if t["scope"] == "revenues/General Fund"}
    for t in out.tot:
        if t["scope"] == "department_expenditures/general_fund" and (t["fiscal_year"], t["basis"]) in rgf:
            out.check(1, "general_fund_expenditure_total_vs_general_fund_revenue_total", "department_expenditures/general_fund", t["fiscal_year"], t["basis"],
                      t["amount"], rgf[(t["fiscal_year"], t["basis"])]["amount"], t["page"], "balanced-budget identity")

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
# ---- cross-book comparison with the 2012 extraction (read-only) --------------------------------------------
X2012 = ROOT / "data/extracted/2012-operating-adopted"
# statement department names (2009 book -> 2012 book); everything else matches after norm()
X_DEPT_ALIASES = {
    "Alcoholic Ctrl. Bd.": "Alcoholic Bvg Ctrl. Bd.", "Alcoholic Beverage Ctrl. Bd.": "Alcoholic Bvg Ctrl. Bd.",
    "City Planning Commission": "City Planning Comm.", "Clerk of Criminal District Court": "Clrk of Crim. Dist. Crt",
    "Economic Development Fund": "Economic Dev. Fund", "Neighborhood Housing Improvement Fund": "N\u2019hood Hsg Imprv. Fnd",
    "Workforce Investment Act": "Workforce Invst. Act", "Office of Training Division": "Office of Training Div.",
}
X_FUND_ALIASES = {"LOCAL LAW ENFORCEMENT GRANTS": "LLE GRANTS"}


def cross_book(out):
    """2009 book actuals (2004-2007) vs the 2012 book's actuals for the same years.  Reports agreement; differences
    are restatements or errors, reported not fixed."""
    res = {}
    if not X2012.exists():
        return {"skipped": "2012 extraction not found"}
    # -- departments, all funds
    ref = {}
    for r in csv.DictReader(open(X2012 / "department_expenditures.csv", encoding="utf-8")):
        if r["fund_group"] == "all_funds" and r["basis"] == "actual":
            ref[(norm(r["dept_name"]), int(r["fiscal_year"]))] = (int(r["amount"]), r["dept_name"])
    cmp_, agree, diffs, unmatched = 0, 0, [], set()
    for r in out.dept_exp:
        if r["fund_group"] != "all_funds" or r["basis"] != "actual":
            continue
        k = (norm(X_DEPT_ALIASES.get(r["dept_name"], r["dept_name"])), r["fiscal_year"])
        if k not in ref:
            unmatched.add(r["dept_name"])
            continue
        cmp_ += 1
        if ref[k][0] == r["amount"]:
            agree += 1
        else:
            diffs.append(dict(dept_2009=r["dept_name"], dept_2012=ref[k][1], fiscal_year=r["fiscal_year"], book_2009=r["amount"], book_2012=ref[k][0],
                              diff=r["amount"] - ref[k][0], page_2009=r["page"]))
    res["department_expenditures_all_funds_actuals"] = dict(compared=cmp_, agree=agree, differ=len(diffs), years="2004-2007",
                                                           departments_without_counterpart_in_2012=sorted(unmatched), differences=diffs)
    # -- revenues: general fund lines and other-funds totals by fund
    refl, reff = {}, defaultdict(int)
    for r in csv.DictReader(open(X2012 / "revenues.csv", encoding="utf-8")):
        if r["basis"] != "actual":
            continue
        if r["fund_group"] == "general_fund":
            refl[(norm(r["category"]), norm(r["line_name"]), int(r["fiscal_year"]))] = int(r["amount"])
        else:
            reff[(norm(r["fund_name"]), int(r["fiscal_year"]))] += int(r["amount"])
    cmp_, agree, diffs, unm = 0, 0, [], set()
    for r in out.rev:
        if r["fund_group"] != "general_fund" or r["basis"] != "actual":
            continue
        cat = r["category"]
        k = (norm(cat), norm(r["line_name"]), r["fiscal_year"])
        if k not in refl:
            unm.add((cat, r["line_name"]))
            continue
        cmp_ += 1
        if refl[k] == r["amount"]:
            agree += 1
        else:
            diffs.append(dict(category=cat, line=r["line_name"], fiscal_year=r["fiscal_year"], book_2009=r["amount"], book_2012=refl[k],
                              diff=r["amount"] - refl[k], page_2009=r["page"]))
    res["revenues_general_fund_lines_actuals"] = dict(compared=cmp_, agree=agree, differ=len(diffs), years="2005-2007",
                                                     lines_without_counterpart_in_2012=sorted("%s / %s" % u for u in unm), differences=diffs)
    sums = defaultdict(int)
    for r in out.rev:
        if r["fund_group"] == "other_funds" and r["basis"] == "actual":
            nm = re.sub(r"\s*-\s*FUND\b.*$", "", r["fund_name"])
            nm = re.sub(r",\s*FUND \d+.*$", "", nm)
            nm = X_FUND_ALIASES.get(nm, nm)
            sums[(norm(nm), r["fiscal_year"], nm)] += r["amount"]
    cmp_, agree, diffs, unm = 0, 0, [], set()
    for (n, fy, nm), v in sorted(sums.items()):
        if (n, fy) not in reff:
            unm.add(nm)
            continue
        cmp_ += 1
        if reff[(n, fy)] == v:
            agree += 1
        else:
            diffs.append(dict(fund=nm, fiscal_year=fy, book_2009=v, book_2012=reff[(n, fy)], diff=v - reff[(n, fy)]))
    res["revenues_other_funds_totals_by_fund_actuals"] = dict(compared=cmp_, agree=agree, differ=len(diffs), years="2005-2007",
                                                             funds_without_counterpart_in_2012=sorted(unm), differences=diffs)
    return res



def spot_checks(out, detail_rows, n_per_file=20, seed=2009):
    """Independent-engine spot check: random cells re-read with pdfplumber (a different PDF text engine from the
    PyMuPDF extractor).  A cell passes when pdfplumber finds the printed number on the cited page on the same text
    line as the first word of the row label."""
    import random
    import pdfplumber
    rnd = random.Random(seed)
    files = {
        "department_expenditures": (out.dept_exp, "dept_name"),
        "revenues": (out.rev, "line_name"),
        "department_detail": (detail_rows, "dim1"),
        "positions": (out.pos, "position_title"),
    }
    results, summary = [], {}
    with pdfplumber.open(str(PDF)) as pl:
        cache = {}

        def page_lines(pno):
            if pno not in cache:
                words = pl.pages[pno - 1].extract_words(keep_blank_chars=False, use_text_flow=False)
                cache[pno] = words
            return cache[pno]
        for name, (rows, labkey) in files.items():
            sample = rnd.sample(rows, min(n_per_file, len(rows)))
            ok = 0
            for r in sample:
                amt = r.get("amount", r.get("fte"))
                if isinstance(amt, Decimal):
                    txt = [str(amt)]
                else:
                    txt = ["{:,}".format(abs(amt))]
                words = page_lines(r["page"])
                nums = [w for w in words if re.sub(r"[$()]", "", w["text"]) in txt]
                first = clean_label(str(r[labkey])).split(" ")[0]
                labw = [w for w in words if w["text"] == first or w["text"].startswith(first[:4])]
                hit = any(abs(n["top"] - l["top"]) < 4 for n in nums for l in labw)
                neg_ok = True
                if not isinstance(amt, Decimal) and amt < 0:
                    neg_ok = any(w["text"].startswith("(") or w["text"].startswith("-") or w["text"].startswith("($") for w in nums)
                passed = bool(hit and neg_ok)
                ok += passed
                results.append(dict(file=name, page=r["page"], label=r[labkey], fiscal_year=r["fiscal_year"], basis=r["basis"],
                                    extracted=str(amt), engine="pdfplumber", passed=passed))
            summary[name] = dict(sampled=len(sample), passed=ok)
    return dict(seed=seed, method="pdfplumber word boxes: printed number and first label word on the same line of the cited page", summary=summary, cells=results)



def footing_share(out, detail_rows, pos_rows):
    """Share of cells that sit in a table which foots.  A 'table' is the set of rows covered by one family of printed-total
    checks: department_expenditures by fund_group, revenues by page, department_detail by (department, table), positions by
    department.  A table foots exactly when every printed-total check on it is exact; 'within_1' when all are exact or
    within $1 (cents rounding in actual-year cells); a table with no printed-total check is 'no_printed_total'."""
    PT = [c for c in out.checks if c["against"] == "printed_total"]
    rank = {"exact": 0, "within_1": 1, "mismatch": 2}

    def worst(preds):
        sts = [c["status"] for c in PT if preds(c)]
        if not sts:
            return "no_printed_total"
        return max(sts, key=lambda s: rank[s])
    files = {}
    # department_expenditures
    files["department_expenditures"] = [worst(lambda c, g=r["fund_group"]: c["scope"] == "department_expenditures/" + g and c["fiscal_year"] == r["fiscal_year"] and c["basis"] == r["basis"]) for r in out.dept_exp]
    def rev_scopes(r):
        # the smallest printed total covering the cell: category total (General Fund), the fund's own total, or the section total
        if r["fund_group"] == "general_fund":
            return {"revenues/General Fund/%s" % r["category"]}
        return {"revenues/%s" % r["fund_name"], {"OTHER FUNDS": "revenues/other_funds", "SPECIAL REVENUE FUND": "revenues/special_revenue_fund",
                                                  "OTHER DISCRETIONARY FUNDS": "revenues/discretionary"}.get(r["category"], "")}
    rcache = {}
    stat = []
    for r in out.rev:
        k = (r["fund_group"], r["category"], r["fund_name"], r["fiscal_year"], r["basis"])
        if k not in rcache:
            sc = rev_scopes(r)
            own = [c for c in PT if c["kind"] == "revenue_total" and c["scope"] in sc and c["fiscal_year"] == r["fiscal_year"] and c["basis"] == r["basis"]]
            # a fund printed with its own total is judged by that total; a single-line fund by the section total
            first = [c for c in own if c["scope"] == "revenues/%s" % r["fund_name"] or r["fund_group"] == "general_fund"] or own
            rcache[k] = max((c["status"] for c in first), key=lambda x: rank[x]) if first else "no_printed_total"
        stat.append(rcache[k])
    files["revenues"] = stat
    cache = {}
    stat = []
    for r in detail_rows:
        k = (r["dept_name"], r["table"], r["fiscal_year"], r["basis"])
        if k not in cache:
            pre = "department_detail/%s/%s" % (r["dept_name"], r["table"])
            cache[k] = worst(lambda c, pre=pre, r=r: (c["scope"] == pre or c["scope"].startswith(pre + "/")) and c["fiscal_year"] == r["fiscal_year"] and c["basis"] == r["basis"])
        stat.append(cache[k])
    files["department_detail"] = stat
    cache = {}
    stat = []
    for r in pos_rows:
        k = (r["dept_name"], r["fiscal_year"], r["basis"])
        if k not in cache:
            pre = "positions/%s" % r["dept_name"]
            cache[k] = worst(lambda c, pre=pre, r=r: (c["scope"] == pre or c["scope"].startswith(pre + "/")) and c["fiscal_year"] == r["fiscal_year"] and c["basis"] == r["basis"])
        stat.append(cache[k])
    files["positions"] = stat
    res = {}
    allc = defaultdict(int)
    for name, st in files.items():
        n = len(st)
        cnt = {k: st.count(k) for k in ("exact", "within_1", "mismatch", "no_printed_total")}
        res[name] = dict(cells=n, **cnt, share_foot_exactly=round(cnt["exact"] / n, 4), share_foot_exactly_or_within_1=round((cnt["exact"] + cnt["within_1"]) / n, 4))
        for k, v in cnt.items():
            allc[k] += v
    n = sum(r["cells"] for r in res.values())
    res["ALL_FILES"] = dict(cells=n, **dict(allc), share_foot_exactly=round(allc["exact"] / n, 4),
                            share_foot_exactly_or_within_1=round((allc["exact"] + allc["within_1"]) / n, 4))
    return res



def write_csv(name, header, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(r[k]) if isinstance(r[k], Decimal) else r[k]) for k in header})


def sanity(doc):
    def has(p, s_):
        return s_ in doc[p - 1].get_text()
    assert has(2, "Adopted") and has(2, "Approved December 1, 2008")
    assert has(P_CITYWIDE_SUMMARY, "CITYWIDE") and has(P_CITYWIDE_SUMMARY, "TOTAL EXPENDITURES")
    assert has(43, "GENERAL FUND - 001") and has(47, "CONSOLIDATED TOTAL")
    assert has(78, "City-Wide Expenditures") and has(79, "Total")
    assert has(P_GF_BRIEF, "GENERAL FUND EXPENDITURES")
    assert has(94, "COUNCIL") and has(483, "PIAZZA")


def main():
    doc = fitz.open(str(PDF))
    assert doc.page_count == 488, doc.page_count
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
    # citywide summary p.77 (parsed with the department-summary parser, kept for checks only)
    lines = get_lines(doc[P_CITYWIDE_SUMMARY - 1])
    seg = segments(lines)[0]
    tmp_type, tmp_fund = [], []
    parse_summary_page(seg[2], 0, P_CITYWIDE_SUMMARY, seg[1], out, tmp_type, tmp_fund)
    out.cw_rows = tmp_type + tmp_fund
    parse_departments(doc, out)
    dept_checks(out)

    # ---- tables that exist but are deliberately not extracted (recorded, not guessed)
    def pages_with(rng, key):
        return [p for p in rng if key in doc[p - 1].get_text()[:900]]
    for key, why in (
            ("Program Name:", "program narrative pages: funding offer text per program (2008/2009 adopted funding, FTE as prose) + performance measures; free text, not tabular"),
            ("Performance", "KPI actual-vs-target tables with mixed units; not parsed")):
        pgs = pages_with(out.narr_pages, key)
        if pgs:
            out.unp("%d pages: %s" % (len(pgs), ",".join(map(str, pgs))), key, why)
    out.unp("1-42 (excl. 10)", "narrative, Budget-in-Brief, financial policies, revenue descriptions, five-year plan (p.36, p.41), fund balance",
            "not attempted: outside the requested tiers (p.10 General Fund expenditures by department is captured; the p.10 revenue table duplicates the p.43-47 statement for 2009 adopted only)")
    out.unp("48-76", "statement of debt, capital budget review (p.56-57 'City Bond and Miscellaneous Capital Funds by Department'), FEMA project lists",
            "not attempted: outside the requested tiers")
    out.unp("80-89", "City-Wide Work Years by Department / Program (printed p.74-83), 2004-2009",
            "table is printed rotated 90 degrees with programs as columns and years as rows; not extracted in this pass (department Personnel Summaries are extracted instead and checked against their printed FTE totals)")
    out.unp("485-488", "glossary", "narrative")
    out.unp("sub-offices of the Mayor and CAO", "no DEPARTMENTAL BUDGET SUMMARY per sub-office",
            "one summary / one set of program tables per department; ORDA, Homeland Security, Emergency Preparedness etc. appear as programs inside MAYOR and CHIEF ADMINISTRATIVE OFFICE")
    printed_checked = {(c["scope"].replace(" TOTAL", ""), c["fiscal_year"], c["basis"]) for c in out.checks}
    unchecked = defaultdict(int)
    for t in out.tot:
        if (t["scope"], t["fiscal_year"], t["basis"]) not in printed_checked:
            unchecked[t["scope"].split("/")[0]] += 1

    labels = set()
    for r in out.rev:
        labels.add(r["line_name"])
    for r in out.dept_exp:
        labels.add(r["dept_name"])
    for r in out.detail:
        labels.add(r["dim1"])
    for r in out.pos:
        labels.add(r["position_title"])
    sus = set()
    for lab in labels:
        toks = lab.split()
        for a_, b_ in zip(toks, toks[1:]):
            ja = (a_ + b_).lower()
            if a_.isalpha() and b_.isalpha() and len(ja) >= 5 and VOCAB.get(ja, 0) >= 2 and (len(a_) <= 2 or len(b_) <= 2):
                sus.add("%s  [%s + %s -> %s]" % (lab, a_, b_, ja))

    # ---- write
    write_csv("department_expenditures.csv", ["book_id", "fiscal_year", "basis", "fund_group", "dept_code", "dept_name", "amount", "page"], out.dept_exp)
    write_csv("revenues.csv", ["book_id", "fiscal_year", "basis", "fund_group", "fund_name", "category", "line_name", "amount", "page"], out.rev)
    detail_rows = [r for r in out.detail if r["dept_name"] != "CITYWIDE"]
    write_csv("department_detail.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "table", "dim1", "dim2", "amount", "page"], detail_rows)
    write_csv("positions.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "program", "fund", "position_title", "pay_grade", "fte", "page"], out.pos)
    write_csv("printed_totals.csv", ["book_id", "fiscal_year", "basis", "scope", "label", "amount", "page"], out.tot)

    # ---- checks.json
    PRINTED_TOTAL_KINDS = {
        "revenue_total", "dept_statement_total", "summary_total", "program_sum_vs_fund_total", "program_sum_vs_dept_total",
        "fund_totals_vs_dept_total", "program_detail_sum_vs_fund_total", "program_detail_sum_vs_dept_total",
        "program_row_PS+OO+DS=Total", "fte_sum_vs_program_total", "fte_sum_vs_fund_total", "fte_sum_vs_dept_total"}
    for c in out.checks:
        c["against"] = "printed_total" if c["kind"] in PRINTED_TOTAL_KINDS else "cross_check"
        d_ = c["diff"]
        c["small_mismatch_le_5"] = bool(c["status"] == "mismatch" and re.fullmatch(r"-?\d+", str(d_)) and abs(int(d_)) <= 5)
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
    for p_, k, d_ in out.sections:
        kinds_pages[k].append(p_)
    report = {
        "book_id": BOOK_ID,
        "cover_check": "PDF p.2 reads 'Adopted 2009 Operating Budget, Approved December 1, 2008'; every page footer reads 'City of New Orleans 2009 Adopted Budget, Page N'. The file name's 'Proposed' is wrong: this is the ADOPTED book (it also prints a 2009 Proposed column next to 2009 Adopted).",
        "row_counts": {
            "department_expenditures.csv": len(out.dept_exp), "revenues.csv": len(out.rev),
            "department_detail.csv": len(detail_rows), "positions.csv": len(out.pos),
            "printed_totals.csv": len(out.tot),
            "department_detail.csv by table": {t: sum(1 for r in detail_rows if r["table"] == t) for t in ("by_type", "by_fund", "by_program", "by_program_type")},
        },
        "check_summary_by_tier": {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in sorted(summ.items())},
        "check_summary_by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "tier_definitions": {"tier1": "citywide statements of revenues (p.43-47) and expenditures (p.78-79 all funds; p.10 general fund) + p.77 citywide summary cross-checks",
                              "tier2": "department sections: by_type, by_fund, by_program(_type) and cross-checks to the citywide statements",
                              "tier3": "positions / FTE (Personnel Summaries)"},
        "mismatches": [c for c in out.checks if c["status"] == "mismatch"],
        "within_1": [c for c in out.checks if c["status"] == "within_1"],
        "checks": out.checks,
        "unparsed": out.unparsed,
        "printed_totals_rows_not_covered_by_any_check": dict(unchecked),
        "revenue_header_anomalies": REV_HEADER_ANOMALIES,
        "footing_share": footing_share(out, detail_rows, out.pos),
        "cross_book_2012": cross_book(out),
        "spot_checks": spot_checks(out, detail_rows),
        "narrative_or_chart_pages_skipped": {"range": "%d-%d" % (DEPT_PAGES.start, DEPT_PAGES.stop - 1), "pages": out.narr_pages,
                                              "content": "mission statements, program narrative / funding-offer pages, performance measures, Mayor/CAO sub-office narratives"},
        "row_level_problems": out.problems,
        "notes": out.notes + ["wrapped position titles joined: %d" % out.wrapped_titles],
        "label_repairs": sorted(REPAIRS),
        "label_suspects_not_repaired": sorted(sus),
        "section_pages": {k: len(v) for k, v in kinds_pages.items()},
        "layout_notes": [
            "One citywide expenditure statement (All Funds) instead of three; General Fund by department only for 2008/2009 adopted (Budget-in-Brief p.10); no other_funds-by-department statement",
            "Revenue statement has 6 columns: 2005-2007 actual, 2008 adopted, 2009 proposed, 2009 adopted; funds grouped General Fund / Other Discretionary Funds / Other Funds / Special Revenue Fund",
            "Department summary pages carry 2007 actual, 2008 adopted, 2009 proposed, 2009 adopted by expenditure type (5 types incl. Reserves and Grants/Contributions/Fund Transfers) and by 13 source-of-funding lines",
            "Program Detail has no Reserves / Grants columns: for a department with such spending its Total column is smaller than its Department Summary total (see tier 2 mismatches)",
        ],
        "deviations_from_schema": [
            "department_detail.table has an extra value by_program_type (Program Detail table; dim2 = '<fund> | <type>'); by_program = Expenditure Summary (dim2 = fund)",
            "no workyears_citywide.csv: the citywide work-years table is printed rotated and was not extracted",
            "dept_code is blank everywhere: the 2009 book prints no department numbers; program numbers are in dim1/program",
            "department_expenditures.csv has only fund_group all_funds (2004-2009, p.78-79) and general_fund (2008/2009 adopted only, p.10); no other_funds rows",
            "revenues.csv fund_group is general_fund for the GF statement and other_funds for Other Discretionary / Other / Special Revenue funds; category = the printed section ('OTHER FUNDS', 'SPECIAL REVENUE FUND', ...) for those; fund_name = fund heading as printed, e.g. 'LIBRARY FUNDS - FUND 691'",
            "revenues: 2007 column is headed 'Adopted' on p.44-47 but 'Actual' on p.43; stored as basis=actual (see revenue_header_anomalies)",
            "variance % column on p.78-79 ignored (derived)",
        ],
    }
    (OUT / "checks.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("row_counts", "check_summary_by_tier", "check_summary_by_kind")}, indent=1))
    print("mismatches:", len(report["mismatches"]), " unparsed:", len(out.unparsed), " problems:", len(out.problems))


if __name__ == "__main__":
    main()
