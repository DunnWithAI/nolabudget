#!/usr/bin/env python3
"""Extract tables from the City of New Orleans 2008 Adopted Operating Budget book.

Run from anywhere:   .venv/bin/python scripts/extract/extract_2008.py

Reads   data/raw/pre-2011/2008/CNO_BudgetBook2008Adopted3.pdf  (read-only)
        data/interim/ocr/2008/pNNNN.json   (Vision OCR cache written by scripts/extract/ocr_2008.py)
Writes  data/extracted/2008-operating-adopted/{department_expenditures,revenues,
        department_detail,positions,printed_totals}.csv, checks.json

IMPORTANT FINDING: the brief described this book as an image-only scan.  It is not.  It is a Crystal Reports
PDF with a real text layer on 455 of 608 pages (all statements and all department tables); the remaining pages are
blank sheets carrying only a page number, the cover, and SIX scanned inserts (PDF p.451, 453-455: the
Neighborhood Housing Improvement Fund summary / program detail / expenditure summary / personnel summary, no text layer
at all; p.467-468: the Economic Development Fund summary and program detail, with an unreliable embedded OCR layer).  So:
  * text-layer pages are read with PyMuPDF exactly like extract_2009.py (this script was copied from it);
  * the six scanned pages are read from the Vision OCR cache (the only place OCR is the SOURCE of a number);
  * the Vision OCR of ALL 608 pages (cached) is used as an independent second reader: every cell's printed number is
    looked up in the OCR of the cited page (checks.json "ocr_agreement"), which measures how good OCR alone would have been.
Landscape tables are printed sideways on portrait pages (text running bottom-to-top) or with /Rotate 270; words are
mapped into the reading frame (see oriented_words).

Method as in extract_2012.py / extract_2009.py: word boxes -> lines (cluster on y) -> label / numeric cells; numbers are
assigned to columns by x-position against the header row of each table, never by their order in the text stream.  No
amount is typed in by hand.

Output shape follows data/extracted/SCHEMA.md; deviations are listed in checks.json.
"""
import collections
import csv
import json
import re
import sys
from collections import defaultdict, namedtuple
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF

ROOT = Path(__file__).resolve().parents[2]
BOOK_ID = "2008-operating-adopted"
BOOK_FY = 2008
PDF = ROOT / "data/raw/pre-2011/2008/CNO_BudgetBook2008Adopted3.pdf"
OCR_DIR = ROOT / "data/interim/ocr/2008"
OUT = ROOT / "data/extracted" / BOOK_ID

# ---- book-specific page map (1-based PDF pages; verified by sanity asserts in main) -------
# printed page number = PDF page - 16 (front matter i-xiv ... then 1-)
P_CITYWIDE_SUMMARY = 83             # printed 67: CITYWIDE departmental budget summary (by type / by fund)
P_GF_BRIEF = 22                     # printed 6: Budget-in-Brief, General Fund revenues / expenditures by department
REV_PAGES = range(68, 73)           # printed 52-56: Statement of Revenues
EXP_PAGES = range(85, 87)           # printed 69-70: City-Wide Expenditures, All Funds
DEPT_PAGES = range(97, 603)         # department sections run to the Enterprise Funds, then the capital-project glossary
# scanned inserts: 451, 453-455 (Neighborhood Housing Improvement Fund) have no text layer at all; 467-468 (Economic Development
# Fund) carry an embedded third-party OCR text layer that silently drops cells (e.g. the whole Source-of-Funding rows of p.467), so
# the Vision OCR is used for all six.
OCR_ONLY_PAGES = (451, 453, 454, 455, 467, 468)
SCAN_PAGES = OCR_ONLY_PAGES
OCR_DROPPED = []                        # tokens dropped as cell-border / zero-with-border artefacts (reported in checks.json)

