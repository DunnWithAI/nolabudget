#!/usr/bin/env python
"""Extract the City of New Orleans 2020 PROPOSED Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2020.py

(The city never posted an adopted 2020 book; the 2021 book's prior-year columns carry the adopted 2020 figures.)

This book's text layer is broken for part of the pages: many table labels and ALL numbers on those pages are
written with a non-embedded Identity-H Arial whose glyph ids are not Unicode, so PyMuPDF's plain text drops the
numbers and mangles the labels.  Words are therefore rebuilt from PyMuPDF's text trace: where the trace has no
Unicode for a character, the glyph id is decoded as Arial's standard glyph order (gid + 29 = ASCII).  The
'DEPARTMENTAL BUDGET SUMMARY' page of each department uses a different, page-specific glyph numbering that cannot
be decoded; those pages are listed under unparsed (their content is carried by the by-type page, the program
tables and the citywide statements).

Outputs (data/extracted/2020-operating-proposed/): department_expenditures.csv, revenues.csv,
department_detail.csv, positions.csv, printed_totals.csv, checks.json   (see data/extracted/SCHEMA.md).

Method: every page is read as positioned words (PyMuPDF).  Words are clustered into text lines by
y, then numbers are assigned to columns by the x-position of the year/basis header that sits above
the line - never by sequence - except in the two cross-check-only tables (pp.100-102) where the
column set is fixed and the number of cells is verified.  No numbers are typed into this script;
page locations are found by their printed titles.

Layout of the file:  1) helpers  2) page locators  3) one parser per table type
4) reconciliation (checks)  5) writers.
"""
import csv
import json
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "data/raw/nola-gov/2020/2020-Proposed-Budget-Book.pdf"
BOOK_ID = "2020-operating-proposed"
OUT = ROOT / "data/extracted" / BOOK_ID

BASES = {"actual": "actual", "adopted": "adopted", "proposed": "proposed", "projected": "projected",
         "estimate": "estimate", "estimated": "estimate"}
DASH = object()          # a printed dash / blank-equivalent cell
NUM_RE = re.compile(r"^\$?\(?-?\$?[\d,]*\.?\d+\)?$")
DASH_RE = re.compile(r"^\$?-$")
YEAR_RE = re.compile(r"^(20[0-3]\d)$")


# --------------------------------------------------------------------------- helpers
def clean(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_num(tok):
    """Return int/float, DASH, or None (not numeric)."""
    t = tok.strip()
    if DASH_RE.match(t):
        return DASH
    if not NUM_RE.match(t):
        return None
    neg = t.startswith("(") or "(" in t or t.startswith("-") or t.startswith("$-")
    body = re.sub(r"[^\d.]", "", t)
    if body in ("", "."):
        return None
    v = float(body) if "." in body else int(body)
    return -v if neg else v


class Word:
    __slots__ = ("x0", "y0", "x1", "y1", "t")

    def __init__(self, w):
        self.x0, self.y0, self.x1, self.y1, self.t = w[0], w[1], w[2], w[3], w[4].strip()

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2


class Line:
    def __init__(self, words):
        self.words = sorted(words, key=lambda w: w.x0)
        self.y = sum(w.cy for w in words) / len(words)
        self.cells = []        # (col_idx, value)
        self.label_words = []
        self.hdr = None
        self.extra = []

    @property
    def text(self):
        return clean(" ".join(w.t for w in self.words if w.t != "|"))

    @property
    def label(self):
        return clean(" ".join(w.t for w in self.label_words if w.t != "|"))


def cluster_lines(words, tol=3.6):
    ws = sorted((w for w in words if w.t and w.t != "|"), key=lambda w: w.cy)
    groups = []
    for w in ws:
        if groups and abs(w.cy - groups[-1][-1].cy) <= tol:
            groups[-1].append(w)
        else:
            groups.append([w])
    return [Line(g) for g in groups]


GID_EXTRA = {182: "\u2019"}      # Arial glyph ids outside the ASCII run (right single quote)


def decoded_words(page, stats=None):
    """Words rebuilt from the text trace.  A trace character with Unicode U+FFFD (or a control code) is decoded as
    Arial standard glyph order: ASCII = gid + 29.  Unknown glyph ids become '\ufffd' and are counted in stats."""
    chars = []
    for s in page.get_texttrace():
        for uni, gid, org, bb in s["chars"]:
            if uni == 65533 or uni < 32:
                if gid in GID_EXTRA:
                    c = GID_EXTRA[gid]
                elif 3 <= gid <= 97:
                    c = chr(gid + 29)
                else:
                    c = "\ufffd"
                    if stats is not None:
                        stats[gid] = stats.get(gid, 0) + 1
                if stats is not None:
                    stats["_decoded"] = stats.get("_decoded", 0) + 1
            else:
                c = chr(uni)
            chars.append((c, bb, s["size"]))
    chars.sort(key=lambda c: ((c[1][1] + c[1][3]) / 2, c[1][0]))
    lines = []
    for c in chars:
        cy = (c[1][1] + c[1][3]) / 2
        if lines and abs(lines[-1][0] - cy) <= 2.0:
            lines[-1][1].append(c)
        else:
            lines.append([cy, [c]])
    out = []
    for cy, cs in lines:
        cs.sort(key=lambda c: c[1][0])
        cur = []
        for c in cs:
            if c[0] in (" ", "\u00a0"):
                if cur:
                    out.append(cur)
                    cur = []
                continue
            if cur and c[1][0] - cur[-1][1][2] > 0.17 * c[2]:
                out.append(cur)
                cur = []
            cur.append(c)
        if cur:
            out.append(cur)
    return [(min(c[1][0] for c in w), min(c[1][1] for c in w), max(c[1][2] for c in w), max(c[1][3] for c in w),
             "".join(c[0] for c in w)) for w in out]


class Doc:
    def __init__(self, path):
        self.d = fitz.open(str(path))
        self.n = len(self.d)
        self._lines = {}
        self.decode_stats = {}

    def lines(self, p):          # p is 1-based
        if p not in self._lines:
            ws = [Word(w) for w in decoded_words(self.d[p - 1], self.decode_stats.setdefault(p, {}))]
            self._lines[p] = cluster_lines(ws)
        return self._lines[p]

    def first_text(self, p, k=1):
        ls = self.lines(p)
        return clean(" ".join(l.text for l in ls[:k]))


def basis_of(tok):
    """Basis word of a column header; tolerant of the book's own typo 'Adoptred' (p.78)."""
    t = tok.lower()
    if t in BASES:
        return BASES[t]
    return "adopted" if t.startswith("adopt") and len(t) <= 9 else None


def header_groups(lines):
    """Find year/basis header rows: lines holding >=3 distinct year tokens.  Returns list of
    dicts {y, cols:[(year, basis, cx)]}.  Basis words are matched by x among words within 30pt."""
    allw = [w for l in lines for w in l.words]
    groups = []
    for l in lines:
        yrs = [w for w in l.words if YEAR_RE.match(w.t)]
        xs = sorted(w.cx for w in yrs)
        if len(yrs) < 2 or any(b - a < 25 for a, b in zip(xs, xs[1:])):
            continue
        cols = []
        for w in yrs:
            best = None
            for b in allw:
                bt = basis_of(b.t)
                if bt and abs(b.cy - w.cy) <= 30 and abs(b.cx - w.cx) <= 45:
                    d = abs(b.cx - w.cx) + abs(b.cy - w.cy) * 0.5
                    if best is None or d < best[0]:
                        best = (d, bt)
            cols.append((int(w.t), best[1] if best else None, w.cx))
        groups.append({"y": l.y, "cols": sorted(cols, key=lambda c: c[2])})
    # a header may be split over lines (e.g. '2019 2020' above 'Expenditure Type 2018 Actual'): add lone year tokens
    # that sit within 18pt of a header group, away from its columns, and have a basis word beside them
    for g in groups:
        for l in lines:
            if abs(l.y - g["y"]) > 18 or l.y == g["y"]:
                continue
            yrs = [w for w in l.words if YEAR_RE.match(w.t)]
            if len(yrs) != 1:
                continue
            w = yrs[0]
            if any(abs(w.cx - c[2]) < 25 for c in g["cols"]):
                continue
            best = None
            for b in allw:
                bt = basis_of(b.t)
                if bt and abs(b.cy - w.cy) <= 30 and abs(b.cx - w.cx) <= 45:
                    d = abs(b.cx - w.cx) + abs(b.cy - w.cy) * 0.5
                    if best is None or d < best[0]:
                        best = (d, bt)
            if best:
                g["cols"] = sorted(g["cols"] + [(int(w.t), best[1], w.cx)], key=lambda c: c[2])
                g["y"] = max(g["y"], l.y)
    return groups


def assign_cells(lines, groups=None, ytop=None):
    """Attach hdr/cells/label_words to each line using year header columns by x-position."""
    groups = header_groups(lines) if groups is None else groups
    for l in lines:
        if re.match(r"^Page \d+$", l.text):      # page footer: the page number must never be read as a cell
            l.label_words = list(l.words)
            continue
        cands = [g for g in groups if g["y"] < l.y - 1]
        l.hdr = max(cands, key=lambda g: g["y"]) if cands else None
        if not l.hdr or len(l.hdr["cols"]) < 1:
            l.label_words = list(l.words)
            continue
        cols = l.hdr["cols"]
        sp = min([b[2] - a[2] for a, b in zip(cols, cols[1:])] or [80])
        tol = 0.62 * sp
        used = set()
        for w in l.words:
            v = parse_num(w.t)
            if v is None:
                continue
            ci = min(range(len(cols)), key=lambda i: abs(cols[i][2] - w.cx))
            if abs(cols[ci][2] - w.cx) <= tol and ci not in used:
                used.add(ci)
                l.cells.append((ci, v, w))
        cw = {id(c[2]) for c in l.cells}
        # numbers right of the last year column are the 'variance / change' columns, which are not captured
        xmax = cols[-1][2] + tol
        l.extra = [w for w in l.words if id(w) not in cw and parse_num(w.t) is not None and w.cx > xmax]
        ex = {id(w) for w in l.extra}
        l.label_words = [w for w in l.words if id(w) not in cw and id(w) not in ex]
    return lines


class Sink:
    """Collects rows for every output file."""

    def __init__(self):
        self.dept_exp, self.revenues, self.detail, self.positions, self.totals = [], [], [], [], []
        self.checks = []           # printed total reconciliations
        self.cross = []            # cross-checks
        self.unparsed = []
        self.notes = []
        self.duplicate_pages = []
        self.not_attempted = []
        self.undecodable = []

    def total(self, year, basis, scope, label, amount, page):
        self.totals.append(dict(book_id=BOOK_ID, fiscal_year=year, basis=basis, scope=scope,
                                label=label, amount=amount, page=page))


def status_of(diff):
    if diff == 0:
        return "exact"
    return "within_1" if abs(diff) <= 1 else "mismatch"


def add_check(sink, tier, scope, label, page, year, basis, printed, summed, method, extra=None):
    diff = printed - summed
    rec = dict(tier=tier, scope=scope, label=label, page=page, fiscal_year=year, basis=basis,
               printed=printed, extracted_sum=summed, diff=diff, status=status_of(diff), method=method)
    if extra:
        rec.update(extra)
    sink.checks.append(rec)


def add_cross(sink, tier, name, key, a_name, a, b_name, b, page=None):
    diff = a - b
    sink.cross.append(dict(tier=tier, check=name, key=key, a=a_name, a_value=a, b=b_name, b_value=b,
                           diff=diff, status=status_of(diff), page=page))


# stack reduction used for hierarchical totals (revenues, expenditure statements, by_program ...)
def stack_total(stack, printed_vals):
    """printed_vals: list per column (None when absent).  Choose the smallest suffix of `stack`
    that reproduces the most printed columns exactly.  Returns (k, sums)."""
    ncol = len(printed_vals)
    best = None
    run = [0] * ncol
    for k in range(1, len(stack) + 1):
        it = stack[-k]
        for c in range(ncol):
            run[c] += it["vals"][c] or 0
        m = sum(1 for c in range(ncol) if printed_vals[c] is not None and run[c] == printed_vals[c])
        if best is None or m > best[0]:
            best = (m, k, list(run))
        if m == sum(1 for p in printed_vals if p is not None):
            break
    return best[1], best[2], best[0]


# --------------------------------------------------------------------------- locators
def find_pages(doc, pred, lo=1, hi=None):
    hi = hi or doc.n
    return [p for p in range(lo, hi + 1) if pred(doc, p)]


# --------------------------------------------------------------------------- tier 1a: dept expenditures
def align_rows(n_stmt, other, same, is_zero, sink, page, stmt_names):
    """Two-pointer alignment of statement rows (0..n_stmt-1) with another table's rows (list).  A statement
    row that has no counterpart is allowed only if it is all-zero (the other table omits it); anything else is
    recorded as an alignment failure."""
    pairs, used = [], set()
    for i in range(n_stmt):
        j = next((j for j in range(len(other)) if j not in used and same(i, j)), None)
        if j is not None:
            used.add(j)
            pairs.append((i, j))
        elif is_zero(i):
            sink.notes.append(f"p.{page}: statement row {stmt_names[i]!r} is all-zero for the compared years and absent from this table")
        else:
            sink.unparsed.append(dict(page=page, what=f"could not align statement row {stmt_names[i]!r} with this table's rows"))
    if len(used) < len(other):
        sink.unparsed.append(dict(page=page, what=f"{len(other) - len(used)} rows of this table not aligned to statements"))
    return pairs


def parse_dept_statement(doc, p, sink, fund_group):
    """One 'General Fund / Other Fund / Total Expenditures' by-department page."""
    lines = assign_cells(doc.lines(p))
    rows, total = [], None
    for l in lines:
        if not l.cells or not l.label:
            continue
        if l.label.lower() == "total":
            total = l
        elif not re.match(r"^Page\b", l.label):
            rows.append(l)
    cols = rows[0].hdr["cols"]
    recs = []
    for l in rows:
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        recs.append((l.label, vals))
        for ci, (yr, basis, _) in enumerate(cols):
            if vals[ci] is not None:
                sink.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=yr, basis=basis, fund_group=fund_group,
                                          dept_code="", dept_name=l.label, amount=vals[ci], page=p))
    tvals = [None] * len(cols)
    for ci, v, _ in total.cells:
        tvals[ci] = None if v is DASH else v
    for ci, (yr, basis, _) in enumerate(cols):
        if tvals[ci] is not None:
            sink.total(yr, basis, f"department_expenditures/{fund_group}", "Total", tvals[ci], p)
            s = sum((r[1][ci] or 0) for r in recs)
            add_check(sink, "tier1", f"department_expenditures/{fund_group}", "Total", p, yr, basis,
                      tvals[ci], s, "sum of all department rows on page")
    return recs, cols


