#!/usr/bin/env python3
"""Extract tables from the City of New Orleans 2014 Adopted Operating Budget book.

Adapted from extract_2013.py / extract_2012.py (same family of layout; the 2014 book is a near-twin of 2013).
Citywide statements carry 7 year columns (2008-2014) in the back-of-book tables (pp.77-85) plus a second printing
of 2004-2006 actuals with a repeated 2014 adopted column in the front (pp.19-25); wrapped revenue labels are detected
from the bold/regular font; department table titles on continuation pages carry no department name.

Run from anywhere:   .venv/bin/python scripts/extract/extract_2014.py

Reads   data/raw/nola-gov/2014/City-of-New-Orleans-2014-Adopted-Budget-Book.pdf  (read-only)
Writes  data/extracted/2014-operating-adopted/{department_expenditures,revenues,
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
BOOK_ID = "2014-operating-adopted"
BOOK_FY = 2014
PDF = ROOT / "data/raw/nola-gov/2014/City-of-New-Orleans-2014-Adopted-Budget-Book.pdf"
OUT = ROOT / "data/extracted" / BOOK_ID

# ---- book-specific page map (1-based PDF pages; verified by sanity asserts in main) -------
P_CITYWIDE_SUMMARY = 57
P_SUMMARY_TABLES = 56               # "Summary of adopted 2014 budget - General Fund only"
FRONT_REV_PAGES = range(19, 22)     # 2004-2006 actuals + repeated 2014 adopted
FRONT_EXP_PAGES = range(23, 26)
REV_PAGES = range(77, 83)
EXP_PAGES = range(83, 86)
FTE_PAGES = range(86, 96)
DEPT_PAGES = range(126, 585)        # department sections run to the Enterprise Funds (p.584), then narrative / appendix

# Dept-section names -> citywide-statement names (cross-check only; output keeps printed names)
DEPT_ALIASES = {
    "CHIEF ADMINISTRATIVE OFFICE": "CAO",
    "PARKWAY": "Parks & Parkways",
    "HISTORIC DIST LANDMARKS COMM.": "HDLC",
    "VIEUX CARRE COMMISION": "VCC",
    "VIEUX CARRE COMMISSION": "VCC",
    "OFFICE OF TRAINING DIVISION": "Office of Training Div.",
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


Tok = namedtuple("Tok", "text x0 x1 y0 y1 bold", defaults=(False,))
Tok.cx = property(lambda t: (t.x0 + t.x1) / 2)


# ======================================================================================
# generic helpers
# ======================================================================================
def get_lines(page, tol=4.0, bold=False):
    """Word boxes -> list of lines (list of Tok, x-sorted).  '|' cell borders dropped.
    bold=True also flags each word with the weight of the font span it sits in."""
    ws = [Tok(w[4], w[0], w[2], w[1], w[3]) for w in page.get_text("words") if w[4].strip() not in ("|", "")]
    if bold:
        spans = [(fitz.Rect(sp["bbox"]), "Bold" in sp["font"]) for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for sp in l["spans"]]
        ws = [t._replace(bold=any(bd for r, bd in spans if r.contains(fitz.Point((t.x0 + t.x1) / 2, (t.y0 + t.y1) / 2)))) for t in ws]
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


def split_row(line, anchors, tol_left=30, decimal=False, edge=False):
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
    xof = (lambda t: t.x1) if edge else (lambda t: t.cx)      # edge=True: anchors are right-edge x positions
    while i < len(toks) and xof(toks[i]) < lo:
        i += 1
    label, run = toks[:i], toks[i:]
    data, problems, seen = {}, [], {}
    ref = sorted((t.y0 + t.y1) / 2 for t in (label or line))[len(label or line) // 2]   # row's typical y
    for t in run:
        c = nearest(xof(t), anchors)
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
        if amount == "DASH":        # a printed dash is a blank cell: omitted, like in every other table
            return
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
# Tier 1a: statement of revenues.  Back of book pp.75-79 (7 columns 2007-2013) and the front-of-book
# reprint pp.19-22 (2004-2006 actuals + a repeated 2013 adopted column, used only as a cross-check).
# ======================================================================================
def stmt_cols(lines, i):
    """Header at lines[i] (a line with >=3 year tokens) -> (cols, next_index).  cols = [(cx, fy, basis)]."""
    htoks, j = header_cols(lines, i)
    years = sorted([t for t in htoks if re.fullmatch(r"20\d\d", t.text)], key=lambda t: t.cx)
    bases = sorted([t for t in htoks if t.text in ("Actual", "Adopted", "Proposed", "Estimated")], key=lambda t: t.cx)
    if len(years) != len(bases) or not years:
        return None, j
    return [(y.cx, int(y.text), b.text.lower()) for y, b in zip(years, bases)], j


def edge_anchors(lines, cols):
    """The front-of-book reprint right-aligns its numbers under centred headers, and its 2014 column is
    narrower, so header centres mis-assign cells.  Cluster the right edges (x1) of the page's numeric
    cells into len(cols) groups instead.  -> sorted anchors or None."""
    lo = min(c[0] for c in cols) - 30
    xs = sorted(t.x1 for ln in lines for t in ln if t.x0 > lo - 20 and (parse_num(t.text) is not None) and t.x1 > lo)
    groups = []
    for x in xs:
        if groups and x - groups[-1][-1] < 12:
            groups[-1].append(x)
        else:
            groups.append([x])
    big = sorted(groups, key=len, reverse=True)[:len(cols)]       # the header's own year tokens form small groups
    if len(big) != len(cols) or len(big[-1]) < 3:
        return None
    return sorted(sorted(g)[len(g) // 2] for g in big)


def n_years(ln):
    return sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln)


def is_header(ln):
    return n_years(ln) >= 3 and all(t.text in ("Department", "Source", "Actual", "Adopted") or re.fullmatch(r"20\d\d", t.text) for t in ln)


def parse_revenue_block(doc, out, pages, front):
    state = dict(group="general_fund", fund=None, cat=None)
    cols = None
    pend = None            # (text, y) of a wrapped label's first line
    for pno in pages:
        lines = get_lines(doc[pno - 1], bold=True)
        anchors = None
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            if is_header(ln):
                newcols, i = stmt_cols(lines, i)
                if newcols is None:       # a header split across a page break: years at the foot of one page, basis words atop the next
                    out.notes.append("p.%d: year header without basis words (split across the page break); previous columns kept" % pno)
                else:
                    cols = newcols
                continue
            if all(t.text in ("Actual", "Adopted", "Source") for t in ln):
                i += 1
                continue                  # basis-word half of a split header
            if front and cols is not None and anchors is None:
                anchors = edge_anchors(lines, cols)
                if anchors is None:
                    out.unp(pno, "front revenue reprint", "could not find right-edge column anchors")
                    break
            i += 1
            if re.match(r"STATEMENT OF REVENUES|Discretionary and Other", text):
                continue
            if cols is None:
                if text.upper().startswith("GENERAL FUND"):
                    state.update(group="general_fund", fund="General Fund", cat=None)
                continue
            if text.upper().startswith("OTHER FUNDS"):
                state.update(group="other_funds", fund=None, cat=None)
                pend = None
                continue
            if text.upper() == "GENERAL FUND":
                state.update(group="general_fund", fund="General Fund", cat=None)
                continue
            label, data, probs = split_row(ln, anchors if front else [c[0] for c in cols], edge=front)
            lab = clean_label(ltext(label))
            if not data:
                bold = all(t.bold for t in ln)
                nxt = lines[i] if i < len(lines) else None
                if nxt is not None and (not bold or lab.upper().startswith("TOTAL")) and 0 < nxt[0].y0 - ln[0].y0 < 12.5 and not pend:
                    pend = lab                      # first line of a wrapped label; numbers are on the next line
                    continue
                if state["group"] == "general_fund":
                    state["cat"] = lab
                else:
                    state.update(fund=lab, cat=None)
                continue
            if pend:
                lab, pend = clean_label(pend + " " + lab), None
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if not lab:
                if front and all(c >= 3 for c in data):
                    continue                         # displaced 2013 cell of the front reprint (cross-check column only)
                out.unp(pno, "unlabelled number(s) %r" % text, "stray cell in revenue statement; ignored")
                continue
            is_tot = bool(re.search(r"\bTOTAL\b|SUB-TOTAL", lab.upper()))
            ctx = dict(state)
            if is_tot:
                out.rev_totals.append(dict(label=lab, page=pno, ctx=ctx, data=data, cols=cols, front=front))
                k = len(out.rev_totals) - 1
            for c, v in data.items():
                if v == "DASH":
                    continue
                if front and cols[c][1] == BOOK_FY:          # repeated 2013 adopted column: cross-check only
                    out.front13.append(dict(kind="total" if is_tot else "row", label=lab, ctx=ctx, amount=v, page=pno))
                    continue
                if is_tot:
                    out.total(cols[c][1], cols[c][2], None, lab, v, pno)
                    out.tot[-1]["_rev_total_idx"] = k
                    continue
                out.rev.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                    fund_group=state["group"], fund_name=state["fund"] or "",
                                    category=state["cat"] or "", line_name=lab, amount=v, page=pno))


SPECIAL_FUNDS = set()      # funds summed by "TOTAL SPECIAL REVENUES & TRUST FUNDS": every fund printed after the Grants, Contributions & Fund Transfers fund


def rev_rule(t):
    U, ctx = t["label"].upper(), t["ctx"]
    if U.startswith("TOTAL SPECIAL REVENUES"):
        return "revenues/special_revenues_and_trust_funds", lambda r: r["fund_group"] == "other_funds" and r["fund_name"] in SPECIAL_FUNDS
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


def parse_revenues(doc, out):
    out.rev_totals = []
    out.front13 = []
    parse_revenue_block(doc, out, FRONT_REV_PAGES, True)
    parse_revenue_block(doc, out, REV_PAGES, False)
    scopes = {}
    for k, t in enumerate(out.rev_totals):
        if t["label"].upper().startswith("TOTAL SPECIAL REVENUES"):
            # the funds whose own totals were printed since the Grants, Contributions & Fund Transfers total
            SPECIAL_FUNDS.clear()
            for u in reversed(out.rev_totals[:k]):
                if u["label"].upper().startswith("TOTAL GRANTS, CONTRIB"):
                    break
                if not u["label"].upper().startswith("TOTAL SPECIAL"):
                    SPECIAL_FUNDS.add(u["ctx"]["fund"])
            out.notes.append("p.%d %s covers the funds printed after the Grants, Contributions & Fund Transfers fund: %s" % (t["page"], t["label"], sorted(SPECIAL_FUNDS)))
        scope, pred = rev_rule(t)
        scopes[k] = scope
        sums = defaultdict(int)
        for r in out.rev:
            if pred(r):
                sums[(r["fiscal_year"], r["basis"])] += r["amount"]
        for c, (cx, fy, basis) in enumerate(t["cols"]):
            v = t["data"].get(c)
            if v is None or (t["front"] and fy == BOOK_FY):
                continue
            printed = 0 if v == "DASH" else v
            out.check(1, "revenue_total", scope, fy, basis, sums.get((fy, basis), 0), printed, t["page"],
                      "printed as dash" if v == "DASH" else "")
    for r in out.tot:
        if "_rev_total_idx" in r:
            r["scope"] = scopes[r.pop("_rev_total_idx")]
    # front-of-book repeated 2013 adopted column vs the back-of-book statement
    main = {}
    for r in out.rev:
        if r["fiscal_year"] == BOOK_FY:
            main[(r["fund_group"], r["fund_name"], r["category"], norm(r["line_name"]))] = r
    main_tot = {}
    for k, t in enumerate(out.rev_totals):
        if not t["front"]:
            for c, (cx, fy, basis) in enumerate(t["cols"]):
                v = t["data"].get(c)
                if fy == BOOK_FY and v not in (None, "DASH"):
                    main_tot[(norm(t["label"]), t["ctx"]["fund"], t["ctx"]["cat"])] = (v, t["page"])
    unmatched = 0
    for f in out.front13:
        ctx = f["ctx"]
        if f["kind"] == "row":
            m = main.get((ctx["group"], ctx["fund"] or "", ctx["cat"] or "", norm(f["label"])))
            if m is None:
                unmatched += 1
                continue
            out.check(1, "front_reprint_2014_vs_statement_row", "revenues/%s/%s/%s" % (ctx["fund"], ctx["cat"], f["label"]), BOOK_FY, "adopted", f["amount"], m["amount"], f["page"])
        else:
            m = main_tot.get((norm(f["label"]), ctx["fund"], ctx["cat"]))
            if m is None:
                unmatched += 1
                continue
            out.check(1, "front_reprint_2014_vs_statement_total", "revenues/%s" % f["label"], BOOK_FY, "adopted", f["amount"], m[0], f["page"])
    if unmatched:
        out.notes.append("front-of-book revenue reprint (pp.19-22): %d 2014-adopted cells have no same-named line in the pp.77-82 statement (label spelling differs); not cross-checked" % unmatched)


# ======================================================================================
# Tier 1b: statements of expenditures by department (back pp.80-82; front reprint pp.23-25 with 2004-2006)
# ======================================================================================
def parse_exp_block(doc, out, pages, front):
    group = "general_fund" if front else None      # the front reprint's first page carries no "General Fund Expenditures" caption
    cols = None
    for pno in pages:
        lines = get_lines(doc[pno - 1])
        anchors = None
        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ltext(ln)
            if is_header(ln):
                newcols, i = stmt_cols(lines, i)
                if newcols is None:
                    out.notes.append("p.%d: year header without basis words (split across the page break); previous columns kept" % pno)
                else:
                    cols = newcols
                continue
            if all(t.text in ("Actual", "Adopted", "Source", "Department") for t in ln):
                i += 1
                continue
            if front and cols is not None and anchors is None:
                anchors = edge_anchors(lines, cols)
                if anchors is None:
                    out.unp(pno, "front expenditure reprint", "could not find right-edge column anchors")
                    break
            i += 1
            m = re.match(r"(General Fund|Other Fund|Total) Expenditures", text, re.I)
            if m:
                group = {"general fund": "general_fund", "other fund": "other_funds", "total": "all_funds"}[m.group(1).lower()]
                continue
            if cols is None or group is None or text.upper().startswith("STATEMENT OF"):
                continue
            label, data, probs = split_row(ln, anchors if front else [c[0] for c in cols], edge=front)
            lab = clean_label(ltext(label))
            for p in probs:
                out.problems.append(dict(page=pno, line=text, problem=p))
            if lab == "Total":
                for c, v in data.items():
                    if v == "DASH":
                        continue
                    if front and cols[c][1] == BOOK_FY:
                        out.front13_exp.append(dict(group=group, label="Total", amount=v, page=pno))
                        continue
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
                if front and cols[c][1] == BOOK_FY:
                    out.front13_exp.append(dict(group=group, label=lab, amount=v, page=pno))
                    continue
                out.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=cols[c][1], basis=cols[c][2],
                                         fund_group=group, dept_code="", dept_name=lab, amount=v, page=pno))


def parse_dept_expenditures(doc, out):
    out.front13_exp = []
    parse_exp_block(doc, out, FRONT_EXP_PAGES, True)
    parse_exp_block(doc, out, EXP_PAGES, False)
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
    keys = {(k[1], k[2]) for k in by}
    for (n, fy, b), g in sorted(by.items()):
        gf, of, af = (g.get(x, (0, 0)) for x in ("general_fund", "other_funds", "all_funds"))
        out.check(1, "general+other=all", "department_expenditures/" + n, fy, b, gf[0] + of[0], af[0],
                  af[1] or gf[1] or of[1])
    pt = {(t["scope"], t["fiscal_year"], t["basis"]): t for t in out.tot if t["scope"].startswith("department_expenditures/")}
    for (fy, b) in sorted(keys):
        g, o, a = (pt.get(("department_expenditures/" + x, fy, b)) for x in ("general_fund", "other_funds", "all_funds"))
        if g and o and a:
            out.check(1, "general+other=all (printed totals)", "department_expenditures/Total", fy, b,
                      g["amount"] + o["amount"], a["amount"], a["page"])
    # front reprint's repeated 2013 adopted column vs the back-of-book statement
    main = {(r["fund_group"], norm(r["dept_name"])): r for r in out.dept_exp if r["fiscal_year"] == BOOK_FY and r["basis"] == "adopted"}
    for f in out.front13_exp:
        if f["label"] == "Total":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/" + f["group"] and x["fiscal_year"] == BOOK_FY]
            for x in ref:
                out.check(1, "front_reprint_2014_vs_statement_total", x["scope"], BOOK_FY, "adopted", f["amount"], x["amount"], f["page"])
        else:
            m = main.get((f["group"], norm(f["label"])))
            if m is None:
                out.notes.append("front reprint p.%d %s %r: no same-named row in the back statement" % (f["page"], f["group"], f["label"]))
                continue
            out.check(1, "front_reprint_2014_vs_statement_row", "department_expenditures/%s/%s" % (f["group"], f["label"]), BOOK_FY, "adopted", f["amount"], m["amount"], f["page"])


# ======================================================================================
# Tier 1c: printed summary tables p.56 / p.57 (cross-check material)
# ======================================================================================
def parse_p57totals(doc, out):
    """p.56 'Summary of adopted 2014 budget - General Fund only': TOTAL REVENUES / TOTAL EXPENDITURES."""
    lines = get_lines(doc[P_SUMMARY_TABLES - 1])
    out.p56 = {}
    for name, key in (("revenue", "TOTAL REVENUES"), ("expenditure", "TOTAL EXPENDITURES")):
        ln = [l for l in lines if ltext(l).upper().startswith(key)]
        if len(ln) != 1:
            out.unp(P_SUMMARY_TABLES, "%s total" % name, "expected exactly one %r line" % key)
            continue
        nums = [parse_num(t.text) for t in ln[0] if parse_num(t.text) not in (None, "DASH")]
        if not nums:
            out.unp(P_SUMMARY_TABLES, "%s total" % name, "no amount on the line")
            continue
        out.total(BOOK_FY, "adopted", "summary_p56/%s/general_fund" % name, key.title(), nums[0], P_SUMMARY_TABLES)
        out.p56[(name, "general_fund", BOOK_FY)] = nums[0]


# ======================================================================================
# Tier 2/3: department sections
# ======================================================================================
TITLE_RE = re.compile(r"^(.*?)\s*(PROGRAM DETAIL|EXPENDITURE SUMMARY|PERSONNEL SUMMARY)$")


CARRY = {}


def classify_line(ln):
    """Table-title line -> ('summary'|'detail'|'expsum'|'personnel', dept_name or None) else None."""
    t = re.sub(r"\s+", " ", ltext(ln)).strip()
    if t == "DEPARTMENTAL BUDGET SUMMARY":
        return "summary", None
    m = TITLE_RE.match(t)
    if m:
        kind = {"PROGRAM DETAIL": "detail", "EXPENDITURE SUMMARY": "expsum", "PERSONNEL SUMMARY": "personnel"}[m.group(2)]
        return kind, (m.group(1) or None)      # continuation pages print the bare title: department carried forward
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
        if dept is None:
            dept = CARRY.get("dept")
            CARRY["carried"] = CARRY.get("carried", 0) + 1
        else:
            CARRY["dept"] = dept
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
        if section is None:             # citywide p.57 prints no "EXPENDITURES" / "SOURCE OF FUNDING" captions
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


FUND_HEAD = re.compile(r"^[A-Z0-9]{3,4}( \S|$)")      # a bare 3-letter fund code ("RIV") is also a heading; 4-letter code "PRIV" in this book
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
    # numbers are right-aligned under centred headers: a narrow "0" is nearer the neighbouring header than its own
    # (Mayor pages), so anchor on the right edges of the cells when the page gives four clean clusters
    edge = edge_anchors(lines[j:], cols)
    anchors = edge or [c[0] for c in cols]
    if edge is None:
        out.notes.append("p.%d %s %s: right-edge column anchors not found; header centres used" % (pno, dept, kind))
    table = "by_program_type" if kind == "detail" else "by_program"
    body = lines[j:]
    for bi, ln in enumerate(body):
        text = clean_label(ltext(ln))
        label, data, probs = split_row(ln, anchors, tol_left=40, edge=edge is not None)
        lab = clean_label(ltext(label))
        if not data:
            nxt = clean_label(ltext(body[bi + 1])) if bi + 1 < len(body) else ""
            if (text == "TOTAL" or text.endswith(" TOTAL")) and not PROG_HEAD.match(nxt):
                continue                # a total row whose cells are all dashes (when a program row follows, the book has printed a fund HEADING with the word TOTAL)
            if not PROG_HEAD.match(text):
                state["fund"] = text    # fund heading (code + name, or just a name such as MISCELLANEOUS OTHER FUNDS)
            else:
                out.unp(pno, "%s: %r" % (dept, text), "unrecognised heading line in %s" % table)
            continue
        for p in probs:
            out.problems.append(dict(page=pno, line=text, problem=p))
        if lab.endswith("TOTAL") or not PROG_HEAD.match(lab):
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
    lm = min((l[0].x0 for l in lines[j:] if l), default=0)      # the table's left margin (pages differ: Miscellaneous is indented)
    plines = merge_orphans(lines[j:], anchors)
    for pi, ln in enumerate(plines):
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
            nxt_is_prog = pi + 1 < len(plines) and bool(PROG_HEAD.match(clean_label(ltext(plines[pi + 1]))))
            if first_x < lm + 25 and (text == "TOTAL" or text.endswith(" TOTAL")) and not nxt_is_prog:
                continue                # a total row whose cells are all dashes (a following program row means the book printed a fund heading ending in TOTAL)
            if first_x < lm + 25 and not PROG_HEAD.match(text):
                state["fund"], state["prog"] = text, ""
                state["pend_rows"], state["pend_tots"] = [], []
            elif first_x < lm + 25 and PROG_HEAD.match(text):
                state["prog"] = text
            elif state.get("last_pos") is not None and first_x >= lm + 25:
                state["last_pos"]["position_title"] = clean_label(state["last_pos"]["position_title"] + " " + text)
                out.wrapped_titles += 1
            else:
                out.unp(pno, "%s: %r" % (dept, text), "unrecognised line in personnel summary")
            continue
        # total rows: "... TOTAL", or (Mayor sub-offices etc.) a bare program / fund heading carrying numbers
        heading_col = ln[0].x0 < lm + 25 and not grade_tok
        if lab.endswith("TOTAL") or heading_col:
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
    for pno in DEPT_PAGES:
        lines = get_lines(doc[pno - 1])
        segs = segments(lines)
        if not segs:
            # a Personnel Summary can run on to a page that carries neither title nor column header (Mayor - Community Development, pp.176-177)
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

    # (e) citywide summary p57: sum of departments vs printed citywide.  Only the departments that appear in
    # the citywide expenditure statement are summed (Enterprise-fund entities and the Mayor's sub-office
    # sections, which are already inside "MAYOR", are not part of p.57).
    in_stmt = {d for d in depts if norm(DEPT_ALIASES.get(d, d)) in stm_names and d != "CITYWIDE"}
    cw = defaultdict(list)
    for r in out.cw_rows:
        cw[(r["table"], r["dim1"])].append(r)
    # This book prints NO departmental summary for the Mayor (pp.127-168 carry only the four sub-offices' program
    # tables), so the Mayor's by_type / by_fund cannot be summed.  What can be summed: the Mayor sub-offices'
    # Expenditure Summary (by_program) rows, in total (all funds) and for the General Fund ('001 GENERAL FUND').
    mayor_all, mayor_gf = defaultdict(int), defaultdict(int)
    for d in depts:
        if d.startswith("MAYOR - "):
            for x in D.get((d, "by_program"), []):
                mayor_all[(x["fiscal_year"], x["basis"])] += x["amount"]
                if x["dim2"] == "001 GENERAL FUND":
                    mayor_gf[(x["fiscal_year"], x["basis"])] += x["amount"]
    nosec = {n for n in stm_names if n not in matched and n != "mayor"}      # statement departments with no section
    def nosec_amt(fy, b, grp):
        return sum(g.get(grp, 0) for (n, f, bb), g in stm.items() if n in nosec and f == fy and bb == b)
    skipped = set()
    for (tbl, dim1), rs in sorted(cw.items()):
        if not (tbl == "by_fund" and dim1 == "GENERAL FUND"):
            skipped.add("%s/%s" % (tbl, dim1))
            continue
        for r in rs:
            s = 0
            for d in depts:
                if d not in in_stmt:
                    continue
                for x in D.get((d, tbl), []):
                    if x["dim1"] == dim1 and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]:
                        s += x["amount"]
            out.check(2, "sum_of_departments+mayor_suboffices_vs_citywide_summary_p57", "citywide/%s/%s" % (tbl, dim1), r["fiscal_year"], r["basis"],
                      s + mayor_gf[(r["fiscal_year"], r["basis"])], r["amount"], r["page"], "Mayor taken from the sub-offices' Expenditure Summary, fund 001; statement departments with no section in this book add %d (general fund) to the printed figure" % nosec_amt(r["fiscal_year"], r["basis"], "general_fund"))
    if skipped:
        out.notes.append("p.57 line-level by_type / by_fund sums not checked (%d lines): the book prints no Mayor departmental summary, and mapping the Mayor sub-offices' funding-source codes onto p.57's fund names would be a guess" % len(skipped))
    for t in [t for t in out.tot if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund")]:
        s = 0
        tbl = t["scope"].rsplit("/", 1)[1]
        for d in depts:
            if d not in in_stmt:
                continue
            for x in D.get((d, tbl), []):
                if x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]:
                    s += x["amount"]
        out.check(2, "sum_of_departments+mayor_suboffices_vs_citywide_summary_p57", t["scope"] + " TOTAL", t["fiscal_year"], t["basis"], s + mayor_all[(t["fiscal_year"], t["basis"])], t["amount"], t["page"],
                  "Mayor taken from the sub-offices' Expenditure Summary (all funds); statement departments with no section in this book add %d (all funds) to the printed figure" % nosec_amt(t["fiscal_year"], t["basis"], "all_funds"))
    # (f) citywide statement totals vs citywide summary / p56
    for t in out.tot:
        if t["scope"] == "department_detail/CITYWIDE/by_type" and t["basis"] != "proposed":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]]
            for x in ref:
                out.check(1, "citywide_summary_p57_vs_statement_total", "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
        if t["scope"] == "department_detail/CITYWIDE/by_fund" and t["basis"] != "proposed":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]]
            for x in ref:
                out.check(1, "citywide_funding_p57_vs_statement_total", "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
    gfrows = [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND"]
    for r in gfrows:
        ref = [x for x in out.tot if x["scope"] == "department_expenditures/general_fund" and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]]
        for x in ref:
            out.check(1, "citywide_summary_p57_GF_vs_statement_GF_total", "department_expenditures/general_fund", r["fiscal_year"], r["basis"], r["amount"], x["amount"], r["page"])
    for (name, grp, fy), v in out.p56.items():
        if name == "expenditure":
            ref = [x for x in out.tot if x["scope"] == "department_expenditures/" + grp and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p57vs_statement_total", "department_expenditures/" + grp, fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)
        else:
            ref = [x for x in out.tot if x["scope"] == ("revenues/General Fund" if grp == "general_fund" else "revenues/other_funds") and x["fiscal_year"] == fy and x["basis"] == "adopted"]
            for x in ref:
                out.check(1, "summary_p57vs_statement_total", x["scope"], fy, "adopted", v, x["amount"], P_SUMMARY_TABLES)

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
    cols = None          # some continuation pages carry no year header: the previous page's columns apply
    for pno in FTE_PAGES:
        if done:
            break
        lines = get_lines(doc[pno - 1], bold=True)
        pend = None        # first line of a wrapped program name (regular weight, no numbers)
        for i, ln in enumerate(lines[:6]):             # (chart axis year labels lower on the page are not headers)
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                htoks, j = header_cols(lines, i)
                cols = year_basis_columns(htoks, 7) or cols
                lines = lines[j:]
                break
        if cols is None:
            out.unp(pno, "citywide work years table", "header columns not recognised")
            continue
        anchors = [c[0] for c in cols]
        bottom = False      # the closing by-fund block (General Fund / Other Funds / City-Wide Total)
        k = 0
        while k < len(lines):
            ln = lines[k]
            k += 1
            if sum(bool(re.fullmatch(r"20\d\d", t.text)) for t in ln) >= 5:
                if bottom or done:
                    break                    # chart axis labels under the closing block
                _, k = header_cols(lines, k - 1)      # repeated mid-page column header: skip it
                continue
            label, data, probs = split_row(ln, anchors, tol_left=25, decimal=True)
            lab = clean_label(ltext(label))
            if not data:
                if lab and not lab.startswith("Page") and not done and not bottom:
                    if all(t.bold for t in ln):
                        dept = lab                    # department headings are bold
                    else:
                        pend = lab                    # wrapped program name: numbers are on the next line
                continue
            if pend:
                lab, pend = clean_label(pend + " " + lab), None
            if not lab:
                out.unp(pno, "unlabelled number(s) %r" % ltext(ln), "stray cell between work-year rows; ignored")
                continue
            for pr in probs:
                out.problems.append(dict(page=pno, line=ltext(ln), problem=pr))
            closing = lab.lower() in ("general fund", "other funds") and any(ltext(x).lower().startswith("city-wide total") for x in lines[k:k + 3])
            if bottom or closing or lab.lower().startswith("city-wide total"):
                bottom = True       # the closing by-fund block (this book prints it without its own year header)
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
def spot_check(out, n_per_file=6, seed=20140):
    """Independent re-read: random cells re-found with pdfplumber (a different text engine than the extractor's PyMuPDF).
    A cell passes when its amount, formatted as printed, sits on the same text line as the first words of its label (or the line below a wrapped label)."""
    import random
    import pdfplumber
    rnd = random.Random(seed)
    pools = {
        "department_expenditures": [(r["page"], r["dept_name"], r["amount"], "%s %s %s" % (r["fund_group"], r["fiscal_year"], r["basis"])) for r in out.dept_exp],
        "revenues": [(r["page"], r["line_name"], r["amount"], "%s %s %s" % (r["fund_name"], r["fiscal_year"], r["basis"])) for r in out.rev],
        "department_detail": [(r["page"], r["dim1"], r["amount"], "%s %s %s %s" % (r["dept_name"], r["table"], r["fiscal_year"], r["basis"])) for r in out.detail if r["dept_name"] != "CITYWIDE"],
        "positions": [(r["page"], r["position_title"], r["fte"], "%s %s %s" % (r["dept_name"], r["fiscal_year"], r["basis"])) for r in out.pos],
    }
    res, npass, ntot = [], 0, 0
    with pdfplumber.open(str(PDF)) as pdf:
        for name, pool in pools.items():
            for page, label, amt, ctx in rnd.sample(pool, min(n_per_file, len(pool))):
                lines = (pdf.pages[page - 1].extract_text() or "").split("\n")
                if isinstance(amt, Decimal):
                    forms = {"%.2f" % amt}
                else:
                    a = abs(amt)
                    forms = {"{:,}".format(a)}
                    if amt < 0:
                        forms = {"(%s)" % f for f in forms} | {"-" + f for f in forms}
                key = re.sub(r"[^a-z0-9]", "", " ".join(label.split()[:2]).lower())
                ok = any(any(f in re.sub(r"\$\s*", "", ln).split() or f in ln for f in forms)
                         and key[:8] in re.sub(r"[^a-z0-9]", "", (lines[k - 1] + ln if k else ln).lower()) for k, ln in enumerate(lines))   # label may wrap onto the line above
                npass += ok
                ntot += 1
                res.append(dict(file=name, page=page, label=label, amount=str(amt), context=ctx, found_with_pdfplumber=ok))
    return dict(engine="pdfplumber vs PyMuPDF extractor", passed=npass, total=ntot, cells=res)


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
    assert has(P_SUMMARY_TABLES, "TOTAL REVENUES")
    assert has(77, "STATEMENT OF REVENUES") and has(77, "GENERAL FUND") and has(82, "TOTAL ALL FUNDS")
    assert has(83, "GENERAL FUND EXPENDITURES") and has(84, "OTHER FUND EXPENDITURES") and has(85, "TOTAL EXPENDITURES")
    assert has(19, "STATEMENT OF REVENUES") and has(23, "STATEMENT OF EXPENDITURES") and has(25, "Total Expenditures")
    assert has(86, "CITYWIDE WORK YEARS")
    assert has(126, "COUNCIL") and has(584, "PERSONNEL SUMMARY")


def main():
    doc = fitz.open(str(PDF))
    assert doc.page_count == 630, doc.page_count
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
    parse_p57totals(doc, out)
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
    out.unp("1-18, 26-76", "front matter: narrative, budget process, strategic framework, financial overview, charts, revenue descriptions, millage rates, fund balance projection", "not attempted: outside the requested tiers (the statements reprinted on pp.19-25 and the totals on pp.56-57 are captured)")
    out.unp("97-125", "capital budget summary, statement of debt (amounts in thousands), financial policies, Council narrative", "not attempted: outside the requested tiers")
    out.unp("585-630", "glossary, appendix: professional services contract obligations by department, organization-level YTD actual listings (pp.601-630)", "not attempted: vendor lists and organization listings, not the budget tables")
    out.unp("Mayor sections (pp.134-194)", "no DEPARTMENTAL BUDGET SUMMARY for the Mayor", "the book prints program-level tables for the Mayor sub-offices (Mayor's Office, Criminal Justice Coordination, Homeland Security, Community Development) but no whole-Mayor by_type / by_fund summary; the Mayor therefore has no by_type / by_fund rows (department_detail) and is not in the p.57 line-level sums")
    out.unp("pp.19-22 reprint, 2014 column", "front-of-book revenue/expenditure reprint repeats the 2014 adopted column", "not written to the CSVs (would double count); used only as a cross-check against the pp.77-85 statements")
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
        "workyears_programs_sum_vs_department_total", "workyears_programs_sum_vs_subtotal",
        "front_reprint_2014_vs_statement_row", "front_reprint_2014_vs_statement_total"}
    for c in out.checks:
        c["against"] = "printed_total" if c["kind"] in PRINTED_TOTAL_KINDS else "cross_check"
        d = c["diff"]
        c["small_mismatch_le_5"] = bool(c["status"] == "mismatch" and re.fullmatch(r"-?\d+", str(d)) and abs(int(d)) <= 5)
    TIER_OF = {"revenue_total": 1, "dept_statement_total": 1, "general+other=all": 1, "general+other=all (printed totals)": 1,
               "citywide_summary_p57_vs_statement_total": 1, "citywide_funding_p57_vs_statement_total": 1,
               "citywide_summary_p57_GF_vs_statement_GF_total": 1, "summary_p57vs_statement_total": 1,
               "front_reprint_2014_vs_statement_row": 1, "front_reprint_2014_vs_statement_total": 1,
               "summary_total": 2, "total_expenditures=total_funding": 2, "dept_section_vs_citywide_all_funds": 2,
               "dept_section_GF_vs_citywide_general_fund": 2, "dept_section_nonGF_vs_citywide_other_funds": 2,
               "sum_of_departments+mayor_suboffices_vs_citywide_summary_p57": 2}
    for c in out.checks:
        c["tier"] = TIER_OF.get(c["kind"], 4 if c["kind"].startswith(("fte_", "workyears", "personnel")) else 3)
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
        "tier_definitions": {"tier1": "citywide statements of revenues and expenditures (+ p.57/p.56 summary cross-checks)",
                              "tier2": "department summaries by_type / by_fund and their cross-checks to the citywide statements",
                              "tier3": "program-level detail (Program Detail, Expenditure Summary)",
                              "tier4": "positions / FTE (Personnel Summary, citywide work years)"},
        "mismatches": [c for c in out.checks if c["status"] == "mismatch"],
        "within_1": [c for c in out.checks if c["status"] == "within_1"],
        "checks": out.checks,
        "spot_checks": spot_check(out),
        "unparsed": out.unparsed,
        "printed_totals_rows_not_covered_by_any_check": dict(unchecked),
        "narrative_or_chart_pages_skipped": {"range": "%d-%d" % (DEPT_PAGES.start, DEPT_PAGES.stop - 1), "pages": out.narr_pages,
                                              "content": "mission statements, performance measures, funding-summary charts, 'Description of Funded Programs' offer tables"},
        "pages_not_attempted": ["1-18,26-76 (narrative, charts, overview)", "97-125 (capital budget, debt, policies, Council narrative)",
                                 "585-630 (glossary, appendix of professional-services contracts, organization listings)"],
        "row_level_problems": out.problems,
        "notes": out.notes + ["wrapped position titles joined: %d" % out.wrapped_titles],
        "label_repairs": sorted(REPAIRS),
        "label_suspects_not_repaired": sorted(sus),
        "section_pages": {k: len(v) for k, v in kinds_pages.items()},
        "deviations_from_schema": [
            "department_detail.table has an extra value by_program_type (Program Detail table; dim2 = '<fund> | <type>'); by_program = Expenditure Summary (dim2 = fund)",
            "workyears_citywide.csv is an extra file (citywide FTE by department/program, 2008-2014)",
            "dept_code is blank everywhere: the 2014 book prints no department numbers; program numbers are in dim1/program",
            "the Mayor has no by_type / by_fund rows: the book prints no whole-Mayor departmental summary (see unparsed)",
            "citywide statements: 2004-2006 come from the front-of-book reprint (pp.19-25), 2008-2014 from pp.77-85; the reprint's repeated 2014 adopted column is used only as a cross-check",
            "department_expenditures.csv: variance % column ignored (derived)",
            "revenues.fund_name: 'General Fund' for the GF statement, other funds as printed (upper case)",
        ],
    }
    (OUT / "checks.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("row_counts", "check_summary_by_tier", "check_summary_by_kind")}, indent=1))
    print("mismatches:", len(report["mismatches"]), " unparsed:", len(out.unparsed), " problems:", len(out.problems))


if __name__ == "__main__":
    main()