# Dept-section names -> citywide-statement names (cross-check only; output keeps printed names)
DEPT_ALIASES = {
    "CHIEF ADMINISTRATIVE OFFICE": "CAO",
    "PARKWAY & PARK COMMISSION": "Parks & Parkways",
    "HISTORIC DIST LANDMARKS COMM.": "HDLC",
    "VIEUX CARRE COMMISION": "VCC",
    "ALCOHOLIC BEVERAGE CONTR BRD.": "Alcoholic Beverage Ctrl. Bd.",
    "N O MOSQUITO CONTROL BRD.": "Mosquito Control Bd.",
    "N O MUSEUM OF ART": "Museum of Art",
    "NEIGHBORHOOD HOUSING IMPROVEMENT FUND": "Neighborhood Housing Improvement Fund",
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
def oriented_words(page):
    """Word boxes (text, x0, y0, x1, y1) in the READING frame of the page.

    The book prints landscape tables either with /Rotate 270 on a landscape media box (PyMuPDF reports the unrotated
    media-box frame, text already horizontal) or sideways on a portrait media box (text lines run bottom-to-top, dir=(0,-1)).
    Vertical lines are rotated 90 degrees clockwise into the reading frame: (x, y) -> (H - y, x).  Horizontal lines on such a
    sideways page are the printed page number and are dropped.  Words are rebuilt from characters so each carries its
    own direction."""
    H = page.mediabox.height
    raw = page.get_text("rawdict")
    words = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            vertical = abs(l["dir"][1]) > 0.9
            chars = [c for s_ in l["spans"] for c in s_["chars"]]
            cur = []

            def flush():
                if cur:
                    words.append((("".join(c["c"] for c in cur)),
                                  min(c["bbox"][0] for c in cur), min(c["bbox"][1] for c in cur),
                                  max(c["bbox"][2] for c in cur), max(c["bbox"][3] for c in cur), vertical))
                    cur.clear()
            for c in chars:
                if c["c"].isspace():
                    flush()
                else:
                    cur.append(c)
            flush()
    sideways = sum(len(w[0]) for w in words if w[5]) > sum(len(w[0]) for w in words if not w[5])
    out = []
    for t, x0, y0, x1, y1, v in words:
        if sideways:
            if not v:
                continue                                  # printed page number on a sideways page
            x0, y0, x1, y1 = H - y1, x0, H - y0, x1
        out.append((t, x0, y0, x1, y1))
    return out


def lines_from_toks(ws, tol=4.0):
    ws = sorted(ws, key=lambda t: (t.y0 + t.y1) / 2)
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
    return lines


def get_lines(page, tol=4.0):
    """Text-layer word boxes -> list of lines (list of Tok, x-sorted).  '|' cell borders dropped; the page number
    printed in the left margin of rotated pages (x < 30) is dropped."""
    ws = [Tok(t, x0, x1, y0, y1) for t, x0, y0, x1, y1 in oriented_words(page) if t not in ("|",)]
    ws = [t for t in ws if not (t.x1 < 30 and re.fullmatch(r"\d+", t.text))]
    return lines_from_toks(ws, tol)


def ocr_lines(pno, tol=4.0):
    """Vision OCR cache -> lines in points (300 dpi pixels * 72/300).  Table borders are read as stray glyphs: '|' / 'I' / 'l' /
    '!' / bullets are dropped, and a zero fused with the border to its right ('01', '0|', 'O I', 'o') is dropped as an UNREADABLE
    cell (never guessed as 0 or 1) and logged in OCR_DROPPED -> checks.json."""
    f = OCR_DIR / ("p%04d.json" % pno)
    j = json.loads(f.read_text(encoding="utf-8"))
    k = 72.0 / j["dpi"]
    ws = []
    for l in j["lines"]:
        prev = None
        for w in l["words"]:
            t = w["text"].strip()
            if t == "1" and prev is not None and prev["text"] in ("0", "o", "O") and 0 <= w["x"] - (prev["x"] + prev["w"]) < w["h"]:
                OCR_DROPPED.append(dict(page=pno, token=t, x=round(w["x"] * k), y=round(w["y"] * k), why="'1' directly after a zero: the cell border read as a digit"))
                prev = w
                continue
            prev = w
            if t in ("", "|", "I", "l", "!", "\u2022", "\u00b7", "1|", "I|"):
                continue
            if re.fullmatch(r"[0oO][1Il|!]?", t) and t != "0" or re.fullmatch(r"0[1Il|!]", t):
                OCR_DROPPED.append(dict(page=pno, token=t, x=round(w["x"] * k), y=round(w["y"] * k), why="zero fused with cell border / unreadable"))
                continue
            ws.append(Tok(t, w["x"] * k, (w["x"] + w["w"]) * k, w["y"] * k, (w["y"] + w["h"]) * k))
    return lines_from_toks(ws, tol)


def page_lines(doc, pno):
    """Lines of a page: the text layer, or (pages with none) the OCR cache."""
    if pno in OCR_ONLY_PAGES:
        return ocr_lines(pno), "vision-ocr"
    return get_lines(doc[pno - 1]), "text"


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
             "-", "Estimated", "/", "Diff", "vs."} | set(extra_ok)
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
    # the citywide statement carries a trailing "Diff 2008 Adopted vs. 2007 Adopted" label: drop every header token at / right of it
    var = [t for t in htoks if t.text in ("Variance", "Diff")]
    if var:
        cut = min(t.x0 for t in var) - 5
        htoks = [t for t in htoks if t.x0 < cut]
    years = sorted([t for t in htoks if re.fullmatch(r"20\d\d", t.text)], key=lambda t: t.cx)
    bases = sorted([t for t in htoks if t.text in ("Actual", "Adopted", "Proposed", "Estimated")], key=lambda t: t.cx)
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
        self.ocr_blank_rows = []   # rows on scanned pages whose cells were all unreadable zeros

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
# Tier 1a: statement of revenues (pp.68-72)
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
             "Debt", "Service", "Total", "Source", "Department", "Variance", "-", "Estimated", "/", "Diff", "vs."}


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
                c2 = year_basis_columns(htoks, 5)
                if c2 is None:
                    out.unp(pno, "revenue header", "could not pair years with basis words")
                else:
                    cols = list(c2)
                k = j2
                continue
            body.append(ln)
            k += 1
        lines = body
        if cols is None:
            continue
        anchors = [c[0] for c in cols]
        lines = attach_label_orphans(lines, anchors)
        margin = min(l[0].x0 for l in lines)
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
            indented = first_x > margin + 8   # other-funds line items are indented; funds / totals sit at the margin
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
# Tier 1b: statement of expenditures by department (pp.85-86, All Funds) + GF by department (p.22)
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
                cols = year_basis_columns(htoks, 6)
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
    # General Fund by department: Budget-in-Brief p.22 (printed 6), right-hand table (2007 adopted, 2008 adopted).
    # Long names wrap ("Alcoholic Beverage Ctrl." / numbers / "Bd.") so a label-only line next to a numbers line is part of its label.
    lines = get_lines(doc[P_GF_BRIEF - 1])
    right = [[t for t in l if t.x0 >= 300] for l in lines]
    right = [l for l in right if l]
    hdr = next((k for k, l in enumerate(right) if ltext(l) == "2007 2008"), None)      # years line sits just above "Department Adopted Adopted"
    if hdr is None or ltext(right[hdr + 1]) != "Department Adopted Adopted":
        out.unp(P_GF_BRIEF, "general fund expenditures by department", "header not found")
    else:
        ys = sorted([t for t in right[hdr]], key=lambda t: t.cx)
        gcols = [(ys[0].cx, 2007, "adopted"), (ys[1].cx, 2008, "adopted")]
        body = right[hdr + 2:]
        isnum = lambda l: any(parse_num(t.text) is not None for t in l)
        yc = lambda l: sum((t.y0 + t.y1) / 2 for t in l) / len(l)
        used = set()
        for k, ln in enumerate(body):
            if k in used or not isnum(ln):
                continue
            label_toks = [t for t in ln if parse_num(t.text) is None and t.text != "$"]
            pre, post = [], []
            if not label_toks:
                # numbers-only line: the label wrapped around it ("Alcoholic Beverage Ctrl." / numbers / "Bd.")
                if k > 0 and (k - 1) not in used and not isnum(body[k - 1]) and abs(yc(body[k - 1]) - yc(ln)) < 12:
                    pre = body[k - 1]
                    used.add(k - 1)
                if k + 1 < len(body) and (k + 1) not in used and not isnum(body[k + 1]) and abs(yc(body[k + 1]) - yc(ln)) < 12:
                    post = body[k + 1]
                    used.add(k + 1)
            lab = clean_label(ltext(list(pre) + label_toks + list(post)))
            label, data, probs = split_row(ln, [c[0] for c in gcols])
            if lab.endswith("Total") or lab == "Total":
                lab = "Total"
            if lab == "Total":
                for c, v in data.items():
                    out.total(gcols[c][1], gcols[c][2], "department_expenditures/general_fund", lab, v, P_GF_BRIEF)
                continue
            for c, v in data.items():
                if v != "DASH":
                    out.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=gcols[c][1], basis=gcols[c][2], fund_group="general_fund",
                                             dept_code="", dept_name=lab, amount=v, page=P_GF_BRIEF))
        for k, ln in enumerate(body):
            if k not in used and not isnum(ln):
                out.unp(P_GF_BRIEF, "line %r" % ltext(ln), "label-only line not attached to a row")
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
    # GF-by-department (p.22, 2007/2008 adopted) must be <= all funds for the same department
    by = defaultdict(dict)
    for r in out.dept_exp:
        by[(norm(r["dept_name"]), r["fiscal_year"], r["basis"])][r["fund_group"]] = (r["amount"], r["page"], r["dept_name"])
    for (n, fy, b), g in sorted(by.items()):
        if "general_fund" in g and "all_funds" in g:
            if g["general_fund"][0] > g["all_funds"][0]:
                out.notes.append("p.22 GF %s (%s %s) exceeds the all-funds statement figure %s" % (g["general_fund"][0], fy, b, g["all_funds"][0]))
        elif "general_fund" in g and "all_funds" not in g:
            out.notes.append("p.22 general-fund department %r (%s %s) has no all-funds statement row" % (g["general_fund"][2], fy, b))


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
        if kind != "summary" and dept and body and lines[i][0].x0 < 150:
            nxt = body[0]
            # a long department name wraps onto the next line ("NEIGHBORHOOD HOUSING" / "IMPROVEMENT FUND")
            if abs(nxt[0].x0 - lines[i][0].x0) < 5 and not any(re.search(r"\d", t.text) or t.text in HDR_VOCAB for t in nxt) and ltext(nxt).isupper():
                dept = clean_label(dept + " " + ltext(nxt))
                body = body[1:]
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
            if pno in SCAN_PAGES and lab:
                # scanned page: every cell of this row was an unreadable zero-with-border (see OCR_DROPPED); the row is omitted, not guessed
                out.ocr_blank_rows.append(dict(page=pno, dept=dept, section=section, label=lab))
                continue
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
        lines, source = page_lines(doc, pno)
        segs = segments(lines)
        if source != "text":
            out.ocr_pages_used.append((pno, source))
        if not segs:
            nums = sum(1 for l in lines for t in l if parse_num(t.text) not in (None, "DASH") or parse_num(t.text, True) not in (None, "DASH"))
            if doc[pno - 1].mediabox.width > 700 and nums >= 2 and last is not None and last[0] != "summary":
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
    # (a0) a printed summary total with NO extracted rows under it is a failure, not a pass
    for scope_, lst in T.items():
        sc = scope_.split("/")
        if len(sc) == 3 and sc[0] == "department_detail" and sc[2] in ("by_type", "by_fund") and (sc[1], sc[2]) not in D and sc[1] != "CITYWIDE":
            for t in lst:
                out.check(2, "summary_total", t["scope"], t["fiscal_year"], t["basis"], 0, t["amount"], t["page"], "printed total but no rows extracted")
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

    # (d) citywide statement (All Funds, p.85-86; General Fund, p.22) vs department sections
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
                out.check(2, "dept_section_GF_vs_p22_general_fund", "department_detail/%s" % d, fy, b, gf.get((fy, b), 0), g["general_fund"], t["page"])
    for n in sorted(stm_names - matched):
        if any(abs(v) > 0 for (k, fy, b), g in stm.items() if k == n for v in g.values()):
            out.notes.append("citywide statement department %r has non-zero amounts but no department section" % n)

    # (e) citywide summary p83: sum of departments vs printed citywide.  Only the departments that appear in
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
    # (f) citywide summary p83 totals vs the citywide statement totals / p22 GF total
    for t in out.tot:
        if t["scope"] in ("department_detail/CITYWIDE/by_type", "department_detail/CITYWIDE/by_fund"):
            kind = "citywide_summary_p83_%s_vs_statement_total" % t["scope"].rsplit("/", 1)[1]
            for x in out.tot:
                if x["scope"] == "department_expenditures/all_funds" and x["fiscal_year"] == t["fiscal_year"] and x["basis"] == t["basis"]:
                    out.check(1, kind, "department_expenditures/all_funds", t["fiscal_year"], t["basis"], t["amount"], x["amount"], t["page"])
    for r in [r for r in out.cw_rows if r["table"] == "by_fund" and r["dim1"] == "GENERAL FUND"]:
        for x in out.tot:
            if x["scope"] == "department_expenditures/general_fund" and x["fiscal_year"] == r["fiscal_year"] and x["basis"] == r["basis"]:
                out.check(1, "citywide_summary_p83_GF_vs_p22_GF_total", "department_expenditures/general_fund", r["fiscal_year"], r["basis"], r["amount"], x["amount"], r["page"])
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
# cross-book comparisons (read-only use of the other extractions)
# ======================================================================================
X2012 = ROOT / "data/extracted/2012-operating-adopted"
X2009 = ROOT / "data/extracted/2009-operating-adopted"
# statement department names (this book -> other books); everything else matches after norm()
X_DEPT_ALIASES_2012 = {
    "Alcoholic Beverage Ctrl. Bd.": "Alcoholic Bvg Ctrl. Bd.", "City Planning Commission": "City Planning Comm.",
    "Clerk of Criminal District Court": "Clrk of Crim. Dist. Crt", "Economic Development Fund": "Economic Dev. Fund",
    "Neighborhood Housing Improvement Fund": "N’hood Hsg Imprv. Fnd", "Workforce Investment Act": "Workforce Invst. Act",
    "Office of Training Division": "Office of Training Div.",
}
X_FUND_ALIASES = {"LOCAL LAW ENFORCEMENT GRANTS": "LLE GRANTS"}