def parse_fixed_cells(doc, p, ncells, xmin=100):
    """Cross-check-only tables (funding by source, departmental totals by fund, FTE schedule): rows with exactly
    `ncells` numbers.  Numbers are assigned to columns by right-edge x against the columns of the page's complete
    rows; a label-less fragment line within 12pt of a labelled line (a wrapped label) is merged into it.
    Returns [(label, [values])] and the rows that did not end up with ncells cells."""
    items = []
    for l in doc.lines(p):
        nums = [(w, parse_num(w.t)) for w in l.words if isinstance(parse_num(w.t), (int, float)) and w.x0 >= xmin]
        lab = clean(" ".join(w.t for w in l.words if parse_num(w.t) is None or w.x0 < xmin))
        if re.match(r"^Page\b", lab):
            continue
        if nums or lab:
            items.append([l, lab, nums])
    full = [it for it in items if len(it[2]) == ncells]
    if not full:
        return [], [(it[1], len(it[2])) for it in items if it[2]]
    refx = [sorted(sorted(it[2], key=lambda t: t[0].x1)[c][0].x1 for it in full)[len(full) // 2] for c in range(ncells)]
    rows, bad, used = [], [], set()
    for i, (l, lab, nums) in enumerate(items):
        if i in used or not nums:
            continue
        group = [i]
        if len(nums) < ncells:
            for j in (i - 1, i + 1):
                if 0 <= j < len(items) and j not in used and items[j][2] and not items[j][1] and abs(items[j][0].y - l.y) <= 12:
                    group.append(j)
        cells = {}
        ok = True
        for j in group:
            for w, v in items[j][2]:
                c = min(range(ncells), key=lambda k: abs(refx[k] - w.x1))
                if c in cells:
                    ok = False
                cells[c] = v
        used.update(group)
        if ok and len(cells) == ncells:
            rows.append((lab, [cells[c] for c in range(ncells)]))
        else:
            bad.append((lab, sum(len(items[j][2]) for j in group)))
    return rows, bad


# --------------------------------------------------------------------------- tier 1b: revenues
JUNK_RE = re.compile(r"^(Statement of Revenues|General Fund|Other Funds|Source|Page \d+|As of.*|\d{2}/\d{2}/\d{4}.*|Actual|Adopted|"
                     r"Proposed|\d{4})( .*)?$", re.I)


def is_junk_label(lab):
    if not lab:
        return True
    if JUNK_RE.match(lab) and not lab.upper().startswith(("GENERAL FUND TOTAL",)):
        return True
    toks = lab.split()
    return all(t.lower() in BASES or YEAR_RE.match(t) or t.lower() in ("as", "of", "source") or
               re.match(r"^\d\d/\d\d/\d{4}$", t) or not re.search(r"[A-Za-z0-9]", t) or
               re.match(r"^[^\x00-\x7f]+$", t) or t in ("!Ɛ", "ŽĨ") for t in toks)


def dedupe_repeat(text):
    """'A B C D A B C' -> 'A B C D' : a wrapped row label printed after an identical-start heading."""
    t = text.split()
    for k in range(len(t) // 2, 2, -1):
        pass
    for i in range(3, len(t)):
        if t[i:] == t[:len(t) - i] and len(t) - i >= 3:
            return " ".join(t[:i])
    return text


def is_total_label(lab):
    return bool(re.search(r"\bTOTAL\b|SUB-?TOTAL", lab, re.I))


def parse_revenues(doc, sink):
    start = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Statement of Revenues")[0]
    pages, p = [], start
    while True:
        pages.append(p)
        if any(re.match(r"TOTAL ALL FUNDS", l.text, re.I) for l in doc.lines(p)):
            break
        p += 1
    fund_group = "general_fund"
    cat, block_fname = "", ""
    heading = []                      # consecutive label-only lines since last reset (Other Funds)
    stacks = {"general_fund": [], "other_funds": []}
    blocks = defaultdict(list)       # fund_group -> printed block totals
    gf_total = other_total = disc = selfgen = None
    last_total_vals = {}
    for p in pages:
        lines = assign_cells(doc.lines(p))
        pend = []                    # label-only line(s) awaiting a cells line
        for l in lines:
            lab = l.label
            if l.text == "Other Funds" or (not l.cells and lab == "Other Funds"):
                fund_group, cat, heading = "other_funds", "", []
                continue
            if not l.cells:
                if is_junk_label(lab):
                    continue
                pend.append(l)
                continue
            if is_junk_label(lab) and not pend:
                continue
            # a line with cells; label may be on this line, or on pending label-only lines above
            if not lab and pend:
                lab = clean(" ".join(x.label for x in pend))
                pend = []
                merged_from_heading = True
            else:
                merged_from_heading = False
                # pending label-only lines are headings
                for x in pend:
                    if fund_group == "general_fund":
                        cat = x.label
                    else:
                        heading.append(x.label)
                pend = []
            cols = l.hdr["cols"]
            vals = [None] * len(cols)
            for ci, v, _ in l.cells:
                vals[ci] = None if v is DASH else v
            if is_total_label(lab):
                memo = bool(re.search(r"DISCRETIONARY", lab, re.I))
                scope_fund = "General Fund" if fund_group == "general_fund" else (block_fname or dedupe_repeat(clean(" ".join(heading))) or lab)
                is_all = bool(re.match(r"TOTAL ALL FUNDS", lab, re.I))
                aggregate = any(it["total"] for it in stacks[fund_group][-stack_total(stacks[fund_group], vals)[0]:])
                scope = f"revenues/{scope_fund}" + (f"/{cat}" if fund_group == "general_fund" and cat and not aggregate else "")
                for ci, (yr, basis, _) in enumerate(cols):
                    if vals[ci] is not None:
                        sink.total(yr, basis, scope, lab, vals[ci], p)
                st = stacks[fund_group]
                if memo or is_all:
                    kind = "memo"
                    sink.revenues_memo = getattr(sink, "revenues_memo", []) + [(lab, vals, cols, p, scope)]
                    continue
                k, sums, m = stack_total(st, vals)
                for ci, (yr, basis, _) in enumerate(cols):
                    if vals[ci] is not None:
                        add_check(sink, "tier1", scope, lab, p, yr, basis, vals[ci], sums[ci],
                                  f"sum of last {k} item(s) of the statement's running stack "
                                  f"(rows and sub-totals not yet rolled into a total)",
                                  dict(items=k))
                del st[-k:]
                st.append(dict(vals=[v or 0 for v in vals], label=lab, total=True))
                blocks[fund_group].append(dict(label=lab, vals=vals, cols=cols, page=p, scope=scope))
                if re.match(r"TOTAL GENERAL FUND", lab, re.I):
                    gf_total = (vals, cols, p)
                if re.match(r"TOTAL OTHER FUNDS", lab, re.I):
                    other_total = (vals, cols, p)
                if fund_group == "other_funds":
                    heading = []
                block_fname = ""
                continue
            # data row
            fname = "General Fund" if fund_group == "general_fund" else (dedupe_repeat(clean(" ".join(heading))) or dedupe_repeat(lab))
            block_fname = fname
            lab = dedupe_repeat(lab)
            for ci, (yr, basis, _) in enumerate(cols):
                if vals[ci] is not None:
                    sink.revenues.append(dict(book_id=BOOK_ID, fiscal_year=yr, basis=basis, fund_group=fund_group,
                                              fund_name=fname, category=cat if fund_group == "general_fund" else "",
                                              line_name=lab, amount=vals[ci], page=p))
            stacks[fund_group].append(dict(vals=[v or 0 for v in vals], label=lab, total=False))
    # memo / grand-total checks
    memo = getattr(sink, "revenues_memo", [])
    for lab, vals, cols, p, scope in memo:
        for ci, (yr, basis, _) in enumerate(cols):
            if vals[ci] is None:
                continue
            if re.match(r"TOTAL ALL FUNDS", lab, re.I):
                s = (gf_total[0][ci] or 0) + (other_total[0][ci] or 0)
                add_check(sink, "tier1", scope, lab, p, yr, basis, vals[ci], s,
                          "TOTAL GENERAL FUND + TOTAL OTHER FUNDS (as printed)")
            else:   # DISCRETIONARY: general fund total + printed self-generated block total
                sg = next((b for b in blocks["other_funds"] if re.search(r"SELF-GENERATED", b["label"], re.I)), None)
                s = (gf_total[0][ci] or 0) + ((sg["vals"][ci] or 0) if sg else 0)
                add_check(sink, "tier1", scope, lab, p, yr, basis, vals[ci], s,
                          "TOTAL GENERAL FUND + printed TOTAL SELF-GENERATED block (memo total outside the Other Funds stack)")
    return dict(gf_total=gf_total, other_total=other_total, pages=pages)


# --------------------------------------------------------------------------- tier 2: department sections
def top_and_sub(lines):
    """Header of program pages: 'TOP  sub-dept  PAGE TITLE' split by wide x gaps on the first line."""
    l = lines[0]
    groups, cur = [], [l.words[0]]
    for a, b in zip(l.words, l.words[1:]):
        if b.x0 - a.x1 > 22:
            groups.append(cur)
            cur = [b]
        else:
            cur.append(b)
    groups.append(cur)
    txt = [clean(" ".join(w.t for w in g)) for g in groups]
    return txt


HEADER_WORDS = {"change", "fy18-19", "fy19-20", "fy20-21", "variance", "actual", "adopted", "proposed", "dollar", "percent"}


def is_header_label(lab):
    """A label-only line that is column-header furniture (Change / FY18-19 / Actual ...), or a bare percentage."""
    toks = [t.lower() for t in lab.split()]
    if not toks:
        return True
    if any(t in HEADER_WORDS for t in toks) or YEAR_RE.match(toks[0]):
        return True
    if all(t.endswith("%") or t in ("-", "type", "source", "expenditure", "funding", "fund") and False for t in toks):
        return True
    if lab.lower() in ("expenditure type", "funding source"):
        return True
    return all(t.endswith("%") for t in toks)


def simple_table(doc, p, wrap=True, pick="last", stop_re=None):
    """Rows (label, vals[cols], hdr_cols, line) below the chosen year header on the page.  A row's label may
    wrap onto label-only lines above and/or below the line carrying the numbers: each label-only line is
    attached to the vertically nearest numeric line (ties go to the following row)."""
    lines = assign_cells(doc.lines(p))
    groups = header_groups(lines)
    if not groups:
        return [], []
    g0 = max(groups, key=lambda g: g["y"]) if pick == "last" else min(groups, key=lambda g: g["y"])
    ytab, cols = g0["y"], g0["cols"]
    body = [l for l in lines if l.y > ytab + 1 and not re.match(r"^Page \d+$", l.text)]
    cell_lines = [l for l in body if l.cells]
    frags = defaultdict(list)
    for l in body:
        own = clean(" ".join(w.t for w in l.label_words if parse_num(w.t) is None and not w.t.endswith("%") and w.t != "-"))
        if l.cells:
            if own:
                frags[id(l)].append((l.y, own))
        elif l.label and not is_header_label(l.label) and cell_lines:
            lab_only = clean(" ".join(t for t in l.label.split() if not t.endswith("%")))
            if not lab_only or lab_only.lower() in ("type", "source"):
                continue
            prev = [c for c in cell_lines if c.y < l.y]
            nxt = [c for c in cell_lines if c.y > l.y]
            cand = []
            if prev:
                cand.append((l.y - prev[-1].y, 0, prev[-1]))
            if nxt:
                cand.append((nxt[0].y - l.y, -1, nxt[0]))      # tie -> next row
            tgt = min(cand, key=lambda t: (t[0], t[1]))[2]
            frags[id(tgt)].append((l.y, lab_only))
    rows = []
    for l in cell_lines:
        lab = clean(" ".join(t for _, t in sorted(frags[id(l)]))) if wrap else clean(" ".join(t for _, t in frags[id(l)][-1:]))
        vals = [None] * len(l.hdr["cols"])
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        rows.append((lab, vals, l.hdr["cols"], l))
        if stop_re and re.match(stop_re, lab):
            break
    return rows, cols


def emit_detail(sink, dept_name, table, dim1, dim2, vals, cols, page):
    for ci, v in enumerate(vals):
        if v is None:
            continue
        yr, basis, _ = cols[ci]
        sink.detail.append(dict(book_id=BOOK_ID, fiscal_year=yr, basis=basis, dept_code="", dept_name=dept_name,
                                table=table, dim1=dim1, dim2=dim2, amount=v, page=page))


def parse_by_type_or_fund(doc, p, sink, kind, groupname):
    tl = next(l.text for l in doc.lines(p) if re.match(r"^(Budget Summary|Expenditures by Funding Source)\s*-", l.text))
    name = re.sub(r"^(Budget Summary|Expenditures by Funding Source)\s*-\s*", "", tl)
    rows, cols = simple_table(doc, p)
    out = []
    table = "by_type" if kind == "by_type" else "by_fund"
    for lab, vals, c, l in rows:
        if re.match(r"^Total\b", lab):
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], f"department_detail/{name}/{table}", lab, v, p)
            out.append(("TOTAL", lab, vals, cols))
        else:
            emit_detail(sink, name, table, lab, "", vals, cols, p)
            out.append(("ROW", lab, vals, cols))
    # row sum check
    tot = next((o for o in out if o[0] == "TOTAL"), None)
    if tot is None:
        sink.unparsed.append(dict(page=p, what=f"{table} total row not found", dept=name))
    else:
        for ci, v in enumerate(tot[2]):
            if v is not None:
                s = sum((o[2][ci] or 0) for o in out if o[0] == "ROW")
                add_check(sink, "tier2", f"department_detail/{name}/{table}", tot[1], p, cols[ci][0], cols[ci][1], v, s,
                          "sum of rows on page")
    return name, out


# --- program tables ---------------------------------------------------------------------------
FUND_HDR_RE = re.compile(r"^([A-Z0-9]{2,4})\s+\S")
PROG_RE = re.compile(r"^(\d{3,5})\s+(.*)$")


def parse_program_exp(doc, p, sink, dept_name, state):
    """state carries current fund and per-fund/program stacks across continuation pages."""
    lines = assign_cells(doc.lines(p))
    groups = header_groups(lines)
    if not groups:
        sink.unparsed.append(dict(page=p, what="program expenditure page without year header", dept=dept_name))
        return
    ytab = max(g["y"] for g in groups)
    cols = max(groups, key=lambda g: g["y"])["cols"]
    for l in lines:
        if l.y <= ytab + 1 or re.match(r"^Page \d+$", l.text):
            continue
        lab = l.label
        if not l.cells:
            if not lab:
                continue
            if FUND_HDR_RE.match(lab):
                state["fund"] = lab
                state["fund_rows"] = []
            else:
                sink.unparsed.append(dict(page=p, what=f"label-only line in program table ignored: {lab!r}", dept=dept_name))
            continue
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        if re.search(r"\bTOT(?:A|AL)?$", lab):
            is_dept = lab.upper().startswith("DEPARTMENT")
            scope = f"department_detail/{dept_name}/by_program" + ("" if is_dept else f"/{state.get('fund')}")
            for ci, v in enumerate(vals):
                if v is None:
                    continue
                yr, basis, _ = cols[ci]
                sink.total(yr, basis, scope, lab, v, p)
                if is_dept:
                    s = sum(t[ci] for t in state["fund_totals"] if t[ci] is not None) if state.get("fund_totals") else 0
                    add_check(sink, "tier3", scope, lab, p, yr, basis, v, s, "sum of the fund TOTAL rows in this program table")
                else:
                    s = sum((r[ci] or 0) for r in state["fund_rows"])
                    add_check(sink, "tier3", scope, lab, p, yr, basis, v, s, "sum of program rows under this fund")
                    state.setdefault("fund_total_vals", {}).setdefault(state.get("fund"), {})[ci] = (v, cols[ci])
            if not is_dept:
                # the book prints no by-funding-source page per department: the fund TOTAL rows of this table are used
                emit_detail(sink, dept_name, "by_fund", state.get("fund", ""), "", vals, cols, p)
            if is_dept:
                state["dept_total"] = (vals, cols, p)
            else:
                state.setdefault("fund_totals", []).append(vals)
            continue
        if not PROG_RE.match(lab):
            sink.unparsed.append(dict(page=p, what=f"row without program number skipped: {lab!r}", dept=dept_name))
            continue
        emit_detail(sink, dept_name, "by_program", lab, state.get("fund", ""), vals, cols, p)
        state.setdefault("fund_rows", []).append(vals)


def parse_program_detail(doc, p, sink, dept_name, state):
    """PROGRAM DETAIL: columns Personal Services / Other Operating / Debt Service / Total (2019 adopted)."""
    lines = doc.lines(p)
    hdr = [w for l in lines for w in l.words if w.t in ("Personal", "Other", "Debt", "Total") and l.y < 140]
    # column centres from the header words
    centers = {}
    for w in hdr:
        centers.setdefault(w.t, w.cx)
    if len(centers) < 4:
        sink.unparsed.append(dict(page=p, what="program detail header columns not found", dept=dept_name))
        return
    names = [("Personal Services", centers["Personal"]), ("Other Operating", centers["Other"]),
             ("Debt Service", centers["Debt"]), ("Total", centers["Total"])]
    ytab = max(w.cy for w in hdr)
    for l in lines:
        if l.y <= ytab + 14 or re.match(r"^Page \d+$", l.text):
            continue
        lab_words, cells = [], {}
        for w in l.words:
            v = parse_num(w.t)
            if v is not None and w.cx > names[0][1] - 40:
                ci = min(range(4), key=lambda i: abs(names[i][1] - w.cx))
                cells[ci] = v
            else:
                lab_words.append(w)
        lab = clean(" ".join(w.t for w in lab_words))
        if not cells:
            if lab and FUND_HDR_RE.match(lab):
                state["pd_fund"] = lab
                state["pd_rows"] = []
            continue
        vals = [None if cells.get(i) is DASH else cells.get(i) for i in range(4)]
        if re.search(r"\bTOT(?:A|AL)?$", lab):
            is_dept = lab.upper().startswith("DEPARTMENT")
            scope = f"department_detail/{dept_name}/by_program_type" + ("" if is_dept else f"/{state.get('pd_fund')}")
            for i in range(4):
                if vals[i] is not None:
                    sink.total(2020, "proposed", scope + f"/{names[i][0]}", lab, vals[i], p)
                    if is_dept:
                        s = sum(t[i] or 0 for t in state.get("pd_fund_totals", []))
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, 2020, "proposed", vals[i], s,
                                  "sum of the fund TOTAL rows in this program-detail table")
                    else:
                        s = sum((r[i] or 0) for r in state["pd_rows"])
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, 2020, "proposed", vals[i], s,
                                  "sum of program rows under this fund")
            if is_dept:
                state["pd_dept_total"] = (vals[3], p)
            else:
                state.setdefault("pd_fund_totals", []).append(vals)
            # row-internal check: Personal + Other + Debt = Total
            continue
        if not PROG_RE.match(lab):
            continue
        for i in range(3):
            if vals[i] is not None:
                sink.detail.append(dict(book_id=BOOK_ID, fiscal_year=2020, basis="proposed", dept_code="", dept_name=dept_name,
                                        table="by_program_type", dim1=lab, dim2=f"{state.get('pd_fund','')} | {names[i][0]}",
                                        amount=vals[i], page=p))
        if vals[3] is not None:
            add_check(sink, "tier3", f"department_detail/{dept_name}/by_program_type/row", lab, p, 2020, "proposed",
                      vals[3], sum(v or 0 for v in vals[:3]), "Personal Services + Other Operating + Debt Service on the row")
        state.setdefault("pd_rows", []).append(vals)


