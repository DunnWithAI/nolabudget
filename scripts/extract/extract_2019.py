#!/usr/bin/env python
"""Extract the City of New Orleans 2019 Adopted Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2019.py

Outputs (data/extracted/2019-operating-adopted/): department_expenditures.csv, revenues.csv,
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
PDF = ROOT / "data/raw/nola-gov/2019/2019-Adopted-Budget-Book_File.pdf"
BOOK_ID = "2019-operating-adopted"
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


class Doc:
    def __init__(self, path):
        self.d = fitz.open(str(path))
        self.n = len(self.d)
        self._lines = {}

    def lines(self, p):          # p is 1-based
        if p not in self._lines:
            ws = [Word(w) for w in self.d[p - 1].get_text("words")]
            self._lines[p] = cluster_lines(ws)
        return self._lines[p]

    def first_text(self, p, k=1):
        ls = self.lines(p)
        return clean(" ".join(l.text for l in ls[:k]))


def header_groups(lines):
    """Find year/basis header rows: lines holding >=3 distinct year tokens.  Returns list of
    dicts {y, cols:[(year, basis, cx)]}.  Basis words are matched by x among words within 30pt."""
    allw = [w for l in lines for w in l.words]
    groups = []
    for l in lines:
        yrs = [w for w in l.words if YEAR_RE.match(w.t)]
        xs = sorted(w.cx for w in yrs)
        if len(yrs) < 3 or any(b - a < 25 for a, b in zip(xs, xs[1:])):
            continue
        cols = []
        for w in yrs:
            best = None
            for b in allw:
                if b.t.lower() in BASES and abs(b.cy - w.cy) <= 30 and abs(b.cx - w.cx) <= 45:
                    d = abs(b.cx - w.cx) + abs(b.cy - w.cy) * 0.5
                    if best is None or d < best[0]:
                        best = (d, BASES[b.t.lower()])
            cols.append((int(w.t), best[1] if best else None, w.cx))
        groups.append({"y": l.y, "cols": sorted(cols, key=lambda c: c[2])})
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
        l.label_words = [w for w in l.words if id(w) not in cw]
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


def parse_fixed_cells(doc, p, ncells, require_total_label=None):
    """Cross-check-only tables (pp.100-102): rows with exactly `ncells` numeric tokens, assigned by
    x order.  Returns list of (label, [values]) and the rows that did not have ncells cells."""
    rows, bad = [], []
    for l in doc.lines(p):
        nums = [(w, v) for w, v in ((w, parse_num(w.t)) for w in l.words) if isinstance(v, (int, float)) and w.x0 >= 100]
        if not nums:
            continue
        lab = clean(" ".join(w.t for w in l.words if parse_num(w.t) is None or w.x0 < 100))
        if len(nums) == ncells:
            rows.append((lab, [v for _, v in sorted(nums, key=lambda t: t[0].x0)]))
        else:
            bad.append((lab, len(nums)))
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


HEADER_WORDS = {"change", "fy18-19", "actual", "adopted", "proposed", "dollar", "percent"}


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


def parse_sections(doc, sink, citywide):
    """Classify every page from the citywide departmental summary to the end by its printed title."""
    first = next(p for p in range(100, 300) if doc.first_text(p, 1) == "DEPARTMENTAL BUDGET SUMMARY")
    types = {}
    for p in range(first, doc.n + 1):
        ft1 = doc.first_text(p, 1)
        if re.match(r"^Budget Summary\s*-\s*", ft1):
            types[p] = "by_type"
        elif any(l.text.startswith("Expenditures by Funding Source -") for l in doc.lines(p)):
            types[p] = "by_fund"
        elif ft1.startswith("Detailed Personnel by Department"):
            types[p] = "personnel_total"
        elif ft1 == "DEPARTMENTAL BUDGET SUMMARY":
            types[p] = "dept_summary"
        else:
            ls = doc.lines(p)
            if not ls:
                continue
            txt = ls[0].text
            for key, tp in (("EXPENDITURE SUMMARY", "program_exp"), ("PROGRAM DETAIL", "program_detail"),
                            ("PERSONNEL SUMMARY", "program_pers")):
                if txt.endswith(key):
                    types[p] = tp
                    break
    return first, types


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


def parse_dept_summary(doc, p, sink, citywide=False):
    """'<TOP> DEPARTMENTAL BUDGET SUMMARY' page: by-type block, TOTAL EXPENDITURES, by-fund block, TOTAL FUNDING."""
    lines = doc.lines(p)
    top = lines[1].text if len(lines) > 1 else ""
    rows = []
    lines = assign_cells(lines)
    groups = header_groups(lines)
    if not groups:
        sink.unparsed.append(dict(page=p, what="no year header on departmental summary", dept=top))
        return top, []
    cols = max(groups, key=lambda g: g["y"])["cols"]
    ytab = max(g["y"] for g in groups)
    pend = []
    for l in lines:
        if l.y <= ytab + 1 or not l.cells:
            if l.y > ytab + 1 and l.label and not re.match(r"^(Page \d+|EXPENDITURES|SOURCE OF FUNDING)$", l.text):
                pend.append(l.label)
            continue
        lab = clean(" ".join(pend + [l.label]))
        pend = []
        vals = [None] * len(l.hdr["cols"])
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        rows.append((lab, vals))
    # split into by-type (up to TOTAL EXPENDITURES) and by-fund (up to TOTAL FUNDING)
    block, out = "by_type", []
    for lab, vals in rows:
        u = lab.upper()
        if u.startswith("TOTAL EXPENDITURES"):
            out.append(("by_type", "TOTAL", lab, vals))
            block = "by_fund"
        elif u.startswith("TOTAL FUNDING"):
            out.append(("by_fund", "TOTAL", lab, vals))
        else:
            out.append((block, "ROW", lab, vals))
    # a label-only 'TOTAL FUNDING' that sits below its cells would be missed; enforce
    tabs = {"by_type": "dept_summary_by_type", "by_fund": "dept_summary_by_fund"}
    for tb, kind, lab, vals in out:
        if kind == "TOTAL":
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], f"department_detail/{top}/{tabs[tb]}", lab, v, p)
        else:
            emit_detail(sink, top, tabs[tb], lab, "", vals, cols, p)
    for tb in ("by_type", "by_fund"):
        tot = next((o for o in out if o[0] == tb and o[1] == "TOTAL"), None)
        if tot is None:
            sink.unparsed.append(dict(page=p, what=f"{tabs[tb]} total row not found", dept=top))
            continue
        for ci, v in enumerate(tot[3]):
            if v is not None:
                s = sum((o[3][ci] or 0) for o in out if o[0] == tb and o[1] == "ROW")
                add_check(sink, "tier2", f"department_detail/{top}/{tabs[tb]}", tot[2], p, cols[ci][0], cols[ci][1], v, s,
                          "sum of rows on page")
    return top, out, cols


# --- program tables ---------------------------------------------------------------------------
FUND_HDR_RE = re.compile(r"^([A-Z0-9]{3,4})\s+\S")
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
        if re.search(r"\bTOTAL$", lab):
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
        if re.search(r"\bTOTAL$", lab):
            is_dept = lab.upper().startswith("DEPARTMENT")
            scope = f"department_detail/{dept_name}/by_program_type" + ("" if is_dept else f"/{state.get('pd_fund')}")
            for i in range(4):
                if vals[i] is not None:
                    sink.total(2019, "adopted", scope + f"/{names[i][0]}", lab, vals[i], p)
                    if is_dept:
                        s = sum(t[i] or 0 for t in state.get("pd_fund_totals", []))
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, 2019, "adopted", vals[i], s,
                                  "sum of the fund TOTAL rows in this program-detail table")
                    else:
                        s = sum((r[i] or 0) for r in state["pd_rows"])
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, 2019, "adopted", vals[i], s,
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
                sink.detail.append(dict(book_id=BOOK_ID, fiscal_year=2019, basis="adopted", dept_code="", dept_name=dept_name,
                                        table="by_program_type", dim1=lab, dim2=f"{state.get('pd_fund','')} | {names[i][0]}",
                                        amount=vals[i], page=p))
        if vals[3] is not None:
            add_check(sink, "tier3", f"department_detail/{dept_name}/by_program_type/row", lab, p, 2019, "adopted",
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
        if re.search(r"\bTOTAL$", lab):
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
    ("General Fund Revenue Projections by Source", "p.71 GF revenue projections 2017-2023: different 2018 'adopted' figures (as-of date differs from the statement); multi-year projections not in the schema"),
    ("Fund Balance Projections", "fund balance projections (as-of date header has garbled glyphs)"),
    ("Summary of the 2019 Adopted Budget - General Fund Only", "narrative/summary chart page"),
    ("Citywide Revenues and Expenditures Overview", "narrative/chart pages"),
    ("2019 Millage Rates & Special Tax Districts", "millage table (rates, not dollars)"),
    ("Special Fees", "fee schedule"),
    ("Statement of Debt", "debt statement, credit ratings, pension/OPEB notes (pp.108-129)"),
    ("2019 CAPITAL BUDGET SUMMARY", "capital budget summary (separate book)"),
]


def note_not_attempted(doc, sink):
    for title, why in NOT_ATTEMPTED_TITLES:
        pgs = [p for p in range(1, doc.n + 1) if any(l.text.startswith(title) for l in doc.lines(p)[:6])]
        sink.not_attempted.append(dict(title=title, pages=pgs, reason=why))
    narrative = [p for p in sorted(sink.section_types_all_pages) if p not in sink.section_types]
    sink.not_attempted.append(dict(title="department narrative pages (overview, org chart, accomplishments, priorities, section dividers)",
                                   pages=narrative, reason="free text / org charts; no budget tables"))


def run_all(doc, sink):
    # ---- tier 1a: citywide department statements -------------------------------------------
    pg = {}
    pg["gf"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "General Fund Expenditures", 90, 120)
    pg["of"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Other Fund Expenditures", 90, 120)
    pg["all"] = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Total Expenditures" and "Department" in d.first_text(p, 3), 90, 120)
    pg["by_fund_dept"] = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Departmental Expenditures by Fund"), 90, 120)
    pg["fund_src"] = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Summary of Departmental Funding by Source"), 90, 120)
    for k, v in pg.items():
        if not v:
            sink.unparsed.append(dict(page=None, what=f"locator found no page for {k}"))
    stm = {}
    for key, fg in (("gf", "general_fund"), ("of", "other_funds"), ("all", "all_funds")):
        if pg[key]:
            stm[fg] = parse_dept_statement(doc, pg[key][0], sink, fg)
    sink.pages = pg
    # general + other = all, per dept and year (rows joined by order; verified by counts + p.102)
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
    # p.102 'Total Departmental Expenditures by Fund' : GF / non-GF / total for 2018 adopted and 2019 adopted.
    # Rows are aligned to the statements by value (p.102 omits an all-zero department that the statements list).
    if pg["by_fund_dept"] and len(stm) == 3:
        p = pg["by_fund_dept"][0]
        rows, bad = parse_fixed_cells(doc, p, 6)
        for b in bad:
            if b[1] >= 3:
                sink.unparsed.append(dict(page=p, what=f"p.{p} cross-check row did not have 6 numeric cells: {b}"))
        body = [r for r in rows if not r[0].upper().startswith("TOTAL")]
        gf, of, al = stm["general_fund"], stm["other_funds"], stm["all_funds"]
        cols = gf[1]
        c18 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2018, "adopted"))
        c19 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2019, "adopted"))
        sf = {"general_fund": gf, "other_funds": of, "all_funds": al}
        pairs = align_rows(len(gf[0]), body,
                           lambda i, j: (al[0][i][1][c18] or 0) == body[j][1][2] and (al[0][i][1][c19] or 0) == body[j][1][5],
                           lambda i: not (al[0][i][1][c18] or al[0][i][1][c19]), sink, p, [r[0] for r in gf[0]])
        for i, j in pairs:
            for (yr, cc, off) in ((2018, c18, 0), (2019, c19, 3)):
                for nm, j2 in (("general_fund", 0), ("other_funds", 1), ("all_funds", 2)):
                    add_cross(sink, "tier1", f"p.{p} 'Total Departmental Expenditures by Fund' vs statement", f"{gf[0][i][0]} {yr} {nm}",
                              "p102", body[j][1][off + j2], f"statement p{pg[{'general_fund':'gf','other_funds':'of','all_funds':'all'}[nm]][0]}",
                              sf[nm][0][i][1][cc] or 0, p)
    # p.100/101: Summary of departmental funding by source: 14 fund columns, 2019 adopted
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
        c19 = next(i for i, c in enumerate(cols) if (c[0], c[1]) == (2019, "adopted"))
        pairs = align_rows(len(gf[0]), body,
                           lambda i, j: (al[0][i][1][c19] or 0) == sum(body[j][1][1]) and (gf[0][i][1][c19] or 0) == body[j][1][1][0],
                           lambda i: not (al[0][i][1][c19]), sink, pg["fund_src"][0], [r[0] for r in gf[0]])
        for i, j in pairs:
            pp, (lab, v) = body[j]
            add_cross(sink, "tier1", "p.100-101 funding by source: GF column = general-fund statement", f"{gf[0][i][0]} 2019 adopted",
                      "p100 GF col", v[0], "statement", gf[0][i][1][c19] or 0, pp)
            add_cross(sink, "tier1", "p.100-101 funding by source: sum of 14 fund columns = all-funds statement", f"{gf[0][i][0]} 2019 adopted",
                      "p100 sum of 14 cols", sum(v), "statement", al[0][i][1][c19] or 0, pp)
            add_cross(sink, "tier1", "p.100-101 funding by source: sum of 13 non-GF columns = other-funds statement", f"{gf[0][i][0]} 2019 adopted",
                      "p100 sum of cols 2-14", sum(v[1:]), "statement", of[0][i][1][c19] or 0, pp)
        funds_src = (body, trow)
    # ---- tier 1b: revenues ----------------------------------------------------------------
    rev = parse_revenues(doc, sink)
    # ---- fund-level summary pages ------------------------------------------------------------
    parse_fund_summaries(doc, sink, stm, rev)
    # ---- tier 2/3/4: department sections ---------------------------------------------------------
    type_by_fund_pages(doc, sink)
    sections(doc, sink, stm, funds_src)
    fte_schedule_check(doc, sink, stm)
    sink.section_types_all_pages = list(range(sink.first_section_page, doc.n + 1))
    note_not_attempted(doc, sink)


def parse_fund_summaries(doc, sink, stm, rev):
    """p.87 'Total Expenditures by Fund Source', p.66 'Total Revenues by Fund', p.73 revenue detail."""
    p87 = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Expenditures by Fund Source"), 80, 110)
    sink.fund_summary = {}
    if p87:
        p = p87[0]
        rows, cols = simple_table(doc, p, wrap=True, pick="first", stop_re=r"^Total Expenditures")
        tot, body = None, []
        for lab, vals, c, l in rows:
            if re.match(r"^Total Expenditures", lab):
                tot = vals
            else:
                body.append((lab, vals))
        sink.fund_summary["p87"] = (body, tot, cols, p)
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
    # p66 / p73
    for key, ttl, scope in (("p66", "Overview of Revenues and Expenditures Total Revenues by Fund", "revenue_summary/by_fund_p66"),
                            ("p73", "Detailed Description of Revenues from All Funds Total Revenues by Fund Source", "revenue_summary/by_fund_p73")):
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
    # compare p66/p73 against the revenue statement grand totals
    gft, ot = rev["gf_total"], rev["other_total"]
    for key in ("p66", "p73"):
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
    pages = find_pages(doc, lambda d, p: d.first_text(p, 1) == "Total Expenditures by Type", 80, 110)
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
    sink.pages["expenditures_by_type"] = pages
    sink.type_agg = agg


def fte_schedule_check(doc, sink, stm):
    """p.106 'Detailed City-Wide Personnel Schedule by Department' (FTE: General / Non-General / Total for 2018
    adopted and 2019 adopted) vs the FTEs summed from the position pages.  Department names differ between the
    two tables, so each department group is matched to a p.106 row by its six FTE figures; a group (or a
    non-zero p.106 row) with no counterpart is reported as a mismatch."""
    pp = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Detailed City-Wide Personnel Schedule"), 90, 120)
    if not pp:
        sink.unparsed.append(dict(page=None, what="citywide personnel schedule (p.106) not located"))
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
    for g in sink.groups:
        if g["enterprise"] or not g["sections"]:
            continue
        names = [s["name"] for s in g["sections"]]
        vec, gfv = [], []
        for (yr, basis) in ((2018, "adopted"), (2019, "adopted")):
            t = sum(fte[(n, yr, basis)] for n in names)
            gf = sum(ftegf[(n, yr, basis)] for n in names)
            vec.append(round(t * 100))
            gfv.append(round(gf * 100))
        j = next((j for j, (lab, v) in enumerate(body) if j not in used
                  and [round(v[2] * 100), round(v[5] * 100)] == vec), None)
        if j is None:
            sink.cross.append(dict(tier="tier4", check="FTE total from position rows (x100; 2018 adopted, 2019 adopted) matches a p.106 row's Total column",
                                   key=g["top"], a="positions.csv", a_value=vec, b="p106", b_value=None, diff=None,
                                   status="no_matching_row", page=p))
        else:
            used.add(j)
            add_cross(sink, "tier4", "FTE total from position rows (x100; 2018 adopted, 2019 adopted) matches a p.106 row's Total column",
                      f"{g['top']} -> p106 '{body[j][0]}'", "positions.csv", 0, "p106", 0, p)
            for k, (yr, off) in enumerate(((2018, 0), (2019, 3))):
                add_cross(sink, "tier4", "FTE in fund 001 from position rows (x100) = p.106 General Fund column",
                          f"{g['top']} {yr} adopted", "positions.csv", gfv[k], "p106", round(body[j][1][off] * 100), p)
    for j, (lab, v) in enumerate(body):
        if j not in used and any(v):
            sink.cross.append(dict(tier="tier4", check="p.106 row with non-zero FTE has no department group in the position pages",
                                   key=lab, a="positions.csv", a_value=None, b="p106", b_value=v, diff=None,
                                   status="no_matching_group", page=p))


def sections(doc, sink, stm, funds_src):
    first, types = parse_sections(doc, sink, stm)
    sink.section_types = types
    sink.first_section_page = first
    # --- citywide summary page
    cw = parse_dept_summary(doc, first, sink, citywide=True)
    # -- grouping
    pages = sorted(types)
    by_type_pages = {}            # p -> parsed
    groups = []                   # each: dict(top, summary_page, sections[(name, by_type_page, by_fund_page, pers_total_page)], program_pages[])
    cur_secs, cur_sec = [], None
    state_prog = []
    last_group = None
    dept_summaries = {}
    for p in pages:
        t = types[p]
        if p == first:
            continue
        if t == "by_type":
            name = re.sub(r"^Budget Summary\s*-\s*", "", doc.first_text(p, 1))
            cur_sec = dict(name=name, by_type=p, by_fund=None, orphan_funds=[], pers_total=None)
            cur_secs.append(cur_sec)
        elif t == "by_fund" and cur_sec:
            fname = next(re.sub(r"^Expenditures by Funding Source\s*-\s*", "", l.text) for l in doc.lines(p)
                         if l.text.startswith("Expenditures by Funding Source -"))
            if cur_sec["by_fund"] is None and fname.lower() == cur_sec["name"].lower():
                cur_sec["by_fund"] = p
            else:
                # a funding-source page whose title is not this section's name and which has no
                # Budget-Summary (by type) page of its own: kept, reported as an orphan page
                cur_sec["orphan_funds"].append(p)
        elif t == "personnel_total" and cur_sec:
            cur_sec["pers_total"] = p
        elif t == "dept_summary":
            g = dict(summary_page=p, sections=cur_secs, program_pages=[])
            groups.append(g)
            last_group = g
            cur_secs, cur_sec = [], None
        elif t in ("program_exp", "program_detail", "program_pers") and last_group is not None:
            last_group["program_pages"].append(p)
    sink.groups = groups
    ent = next((p for p in range(first, doc.n + 1) if doc.first_text(p, 1) == "Enterprise Funds"), None)
    for g in groups:
        g["enterprise"] = ent is not None and g["summary_page"] > ent
    # --- parse each group
    all_sec_by_type = {}
    for g in groups:
        sp = g["summary_page"]
        res = parse_dept_summary(doc, sp, sink)
        g["top"], g["summary_rows"], g["summary_cols"] = res if len(res) == 3 else (res[0], [], None)
        for s in g["sections"]:
            nm, out = parse_by_type_or_fund(doc, s["by_type"], sink, "by_type", g["top"])
            s["by_type_rows"] = out
            if s["by_fund"]:
                nm2, outf = parse_by_type_or_fund(doc, s["by_fund"], sink, "by_fund", g["top"])
                s["by_fund_rows"] = outf
            else:
                sink.unparsed.append(dict(page=s["by_type"], what="no 'Expenditures by Funding Source' page with the section's own name", dept=s["name"]))
            s["orphan_rows"] = []
            for op in s["orphan_funds"]:
                nm3, outo = parse_by_type_or_fund(doc, op, sink, "by_fund", g["top"])
                s["orphan_rows"].append((nm3, op, outo))
                sink.notes.append(f"p.{op}: funding-source page titled {nm3!r} has no matching 'Budget Summary' (by type) page; "
                                  f"kept as by_fund rows under that printed title and counted into group {g['top']!r}")
            if s["pers_total"]:
                parse_pers_total(doc, s, sink)
        # group-level checks: sum of sub-section by_type totals vs dept summary total
        group_checks(doc, g, sink, stm)
        # program pages
        program_pages(doc, g, sink)
    # citywide p134 -> sum over groups
    citywide_checks(doc, sink, groups, cw, stm, funds_src)


def parse_pers_total(doc, s, sink):
    p = s["pers_total"]
    rows, cols = simple_table(doc, p, wrap=True)
    s["pers_total_rows"] = {}
    for lab, vals, c, l in rows:
        s["pers_total_rows"][lab] = (vals, cols)


def group_checks(doc, g, sink, stm):
    top, out, cols = g["top"], g["summary_rows"], g["summary_cols"]
    if not out:
        return
    tot = next((o for o in out if o[0] == "by_type" and o[1] == "TOTAL"), None)
    sections = g["sections"]
    g["match"] = None
    # match to citywide all-funds statement by value (all printed year columns equal)
    if tot and "all_funds" in stm:
        al_rows, al_cols = stm["all_funds"]
        key = {(c[0], c[1]): ci for ci, c in enumerate(cols)}
        for lab, vals in al_rows:
            ok, n = True, 0
            for (yb, ci) in key.items():
                a_ci = next((i for i, c in enumerate(al_cols) if (c[0], c[1]) == yb), None)
                if a_ci is None:
                    continue
                if (vals[a_ci] or 0) != (tot[3][ci] or 0):
                    ok = False
                n += 1
            if ok and n and any(v for v in vals):
                g["match"] = lab
                break
        for ci, (yr, basis, _) in enumerate(cols):
            if tot[3][ci] is None:
                continue
            if g["match"]:
                a_ci = next(i for i, c in enumerate(al_cols) if (c[0], c[1]) == (yr, basis))
                v = next(vv for ll, vv in al_rows if ll == g["match"])[a_ci] or 0
                add_cross(sink, "tier2", "department summary TOTAL EXPENDITURES = citywide all-funds statement", f"{top} -> {g['match']} {yr} {basis}",
                          "dept summary", tot[3][ci], "p105-style statement", v, g["summary_page"])
        if not g["match"]:
            # report as mismatch: no citywide row has identical values
            sink.cross.append(dict(tier="tier2", check="department summary TOTAL EXPENDITURES = citywide all-funds statement",
                                   key=top, a="dept summary", a_value=None, b="statement", b_value=None, diff=None,
                                   status="not_in_citywide_statements" if g["enterprise"] else "no_matching_citywide_row",
                                   page=g["summary_page"]))
    # sections' by_type totals vs summary
    if sections:
        for ci, (yr, basis, _) in enumerate(cols):
            if tot and tot[3][ci] is not None:
                ssum, have = 0, True
                for s in sections:
                    t = next((o for o in s["by_type_rows"] if o[0] == "TOTAL"), None)
                    scols = t[3] if t else None
                    sci = next((i for i, c in enumerate(scols) if (c[0], c[1]) == (yr, basis)), None) if scols else None
                    if t is None or sci is None:
                        have = False
                        continue
                    ssum += t[2][sci] or 0
                if have:
                    add_cross(sink, "tier2", "sum of section by_type totals = departmental summary TOTAL EXPENDITURES",
                              f"{top} {yr} {basis}", "sum of sections", ssum, "dept summary", tot[3][ci], g["summary_page"])
                    orphans = [(nm3, op, outo) for s in sections for nm3, op, outo in s.get("orphan_rows", [])]
                    if orphans:
                        osum = 0
                        for nm3, op, outo in orphans:
                            t3 = next((o for o in outo if o[0] == "TOTAL"), None)
                            c3 = next((i for i, c in enumerate(t3[3]) if (c[0], c[1]) == (yr, basis)), None) if t3 else None
                            osum += (t3[2][c3] or 0) if c3 is not None else 0
                        add_cross(sink, "tier2", "dept summary total - sum of section by_type totals = totals of orphan funding-source pages",
                                  f"{top} {yr} {basis} (orphan pages {[o[1] for o in orphans]})", "summary minus sections",
                                  tot[3][ci] - ssum, "orphan pages", osum, g["summary_page"])
        # by_fund: each section's by_fund total = by_type total
    for s in sections:
        tt = next((o for o in s["by_type_rows"] if o[0] == "TOTAL"), None)
        tf = next((o for o in s.get("by_fund_rows", []) if o[0] == "TOTAL"), None)
        if tt and tf:
            for ci, (yr, basis, _) in enumerate(tt[3]):
                cf = next((i for i, c in enumerate(tf[3]) if (c[0], c[1]) == (yr, basis)), None)
                if cf is not None and tt[2][ci] is not None:
                    add_cross(sink, "tier2", "section by_type total = by_fund total", f"{s['name']} {yr} {basis}",
                              "by_type", tt[2][ci], "by_fund", tf[2][cf] or 0, s["by_type"])
        # personal services vs 'Dollar amount' on personnel page
        pr = s.get("pers_total_rows")
        if pr:
            dollar = next((v for k, v in pr.items() if k.lower().startswith("dollar amount")), None)
            ps = next((o for o in s["by_type_rows"] if o[0] == "ROW" and o[1].lower().startswith("personal")), None)
            if dollar and ps:
                dv, dc = dollar
                for ci, (yr, basis, _) in enumerate(dc):
                    pci = next((i for i, c in enumerate(ps[3]) if (c[0], c[1]) == (yr, basis)), None)
                    if pci is not None and dv[ci] is not None:
                        add_cross(sink, "tier4", "personnel page 'Dollar amount' = by_type Personal Services", f"{s['name']} {yr} {basis}",
                                  "personnel page", dv[ci], "by_type", ps[2][pci] or 0, s["pers_total"])


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
                    ti = next((k for k, c in enumerate(tt[3]) if (c[0], c[1]) == (2019, "adopted")), None)
                    if ti is not None:
                        add_cross(sink, "tier3", "PROGRAM DETAIL DEPARTMENTAL TOTAL (2019 adopted) = section by_type total", secs[i]["name"],
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


def citywide_checks(doc, sink, groups, cw, stm, funds_src):
    top, out, cols = cw
    # citywide by_fund / by_type totals = sum over departmental summaries
    sums = defaultdict(lambda: defaultdict(int))
    norm = lambda t: re.sub(r"[^A-Z]", "", t.upper())
    for g in groups:
        if g["enterprise"]:
            continue
        for tb, kind, lab, vals in g["summary_rows"]:
            for ci, v in enumerate(vals):
                if v is not None:
                    sums[(tb, norm(lab))][(g["summary_cols"][ci][0], g["summary_cols"][ci][1])] += v
    for tb, kind, lab, vals in out:
        for ci, v in enumerate(vals):
            if v is None:
                continue
            yb = (cols[ci][0], cols[ci][1])
            s = sums.get((tb, norm(lab)), {}).get(yb)
            if s is None:
                sink.cross.append(dict(tier="tier2", check="citywide summary line = sum of departmental summaries", key=f"{lab} {yb}",
                                       a="citywide p134", a_value=v, b="sum of dept summaries", b_value=None, diff=None,
                                       status="no_counterpart", page=None))
            else:
                add_cross(sink, "tier2", "citywide summary line = sum of departmental summaries (non-enterprise)", f"{lab} {yb[0]} {yb[1]}",
                          "citywide", v, "sum of dept summaries", s, None)
    # citywide lines vs sum of dept summaries + statement rows that have no departmental summary page
    if "all_funds" in stm:
        matched = {g["match"] for g in groups if not g["enterprise"] and g.get("match")}
        al_rows, al_cols = stm["all_funds"]
        gf_rows, gf_cols = stm["general_fund"]
        unmatched = [(i, lab) for i, (lab, v) in enumerate(al_rows) if lab not in matched and any(v)]
        sink.unmatched_statement_rows = [lab for i, lab in unmatched]
        for tb, kind, lab, vals in out:
            if not (lab.upper().startswith(("TOTAL", "GENERAL FUND"))):
                continue
            for ci, v in enumerate(vals):
                if v is None:
                    continue
                yb = (cols[ci][0], cols[ci][1])
                s0 = sums.get((tb, norm(lab)), {}).get(yb, 0)
                a_ci = next(i for i, c in enumerate(al_cols) if (c[0], c[1]) == yb)
                if lab.upper().startswith("GENERAL FUND"):
                    extra = sum((gf_rows[i][1][a_ci] or 0) for i, _ in unmatched)
                else:
                    extra = sum((al_rows[i][1][a_ci] or 0) for i, _ in unmatched)
                add_cross(sink, "tier2", "citywide line = sum of dept summaries + statement rows that have no departmental summary page "
                          f"({', '.join(l for _, l in unmatched)})", f"{lab} {yb[0]} {yb[1]}", "citywide", v, "sum + unmatched rows", s0 + extra)
    # pp.88-99 per-fund type tables summed over funds = citywide by-type lines
    agg = getattr(sink, "type_agg", {})
    for tb, kind, lab, vals in out:
        if tb != "by_type" or kind != "ROW":
            continue
        for ci, v in enumerate(vals):
            yb = (cols[ci][0], cols[ci][1])
            a = agg.get(norm(lab), {}).get(yb)
            if v is not None and a is not None:
                add_cross(sink, "tier2", "sum over pp.88-99 fund tables of a type row = p134 citywide line", f"{lab} {yb[0]} {yb[1]}",
                          "pp88-99", a, "p134", v, None)
    # citywide total vs p105
    tot = next((o for o in out if o[0] == "by_type" and o[1] == "TOTAL"), None)
    if tot and "all_funds" in stm:
        al_rows, al_cols = stm["all_funds"]
        tp = next((t for t in sink.totals if t["scope"] == "department_expenditures/all_funds"), None)
        for ci, v in enumerate(tot[3]):
            yb = (cols[ci][0], cols[ci][1])
            r = [t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == yb]
            if r and v is not None:
                add_cross(sink, "tier2", "citywide summary TOTAL EXPENDITURES = statement Total (all funds)", f"{yb[0]} {yb[1]}",
                          "p134", v, "statement", r[0]["amount"], None)
    # p87 funds vs p134 fund lines (matching by value, not name)
    s87 = sink.fund_summary.get("p87")
    if s87:
        body, totv, c87, p87 = s87
        fund_lines = [o for o in out if o[0] == "by_fund" and o[1] == "ROW"]
        for lab, vals in body:
            ok = False
            for o in fund_lines:
                if all((vals[ci] or 0) == (o[3][next(i for i, c in enumerate(cols) if (c[0], c[1]) == (c87[ci][0], c87[ci][1]))] or 0)
                       for ci in range(len(vals)) if c87[ci][2] is not None
                       and any((c[0], c[1]) == (c87[ci][0], c87[ci][1]) for c in cols)):
                    ok = True
                    add_cross(sink, "tier2", "p87 fund total matches a p134 fund line (all year columns, matched by value)", lab,
                              "p87", 1, "p134 line " + o[2], 1, p87)
                    break
            if not ok:
                sink.cross.append(dict(tier="tier2", check="p87 fund total matches a p134 fund line (all year columns, matched by value)",
                                       key=lab, a="p87", a_value=None, b="p134", b_value=None, diff=None, status="no_matching_line", page=p87))
    # p100-101 column totals (2019 adopted) vs p134 fund lines (by sequence order of columns)
    if funds_src:
        body, trow = funds_src
        fund_lines = [o for o in out if o[0] == "by_fund" and o[1] == "ROW"]
        ci19 = next((i for i, c in enumerate(cols) if (c[0], c[1]) == (2019, "adopted")), None)
        if trow and len(trow[0][1][1]) == len(fund_lines):
            for k, o in enumerate(fund_lines):
                add_cross(sink, "tier2", "p101 TOTAL EXPENDITURES by fund column = p134 fund line (2019 adopted; column order = p134 order)",
                          o[2], "p101", trow[0][1][1][k], "p134", o[3][ci19] or 0, trow[0][0])
            gfs = [(p, r) for p, r in body]
            for k in range(len(fund_lines)):
                s = sum(r[1][k] for _, r in gfs)
                add_cross(sink, "tier2", "p100-101 sum of department rows per fund column = TOTAL EXPENDITURES row", fund_lines[k][2],
                          "sum of rows", s, "printed total", trow[0][1][1][k], trow[0][0])


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


def spot_checks(sink, n_each=8, seed=2019):
    """Independent re-read: pick seeded-random output cells and look for the same number, on the same text
    line as the row's label, in pdfplumber's text extraction of the cited page (a different extraction
    engine and line-building algorithm from the PyMuPDF word clustering used above)."""
    import random
    import pdfplumber
    rng = random.Random(seed)
    sets = [("department_expenditures", sink.dept_exp, lambda r: r["dept_name"], False),
            ("revenues", sink.revenues, lambda r: r["line_name"], False),
            ("department_detail", sink.detail, lambda r: r["dim1"], False),
            ("positions", sink.positions, lambda r: r["position_title"] or r["program"], True)]
    out = []
    with pdfplumber.open(str(PDF)) as pdf:
        cache = {}
        for name, rows, labf, dec in sets:
            for r in rng.sample(rows, min(n_each, len(rows))):
                pg = r["page"]
                if pg not in cache:
                    cache[pg] = [clean(x) for x in (pdf.pages[pg - 1].extract_text() or "").split("\n")]
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
        "tier1": "citywide statements: department expenditures (general/other/all funds), revenues, fund summaries (pp.66, 73, 87, 88-99, 100-102)",
        "tier2": "department summaries by expenditure type and by fund (Budget Summary / Funding Source / DEPARTMENTAL BUDGET SUMMARY pages)",
        "tier3": "program-level detail (EXPENDITURE SUMMARY, PROGRAM DETAIL)",
        "tier4": "positions / FTE (PERSONNEL SUMMARY, personnel totals, p.106 schedule)"}
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