def _load(path, **flt):
    return [r for r in csv.DictReader(open(path, encoding="utf-8")) if all(r[k] == v for k, v in flt.items())]


def _fund_key(name):
    nm = re.sub(r"\s*-\s*FUND\b.*$", "", name)
    nm = re.sub(r",\s*FUND \d+.*$", "", nm)
    return norm(X_FUND_ALIASES.get(nm, nm)), nm


def cross_book(out, other_dir, other_name, dept_alias, years_dept, years_rev, basis="actual"):
    """This book's `basis` figures for the given years vs another extracted book's.  Reports agreement; differences are
    restatements or errors and are reported, not fixed."""
    res = {}
    if not (other_dir / "department_expenditures.csv").exists():
        return {"skipped": "%s extraction not found" % other_name}
    ref = {}
    for r in _load(other_dir / "department_expenditures.csv", fund_group="all_funds", basis=basis):
        ref[(norm(r["dept_name"]), int(r["fiscal_year"]))] = (int(r["amount"]), r["dept_name"])
    cmp_, agree, diffs, unmatched = 0, 0, [], set()
    for r in out.dept_exp:
        if r["fund_group"] != "all_funds" or r["basis"] != basis or r["fiscal_year"] not in years_dept:
            continue
        k = (norm(dept_alias.get(r["dept_name"], r["dept_name"])), r["fiscal_year"])
        if k not in ref:
            unmatched.add(r["dept_name"])
            continue
        cmp_ += 1
        if ref[k][0] == r["amount"]:
            agree += 1
        else:
            diffs.append(dict(dept_2008book=r["dept_name"], dept_other=ref[k][1], fiscal_year=r["fiscal_year"], book_2008=r["amount"],
                              book_other=ref[k][0], diff=r["amount"] - ref[k][0], page_2008=r["page"]))
    res["department_expenditures_all_funds_%s" % basis] = dict(compared=cmp_, agree=agree, differ=len(diffs), years=sorted(years_dept),
                                                               departments_without_counterpart=sorted(unmatched), differences=diffs)
    refl, reff = {}, defaultdict(int)
    for r in _load(other_dir / "revenues.csv", basis=basis):
        if r["fund_group"] == "general_fund":
            refl[(norm(r["category"]), norm(r["line_name"]), int(r["fiscal_year"]))] = int(r["amount"])
        else:
            reff[(_fund_key(r["fund_name"])[0], int(r["fiscal_year"]))] += int(r["amount"])
    cmp_, agree, diffs, unm = 0, 0, [], set()
    for r in out.rev:
        if r["fund_group"] != "general_fund" or r["basis"] != basis or r["fiscal_year"] not in years_rev:
            continue
        k = (norm(r["category"]), norm(r["line_name"]), r["fiscal_year"])
        if k not in refl:
            unm.add("%s / %s" % (r["category"], r["line_name"]))
            continue
        cmp_ += 1
        if refl[k] == r["amount"]:
            agree += 1
        else:
            diffs.append(dict(category=r["category"], line=r["line_name"], fiscal_year=r["fiscal_year"], book_2008=r["amount"], book_other=refl[k],
                              diff=r["amount"] - refl[k], page_2008=r["page"]))
    res["revenues_general_fund_lines_%s" % basis] = dict(compared=cmp_, agree=agree, differ=len(diffs), years=sorted(years_rev),
                                                         lines_without_counterpart=sorted(unm), differences=diffs)
    sums = defaultdict(int)
    for r in out.rev:
        if r["fund_group"] == "other_funds" and r["basis"] == basis and r["fiscal_year"] in years_rev:
            n, nm = _fund_key(r["fund_name"])
            sums[(n, r["fiscal_year"], nm)] += r["amount"]
    cmp_, agree, diffs, unm = 0, 0, [], set()
    for (n, fy, nm), v in sorted(sums.items()):
        if (n, fy) not in reff:
            unm.add(nm)
            continue
        cmp_ += 1
        if reff[(n, fy)] == v:
            agree += 1
        else:
            diffs.append(dict(fund=nm, fiscal_year=fy, book_2008=v, book_other=reff[(n, fy)], diff=v - reff[(n, fy)]))
    res["revenues_other_funds_totals_by_fund_%s" % basis] = dict(compared=cmp_, agree=agree, differ=len(diffs), years=sorted(years_rev),
                                                                 funds_without_counterpart=sorted(unm), differences=diffs)
    return res