def parse_personnel(doc, p, sink, dept_name, state):
    lines = assign_cells(doc.lines(p))
    groups = header_groups(lines)
    if not groups:
        sink.unparsed.append(dict(page=p, what="personnel page without year header", dept=dept_name))
        return
    ytab = max(g["y"] for g in groups)
    cols = max(groups, key=lambda g: g["y"])["cols"]
    pay_x = None
    for l in doc.lines(p)[:8]:
        for w in l.words:
            if w.t in ("Pay", "Grade"):
                pay_x = w.x0 if pay_x is None else min(pay_x, w.x0)
    pay_x = (pay_x - 18) if pay_x is not None else None
    for l in lines:
        if l.y <= ytab + 1 or re.match(r"^Page \d+$", l.text):
            continue
        lab = l.label
        if not l.cells:
            if not lab:
                continue
            m = FUND_HDR_RE.match(lab)
            if PROG_RE.match(lab) and len(lab.split()[0]) >= 4:
                state["program"] = lab
            elif m:
                state["fund"] = lab
            elif PROG_RE.match(lab):
                state["program"] = lab
            else:
                state["pending_title"] = (state.get("pending_title", "") + " " + lab).strip()
            continue
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        if re.search(r"\bTOT(?:A|AL)?$", lab):
            hv = [None if v is None else round(v * 100) for v in vals]       # hundredths of an FTE, exact integers
            stk = state.setdefault("stack", [])
            k, sums, m = stack_total(stk, hv)
            for ci, v in enumerate(hv):
                if v is not None:
                    add_check(sink, "tier4", f"positions/{dept_name}/{lab}", lab, p, cols[ci][0], cols[ci][1], v, sums[ci],
                              f"FTE x100: sum of last {k} item(s) of the running stack (position rows, program totals, fund totals)")
            del stk[-k:]
            stk.append(dict(vals=[v or 0 for v in hv], total=True))
            if lab.upper().startswith("DEPARTMENT"):
                state["dept_total"] = (vals, cols, p)
            state["pending_title"] = ""
            continue
        # position row
        title_words = [w for w in l.label_words if pay_x is None or w.x0 < pay_x]
        grade_words = [w for w in l.label_words if pay_x is not None and w.x0 >= pay_x]
        title = clean(" ".join(w.t for w in title_words))
        if state.get("pending_title"):
            title = clean(state["pending_title"] + " " + title)
            state["pending_title"] = ""
        grade = clean(" ".join(w.t for w in grade_words))
        for ci, v in enumerate(vals):
            if v is None:
                continue
            yr, basis, _ = cols[ci]
            sink.positions.append(dict(book_id=BOOK_ID, fiscal_year=yr, basis=basis, dept_code="", dept_name=dept_name,
                                       program=state.get("program", ""), fund=state.get("fund", ""), position_title=title,
                                       pay_grade=grade, fte=v, page=p))
        state.setdefault("stack", []).append(dict(vals=[0 if v is None else round(v * 100) for v in vals], total=False))


def main():
    doc = Doc(PDF)
    sink = Sink()
    run_all(doc, sink)
    write_outputs(sink, doc)


NOT_ATTEMPTED_TITLES = [
    ("Revenue Projections by Source", "multi-year General Fund revenue projections 2018-2024 (projected columns; not in the schema)"),
    ("Fund Balance Projections", "fund balance projections 2018-2023 (planning table, not part of the budget statements)"),
    ("Summary of Revenues and Expenditures", "narrative / summary chart page (percent-of-total tables)"),
    ("2019 Millage Rates & Special Tax Districts", "millage table (rates, not dollars)"),
    ("Special Fees", "fee schedule"),
    ("Statement of Debt", "debt statement, credit ratings, pension/OPEB notes (pp.101-120)"),
    ("2020 CAPITAL BUDGET SUMMARY", "capital budget summary (separate book)"),
]


def note_not_attempted(doc, sink):
    for title, why in NOT_ATTEMPTED_TITLES:
        pgs = [p for p in range(1, doc.n + 1) if any(l.text.startswith(title) for l in doc.lines(p)[:6])]
        sink.not_attempted.append(dict(title=title, pages=pgs, reason=why))
    narrative = [p for p in sorted(sink.section_types_all_pages) if p not in sink.section_types and p not in sink.undecodable]
    sink.not_attempted.append(dict(title="department narrative pages (overview, org chart, accomplishments, priorities, section dividers)",
                                   pages=narrative, reason="free text / org charts; no budget tables"))
    if sink.undecodable:
        ent = next((p for p in range(first_enterprise_search(doc), doc.n + 1) if doc.first_text(p, 2).startswith(("Enterprise Funds", "(QWHUSULVH"))), None)
        land = [p for p in sorted(sink.undecodable) if doc.d[p - 1].rect.width > doc.d[p - 1].rect.height]
        dept_sum = [p for p in land if ent is None or p < ent]
        ent_pages = [p for p in land if ent is not None and p >= ent]
        other = [p for p in sorted(sink.undecodable) if p not in land]
        sink.unparsed.append(dict(pages=dept_sum,
                                  what="per-department 'DEPARTMENTAL BUDGET SUMMARY' pages (landscape): numbers and labels are written in subset fonts with a "
                                       "page-specific glyph numbering that cannot be decoded. Not parsed. Their by-type figures are carried by each "
                                       "department's 'Budget Summary' page and the by-fund figures by the fund TOTAL rows of the EXPENDITURE SUMMARY "
                                       "tables (stored as by_fund); the variance column is not captured."))
        sink.unparsed.append(dict(pages=ent_pages,
                                  what="Enterprise Funds budget summaries and personnel pages (landscape, same undecodable fonts): not parsed; enterprise "
                                       "funds are not part of the citywide department statements."))
        sink.unparsed.append(dict(pages=other, what="organizational-chart pages with undecodable text (graphics, no budget tables)"))