def cross_book_2009_adopted2008(out):
    """The 2009 book re-prints 2008 as an ADOPTED column: compare it with this book's own 2008 adopted (differences = amendments
    between adoption in Dec 2007 and the 2009 book, or restatements)."""
    if not (X2009 / "department_expenditures.csv").exists():
        return {"skipped": "2009 extraction not found"}
    ref = {}
    for r in _load(X2009 / "department_expenditures.csv", fund_group="all_funds", basis="adopted", fiscal_year="2008"):
        ref[norm(r["dept_name"])] = (int(r["amount"]), r["dept_name"])
    cmp_, agree, diffs = 0, 0, []
    for r in out.dept_exp:
        if r["fund_group"] == "all_funds" and r["basis"] == "adopted" and r["fiscal_year"] == 2008:
            k = norm(r["dept_name"])
            if k in ref:
                cmp_ += 1
                if ref[k][0] == r["amount"]:
                    agree += 1
                else:
                    diffs.append(dict(dept=r["dept_name"], book_2008=r["amount"], book_2009=ref[k][0], diff=ref[k][0] - r["amount"]))
    return dict(compared=cmp_, agree=agree, differ=len(diffs), note="2008 adopted all-funds by department: this book vs the same column printed in the 2009 book",
                differences=diffs)