def first_enterprise_search(doc):
    return 700


def run_all(doc, sink):
    # ---- tier 1a: citywide department statements -------------------------------------------
    pg = {}
    pg["gf"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "General Fund Expenditures", 60, 110)
    pg["of"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Other Fund Expenditures", 60, 110)
    pg["all"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Total Expenditures" and "Department" in d.first_text(p, 3), 60, 110)
    pg["by_fund_dept"] = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Departmental Expenditures by Fund"), 60, 110)
    pg["fund_src"] = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Summary of Departmental Funding by Source"), 60, 110)
    for k, v in pg.items():
        if not v:
            sink.unparsed.append(dict(page=None, what=f"locator found no page for {k}"))
    stm = {}
    for key, fg in (("gf", "general_fund"), ("of", "other_funds"), ("all", "all_funds")):
        if pg[key]:
            stm[fg] = parse_dept_statement(doc, pg[key][0], sink, fg)
    sink.pages = pg
    if len(stm) == 3:
        gf, of, al = stm["general_fund"], stm["other_funds"], stm["all_funds"]
        if not (len(gf[0]) == len(of[0]) == len(al[0])):
            sink.unparsed.append(dict(page=None, what="department row counts differ across the three statements"))
        else:
            for (lg, vg), (lo, vo), (la, va) in zip(gf[0], of[0], al[0]):
                for ci, (yr, basis, _) in enumerate(gf[1]):
                    if va[ci] is None and vg[ci] is None and vo[ci] is None:
                        continue
                    add_cross(sink, "tier1", "general_fund + other_funds = all_funds", f"{la} {yr} {basis}",
                              "general+other", (vg[ci] or 0) + (vo[ci] or 0), "all_funds", va[ci] or 0, pg["all"][0])
    # p.95 'Total Departmental Expenditures by Fund' : GF / non-GF / total for 2019 adopted and 2020 proposed.
    if pg["by_fund_dept"] and len(stm) == 3:
        p = pg["by_fund_dept"][0]
        rows, bad = parse_fixed_cells(doc, p, 6)
        for b in bad:
            if b[1] >= 3:
                sink.unparsed.append(dict(page=p, what=f"p.{p} cross-check row did not have 6 numeric cells: {b}"))
        body = [r for r in rows if not r[0].upper().startswith("TOTAL")]
        trow = [r for r in rows if r[0].upper().startswith("TOTAL")]
        gf, of, al = stm["general_fund"], stm["other_funds"], stm["all_funds"]
        cols = gf[1]
        c19 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2019, "adopted"))
        c20 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2020, "proposed"))
        sf = {"general_fund": gf, "other_funds": of, "all_funds": al}
        pairs = align_rows(len(gf[0]), body,
                           lambda i, j: (al[0][i][1][c19] or 0) == body[j][1][2] and (al[0][i][1][c20] or 0) == body[j][1][5],
                           lambda i: not (al[0][i][1][c19] or al[0][i][1][c20]), sink, p, [r[0] for r in gf[0]])
        for i, j in pairs:
            for (yr, cc, off) in ((2019, c19, 0), (2020, c20, 3)):
                for nm, j2 in (("general_fund", 0), ("other_funds", 1), ("all_funds", 2)):
                    add_cross(sink, "tier1", f"p.{p} 'Total Departmental Expenditures by Fund' vs statement", f"{gf[0][i][0]} {yr} {nm}",
                              "p95", body[j][1][off + j2], f"statement p{pg[{'general_fund':'gf','other_funds':'of','all_funds':'all'}[nm]][0]}",
                              sf[nm][0][i][1][cc] or 0, p)
        for lab, v in trow:
            for (yr, cc, off) in ((2019, c19, 0), (2020, c20, 3)):
                for nm, j2 in (("general_fund", 0), ("other_funds", 1), ("all_funds", 2)):
                    t = next((t for t in sink.totals if t["scope"] == f"department_expenditures/{nm}" and (t["fiscal_year"], t["basis"]) == (yr, cols[cc][1])), None)
                    if t:
                        add_cross(sink, "tier1", f"p.{p} printed Total Expenditures vs statement printed Total", f"{yr} {nm}", "p95", v[off + j2], "statement", t["amount"], p)
                        sink.total(yr, cols[cc][1], f"departmental_expenditures_by_fund_p95/{nm}", lab, v[off + j2], p)
    # pp.93-94 Summary of departmental funding by source: 14 fund columns, 2020 proposed
    funds_src = None
    if pg["fund_src"] and len(stm) == 3:
        allrows = []
        for p in range(pg["fund_src"][0], pg["fund_src"][0] + 2):
            rows, bad = parse_fixed_cells(doc, p, 14)
            for b in bad:
                if b[1] >= 3:
                    sink.unparsed.append(dict(page=p, what=f"funding-by-source row did not have 14 numeric cells: {b}"))
            allrows += [(p, r) for r in rows]
        body = [(p, r) for p, r in allrows if not r[0].upper().startswith("TOTAL")]
        trow = [(p, r) for p, r in allrows if r[0].upper().startswith("TOTAL")]
        gf, of, al = stm["general_fund"], stm["other_funds"], stm["all_funds"]
        cols = gf[1]
        c20 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2020, "proposed"))
        pairs = align_rows(len(gf[0]), body,
                           lambda i, j: (al[0][i][1][c20] or 0) == sum(body[j][1][1]) and (gf[0][i][1][c20] or 0) == body[j][1][1][0],
                           lambda i: not (al[0][i][1][c20]), sink, pg["fund_src"][0], [r[0] for r in gf[0]])
        for i, j in pairs:
            pp, (lab, v) = body[j]
            add_cross(sink, "tier1", "pp.93-94 funding by source: GF column = general-fund statement", f"{gf[0][i][0]} 2020 proposed",
                      "p93 GF col", v[0], "statement", gf[0][i][1][c20] or 0, pp)
            add_cross(sink, "tier1", "pp.93-94 funding by source: sum of 14 fund columns = all-funds statement", f"{gf[0][i][0]} 2020 proposed",
                      "p93 sum of 14 cols", sum(v), "statement", al[0][i][1][c20] or 0, pp)
            add_cross(sink, "tier1", "pp.93-94 funding by source: sum of 13 non-GF columns = other-funds statement", f"{gf[0][i][0]} 2020 proposed",
                      "p93 sum of cols 2-14", sum(v[1:]), "statement", of[0][i][1][c20] or 0, pp)
        for pp, (lab, v) in trow:
            for k in range(14):
                s_ = sum(r[1][k] for _, r in body)
                add_check(sink, "tier1", "funding_by_source/TOTAL EXPENDITURES by fund column", f"column {k + 1}", pp, 2020, "proposed", v[k], s_,
                          "sum of the department rows of pp.93-94 in this fund column")
                sink.total(2020, "proposed", f"funding_by_source/fund_column_{k + 1}", lab, v[k], pp)
        funds_src = (body, trow)
    # ---- tier 1b: revenues ----------------------------------------------------------------
    rev = parse_revenues(doc, sink)
    # revenue vs expenditure (the book's own balance): informational cross-checks
    if len(stm) == 3:
        gft = rev["gf_total"]
        for kind, tot_t, rev_vals in (("general fund", "department_expenditures/general_fund", rev["gf_total"]),):
            for ci, (yr, basis, _) in enumerate(rev_vals[1]):
                t = next((t for t in sink.totals if t["scope"] == tot_t and (t["fiscal_year"], t["basis"]) == (yr, basis)), None)
                if t and rev_vals[0][ci] is not None:
                    add_cross(sink, "tier1", "BOOK BALANCE: statement of revenues TOTAL GENERAL FUND = General Fund Expenditures total", f"{yr} {basis}",
                              "revenue statement", rev_vals[0][ci], "expenditure statement", t["amount"], rev_vals[2])
        for ci, (yr, basis, _) in enumerate(rev["gf_total"][1]):
            t = next((t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == (yr, basis)), None)
            if t:
                ra = (rev["gf_total"][0][ci] or 0) + (rev["other_total"][0][ci] or 0)
                add_cross(sink, "tier1", "BOOK BALANCE: statement of revenues TOTAL ALL FUNDS = Total Expenditures (all funds)", f"{yr} {basis}",
                          "revenue statement", ra, "expenditure statement", t["amount"], rev["other_total"][2])
    parse_fund_summaries(doc, sink, stm, rev)
    # ---- tier 2/3/4: department sections ---------------------------------------------------------
    type_by_fund_pages(doc, sink)
    sections(doc, sink, stm, funds_src)
    fte_schedule_check(doc, sink, stm)
    first = sink.first_section_page
    sink.undecodable = [p for p in range(first, doc.n + 1)
                        if sum(c for k, c in doc.decode_stats.setdefault(p, {}).items() if k != "_decoded") >= 25 and p not in sink.section_types]
    sink.section_types_all_pages = list(range(first, doc.n + 1))
    note_not_attempted(doc, sink)


def parse_fund_summaries(doc, sink, stm, rev):
    """p.87 'Total Expenditures by Fund Source', p.66 'Total Revenues by Fund', p.73 revenue detail."""
    p80 = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Expenditures by Fund Source"), 70, 110)
    sink.fund_summary = {}
    if p80:
        p = p80[0]
        rows, cols = simple_table(doc, p, wrap=True, pick="first", stop_re=r"^Total Expenditures")
        tot, body = None, []
        for lab, vals, c, l in rows:
            if re.match(r"^Total Expenditures", lab):
                tot = vals
            else:
                body.append((lab, vals))
        sink.fund_summary["p80"] = (body, tot, cols, p)
        for lab, vals in body:
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], "fund_totals/expenditures_by_fund", lab, v, p)
        if tot:
            for ci, v in enumerate(tot):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], "fund_totals/expenditures_by_fund", "Total Expenditures", v, p)
                    add_check(sink, "tier1", "fund_totals/expenditures_by_fund", "Total Expenditures", p, cols[ci][0], cols[ci][1], v,
                              sum((b[1][ci] or 0) for b in body), "sum of fund rows on page")
            if "all_funds" in stm:
                al = stm["all_funds"]
                tl = al[0]
                # compare with p105 total
        # compare with statements totals
    # p60 / p66
    for key, ttl, scope in (("p60", "Overview of Revenues and Expenditures Total Revenues by Fund", "revenue_summary/by_fund_p60"),
                            ("p66", "Detailed Description of Revenues from All Funds Total Revenues by Fund Source", "revenue_summary/by_fund_p66")):
        pp = find_pages(doc, lambda d, p: d.first_text(p, 2).startswith(ttl), 55, 90)
        if not pp:
            sink.unparsed.append(dict(page=None, what=f"locator found no page for {key}"))
            continue
        p = pp[0]
        rows, cols = simple_table(doc, p, wrap=True, pick="first", stop_re=r"^Total (Funding|Revenues)\b")
        body, tot = [], None
        for lab, vals, c, l in rows:
            if re.match(r"^Total (Funding|Revenues)\b", lab):
                tot = (lab, vals)
            elif lab:
                body.append((lab, vals))
        sink.fund_summary[key] = (body, tot, cols, p)
        for lab, vals in body + ([tot] if tot else []):
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], scope, lab, v, p)
        if tot:
            for ci, v in enumerate(tot[1]):
                if v is not None:
                    add_check(sink, "tier1", scope, tot[0], p, cols[ci][0], cols[ci][1], v,
                              sum((b[1][ci] or 0) for b in body), "sum of fund rows on page")
    # compare p60/p66 against the revenue statement grand totals
    gft, ot = rev["gf_total"], rev["other_total"]
    for key in ("p60", "p66"):
        if key not in sink.fund_summary:
            continue
        body, tot, cols, p = sink.fund_summary[key]
        for ci, (yr, basis, _) in enumerate(cols):
            gv = body[0][1][ci] if body else None   # first row = General Fund
            for j, (c) in enumerate(gft[1]):
                if (c[0], c[1]) == (yr, basis) and gv is not None:
                    add_cross(sink, "tier1", f"{key} General Fund revenue = statement TOTAL GENERAL FUND", f"{yr} {basis}",
                              key, gv, "statement", gft[0][j] or 0, p)
            if tot and tot[1][ci] is not None:
                for j, c in enumerate(gft[1]):
                    if (c[0], c[1]) == (yr, basis):
                        add_cross(sink, "tier1", f"{key} total revenues = statement GF total + OTHER FUNDS total", f"{yr} {basis}",
                                  key, tot[1][ci], "statement", (gft[0][j] or 0) + (ot[0][j] or 0), p)


def type_by_fund_pages(doc, sink):
    """pp.88-99 'Total Expenditures by Type - <fund>': one small table per fund.  Stored as printed totals
    (they are fund-level aggregates) and used to cross-check the citywide by-type lines on p.134."""
    pages = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Total Expenditures by Type", 70, 110)
    agg = defaultdict(lambda: defaultdict(int))
    for p in pages:
        tl = next((l.text for l in doc.lines(p) if re.match(r"^Expenditures by Type\s*-", l.text)), None)
        if tl is None:
            sink.unparsed.append(dict(page=p, what="Total Expenditures by Type page without a fund title"))
            continue
        fund = re.sub(r"^Expenditures by Type\s*-\s*", "", tl)
        rows, cols = simple_table(doc, p, wrap=True, pick="last")
        body = [(lab, vals) for lab, vals, _, _ in rows if not lab.lower().startswith("total")]
        tot = next((vals for lab, vals, _, _ in rows if lab.lower().startswith("total")), None)
        scope = f"expenditures_by_type/{fund}"
        for lab, vals in body + ([("Total Expenditures", tot)] if tot else []):
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], scope, lab, v, p)
        if tot:
            for ci, v in enumerate(tot):
                if v is not None:
                    add_check(sink, "tier1", scope, "Total Expenditures", p, cols[ci][0], cols[ci][1], v,
                              sum((b[1][ci] or 0) for b in body), "sum of type rows on page")
        for lab, vals in body:
            for ci, v in enumerate(vals):
                agg[re.sub(r"[^A-Z]", "", lab.upper())][(cols[ci][0], cols[ci][1])] += v or 0
        if tot:
            for ci, v in enumerate(tot):
                if v is not None:
                    agg["__TOTAL__"][(cols[ci][0], cols[ci][1])] += v
    sink.pages["expenditures_by_type"] = pages
    sink.type_agg = agg


def fte_schedule_check(doc, sink, stm):
    """p.99 'Detailed City-Wide Personnel Schedule by Department' (FTE: General / Non-General / Total for 2018
    adopted and 2019 adopted) vs the FTEs summed from the position pages.  Department names differ between the
    two tables, so each department group is matched to a p.99 row by its six FTE figures; a group (or a
    non-zero p.99 row) with no counterpart is reported as a mismatch."""
    pp = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Detailed City-Wide Personnel Schedule"), 60, 120)
    if not pp:
        sink.unparsed.append(dict(page=None, what="citywide personnel schedule (p.99) not located"))
        return
    p = pp[0]
    sink.pages["fte_schedule"] = pp
    rows, bad = parse_fixed_cells(doc, p, 6)
    body = [r for r in rows if not r[0].upper().startswith("TOTAL")]
    used = set()
    fte, ftegf = defaultdict(float), defaultdict(float)
    for r in sink.positions:
        k = (r["dept_name"], r["fiscal_year"], r["basis"])
        fte[k] += r["fte"]
        if r["fund"].startswith("001"):
            ftegf[k] += r["fte"]
    for fam in sink.families:
        names = set(fam["names"])
        label = fam["label"]
        vec, gfv = [], []
        for (yr, basis) in ((2019, "adopted"), (2020, "proposed")):
            t = sum(fte[(n, yr, basis)] for n in names)
            gf = sum(ftegf[(n, yr, basis)] for n in names)
            vec.append(round(t * 100))
            gfv.append(round(gf * 100))
        j = next((j for j, (lab, v) in enumerate(body) if j not in used
                  and [round(v[2] * 100), round(v[5] * 100)] == vec), None)
        if j is None:
            # nothing matches on both years: report the closest row by name-independent first-year match, as a mismatch record
            sink.cross.append(dict(tier="tier4", check="FTE total from position rows (x100; 2019 adopted, 2020 proposed) matches a p.99 row's Total columns",
                                   key=label, a="positions.csv", a_value=vec, b="p99", b_value=None, diff=None,
                                   status="no_matching_row", page=p))
        else:
            used.add(j)
            for k, (yr, off) in enumerate(((2019, 0), (2020, 3))):
                add_cross(sink, "tier4", "FTE total from position rows (x100) = p.99 Total column", f"{label} -> p99 '{body[j][0]}' {yr}",
                          "positions.csv", vec[k], "p99", round(body[j][1][off + 2] * 100), p)
                add_cross(sink, "tier4", "FTE in fund 001 from position rows (x100) = p.99 General Fund column", f"{label} -> p99 '{body[j][0]}' {yr}",
                          "positions.csv", gfv[k], "p99", round(body[j][1][off] * 100), p)
    for j, (lab, v) in enumerate(body):
        if j not in used and any(v):
            sink.cross.append(dict(tier="tier4", check="p.99 row with non-zero FTE has no department group in the position pages",
                                   key=lab, a="positions.csv", a_value=None, b="p99", b_value=v, diff=None,
                                   status="no_matching_group", page=p))


def classify_pages(doc, first):
    types = {}
    for p in range(first, doc.n + 1):
        ls = doc.lines(p)
        if not ls:
            continue
        ft1 = doc.first_text(p, 1)
        if re.match(r"^Budget Summary\s*-\s*", ft1):
            types[p] = "by_type"
        elif ft1.startswith("Detailed Personnel by Department"):
            types[p] = "personnel_total"
        else:
            for key, tp in (("EXPENDITURE SUMMARY", "program_exp"), ("PROGRAM DETAIL", "program_detail"),
                            ("PERSONNEL SUMMARY", "program_pers")):
                if ls[0].text.endswith(key):
                    types[p] = tp
                    break
    return types


def sections(doc, sink, stm, funds_src):
    first = next(p for p in range(100, 300) if re.match(r"^Budget Summary\s*-\s*", doc.first_text(p, 1)))
    types = classify_pages(doc, first)
    sink.section_types = types
    sink.first_section_page = first
    groups, cur_sec, last_group, prev_kind = [], None, None, None
    for p in sorted(types):
        t = types[p]
        if t == "by_type":
            name = re.sub(r"^Budget Summary\s*-\s*", "", doc.first_text(p, 1))
            cur_sec = dict(name=name, by_type=p, by_fund=None, orphan_funds=[], pers_total=None)
            if last_group is None or prev_kind in ("program_exp", "program_detail", "program_pers"):
                last_group = dict(sections=[], program_pages=[])
                groups.append(last_group)
            last_group["sections"].append(cur_sec)
        elif t == "personnel_total" and cur_sec:
            cur_sec["pers_total"] = p
        elif t in ("program_exp", "program_detail", "program_pers") and last_group is not None:
            last_group["program_pages"].append(p)
        prev_kind = t
    sink.groups = groups
    for g in groups:
        g["top"] = g["sections"][0]["name"]
        g["enterprise"] = False
        for s_ in g["sections"]:
            nm, out = parse_by_type_or_fund(doc, s_["by_type"], sink, "by_type", s_["name"])
            s_["by_type_rows"] = out
            s_["orphan_rows"] = []
            if s_["pers_total"]:
                parse_pers_total(doc, s_, sink)
        group_checks(doc, g, sink, stm)
        program_pages(doc, g, sink)
    build_families(doc, sink, groups, stm)
    citywide_checks(doc, sink, groups, stm, funds_src)


def parse_pers_total(doc, s, sink):
    p = s["pers_total"]
    rows, cols = simple_table(doc, p, wrap=True)
    s["pers_total_rows"] = {}
    for lab, vals, c, l in rows:
        s["pers_total_rows"][lab] = (vals, cols)