# ======================================================================================
# OCR as an independent reader
# ======================================================================================
_OCR = {}


def ocr_page(pno):
    if pno not in _OCR:
        f = OCR_DIR / ("p%04d.json" % pno)
        _OCR[pno] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    return _OCR[pno]


def fmt_amount(v):
    """How an amount is printed: 1,234  /  1.00 for FTE (Decimal keeps its places)."""
    if isinstance(v, Decimal):
        return [str(abs(v))]
    return ["{:,}".format(abs(v))]


def _clean_tok(t):
    return re.sub(r"[$()\s]", "", t)


def _lev1(a, b):
    """True when a and b differ by exactly one substitution / insertion / deletion."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def ocr_agreement(out, detail_rows, max_examples=40):
    """For every extracted cell on a text-layer page: does the Vision OCR of the cited page contain the same printed number?
    This is the accuracy OCR alone would have given.  Cells are split into NON-ZERO amounts and ZERO amounts (Vision fuses a zero
    with the table border to its right, '01', or drops it, so zeros are a different problem).  A miss is classified as a
    one-character OCR difference (some OCR token on the page differs by one character), a split number (two adjacent OCR tokens
    on one line whose digits concatenate to the number) or not found."""
    files = {"department_expenditures": out.dept_exp, "revenues": out.rev, "department_detail": detail_rows, "positions": out.pos}
    res = {}
    for name, rows in files.items():
        st = {z: dict(cells=0, hit=0, one_char=0, split=0, miss=0) for z in ("nonzero", "zero")}
        ex = []
        for r in rows:
            if r["page"] in SCAN_PAGES:
                continue
            pg = ocr_page(r["page"])
            if pg is None:
                continue
            if "_toks" not in pg:
                pg["_toks"] = [_clean_tok(w["text"]) for l in pg["lines"] for w in l["words"]]
                pg["_tset"] = set(pg["_toks"])
                pairs = set()
                for l in pg["lines"]:
                    ts = [_clean_tok(w["text"]) for w in l["words"]]
                    for u, v in zip(ts, ts[1:]):
                        pairs.add(re.sub(r"\D", "", u + v))
                pg["_pairs"] = pairs
            amt = r.get("amount", r.get("fte"))
            want = fmt_amount(amt)[0]
            z = "zero" if amt == 0 else "nonzero"
            sdict = st[z]
            sdict["cells"] += 1
            if want in pg["_tset"]:
                sdict["hit"] += 1
            elif re.sub(r"\D", "", want) in pg["_pairs"] and len(re.sub(r"\D", "", want)) >= 4:
                sdict["split"] += 1
            elif any(_lev1(want, t) for t in pg["_toks"] if t[:1].isdigit() or t[:1] in "-."):
                sdict["one_char"] += 1
                if len(ex) < max_examples:
                    ex.append(dict(page=r["page"], fiscal_year=r["fiscal_year"], extracted=want, kind="one-character OCR difference"))
            else:
                sdict["miss"] += 1
                if z == "nonzero" and len(ex) < max_examples:
                    ex.append(dict(page=r["page"], fiscal_year=r["fiscal_year"], extracted=want, kind="not found in OCR of that page"))
        nz, zr = st["nonzero"], st["zero"]
        tot = nz["cells"] + zr["cells"]
        hit = nz["hit"] + zr["hit"]
        res[name] = dict(cells=tot, exact_in_ocr=hit, share_exact=round(hit / tot, 4) if tot else None,
                         nonzero=dict(nz, share_exact=round(nz["hit"] / nz["cells"], 4) if nz["cells"] else None),
                         zero=dict(zr, share_exact=round(zr["hit"] / zr["cells"], 4) if zr["cells"] else None),
                         examples_nonzero_misses=ex)
    return res


def spot_checks(out, detail_rows, n_per_file=20, seed=2008):
    """Independent spot check of random cells with TWO other readers: (1) pdfplumber (another text-layer engine; words of
    sideways pages come out reversed and are re-reversed here) and (2) the Vision OCR (an image reader).  A cell passes a
    reader when that reader sees the printed number on the same line of the cited page as the first word of the row label."""
    import random
    import pdfplumber
    rnd = random.Random(seed)
    files = {"department_expenditures": (out.dept_exp, "dept_name"), "revenues": (out.rev, "line_name"),
             "department_detail": (detail_rows, "dim1"), "positions": (out.pos, "position_title")}
    results, summary = [], {}
    with pdfplumber.open(str(PDF)) as pl:
        cache = {}

        def plwords(pno):
            if pno not in cache:
                raw = pl.pages[pno - 1].extract_words()
                longw = [w for w in raw if len(w["text"]) > 2]
                sideways = sum((w["bottom"] - w["top"]) > (w["x1"] - w["x0"]) * 1.2 for w in longw) > len(longw) / 2
                # on a sideways page every word is vertical: its text comes out reversed and its line coordinate is x
                cache[pno] = [(w["text"][::-1] if sideways else w["text"], (w["x0"] + w["x1"]) / 2 if sideways else (w["top"] + w["bottom"]) / 2, sideways) for w in raw]
            return cache[pno]
        for name, (rows, labkey) in files.items():
            sample = rnd.sample([r for r in rows if r["page"] not in SCAN_PAGES], n_per_file)
            ok_pl = ok_ocr = zc = zo = 0
            for r in sample:
                amt = r.get("amount", r.get("fte"))
                want = fmt_amount(amt)[0]
                first = clean_label(str(r[labkey])).split(" ")[0]
                ws = plwords(r["page"])
                nums = [w for w in ws if _clean_tok(w[0]) == want]
                labw = [w for w in ws if w[0] == first or w[0].startswith(first[:4])]
                pl_ok = any(abs(n[1] - l[1]) < 4 and n[2] == l[2] for n in nums for l in labw)
                pg = ocr_page(r["page"])
                ocr_ok = False
                if pg:
                    k = 72.0 / pg["dpi"]
                    on = [((w["y"] + w["h"] / 2) * k) for l in pg["lines"] for w in l["words"] if _clean_tok(w["text"]) == want]
                    ol = [((w["y"] + w["h"] / 2) * k) for l in pg["lines"] for w in l["words"] if w["text"] == first or w["text"].startswith(first[:4])]
                    ocr_ok = any(abs(a - b) < 4 for a in on for b in ol)
                ok_pl += pl_ok
                ok_ocr += ocr_ok
                zc += amt == 0
                zo += (amt == 0) and ocr_ok
                results.append(dict(file=name, page=r["page"], label=r[labkey], fiscal_year=r["fiscal_year"], basis=r["basis"],
                                    extracted=str(amt), zero_cell=bool(amt == 0), pdfplumber=pl_ok, vision_ocr=ocr_ok))
            summary[name] = dict(sampled=len(sample), pdfplumber_pass=ok_pl, vision_ocr_pass=ok_ocr, zero_cells_in_sample=int(zc), zero_cells_read_by_ocr=int(zo))
    return dict(seed=seed, method="random cells; reader sees the printed number and the first label word on the same line of the cited page",
                summary=summary, cells=results)


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
    def has(p, s_):
        return s_ in doc[p - 1].get_text()
    assert has(3, "ADOPTED DECEMBER 1, 2007")
    assert has(P_CITYWIDE_SUMMARY, "CITYWIDE") and has(P_CITYWIDE_SUMMARY, "TOTAL EXPENDITURES")
    assert has(68, "GENERAL FUND - 001") and has(72, "CONSOLIDATED TOTAL")
    assert has(85, "City-Wide Expenditures") and has(86, "Total")
    assert has(P_GF_BRIEF, "GENERAL FUND EXPENDITURES") or has(P_GF_BRIEF, "GENERAL") and has(P_GF_BRIEF, "EXPENDITURES")
    assert has(103, "COUNCIL") and has(599, "CANAL")
    for p_ in OCR_ONLY_PAGES:
        assert (OCR_DIR / ("p%04d.json" % p_)).exists() and len(doc[p_ - 1].get_images()) >= 1, p_


def main():
    doc = fitz.open(str(PDF))
    assert doc.page_count == 608, doc.page_count
    sanity(doc)
    for pg in doc:
        for w in pg.get_text("words"):
            k = re.sub(r"[^a-z0-9]", "", w[4].lower())
            VOCAB[k] = VOCAB.get(k, 0) + 1
    out = Out()
    out.narr_pages = []
    out.cw_rows = []
    out.ocr_pages_used = []
    parse_revenues(doc, out)
    parse_dept_expenditures(doc, out)
    lines = get_lines(doc[P_CITYWIDE_SUMMARY - 1])
    seg = segments(lines)[0]
    tmp_type, tmp_fund = [], []
    parse_summary_page(seg[2], 0, P_CITYWIDE_SUMMARY, seg[1], out, tmp_type, tmp_fund)
    out.cw_rows = tmp_type + tmp_fund
    parse_departments(doc, out)
    dept_checks(out)

    def pages_with(rng, key):
        return [p for p in rng if key in doc[p - 1].get_text()[:900]]
    for key, why in (
            ("Program Name:", "program narrative pages (funding offer text, result areas, performance measures); free text"),
            ("Result:", "result-area narrative / 'Funding Summary' / 'Key Programs Funded' / 'Unfunded Priority Programs' pages: text and small charts, not budget tables"),
            ("Performance", "KPI actual-vs-target tables with mixed units; not parsed")):
        pgs = pages_with(out.narr_pages, key)
        if pgs:
            out.unp("%d pages: %s" % (len(pgs), ",".join(map(str, pgs))), key, why)
    out.unp("1-67 (excl. 22)", "narrative, Budget-in-Brief, financial policies, federal recovery funds, capital budget review, description of revenues, millage rates, long-term projections",
            "not attempted: outside the requested tiers (p.22 General Fund by department is captured)")
    out.unp("73-82", "statement of debt (Board of Liquidation outstanding debt summary)", "not attempted: outside the requested tiers")
    out.unp("87-96", "City-Wide Work Years by Department / Program (printed p.71-80), 2004-2008", "printed rotated with programs as columns; not extracted (department Personnel Summaries are extracted and checked against their printed FTE totals)")
    out.unp("603-608", "capital-project glossary / back matter", "narrative")
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

    detail_rows = [r for r in out.detail if r["dept_name"] != "CITYWIDE"]
    PRINTED_TOTAL_KINDS = {
        "revenue_total", "dept_statement_total", "summary_total", "program_sum_vs_fund_total", "program_sum_vs_dept_total",
        "fund_totals_vs_dept_total", "program_detail_sum_vs_fund_total", "program_detail_sum_vs_dept_total",
        "program_row_PS+OO+DS=Total", "fte_sum_vs_program_total", "fte_sum_vs_fund_total", "fte_sum_vs_dept_total"}
    for c in out.checks:
        c["against"] = "printed_total" if c["kind"] in PRINTED_TOTAL_KINDS else "cross_check"
        d_ = c["diff"]
        c["small_mismatch_le_5"] = bool(c["status"] == "mismatch" and re.fullmatch(r"-?\d+", str(d_)) and abs(int(d_)) <= 5)

    # ---- OCR-sourced pages: which tables foot?  Rows from OCR pages that sit in a table that does not foot are listed
    # as UNVERIFIED (and are not written to the CSVs).
    unverified, keep_detail, keep_pos = [], [], []
    # mismatches on scanned pages that were checked against the page IMAGE by eye and are the book's own printing error
    # (not an OCR misread): they are reported as mismatches but do not make the rows "unverified"
    IMAGE_CONFIRMED = [(453, "program_detail_sum_vs_dept_total", "Personal Services"), (453, "program_detail_vs_budget_summary", "Personal Services")]
    def confirmed(c):
        return any(c["page"] == pg_ and c["kind"] == k_ and sc_ in c["scope"] for pg_, k_, sc_ in IMAGE_CONFIRMED)
    out.notes.append("p.453 (scan): DEPARTMENT TOTAL row prints Personal Services $422,333 (the 2007 adopted figure) while its program rows and fund total are 528,955; "
                     "checked against the page image: the book prints $422,333, so the program-detail Personal Services total mismatches the department summary (528,955) -- the book's own error, kept as a mismatch")
    bad_scopes = [c for c in out.checks if c["page"] in SCAN_PAGES and c["status"] != "exact" and c["against"] == "printed_total" and not confirmed(c)]
    bad_detail, bad_pos = set(), set()
    for c in bad_scopes:
        sc = c["scope"].split("/")
        if sc[0] == "department_detail":
            tbl = sc[2] if len(sc) > 2 else None
            if tbl in ("Personal Services", "Other Operating", "Debt Service", "Total"):
                tbl = "by_program_type"           # 'department_detail/<dept>/<type>' = program detail vs department summary
            bad_detail.add((sc[1], tbl, c["fiscal_year"], c["basis"]))
        elif sc[0] == "positions":
            bad_pos.add((sc[1], c["fiscal_year"], c["basis"]))
    for r in detail_rows:
        if r["page"] in SCAN_PAGES and (r["dept_name"], r["table"], r["fiscal_year"], r["basis"]) in bad_detail:
            unverified.append(dict(file="department_detail.csv", **{k: r[k] for k in ("fiscal_year", "basis", "dept_name", "table", "dim1", "dim2", "amount", "page")}))
        else:
            keep_detail.append(r)
    for r in out.pos:
        if r["page"] in SCAN_PAGES and (r["dept_name"], r["fiscal_year"], r["basis"]) in bad_pos:
            unverified.append(dict(file="positions.csv", **{k: r[k] for k in ("fiscal_year", "basis", "dept_name", "program", "fund", "position_title", "fte", "page")}))
        else:
            keep_pos.append(r)

    write_csv("department_expenditures.csv", ["book_id", "fiscal_year", "basis", "fund_group", "dept_code", "dept_name", "amount", "page"], out.dept_exp)
    write_csv("revenues.csv", ["book_id", "fiscal_year", "basis", "fund_group", "fund_name", "category", "line_name", "amount", "page"], out.rev)
    write_csv("department_detail.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "table", "dim1", "dim2", "amount", "page"], keep_detail)
    write_csv("positions.csv", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "program", "fund", "position_title", "pay_grade", "fte", "page"], keep_pos)
    write_csv("printed_totals.csv", ["book_id", "fiscal_year", "basis", "scope", "label", "amount", "page"], out.tot)

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
    ocr_cells = {"department_detail.csv": sum(1 for r in keep_detail if r["page"] in SCAN_PAGES), "positions.csv": sum(1 for r in keep_pos if r["page"] in SCAN_PAGES)}
    report = {
        "book_id": BOOK_ID,
        "cover_check": "PDF p.3 reads 'OPERATING BUDGET FOR CALENDAR AND FISCAL YEAR 2008, ADOPTED DECEMBER 1, 2007 BY MAYOR C. RAY NAGIN AND THE NEW ORLEANS CITY COUNCIL': the ADOPTED 2008 book (it also prints a 2008 Proposed column).",
        "text_layer_finding": "The brief called this an image-only scan with no text layer. It is a Crystal Reports PDF (creator 'Crystal Reports', 2008-02) with a text layer on 455 of 608 pages, including every statement and department table. 153 pages carry under 100 characters of text: 132 of them are blank sheets bearing only a page number, the others are the cover (p.1), the scanned inserts in ocr_only_pages and a few near-empty title / divider pages. Landscape tables are printed sideways on portrait pages (text running bottom-to-top) or with /Rotate 270.",
        "ocr_only_pages": {"pages": list(OCR_ONLY_PAGES),
                           "content": "scanned inserts read from the Vision OCR cache: NEIGHBORHOOD HOUSING IMPROVEMENT FUND departmental budget summary (p.451), program detail (p.453), expenditure summary (p.454), personnel summary (p.455); ECONOMIC DEVELOPMENT FUND departmental budget summary (p.467) and program detail (p.468; its embedded text layer is an unreliable third-party OCR that drops cells, so it is NOT used)",
                           "cells_written_from_ocr": ocr_cells,
                           "ocr_tokens_dropped_as_border_or_unreadable_zero": {"count": len(OCR_DROPPED), "by_page": dict(sorted(collections.Counter(d["page"] for d in OCR_DROPPED).items())),
                                                                               "examples": OCR_DROPPED[:25],
                                                                               "policy": "zeros fused with the cell border ('01', '0|', 'o') are unreadable and omitted (blank cells are omitted in this schema); they are never guessed"},
                           "rows_with_only_unreadable_cells": out.ocr_blank_rows,
                           "ocr_digit_misreads_found": "p.455: program number 7821 read as '78:21' (that program's positions fail to foot and are listed under unverified_ocr_rows, not written to positions.csv)"},
        "row_counts": {
            "department_expenditures.csv": len(out.dept_exp), "revenues.csv": len(out.rev),
            "department_detail.csv": len(keep_detail), "positions.csv": len(keep_pos), "printed_totals.csv": len(out.tot),
            "department_detail.csv by table": {t: sum(1 for r in keep_detail if r["table"] == t) for t in ("by_type", "by_fund", "by_program", "by_program_type")},
        },
        "check_summary_by_tier": {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in sorted(summ.items())},
        "check_summary_by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "footing_share": footing_share(out, keep_detail, keep_pos),
        "tier_definitions": {"tier1": "citywide statements of revenues (p.68-72) and expenditures (p.85-86 all funds; p.22 general fund) + p.83 citywide summary cross-checks",
                              "tier2": "department sections: by_type, by_fund, by_program(_type) and cross-checks to the citywide statements",
                              "tier3": "positions / FTE (Personnel Summaries)"},
        "unverified_ocr_rows": unverified,
        "mismatches": [c for c in out.checks if c["status"] == "mismatch"],
        "within_1": [c for c in out.checks if c["status"] == "within_1"],
        "checks": out.checks,
        "unparsed": out.unparsed,
        "printed_totals_rows_not_covered_by_any_check": dict(unchecked),
        "cross_book_2012": cross_book(out, X2012, "2012", X_DEPT_ALIASES_2012, {2004, 2005, 2006}, {2005, 2006}),
        "cross_book_2009_actuals": cross_book(out, X2009, "2009", {}, {2004, 2005, 2006}, {2005, 2006}),
        "cross_book_2009_adopted_2008": cross_book_2009_adopted2008(out),
        "ocr_agreement": ocr_agreement(out, detail_rows),
        "spot_checks": spot_checks(out, detail_rows),
        "narrative_or_chart_pages_skipped": {"range": "%d-%d" % (DEPT_PAGES.start, DEPT_PAGES.stop - 1), "pages": out.narr_pages},
        "row_level_problems": out.problems,
        "notes": out.notes + ["wrapped position titles joined: %d" % out.wrapped_titles],
        "label_repairs": sorted(REPAIRS),
        "label_suspects_not_repaired": sorted(sus),
        "section_pages": {k: len(v) for k, v in kinds_pages.items()},
        "layout_notes": [
            "Same family of layouts as the 2009 book (both Crystal Reports): one All-Funds citywide expenditure statement (p.85-86) + General Fund by department only on the Budget-in-Brief page (p.22)",
            "Revenue statement has 5 columns: 2005-2006 actual, 2007 adopted, 2008 proposed, 2008 adopted; expenditure statement 6 columns: 2004-2006 actual, 2007 adopted, 2008 proposed, 2008 adopted (+ a 'Diff 2008 adopted vs 2007 adopted' % column, ignored)",
            "Department summary pages carry 2006 actual, 2007 adopted, 2008 proposed, 2008 adopted; personnel summaries 2007 adopted, 2008 proposed, 2008 adopted",
            "Unlike 2009 (2007 = actual) this book has no 2007 actuals: its latest actual year is 2006",
        ],
        "deviations_from_schema": [
            "department_detail.table has an extra value by_program_type (Program Detail table; dim2 = '<fund> | <type>'); by_program = Expenditure Summary (dim2 = fund)",
            "no workyears_citywide.csv: the citywide work-years table is printed rotated and was not extracted",
            "dept_code is blank everywhere: the 2008 book prints no department numbers; program numbers are in dim1/program",
            "department_expenditures.csv has only fund_group all_funds (2004-2008, p.85-86) and general_fund (2007/2008 adopted only, p.22); no other_funds rows",
            "revenues.csv fund_group is general_fund for the GF statement and other_funds for Other Discretionary / Other / Special Revenue funds; category = the printed section for those; fund_name = fund heading as printed",
            "variance (Diff) % column on p.85-86 ignored (derived)",
            "OCR-sourced rows that sit in a table that does not foot are NOT written to the CSVs; they are listed in unverified_ocr_rows",
        ],
    }
    (OUT / "checks.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("row_counts", "check_summary_by_tier", "footing_share")}, indent=1))
    print("mismatches:", len(report["mismatches"]), " unparsed:", len(out.unparsed), " problems:", len(out.problems), " unverified OCR rows:", len(unverified))


if __name__ == "__main__":
    main()