def sec_total(s, kind="by_type_rows"):
    return next((o for o in s.get(kind, []) if o[0] == "TOTAL"), None)


def group_checks(doc, g, sink, stm):
    """Per section: personnel page 'Dollar amount' = by_type Personal Services."""
    for s_ in g["sections"]:
        pr = s_.get("pers_total_rows")
        if pr:
            dollar = next((v for k, v in pr.items() if k.lower().startswith("dollar amount")), None)
            ps = next((o for o in s_["by_type_rows"] if o[0] == "ROW" and o[1].lower().startswith("personal")), None)
            if dollar and ps:
                dv, dc = dollar
                for ci, (yr, basis, _) in enumerate(dc):
                    pci = next((i for i, c in enumerate(ps[3]) if (c[0], c[1]) == (yr, basis)), None)
                    if pci is not None and dv[ci] is not None:
                        add_cross(sink, "tier4", "personnel page 'Dollar amount' = by_type Personal Services", f"{s_['name']} {yr} {basis}",
                                  "personnel page", dv[ci], "by_type", ps[2][pci] or 0, s_["pers_total"])


def build_families(doc, sink, groups, stm):
    """A 'family' = the contiguous run of department sections whose by_type totals add up to one row of the citywide
    all-funds statement (e.g. the five Mayor sub-departments = 'Mayor').  Matching is by value on at least two of the
    three year columns; every year column is then checked and any disagreement is reported as a mismatch."""
    sink.families = []
    if "all_funds" not in stm:
        return
    al_rows, al_cols = stm["all_funds"]
    gf_rows, gf_cols = stm["general_fund"]
    ycol = {(c[0], c[1]): i for i, c in enumerate(al_cols)}
    units = [(g, s_) for g in groups for s_ in g["sections"]]
    gsum = []
    for g, s_ in units:
        acc = {}
        t = sec_total(s_)
        if t:
            for ci, c in enumerate(t[3]):
                acc[(c[0], c[1])] = acc.get((c[0], c[1]), 0) + (t[2][ci] or 0)
        gsum.append(acc)
    used = set()
    for ri, (lab, vals) in enumerate(al_rows):
        if not any(vals):
            continue
        best = None
        for i in range(len(units)):
            if i in used:
                continue
            acc = defaultdict(int)
            for k in range(i, min(i + 8, len(units))):
                if k in used:
                    break
                for yb, v in gsum[k].items():
                    acc[yb] += v
                m = sum(1 for yb, v in acc.items() if yb in ycol and (vals[ycol[yb]] or 0) == v and v != 0)
                if m >= 2 and (best is None or (m, -(k - i)) > (best[0], -(best[2] - best[1]))):
                    best = (m, i, k, dict(acc))
        if best is None:
            sink.cross.append(dict(tier="tier2", check="sum of a run of department sections' by_type totals = citywide all-funds statement row",
                                   key=lab, a="sections", a_value=None, b="statement", b_value={f"{k[0]} {k[1]}": vals[v] for k, v in ycol.items()},
                                   diff=None, status="no_matching_section_group", page=stm_page(sink, "all")))
            continue
        m, i, k, acc = best
        run = units[i:k + 1]
        names_set = {s_["name"] for g, s_ in run}
        for g, s_ in run:                       # program pages that carry a printed sub-department name rather than a section name
            if s_ is g["sections"][0]:
                names_set |= g.get("dept_names", set()) - {x["name"] for x in g["sections"]} if len(g["sections"]) == 1 else set()
        fam = dict(label=lab, sections=[s_ for g, s_ in run], groups=[g for g, s_ in run], names=names_set, row=ri)
        used.update(range(i, k + 1))
        sink.families.append(fam)
        names = " + ".join(s_["name"] for g, s_ in run)
        for yb, ci in ycol.items():
            if yb in acc:
                add_cross(sink, "tier2", "sum of the department sections' by_type totals = citywide all-funds statement row",
                          f"{lab} <- {names} {yb[0]} {yb[1]}", "sum of sections", acc[yb], "all-funds statement", vals[ci] or 0, stm_page(sink, "all"))
        gfs = defaultdict(int)
        for g, s_ in run:
            for st in g.get("prog_states", {}).get("exp", []):
                if st.get("dept_name") != s_["name"]:
                    continue
                for fund, d in st.get("fund_total_vals", {}).items():
                    if fund and fund.startswith("001"):
                        for ci2, (v, c) in d.items():
                            gfs[(c[0], c[1])] += v
        for yb, ci in ycol.items():
            if yb in acc:
                add_cross(sink, "tier3", "sum of '001 GENERAL FUND' fund TOTAL rows of the department's EXPENDITURE SUMMARY tables = General Fund statement row",
                          f"{lab} {yb[0]} {yb[1]}", "program tables", gfs.get(yb, 0), "general-fund statement", gf_rows[ri][1][ci] or 0, stm_page(sink, "gf"))


def stm_page(sink, key):
    return sink.pages[key][0] if sink.pages.get(key) else None


def program_pages(doc, g, sink):
    """Route this group's program pages (EXPENDITURE SUMMARY / PROGRAM DETAIL / PERSONNEL SUMMARY) to the parsers.
    Sub-departments are told apart by the sub-department text in each page's header and paired with the
    'Budget Summary' sections of the group by order of appearance."""
    types = sink.section_types
    secs = g["sections"]
    last_hdr, cur, allst = {}, {}, defaultdict(list)
    seen_pages = {}
    for p in g["program_pages"]:
        kind = {"program_exp": "exp", "program_detail": "det", "program_pers": "pers"}[types[p]]
        fp = (kind, tuple(l.text for l in doc.lines(p) if not re.match(r"^Page \d+$", l.text)))
        if fp in seen_pages:
            sink.notes.append(f"p.{p}: {kind} page is a verbatim repeat of p.{seen_pages[fp]} (same group {g['top']!r}); skipped to avoid double counting")
            sink.duplicate_pages.append(dict(page=p, duplicate_of=seen_pages[fp], kind=kind, group=g["top"]))
            continue
        seen_pages[fp] = p
        hdr = top_and_sub(doc.lines(p)) if doc.lines(p) else []
        sub = hdr[1] if len(hdr) >= 2 else ""
        if last_hdr.get(kind) != sub or kind not in cur:
            last_hdr[kind] = sub
            cur[kind] = dict(idx=len(allst[kind]), sub=sub, pages=[])
            allst[kind].append(cur[kind])
        st = cur[kind]
        st["pages"].append(p)
        i = st["idx"]
        if secs:
            dept_name = secs[i]["name"] if i < len(secs) else (sub or g["top"])
            if i >= len(secs):
                sink.notes.append(f"p.{p}: {kind} page for sub-department #{i + 1} ({sub!r}) but the group {g['top']!r} has only "
                                  f"{len(secs)} 'Budget Summary' section(s); using the printed sub-department name")
        else:
            dept_name = g["top"]
        st["dept_name"] = dept_name
        g.setdefault("dept_names", set()).add(dept_name)
        if kind == "exp":
            parse_program_exp(doc, p, sink, dept_name, st)
        elif kind == "det":
            parse_program_detail(doc, p, sink, dept_name, st)
        else:
            parse_personnel(doc, p, sink, dept_name, st)
    g["prog_states"] = allst
    # --- cross-checks of program-level tables against the section's own summary
    for kind in ("exp", "det"):
        for st in allst.get(kind, []):
            i = st["idx"]
            if i >= len(secs) or "dept_total" not in st and kind == "exp":
                if kind == "exp" and "dept_total" not in st:
                    sink.unparsed.append(dict(page=st["pages"][0], what="program expenditure table: no DEPARTMENT TOTAL row found", dept=st["dept_name"]))
                continue
            tt = next((o for o in secs[i]["by_type_rows"] if o[0] == "TOTAL"), None)
            if tt is None:
                continue
            if kind == "exp":
                vals, cols, pg = st["dept_total"]
                for ci, v in enumerate(vals):
                    if v is None:
                        continue
                    yb = (cols[ci][0], cols[ci][1])
                    ti = next((k for k, c in enumerate(tt[3]) if (c[0], c[1]) == yb), None)
                    if ti is not None:
                        add_cross(sink, "tier3", "by_program DEPARTMENT TOTAL = section by_type total", f"{secs[i]['name']} {yb[0]} {yb[1]}",
                                  "program table", v, "by_type total", tt[2][ti] or 0, pg)
            else:
                dt = st.get("pd_dept_total")
                if dt:
                    ti = next((k for k, c in enumerate(tt[3]) if (c[0], c[1]) == (2020, "proposed")), None)
                    if ti is not None:
                        add_cross(sink, "tier3", "PROGRAM DETAIL DEPARTMENTAL TOTAL (2020 proposed) = section by_type total", secs[i]["name"],
                                  "program detail", dt[0], "by_type total", tt[2][ti] or 0, dt[1])
    for st in allst.get("pers", []):
        i = st["idx"]
        if i >= len(secs) or "dept_total" not in st:
            if "dept_total" not in st:
                sink.unparsed.append(dict(page=st["pages"][0], what="personnel table: no DEPARTMENT TOTAL row found", dept=st["dept_name"]))
            continue
        pr = secs[i].get("pers_total_rows")
        row = next((v for k, v in (pr or {}).items() if k.lower().startswith("total department ftes")), None)
        if not row:
            continue
        pv, pc = row
        vals, cols, pg = st["dept_total"]
        for ci, v in enumerate(vals):
            yb = (cols[ci][0], cols[ci][1])
            ti = next((k for k, c in enumerate(pc) if (c[0], c[1]) == yb), None)
            if ti is not None and v is not None and pv[ti] is not None:
                add_cross(sink, "tier4", "personnel summary DEPARTMENT TOTAL FTE (x100) = 'Total Department FTEs' on the personnel page",
                          f"{secs[i]['name']} {yb[0]} {yb[1]}", "positions table", round(v * 100), "personnel page", round(pv[ti] * 100), pg)


def citywide_checks(doc, sink, groups, stm, funds_src):
    secs = [s_ for g in groups for s_ in g["sections"]]
    sums = defaultdict(lambda: defaultdict(int))
    norm = lambda t: re.sub(r"[^A-Z]", "", t.upper())
    for s_ in secs:
        for o in s_["by_type_rows"]:
            if o[0] == "FTE":
                continue
            for ci, v in enumerate(o[2]):
                if v is not None:
                    c = o[3][ci]
                    sums[("TOTAL" if o[0] == "TOTAL" else norm(o[1]))][(c[0], c[1])] += v
    if "all_funds" in stm:
        for yb, s0 in sums["TOTAL"].items():
            r = [t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == yb]
            if r:
                add_cross(sink, "tier2", "sum of all sections' by_type totals = citywide all-funds statement Total", f"{yb[0]} {yb[1]}",
                          "sum of sections", s0, "statement", r[0]["amount"], None)
        matched = {f["label"] for f in sink.families}
        al_rows, al_cols = stm["all_funds"]
        sink.unmatched_statement_rows = [lab for lab, v in al_rows if lab not in matched and any(v)]
        for lab in sink.unmatched_statement_rows:
            sink.notes.append(f"citywide all-funds statement row {lab!r} (non-zero) is not matched by any department section group")
        for yb, s0 in sums["TOTAL"].items():
            r = [t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == yb]
            ci = next((i for i, c in enumerate(al_cols) if (c[0], c[1]) == yb), None)
            if r and ci is not None:
                extra = sum((v[ci] or 0) for lab, v in al_rows if lab in sink.unmatched_statement_rows)
                add_cross(sink, "tier2", "sum of all sections' by_type totals + statement rows without a department section ("
                          + ", ".join(sink.unmatched_statement_rows) + ") = statement Total", f"{yb[0]} {yb[1]}",
                          "sections + unmatched rows", s0 + extra, "statement", r[0]["amount"], None)
    # sum over the fund tables of the printed fund totals = citywide all-funds statement total
    agg0 = getattr(sink, "type_agg", {}).get("__TOTAL__", {})
    for yb, v in agg0.items():
        r = [t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == yb]
        if r:
            add_cross(sink, "tier1", "sum over the by-type fund tables of 'Total Expenditures' = citywide all-funds statement Total", f"{yb[0]} {yb[1]}",
                      "fund by-type tables", v, "statement", r[0]["amount"], None)
    # sum over pp.81-92 fund type tables of a type row = sum over sections of that type row
    agg = getattr(sink, "type_agg", {})
    for key, ycs in sums.items():
        if key == "TOTAL":
            continue
        for yb, s0 in ycs.items():
            a = agg.get(key, {}).get(yb)
            if a is None:
                sink.cross.append(dict(tier="tier2", check="sum over pp.81-92 fund tables of a type row = sum over department sections", key=f"{key} {yb}",
                                       a="pp81-92", a_value=None, b="sections", b_value=s0, diff=None, status="no_counterpart", page=None))
            else:
                add_cross(sink, "tier2", "sum over pp.81-92 fund tables of a type row = sum over department sections", f"{key} {yb[0]} {yb[1]}",
                          "pp81-92", a, "sections", s0, None)
    # p80 fund totals vs sum of fund TOTAL rows of program tables, matched by fund code order is not available: compare
    # the GF line of p80 with the sum of '001 GENERAL FUND' totals
    s80 = sink.fund_summary.get("p80")
    gfsum = defaultdict(int)
    for g in groups:
        for st in g.get("prog_states", {}).get("exp", []):
            for fund, d in st.get("fund_total_vals", {}).items():
                if fund and fund.startswith("001"):
                    for ci, (v, c) in d.items():
                        gfsum[(c[0], c[1])] += v
    if s80:
        body, totv, c80, p80 = s80
        gfl = next((vals for lab, vals in body if lab.lower().startswith("general fund")), None)
        if gfl:
            for ci, v in enumerate(gfl):
                yb = (c80[ci][0], c80[ci][1])
                if v is not None and yb in gfsum:
                    add_cross(sink, "tier2", "p80 General Fund line = sum of '001 GENERAL FUND' fund TOTAL rows of all EXPENDITURE SUMMARY tables",
                              f"{yb[0]} {yb[1]}", "p80", v, "program tables", gfsum[yb], p80)


# --------------------------------------------------------------------------- writers
def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def fmt_amount(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def write_outputs(sink, doc):
    OUT.mkdir(parents=True, exist_ok=True)
    for lst in (sink.dept_exp, sink.revenues, sink.detail, sink.totals):
        for r in lst:
            r["amount"] = fmt_amount(r["amount"])
    write_csv(OUT / "department_expenditures.csv", sink.dept_exp,
              "book_id fiscal_year basis fund_group dept_code dept_name amount page".split())
    write_csv(OUT / "revenues.csv", sink.revenues,
              "book_id fiscal_year basis fund_group fund_name category line_name amount page".split())
    write_csv(OUT / "department_detail.csv", sink.detail,
              "book_id fiscal_year basis dept_code dept_name table dim1 dim2 amount page".split())
    write_csv(OUT / "positions.csv", [dict(r, fte=f"{r['fte']:.2f}") for r in sink.positions],
              "book_id fiscal_year basis dept_code dept_name program fund position_title pay_grade fte page".split())
    write_csv(OUT / "printed_totals.csv", sink.totals, "book_id fiscal_year basis scope label amount page".split())
    write_checks(sink)


def fmt_for_search(v, decimal=False):
    if decimal:
        return f"{v:.2f}".lstrip("0") if v < 1 else f"{v:.2f}"
    return f"{abs(int(v)):,}"


def cid_decode(text):
    """pdfplumber/pdfminer shows the undecodable glyphs as '(cid:N)': decode with the same Arial glyph-order rule"""
    def rep(m):
        g = int(m.group(1))
        return GID_EXTRA.get(g) or (chr(g + 29) if 3 <= g <= 97 else "\ufffd")
    return re.sub(r"\(cid:(\d+)\)", rep, text)


def spot_checks(sink, n_each=20, seed=2020):
    """Independent re-read: pick seeded-random output cells and look for the same number, on the same text
    line as the row's label, in pdfplumber's text extraction of the cited page (a different extraction
    engine and line-building algorithm from the PyMuPDF word clustering used above)."""
    import random
    import pdfplumber
    rng = random.Random(seed)
    sets = [("department_expenditures", sink.dept_exp, lambda r: r["dept_name"], False),
            ("revenues", sink.revenues, lambda r: r["line_name"], False),
            ("department_detail", sink.detail, lambda r: re.sub(r"^\S{2,4}\s+", "", r["dim1"]) if r["table"] == "by_fund" else r["dim1"], False),
            ("positions", sink.positions, lambda r: r["position_title"] or r["program"], True)]
    out = []
    with pdfplumber.open(str(PDF)) as pdf:
        cache = {}
        for name, rows, labf, dec in sets:
            for r in rng.sample(rows, min(n_each, len(rows))):
                pg = r["page"]
                if pg not in cache:
                    cache[pg] = [clean(cid_decode(x)) for x in (pdf.pages[pg - 1].extract_text() or "").split("\n")]
                amt = r["amount"] if "amount" in r else r["fte"]
                needle = fmt_for_search(amt, dec)
                lab = labf(r)
                nl = re.sub(r"[^a-z0-9]", "", lab.lower())
                hits = [ln for ln in cache[pg] if needle in ln and nl and nl in re.sub(r"[^a-z0-9]", "", ln.lower())]
                out.append(dict(file=name, page=pg, fiscal_year=r["fiscal_year"], basis=r["basis"], label=lab,
                                amount=amt, found=bool(hits), matched_line=hits[0] if hits else None))
    return out


def write_checks(sink):
    tiers = defaultdict(Counter)
    for c in sink.checks:
        tiers[c["tier"]][c["status"]] += 1
    cross = defaultdict(Counter)
    for c in sink.cross:
        cross[c["tier"]][c["status"]] += 1
    out = OrderedDict()
    out["book_id"] = BOOK_ID
    out["row_counts"] = dict(department_expenditures=len(sink.dept_exp), revenues=len(sink.revenues),
                             department_detail=len(sink.detail), positions=len(sink.positions),
                             printed_totals=len(sink.totals))
    out["tier_definitions"] = {
        "tier1": "citywide statements: department expenditures (general/other/all funds), revenues, fund summaries (pp.60, 66, 80, 81-92, 93-95)",
        "tier2": "department by-type (Budget Summary pages) and by-fund (fund TOTAL rows of the EXPENDITURE SUMMARY tables)",
        "tier3": "program-level detail (EXPENDITURE SUMMARY, PROGRAM DETAIL)",
        "tier4": "positions / FTE (PERSONNEL SUMMARY, personnel totals, p.99 schedule)"}
    out["year_columns_captured"] = {
        name: sorted({f"{r['fiscal_year']} {r['basis']}" for r in rows})
        for name, rows in (("department_expenditures", sink.dept_exp), ("revenues", sink.revenues),
                           ("department_detail", sink.detail), ("positions", sink.positions), ("printed_totals", sink.totals))}
    out["printed_total_checks_summary"] = {t: dict(v) for t, v in sorted(tiers.items())}
    out["cross_checks_summary"] = {t: dict(v) for t, v in sorted(cross.items())}
    out["printed_total_checks"] = sink.checks
    out["cross_checks"] = sink.cross
    sc = spot_checks(sink)
    out["spot_checks_summary"] = dict(sampled=len(sc), found=sum(1 for x in sc if x["found"]))
    out["spot_checks"] = sc
    out["duplicate_pages_skipped"] = sink.duplicate_pages
    out["not_attempted"] = sink.not_attempted
    out["unparsed"] = sink.unparsed
    out["notes"] = sink.notes
    out["pages_located"] = {k: v for k, v in sink.pages.items()}
    out["page_types_after_citywide_summary"] = Counter(sink.section_types.values())
    with open(OUT / "checks.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=lambda o: dict(o) if isinstance(o, Counter) else str(o))


if __name__ == "__main__":
    main()
