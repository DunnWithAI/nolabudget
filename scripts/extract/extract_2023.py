#!/usr/bin/env python
"""Extract the City of New Orleans 2023 Adopted Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2023.py

Outputs (data/extracted/2023-operating-adopted/): department_expenditures.csv, revenues.csv,
department_detail.csv, positions.csv, workyears_citywide.csv, printed_totals.csv, checks.json
(see data/extracted/SCHEMA.md).

This book (and the 2022 and 2024 books, which print the identical layout) is a PDF export of the
budgeting system's tables.  The engine lives in this file and is parameterised by a Config, so that
extract_2024.py / extract_2022.py import it and only supply their own PDF path, book id and
book-specific quirks.

Method: every page is read as positioned words (PyMuPDF).  Words are clustered into text lines by y;
lines closer than a wrap-gap are merged into one "row" (a row label may wrap above and below the
line that carries its numbers).  Numbers are assigned to columns by x-position against the
Actual/Adopted/Proposed + year header found on the same page - never by sequence.  Tables are
located by their printed titles, not by fixed page numbers.  No numbers are typed into this script.

Layout:  1) helpers  2) page classification  3) tier parsers  4) checks  5) writers.
"""
import csv
import json
import random
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]

BASES = {"actual": "actual", "adopted": "adopted", "proposed": "proposed", "projected": "projected",
         "estimate": "estimate", "estimated": "estimate"}
DASH = object()
NUM_RE = re.compile(r"^\$?\(?-?\$?[\d,]*\.?\d+\)?%?$")
DASH_RE = re.compile(r"^\$?-$")
YEAR_RE = re.compile(r"^(?:FY)?(20[0-3]\d)$")
WRAP_GAP = 9.0          # lines whose centres are closer than this belong to the same wrapped row


@dataclass
class Config:
    pdf: Path
    book_id: str
    seed: int = 2023
    nonbreaking_fix: bool = True


# --------------------------------------------------------------------------- helpers
def clean(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def parse_num(tok):
    """int/float, DASH, or None (not numeric).  Percent tokens are not amounts."""
    t = tok.strip()
    if DASH_RE.match(t):
        return DASH
    if t.endswith("%") or not NUM_RE.match(t):
        return None
    neg = "(" in t or t.startswith("-") or t.startswith("$-")
    body = re.sub(r"[^\d.]", "", t)
    if body in ("", "."):
        return None
    v = float(body) if "." in body else int(body)
    return -v if neg else v


class W:
    __slots__ = ("x0", "y0", "x1", "y1", "t", "cx", "cy")

    def __init__(self, w):
        self.x0, self.y0, self.x1, self.y1 = w[0], w[1], w[2], w[3]
        self.t = clean(w[4])
        self.cx = (self.x0 + self.x1) / 2
        self.cy = (self.y0 + self.y1) / 2


class Row:
    """One logical table row: possibly several wrapped text lines."""

    def __init__(self, lines):
        self.lines = lines                                   # list of lists of W (top to bottom)
        self.words = [w for ln in lines for w in ln]
        self.y = sum(w.cy for w in self.words) / len(self.words)
        self.y0 = min(w.cy for w in self.words)
        self.y1 = max(w.cy for w in self.words)
        self.cells = []        # (col_idx, value, word)
        self.extras = []       # numeric words right of the first column that matched no column
        self.label_words = list(self.words)
        self.cols = None

    @property
    def text(self):
        return clean(" ".join(w.t for ln in self.lines for w in ln))

    @property
    def label(self):
        return clean(" ".join(w.t for w in self.label_words))

    def label_x0(self):
        return min((w.x0 for w in self.label_words), default=None)


def cluster_lines(words, tol=3.2):
    ws = sorted((w for w in words if w.t), key=lambda w: w.cy)
    groups = []
    for w in ws:
        if groups and abs(w.cy - groups[-1][-1].cy) <= tol:
            groups[-1].append(w)
        else:
            groups.append([w])
    return [sorted(g, key=lambda w: w.x0) for g in groups]


def build_rows(words, gap=WRAP_GAP):
    lines = cluster_lines(words)
    rows, cur = [], []
    prev = None
    for ln in lines:
        cy = sum(w.cy for w in ln) / len(ln)
        if cur and cy - prev > gap:
            rows.append(Row(cur))
            cur = []
        cur.append(ln)
        prev = cy
    if cur:
        rows.append(Row(cur))
    return rows


class Doc:
    def __init__(self, path):
        self.d = fitz.open(str(path))
        self.n = len(self.d)
        self._w, self._t = {}, {}

    def words(self, p):
        if p not in self._w:
            self._w[p] = [W(w) for w in self.d[p - 1].get_text("words") if clean(w[4])]
        return self._w[p]

    def lines(self, p):
        return cluster_lines(self.words(p))

    def line_texts(self, p):
        return [clean(" ".join(w.t for w in ln)) for ln in self.lines(p)]

    def text(self, p):
        if p not in self._t:
            self._t[p] = self.d[p - 1].get_text()
        return self._t[p]


# --------------------------------------------------------------------------- header / columns
@dataclass
class Col:
    year: int
    basis: str
    cx: float
    x1: float
    label: str = ""


def find_header(words, min_years=2, ymax=None, ymin=None):
    """Locate the column header on a page: lines with >= min_years year tokens that are well separated.
    The basis word (Actual/Adopted/Proposed) is the nearest one above (or on) the year token.
    Returns (list[Col], y_of_header_bottom) for the LAST such header block, or ([], None)."""
    lines = cluster_lines(words)
    best = None
    for ln in lines:
        cy = sum(w.cy for w in ln) / len(ln)
        if ymax is not None and cy > ymax:
            continue
        if ymin is not None and cy < ymin:
            continue
        yrs = [w for w in ln if YEAR_RE.match(w.t)]
        xs = sorted(w.cx for w in yrs)
        if len(yrs) < min_years or any(b - a < 25 for a, b in zip(xs, xs[1:])):
            continue
        cols = []
        for w in yrs:
            cand = None
            for b in words:
                if b.t.lower() in BASES and -2 <= (w.cy - b.cy) <= 30 and abs(b.cx - w.cx) <= 45:
                    d = abs(b.cx - w.cx) + abs(b.cy - w.cy) * 0.5
                    if cand is None or d < cand[0]:
                        cand = (d, b)
            if cand is None:
                continue
            b = cand[1]
            x0 = min(w.x0, b.x0)
            x1 = max(w.x1, b.x1)
            cols.append(Col(int(YEAR_RE.match(w.t).group(1)), BASES[b.t.lower()], (x0 + x1) / 2, x1))
        if len(cols) >= min_years:
            cols.sort(key=lambda c: c.cx)
            best = (cols, cy)
            if ymin is not None:       # first header after ymin
                return best
    return best if best else ([], None)


def assign_row(row, cols, tol_frac=0.42):
    """Assign numeric words in `row` to header columns by x position (centre or right edge).
    Words left of the first column's zone stay in the label; numeric words to the right that match no
    column are `extras` (change / percent columns) and are removed from the label."""
    row.cols = cols
    row.cells, row.extras = [], []
    if not cols:
        return
    sp = min([b.cx - a.cx for a, b in zip(cols, cols[1:])] or [80])
    tol = tol_frac * sp
    first = cols[0].cx - 0.75 * sp
    used = set()
    taken = set()
    cand = []
    for w in row.words:
        v = parse_num(w.t)
        pct = (w.t.endswith("%") and NUM_RE.match(w.t)) or re.match(r"^-?%$", w.t)
        if v is None and not pct:
            continue
        if w.cx < first:
            continue
        if pct:
            taken.add(id(w))
            continue
        costs = [(min(abs(w.cx - c.cx), abs(w.x1 - c.x1)), i) for i, c in enumerate(cols)]
        cost, ci = min(costs)
        cand.append((cost, ci, v, w))
    for cost, ci, v, w in sorted(cand, key=lambda t: t[0]):
        if cost <= tol and ci not in used:
            used.add(ci)
            row.cells.append((ci, v, w))
            taken.add(id(w))
        else:
            row.extras.append(w)
            taken.add(id(w))
    row.cells.sort(key=lambda c: c[0])
    row.label_words = [w for w in row.words if id(w) not in taken]


def cell_vals(row, ncols):
    vals = [None] * ncols
    for ci, v, _ in row.cells:
        vals[ci] = None if v is DASH else v
    return vals


FOOTER_RE = re.compile(r"^Page \d+$")


def table_rows(doc, p, cols, ytop, drop_footer=True, gap=WRAP_GAP):
    """Rows below ytop with cells assigned; footer 'Page N' removed."""
    ws = [w for w in doc.words(p) if w.cy > ytop + 2]
    rows = build_rows(ws, gap)
    out = []
    for r in rows:
        if drop_footer and FOOTER_RE.match(r.text):
            continue
        assign_row(r, cols)
        out.append(r)
    return out


# --------------------------------------------------------------------------- result sink + checks
def status_of(diff):
    if diff == 0:
        return "exact"
    return "within_1" if abs(diff) <= 1 else "mismatch"


class Sink:
    def __init__(self, book_id):
        self.book_id = book_id
        self.dept_exp, self.revenues, self.detail, self.positions, self.totals = [], [], [], [], []
        self.workyears = []
        self.by_fund = []
        self.checks, self.cross = [], []
        self.unparsed, self.notes, self.not_attempted = [], [], []
        self.duplicate_pages = []
        self.pages = {}
        self.page_types = {}

    def total(self, year, basis, scope, label, amount, page):
        self.totals.append(dict(book_id=self.book_id, fiscal_year=year, basis=basis, scope=scope,
                                label=label, amount=amount, page=page))

    def check(self, tier, scope, label, page, year, basis, printed, summed, method, extra=None):
        if isinstance(printed, float) or isinstance(summed, float):
            printed, summed = round(printed, 2), round(summed, 2)
        diff = round(printed - summed, 2)
        rec = dict(tier=tier, scope=scope, label=label, page=page, fiscal_year=year, basis=basis,
                   printed=printed, extracted_sum=summed, diff=diff, status=status_of(diff), method=method)
        if extra:
            rec.update(extra)
        self.checks.append(rec)

    def xcheck(self, tier, name, key, a_name, a, b_name, b, page=None):
        if isinstance(a, float) or isinstance(b, float):
            a, b = round(a, 2), round(b, 2)
        diff = round(a - b, 2)
        self.cross.append(dict(tier=tier, check=name, key=key, a=a_name, a_value=a, b=b_name, b_value=b,
                               diff=diff, status=status_of(diff), page=page))


# --------------------------------------------------------------------------- generic table utilities
def first_rows_text(doc, p, ymax=130):
    return build_rows([w for w in doc.words(p) if w.cy < ymax])


def row_vals(row, ncols):
    """Per-column values: None = no printed cell, DASH kept as 0 only by callers that need it."""
    v = [None] * ncols
    for ci, val, _ in row.cells:
        v[ci] = val
    return v


def pv(v):
    """A printed amount (not blank, not a dash)."""
    return v is not None and v is not DASH


def num0(v):
    return 0 if v is None or v is DASH else v


def total_label(lab):
    return bool(re.search(r"\bTotal\b|Sub-Total|Sub-total", lab))


def emit(sink_list, **kw):
    sink_list.append(kw)


def sum_check_rows(rows_vals, ncols):
    s = [0] * ncols
    for v in rows_vals:
        for i in range(ncols):
            s[i] += num0(v[i])
    return s


def stack_match(stack, printed, grand=False):
    """Smallest k such that the last k stack items (markers count 0) sum to the printed total in every
    column where something was printed.  Returns (k, sums) or (None, sums_of_all_since_last_marker)."""
    ncol = len(printed)
    run = [0] * ncol
    for k in range(1, len(stack) + 1):
        it = stack[-k]
        for c in range(ncol):
            run[c] += it["vals"][c]
        if all((printed[c] is None) or run[c] == num0(printed[c]) for c in range(ncol)) and \
                any(printed[c] is not None for c in range(ncol)) and not it.get("marker"):
            return k, list(run)
    # no exact match: report the sum back to the nearest marker (or, for a grand total, of the whole statement)
    run = [0] * ncol
    k = 0
    for it in reversed(stack):
        if it.get("marker") and not grand:
            break
        k += 1
        for c in range(ncol):
            run[c] += it["vals"][c]
    return None, run, max(k, 1)


# --------------------------------------------------------------------------- tier 1: revenues
def parse_revenues(doc, sink):
    pages = [p for p in range(1, doc.n + 1)
             if re.match(r"^Statements? of Revenues$", (first_rows_text(doc, p, 60)[0].text if first_rows_text(doc, p, 60) else ""))]
    if not pages:
        sink.unparsed.append(dict(what="revenue statement pages not found"))
        return None
    sink.pages["revenues"] = pages
    stacks = {"general_fund": [], "other_funds": []}
    cur_heading, heading_y, last_heading_row = "", None, None
    tot = {}
    memo = []
    for p in pages:
        top = [r.text for r in first_rows_text(doc, p, 100)]
        fund_group = "general_fund" if any(t == "General Fund" for t in top[:3]) else "other_funds"
        cols, y = find_header(doc.words(p), min_years=3)
        if not cols:
            sink.unparsed.append(dict(page=p, what="revenue page without year header"))
            continue
        ncol = len(cols)
        stack = stacks[fund_group]
        cat = ""
        heading, heading_y = "", None
        for r in table_rows(doc, p, cols, y):
            lab = r.label
            if not r.cells:
                if not lab or lab.lower() in ("source",):
                    continue
                if fund_group == "general_fund":
                    cat = lab
                    stack.append(dict(vals=[0] * ncol, marker=True))
                else:
                    if heading and heading_y is not None and r.y - heading_y <= 15:
                        heading = clean(heading + " " + lab)
                    else:
                        heading = lab
                    heading_y = r.y
                    stack.append(dict(vals=[0] * ncol, marker=True))
                continue
            vals = row_vals(r, ncol)
            if total_label(lab):
                scope = "revenues/General Fund" if fund_group == "general_fund" else "revenues/Other Funds"
                for ci, c in enumerate(cols):
                    if pv(vals[ci]):
                        sink.total(c.year, c.basis, scope, lab, num0(vals[ci]), p)
                if re.match(r"Total (Discretionary Revenue|All Funds)", lab, re.I):
                    memo.append((lab, vals, cols, p, scope))
                    continue
                res = stack_match(stack, vals, grand=bool(re.search(r"(General Fund|Other Funds)$", lab)))
                if res[0] is not None:
                    k, sums = res
                    method = f"sum of last {k} item(s) on the statement's running stack (rows and sub-totals not yet rolled into a total)"
                else:
                    _, sums, k = res
                    method = f"no combination of the preceding rows/sub-totals reproduces the printed total; showing sum of the {k} rows since the section heading"
                for ci, c in enumerate(cols):
                    if vals[ci] is not None:
                        sink.check("tier1", scope, lab, p, c.year, c.basis, num0(vals[ci]), sums[ci], method, dict(items=k))
                del stack[-k:]
                stack.append(dict(vals=[num0(v) for v in vals], label=lab, total=True))
                tot[(fund_group, lab)] = (vals, cols, p)
                heading = "" if fund_group == "other_funds" else heading
                continue
            fname = "General Fund" if fund_group == "general_fund" else (heading or lab)
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.revenues.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis,
                                              fund_group=fund_group, fund_name=fname,
                                              category=cat if fund_group == "general_fund" else "",
                                              line_name=lab, amount=num0(vals[ci]), page=p))
            stack.append(dict(vals=[num0(v) for v in vals], label=lab))
    # memo totals
    def find(prefix, fg=None):
        for (g, lab), v in tot.items():
            if re.match(prefix, lab, re.I) and (fg is None or g == fg):
                return v
        return None
    gft, oft, sg = find(r"Total General Fund", "general_fund"), find(r"Total Other Funds", "other_funds"), find(r"Total Self-Generated")
    for lab, vals, cols, p, scope in memo:
        for ci, c in enumerate(cols):
            if vals[ci] is None:
                continue
            if re.match(r"Total All Funds", lab, re.I) and gft and oft:
                s = num0(gft[0][ci]) + num0(oft[0][ci])
                sink.check("tier1", scope, lab, p, c.year, c.basis, num0(vals[ci]), s, "Total General Fund + Total Other Funds (as printed)")
            elif re.match(r"Total Discretionary", lab, re.I) and gft:
                s = num0(gft[0][ci]) + (num0(sg[0][ci]) if sg else 0)
                sink.check("tier1", scope, lab, p, c.year, c.basis, num0(vals[ci]), s,
                           "Total General Fund + printed Total Self-Generated (memo total outside the Other Funds stack)")
    return dict(gf=gft, other=oft, pages=pages)


# --------------------------------------------------------------------------- tier 1: department statements
STMT_RE = [(r"^Total Departmental Expenditures\s*-\s*General Fund$", "general_fund"),
           (r"^Total Departmental Expenditures\s*-\s*Non-General Fund$", "other_funds"),
           (r"^Total Expenditures by Department$", "all_funds")]


def parse_dept_statements(doc, sink, p_lo, p_hi):
    """Statement pages are located by title; untitled continuation pages (header 'Department ...')
    belong to the statement above them."""
    cur = None
    stm = {}                       # fund_group -> dict(rows=[(label, vals)], cols, total, pages)
    pages = []
    for p in range(p_lo, p_hi + 1):
        rows0 = first_rows_text(doc, p, 100)
        title = rows0[0].text if rows0 else ""
        hit = next((g for rx, g in STMT_RE if re.match(rx, title)), None)
        if hit:
            cur = hit
            stm[cur] = dict(rows=[], cols=None, total=None, pages=[])
        elif cur is None or not (title.startswith("Department") or title.startswith("Actual")):
            if cur is not None and stm[cur]["total"] is not None:
                continue
            continue
        if cur is None:
            continue
        cols, y = find_header(doc.words(p), min_years=3)
        if not cols:
            sink.unparsed.append(dict(page=p, what="department statement page without header"))
            continue
        s = stm[cur]
        s["pages"].append(p)
        s["cols"] = s["cols"] or cols
        for r in table_rows(doc, p, cols, y):
            if not r.cells:
                continue
            lab = r.label
            vals = row_vals(r, len(cols))
            if re.match(r"^Total Expenditures$", lab):
                s["total"] = (vals, p)
            else:
                s["rows"].append((lab, vals, p))
    for g, s in stm.items():
        cols = s["cols"]
        sink.pages[f"dept_statement/{g}"] = s["pages"]
        for lab, vals, p in s["rows"]:
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.dept_exp.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, fund_group=g,
                                              dept_code="", dept_name=lab, amount=num0(vals[ci]), page=p))
        if s["total"]:
            tv, tp = s["total"]
            for ci, c in enumerate(cols):
                if pv(tv[ci]):
                    sink.total(c.year, c.basis, f"department_expenditures/{g}", "Total Expenditures", num0(tv[ci]), tp)
                    sink.check("tier1", f"department_expenditures/{g}", "Total Expenditures", tp, c.year, c.basis,
                               num0(tv[ci]), sum(num0(v[ci]) for _, v, _ in s["rows"]), "sum of all department rows in the statement")
        else:
            sink.unparsed.append(dict(what=f"no Total Expenditures row found for statement {g}"))
    # general + other = all, per department and year
    if all(k in stm for k in ("general_fund", "other_funds", "all_funds")):
        cols = stm["all_funds"]["cols"]
        d = {g: {lab: (vals, p) for lab, vals, p in stm[g]["rows"]} for g in stm}
        names = list(d["all_funds"].keys())
        for g in ("general_fund", "other_funds"):
            for lab in d[g]:
                if lab not in d["all_funds"]:
                    sink.unparsed.append(dict(what=f"department {lab!r} in {g} statement is not in all-funds statement"))
        for lab in names:
            av, ap = d["all_funds"][lab]
            gv = d["general_fund"].get(lab, ([None] * len(cols), None))[0]
            ov = d["other_funds"].get(lab, ([None] * len(cols), None))[0]
            for ci, c in enumerate(cols):
                sink.xcheck("tier1", "general_fund + other_funds = all_funds (per department)", f"{lab} | {c.year} {c.basis}",
                            "general+other", num0(gv[ci]) + num0(ov[ci]), "all_funds", num0(av[ci]), ap)
        # totals
        for ci, c in enumerate(cols):
            ts = {g: (stm[g]["total"][0][ci] if stm[g]["total"] else None) for g in stm}
            if all(v is not None for v in ts.values()):
                sink.xcheck("tier1", "printed total: general + other = all funds", f"{c.year} {c.basis}", "general+other",
                            num0(ts["general_fund"]) + num0(ts["other_funds"]), "all_funds", num0(ts["all_funds"]),
                            stm["all_funds"]["total"][1])
    return stm


# --------------------------------------------------------------------------- tier 1: citywide fund tables
def parse_fund_tables(doc, sink, p_lo, p_hi):
    """'Total Revenues by Fund', 'Total Expenditures by Fund Source', 'Total Expenditures by Type - <fund>'.
    A table may continue on an untitled page that repeats only the column header.
    Output rows go to citywide_by_fund.csv (extra file)."""
    out = {"fund_source": None, "by_type": {}, "rev_by_fund": None}
    cur = None

    def finish(rec):
        if rec is None:
            return
        kind, fund, rows, total, cols, pages = rec["kind"], rec["fund"], rec["rows"], rec["total"], rec["cols"], rec["pages"]
        for lab, vals, p in rows:
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.by_fund.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, table=kind,
                                             fund_name=fund or lab, line_name=lab if fund else "", amount=num0(vals[ci]), page=p))
        scope = f"citywide_by_fund/{kind}" + (f"/{fund}" if fund else "")
        if total:
            for ci, c in enumerate(cols):
                if pv(total[1][ci]):
                    sink.total(c.year, c.basis, scope, total[0], num0(total[1][ci]), total[2])
                    sink.check("tier1", scope, total[0], total[2], c.year, c.basis, num0(total[1][ci]),
                               sum(num0(v[ci]) for _, v, _ in rows), "sum of the rows of the table")
        else:
            sink.unparsed.append(dict(page=pages[0], what=f"{kind} {fund}: no total row"))
        r2 = dict(page=pages[0], rows=[(l, v) for l, v, _ in rows], total=(total[0], total[1]) if total else None, cols=cols, fund=fund)
        if kind == "expenditures_by_fund_source":
            out["fund_source"] = r2
        elif kind == "revenues_by_fund":
            out["rev_by_fund"] = r2
        else:
            out["by_type"][fund] = r2

    for p in range(p_lo, p_hi + 1):
        rows0 = first_rows_text(doc, p, 100)
        if not rows0:
            continue
        title = rows0[0].text
        m_type = re.match(r"^Total Expenditures by Type - (.+)$", title)
        kind = None
        if re.match(r"^Total Expenditures by Fund Source$", title):
            kind = "expenditures_by_fund_source"
        elif m_type:
            kind = "expenditures_by_type"
        elif re.match(r"^Total Revenues by Fund$", title):
            kind = "revenues_by_fund"
        if kind:
            finish(cur)
            cur = dict(kind=kind, fund=clean(m_type.group(1)) if m_type else "", rows=[], total=None, cols=None, pages=[p])
        elif cur is not None and cur["total"] is None and re.match(r"^(Fund|Type|Expenditure Type|Dollar|Percent|Actual)\b", title):
            cur["pages"].append(p)
        else:
            continue
        cols, y = find_header(doc.words(p), min_years=3)
        if not cols:
            sink.unparsed.append(dict(page=p, what=f"{title}: no header"))
            continue
        cur["cols"] = cur["cols"] or cols
        for r in table_rows(doc, p, cols, y):
            if not r.cells:
                continue
            lab = r.label
            vals = row_vals(r, len(cols))
            if re.match(r"^Total (Expenditures|Funding)$", lab):
                cur["total"] = (lab, vals, p)
            else:
                cur["rows"].append((lab, vals, p))
    finish(cur)
    return out


def parse_personnel_schedule(doc, sink, p_lo, p_hi):
    res = {}
    cur = None
    for p in range(p_lo, p_hi + 1):
        rows0 = first_rows_text(doc, p, 100)
        if not rows0:
            continue
        m = re.match(r"^Detailed Personnel Schedule by Department\s*-\s*(General Fund|Non-General Fund)$", rows0[0].text)
        m2 = re.match(r"^Total Detailed Personnel Schedule by Department$", rows0[0].text)
        if not (m or m2):
            continue
        g = "general_fund" if m and m.group(1) == "General Fund" else ("other_funds" if m else "all_funds")
        cols, y = find_header(doc.words(p), min_years=2)
        if not cols:
            sink.unparsed.append(dict(page=p, what="personnel schedule without header"))
            continue
        rows, total = [], None
        for r in table_rows(doc, p, cols, y):
            if not r.cells:
                continue
            vals = row_vals(r, len(cols))
            if r.label == "Total":
                total = vals
            else:
                rows.append((r.label, vals))
        for lab, vals in rows:
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.workyears.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, fund_group=g,
                                               dept_name=lab, fte=num0(vals[ci]), page=p))
        for ci, c in enumerate(cols):
            if total and pv(total[ci]):
                sink.total(c.year, c.basis, f"workyears_citywide/{g}", "Total", num0(total[ci]), p)
                sink.check("tier4", f"workyears_citywide/{g}", "Total", p, c.year, c.basis, num0(total[ci]),
                           sum(num0(v[ci]) for _, v in rows), "sum of department rows on page")
        res[g] = dict(rows=dict(rows), cols=cols, total=total, page=p)
    if len(res) == 3:
        cols = res["all_funds"]["cols"]
        for lab, av in res["all_funds"]["rows"].items():
            gv = res["general_fund"]["rows"].get(lab, [None] * len(cols))
            ov = res["other_funds"]["rows"].get(lab, [None] * len(cols))
            for ci, c in enumerate(cols):
                sink.xcheck("tier4", "personnel schedule: general + non-general = total (per department)", f"{lab} | {c.year} {c.basis}",
                            "general+other", num0(gv[ci]) + num0(ov[ci]), "total", num0(av[ci]), res["all_funds"]["page"])
    sink.pages["personnel_schedule"] = {g: v["page"] for g, v in res.items()}
    return res


# --------------------------------------------------------------------------- department sections: classification
TITLE_RE = re.compile(r"^(Budget Summary|Expenditure by Type|Expenditures by Funding Source)\s+-\s+(.+)$")
PROG_TYPES = ("PROGRAM DETAIL", "EXPENDITURE SUMMARY", "PERSONNEL SUMMARY")
SUBTITLES = {"expenditure by type", "expenditures by funding source"}


def classify(doc, p):
    """Return None or dict(kind, name, group). kinds: btype, bfund, pdet, esum, pers."""
    rows = first_rows_text(doc, p, 130)
    if not rows:
        return None
    ti = 0
    m = TITLE_RE.match(rows[0].text)
    if not m and len(rows) > 1 and not re.match(r"^(Dollar|Percent|Actual|Expenditure Type|Funding Source|Change)", rows[0].text):
        # 2024 variant: a department banner line precedes the '<table> - <department>' title line
        m2 = TITLE_RE.match(rows[1].text)
        if m2 and rows[1].y0 - rows[0].y1 <= 40:
            m, ti = m2, 1
    if m:
        name = m.group(2)
        if len(rows) > ti + 1 and rows[ti + 1].y0 - rows[ti].y1 <= 22 and rows[ti + 1].text.lower() not in SUBTITLES \
                and not re.match(r"^(Dollar|Percent|Actual|Expenditure Type|Funding Source|Change)", rows[ti + 1].text):
            name = name + " " + rows[ti + 1].text
        kind = "bfund" if m.group(1).startswith("Expenditures by Funding") else "btype"
        return dict(kind=kind, name=clean(name), group=None, title=m.group(1))
    top = build_rows([w for w in doc.words(p) if w.cy < 62])
    if not top:
        return None
    txt = clean(" ".join(r.text for r in top))
    mt = re.match(r"^(.+?)\s*(PROGRAM DETAIL|EXPENDITURE SUMMARY|PERSONNEL SUMMARY)$", txt)
    if not mt:
        return None
    kind = {"PROGRAM DETAIL": "pdet", "EXPENDITURE SUMMARY": "esum", "PERSONNEL SUMMARY": "pers"}[mt.group(2)]
    first = cluster_lines([w for w in doc.words(p) if w.cy < 62])[0]
    groups, cur = [], [first[0]]
    for a, b in zip(first, first[1:]):
        if b.x0 - a.x1 > 22:
            groups.append(cur)
            cur = [b]
        else:
            cur.append(b)
    groups.append(cur)
    gt = [clean(" ".join(w.t for w in g)) for g in groups]
    if len(gt) >= 3 or (len(gt) == 2 and not gt[-1].startswith(("EXPENDITURE", "PROGRAM", "PERSONNEL"))):
        group, sub = gt[0], gt[1]
    else:
        body = mt.group(1)
        mm = re.match(r"^([A-Z0-9 &'/.,\-]*)([A-Z][a-z].*)$", body)
        group, sub = (clean(mm.group(1)), clean(mm.group(2))) if mm else (body, body)
    return dict(kind=kind, name=sub, group=group)


def page_signature(doc, p):
    t = doc.text(p)
    return re.sub(r"Page \d+", "", clean(t))


def norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------------------- tier 2: by_type / by_fund
def parse_type_fund(doc, p, sink, info, st):
    kind, name = info["kind"], info["name"]
    cols, y = find_header(doc.words(p), min_years=3)
    if not cols:
        sink.unparsed.append(dict(page=p, what=f"{kind} {name}: no year header"))
        return None
    ncol = len(cols)
    table = "by_type" if kind == "btype" else "by_fund"
    rows, total, fte = [], None, None
    for r in table_rows(doc, p, cols, y):
        if not r.cells:
            continue
        lab = r.label
        vals = row_vals(r, ncol)
        if re.match(r"^Total (Expenditures|Funding)$", lab):
            total = vals
        elif re.match(r"^Department FTEs?$", lab):
            fte = vals
        else:
            rows.append((lab, vals))
    scope = f"department_detail/{name}/{table}"
    for lab, vals in rows:
        for ci, c in enumerate(cols):
            if pv(vals[ci]):
                sink.detail.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, dept_code="", dept_name=name,
                                        table=table, dim1=lab, dim2="", amount=num0(vals[ci]), page=p))
    if total is None:
        sink.unparsed.append(dict(page=p, what=f"{table} {name}: no total row"))
    else:
        for ci, c in enumerate(cols):
            if pv(total[ci]):
                sink.total(c.year, c.basis, scope, "Total Expenditures" if kind == "btype" else "Total Funding", num0(total[ci]), p)
                sink.check("tier2", scope, "Total", p, c.year, c.basis, num0(total[ci]),
                           sum(num0(v[ci]) for _, v in rows), "sum of the rows on the page")
    if fte is not None:
        for ci, c in enumerate(cols):
            if pv(fte[ci]):
                sink.total(c.year, c.basis, scope, "Department FTEs", num0(fte[ci]), p)
    return dict(kind=kind, name=name, page=p, cols=cols, total=total, rows=rows, fte=fte)


# --------------------------------------------------------------------------- tier 3: EXPENDITURE SUMMARY
FUND_X, PROG_X = 48, 75
PROG_GAP = 13.0          # program pages: row pitch is >= 16, wrapped label lines sit 12 apart


def code_name(label):
    return clean(label)


def parse_esum(doc, p, sink, info, st):
    """Program x fund x year.  st carries the current fund across pages of one section."""
    cols, y = find_header(doc.words(p), min_years=3)
    if not cols:
        sink.unparsed.append(dict(page=p, what=f"esum {info['name']}: no header"))
        return
    ncol = len(cols)
    name = info["name"]
    scope = f"department_detail/{name}/by_program"
    for r in table_rows(doc, p, cols, y, gap=PROG_GAP):
        lab = r.label
        x0 = r.label_x0()
        if not lab or x0 is None:
            continue
        vals = row_vals(r, ncol) if r.cells else None
        if re.match(r"^DEPARTMENT TOTAL$", lab):
            st["dept_total"] = dict(vals=vals, cols=cols, page=p)
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.total(c.year, c.basis, scope, "DEPARTMENT TOTAL", num0(vals[ci]), p)
            _finish_esum_dept(sink, st, name, scope, cols, p)
            continue
        if x0 < FUND_X:
            if vals is None:
                st["fund"] = lab
                st["fund_rows"] = []
                continue
            if lab.endswith(" Total"):
                f = lab[:-6]
                for ci, c in enumerate(cols):
                    if pv(vals[ci]):
                        sink.total(c.year, c.basis, scope, lab, num0(vals[ci]), p)
                        sink.check("tier3", scope, lab, p, c.year, c.basis, num0(vals[ci]),
                                   sum(num0(v[ci]) for v in st["fund_rows"]), "sum of the program rows under this fund heading")
                if st.get("fund") != f:
                    sink.unparsed.append(dict(page=p, what=f"{name}: fund total {lab!r} but current heading is {st.get('fund')!r}"))
                st.setdefault("fund_totals", []).append(vals)
                st["fund"], st["fund_rows"] = None, []
                continue
        if vals is None:
            # program heading in an expenditure summary shouldn't occur
            sink.unparsed.append(dict(page=p, what=f"{name}: unexpected label-only row {lab!r}"))
            continue
        if st.get("fund") is None:
            sink.unparsed.append(dict(page=p, what=f"{name}: program row {lab!r} without a fund heading"))
            continue
        st["fund_rows"].append(vals)
        for ci, c in enumerate(cols):
            if pv(vals[ci]):
                sink.detail.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, dept_code="", dept_name=name,
                                        table="by_program", dim1=lab, dim2=st["fund"], amount=num0(vals[ci]), page=p))


def _finish_esum_dept(sink, st, name, scope, cols, p):
    ft = st.get("fund_totals", [])
    dt = st["dept_total"]["vals"]
    for ci, c in enumerate(cols):
        if pv(dt[ci]):
            sink.check("tier3", scope, "DEPARTMENT TOTAL", p, c.year, c.basis, num0(dt[ci]),
                       sum(num0(v[ci]) for v in ft), "sum of the printed fund totals (… Total rows) of this sub-department")
    st["esum_done"] = dict(vals=dt, cols=cols, page=p)
    st["fund_totals"] = []


# --------------------------------------------------------------------------- tier 3: PROGRAM DETAIL
def pdet_columns(doc, p):
    ws = [w for w in doc.words(p) if 54 < w.cy < 80]
    def find(word, after=None):
        c = [w for w in ws if w.t == word]
        return c
    cols = []
    names = [("Personal Services", "Services"), ("Other Operating", "Operating"), ("Debt Service", "Service"), ("Total", "Total")]
    sv = sorted([w for w in ws if w.t == "Services"], key=lambda w: w.x0)
    op = [w for w in ws if w.t == "Operating"]
    sr = sorted([w for w in ws if w.t == "Service"], key=lambda w: w.x0)
    to = [w for w in ws if w.t == "Total"]
    if not (sv and op and sr and to):
        return None
    return [("Personal Services", sv[0].x1), ("Other Operating", op[0].x1), ("Debt Service", sr[0].x1), ("Total", max(to, key=lambda w: w.x0).x1)]


def parse_pdet(doc, p, sink, info, st, year, basis):
    hc = pdet_columns(doc, p)
    if not hc:
        sink.unparsed.append(dict(page=p, what=f"pdet {info['name']}: no column header"))
        return
    name = info["name"]
    scope = f"department_detail/{name}/by_program_type"
    ws = [w for w in doc.words(p) if w.cy > 76]
    for r in build_rows(ws, PROG_GAP):
        if FOOTER_RE.match(r.text):
            continue
        cells, lw = [], []
        for w in r.words:
            v = parse_num(w.t)
            if v is not None and w.cx > 380:
                ci = min(range(4), key=lambda i: abs(w.x1 - hc[i][1]))
                if abs(w.x1 - hc[ci][1]) <= 30:
                    cells.append((ci, v))
                    continue
            lw.append(w)
        lab = clean(" ".join(w.t for w in lw))
        x0 = lw[0].x0 if lw else None
        vals = [None] * 4
        for ci, v in cells:
            vals[ci] = v
        if not lab:
            if cells and st.get("pending_total"):
                lab = st["pending_total"] + " Total"
                x0 = 0
                st["pending_total"] = None
                sink.notes.append(f"p.{p}: {name}: fund total row printed without its label; label {lab!r} taken from the preceding fused line")
            else:
                if cells:
                    sink.unparsed.append(dict(page=p, what=f"{name}: pdet numbers without a label"))
                continue
        elif st.get("fund") and x0 < FUND_X and lab.endswith(" " + st["fund"] + " Total") and lab != st["fund"] + " Total":
            # glitch: program row and the fund-total label are printed on one line
            lab = lab[:-(len(st["fund"]) + 7)]
            x0 = PROG_X - 1
            st["pending_total"] = st["fund"]
            sink.notes.append(f"p.{p}: {name}: program row {lab!r} printed fused with the label of the fund total that follows")
        if lab == "DEPARTMENT TOTAL":
            st["dept_total"] = vals
            for ci in range(4):
                if pv(vals[ci]):
                    sink.total(year, basis, scope, f"DEPARTMENT TOTAL | {hc[ci][0]}", num0(vals[ci]), p)
                    sink.check("tier3", scope, f"DEPARTMENT TOTAL | {hc[ci][0]}", p, year, basis, num0(vals[ci]),
                               sum(num0(v[ci]) for v in st.get("fund_totals", [])), "sum of the printed fund totals of this sub-department")
            st["pdet_done"] = dict(vals=vals, page=p)
            st["fund_totals"] = []
            continue
        if x0 < FUND_X:
            if not cells:
                st["fund"], st["fund_rows"] = lab, []
                continue
            if lab.endswith(" Total"):
                for ci in range(4):
                    if pv(vals[ci]):
                        sink.total(year, basis, scope, f"{lab} | {hc[ci][0]}", num0(vals[ci]), p)
                        if ci < 3:
                            sink.check("tier3", scope, f"{lab} | {hc[ci][0]}", p, year, basis, num0(vals[ci]),
                                       sum(num0(v[ci]) for v in st["fund_rows"]), "sum of the program rows under this fund heading")
                        else:
                            sink.check("tier3", scope, f"{lab} | Total", p, year, basis, num0(vals[3]),
                                       sum(num0(vals[i]) for i in range(3)), "sum of the three type columns of the printed fund total")
                if st.get("fund") != lab[:-6]:
                    sink.unparsed.append(dict(page=p, what=f"{name}: pdet fund total {lab!r} but heading {st.get('fund')!r}"))
                st.setdefault("fund_totals", []).append(vals)
                st["fund"], st["fund_rows"] = None, []
                continue
        if not cells:
            sink.unparsed.append(dict(page=p, what=f"{name}: pdet label-only row {lab!r}"))
            continue
        if st.get("fund") is None:
            sink.unparsed.append(dict(page=p, what=f"{name}: pdet program row {lab!r} without fund heading"))
            continue
        st["fund_rows"].append(vals)
        for ci in range(3):
            if pv(vals[ci]):
                sink.detail.append(dict(book_id=sink.book_id, fiscal_year=year, basis=basis, dept_code="", dept_name=name,
                                        table="by_program_type", dim1=lab, dim2=f"{st['fund']} | {hc[ci][0]}", amount=num0(vals[ci]), page=p))
        if pv(vals[3]):
            sink.total(year, basis, scope, f"{st['fund']} | {lab} | Total", num0(vals[3]), p)
            sink.check("tier3", scope, f"{st['fund']} | {lab} | Total", p, year, basis, num0(vals[3]),
                       sum(num0(vals[i]) for i in range(3)), "sum of the three type columns on the program row")


# --------------------------------------------------------------------------- tier 4: PERSONNEL SUMMARY
GRADE_RE = re.compile(r"^[A-Z]{0,4}\d{1,4}[A-Z]?$")


def _flush_pos(sink, st, name):
    """Emit pending position records whose fund and program are both known."""
    keep = []
    for rec in st["pend"]:
        if rec["fund"] is None or rec["prog"] is None:
            keep.append(rec)
            continue
        for ci, c in enumerate(rec["cols"]):
            if pv(rec["vals"][ci]):
                sink.positions.append(dict(book_id=sink.book_id, fiscal_year=c.year, basis=c.basis, dept_code="", dept_name=name,
                                           program=rec["prog"], fund=rec["fund"], position_title=rec["title"],
                                           pay_grade=rec["grade"], fte=num0(rec["vals"][ci]), page=rec["page"]))
    st["pend"] = keep


def parse_pers(doc, p, sink, info, st):
    """PERSONNEL SUMMARY: fund heading > program heading > position rows > '<program> Total' > '<fund> Total'.
    The book sometimes omits a fund / program heading (or a position title); position rows seen before their
    heading are held and attached when the matching '… Total' row names the program / fund."""
    cols, y = find_header(doc.words(p), min_years=3)
    if not cols:
        sink.unparsed.append(dict(page=p, what=f"pers {info['name']}: no header"))
        return
    ncol = len(cols)
    name = info["name"]
    scope = f"positions/{name}"
    for k, v in (("pend", []), ("prog_rows", []), ("fund_progs", []), ("fund_totals", [])):
        st.setdefault(k, v)
    st.setdefault("fund", None)
    st.setdefault("prog", None)
    pg = [w for w in doc.words(p) if w.t == "Pay" and 54 < w.cy < 80]
    pg_x0 = pg[0].x0 - 15 if pg else None
    for r in table_rows(doc, p, cols, y, gap=PROG_GAP):
        lab = r.label
        vals = row_vals(r, ncol) if r.cells else None
        x0 = r.label_words[0].x0 if r.label_words else None
        if lab == "DEPARTMENT TOTAL":
            _flush_pos(sink, st, name)
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.total(c.year, c.basis, scope, lab, num0(vals[ci]), p)
                    sink.check("tier4", scope, lab, p, c.year, c.basis, num0(vals[ci]),
                               sum(num0(v[ci]) for v in st["fund_totals"]), "sum of the printed fund totals of this sub-department")
            if st["pend"]:
                sink.unparsed.append(dict(page=p, what=f"{name}: {len(st['pend'])} position rows never attached to a fund/program"))
            st["pers_done"] = dict(vals=vals, cols=cols, page=p)
            st["fund_totals"], st["pend"] = [], []
            continue
        if x0 is not None and x0 < FUND_X and not (vals is not None and not lab.endswith(" Total")):
            if vals is None:
                st["fund"], st["prog"], st["fund_progs"], st["prog_rows"] = lab, None, [], []
                continue
            F = lab[:-6]
            if st["fund"] is None:
                for rec in st["pend"]:
                    if rec["fund"] is None:
                        rec["fund"] = F
                sink.notes.append(f"p.{p}: {name}: fund heading {F!r} not printed; taken from its '… Total' row")
                _flush_pos(sink, st, name)
            elif st["fund"] != F:
                sink.unparsed.append(dict(page=p, what=f"{name}: personnel fund total {lab!r} but heading {st['fund']!r}"))
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.total(c.year, c.basis, scope, lab, num0(vals[ci]), p)
                    sink.check("tier4", scope, lab, p, c.year, c.basis, num0(vals[ci]),
                               sum(num0(v[ci]) for v in st["fund_progs"]), "sum of the printed program totals under this fund heading")
            st["fund_totals"].append(vals)
            st["fund"], st["fund_progs"] = None, []
            continue
        if x0 is not None and x0 < PROG_X and not (vals is not None and not lab.endswith(" Total")):
            if vals is None:
                st["prog"], st["prog_rows"] = lab, []
                continue
            P_ = lab[:-6]
            if st["prog"] is None:
                for rec in st["pend"]:
                    if rec["prog"] is None:
                        rec["prog"] = P_
                sink.notes.append(f"p.{p}: {name}: program heading {P_!r} not printed; taken from its '… Total' row")
            elif st["prog"] != P_:
                sink.unparsed.append(dict(page=p, what=f"{name}: program total {lab!r} but heading {st['prog']!r}"))
            _flush_pos(sink, st, name)
            for ci, c in enumerate(cols):
                if pv(vals[ci]):
                    sink.total(c.year, c.basis, scope, f"{st['fund'] or '(fund heading not printed)'} | {lab}", num0(vals[ci]), p)
                    sink.check("tier4", scope, f"{st['fund'] or '(fund heading not printed)'} | {lab}", p, c.year, c.basis, num0(vals[ci]),
                               sum(num0(v[ci]) for v in st["prog_rows"]), "sum of the position rows under this program heading")
            st["fund_progs"].append(vals)
            st["prog"], st["prog_rows"] = None, []
            continue
        # position row (a row with FTE cells; title may be absent from the PDF)
        if vals is None:
            if lab:
                sink.unparsed.append(dict(page=p, what=f"{name}: personnel label-only row not placed: {lab!r}"))
            continue
        toks = list(r.label_words)
        grade = ""
        if pg_x0 is not None and toks and toks[-1].x0 >= pg_x0 and GRADE_RE.match(toks[-1].t):
            grade = toks[-1].t
            toks = toks[:-1]
        title = clean(" ".join(w.t for w in toks))
        if not title:
            sink.notes.append(f"p.{p}: {name}: position row with FTEs but no printed title; title left blank")
        st["prog_rows"].append(vals)
        st["pend"].append(dict(vals=vals, cols=cols, title=title, grade=grade, page=p, fund=st["fund"], prog=st["prog"]))
        _flush_pos(sink, st, name)


# --------------------------------------------------------------------------- sections driver
def colkey(cols):
    return {(c.year, c.basis): i for i, c in enumerate(cols)}


def parse_sections(doc, sink, p_start):
    infos = {}
    for p in range(p_start, doc.n + 1):
        info = classify(doc, p)
        if info:
            infos[p] = info
        sink.page_types[p] = info["kind"] if info else "other"
    # book's adopted year column (used for PROGRAM DETAIL, which prints no year header)
    detail_col = None
    for p, info in infos.items():
        if info["kind"] == "esum":
            cols, _ = find_header(doc.words(p), min_years=3)
            ad = [c for c in cols if c.basis == "adopted"]
            if ad:
                detail_col = max(ad, key=lambda c: c.year)
                break
    sections, cur = [], None
    sigs = {}
    for p in range(p_start, doc.n + 1):
        info = infos.get(p)
        if not info:
            continue
        kind = info["kind"]
        sig = (kind, info["name"], page_signature(doc, p))
        if sig in sigs and kind in ("pdet", "esum", "pers"):
            sink.duplicate_pages.append(dict(page=p, same_as=sigs[sig], kind=kind, name=info["name"]))
            continue
        sigs[sig] = p
        if kind == "btype":
            cur = dict(name=info["name"], btype=None, bfund=None, subs=OrderedDict(), group=None, pages=[p])
            sections.append(cur)
            cur["btype"] = parse_type_fund(doc, p, sink, info, None)
            continue
        if kind == "bfund":
            if cur is None or cur["bfund"] is not None:
                sink.unparsed.append(dict(page=p, what=f"by_fund page {info['name']!r} without a preceding by_type page"))
                continue
            cur["bfund"] = parse_type_fund(doc, p, sink, info, None)
            cur["pages"].append(p)
            continue
        # program pages
        if cur is None or (cur["group"] is not None and info["group"] != cur["group"]):
            cur = dict(name=info["name"], btype=None, bfund=None, subs=OrderedDict(), group=None, pages=[])
            sections.append(cur)
        cur["group"] = cur["group"] or info["group"]
        cur["pages"].append(p)
        sub = cur["subs"].setdefault(info["name"], dict(esum={}, pdet={}, pers={}, group=info["group"]))
        if kind == "esum":
            parse_esum(doc, p, sink, info, sub["esum"])
        elif kind == "pdet":
            if detail_col is None:
                sink.unparsed.append(dict(page=p, what="no year for PROGRAM DETAIL"))
            else:
                parse_pdet(doc, p, sink, info, sub["pdet"], detail_col.year, detail_col.basis)
        else:
            parse_pers(doc, p, sink, info, sub["pers"])
    # sections never closed by a DEPARTMENT TOTAL
    for sec in sections:
        for sname, sub in sec["subs"].items():
            for k, done in (("esum", "esum_done"), ("pdet", "pdet_done"), ("pers", "pers_done")):
                if sub[k] and done not in sub[k]:
                    sink.unparsed.append(dict(what=f"{sname}: {k} pages without DEPARTMENT TOTAL row"))
    sink.pages["sections"] = len(sections)
    return sections, detail_col


def section_checks(sink, sections, detail_col, stm):
    ncount = Counter()
    for sec in sections:
        bt, bf = sec["btype"], sec["bfund"]
        subs = sec["subs"]
        if bt and bf:
            kb, kf = colkey(bt["cols"]), colkey(bf["cols"])
            for key, i in kb.items():
                if key in kf and bt["total"][i] is not None and bf["total"] is not None and bf["total"][kf[key]] is not None:
                    sink.xcheck("tier2", "by_type Total Expenditures = by_fund Total Funding", f"{bt['name']} | {key[0]} {key[1]}",
                                "by_type total", num0(bt["total"][i]), "by_fund total", num0(bf["total"][kf[key]]), bt["page"])
        if bt and not bf:
            sink.xcheck("tier2", "by_fund page exists for each by_type page", bt["name"], "by_type pages", 1, "by_fund pages", 0, bt["page"])
        if bt and bt["total"] is not None:
            kb = colkey(bt["cols"])
            es = [(n, s["esum"]["esum_done"]) for n, s in subs.items() if s["esum"].get("esum_done")]
            if es:
                for key, i in kb.items():
                    tot = [num0(d["vals"][colkey(d["cols"])[key]]) for _, d in es if key in colkey(d["cols"]) and d["vals"][colkey(d["cols"])[key]] is not None]
                    if bt["total"][i] is not None and tot:
                        sink.xcheck("tier3", "by_type Total Expenditures = sum of sub-department EXPENDITURE SUMMARY DEPARTMENT TOTALs",
                                    f"{bt['name']} | {key[0]} {key[1]}", "by_type total", num0(bt["total"][i]),
                                    "sum of DEPARTMENT TOTALs (" + "; ".join(n for n, _ in es) + ")", sum(tot), bt["page"])
            if bt["fte"] is not None:
                ps = [(n, s["pers"]["pers_done"]) for n, s in subs.items() if s["pers"].get("pers_done")]
                if ps:
                    for key, i in kb.items():
                        tot = [num0(d["vals"][colkey(d["cols"])[key]]) for _, d in ps if key in colkey(d["cols"]) and d["vals"][colkey(d["cols"])[key]] is not None]
                        if bt["fte"][i] is not None and tot:
                            sink.xcheck("tier4", "by_type Department FTEs = sum of sub-department PERSONNEL SUMMARY DEPARTMENT TOTALs",
                                        f"{bt['name']} | {key[0]} {key[1]}", "Department FTEs", num0(bt["fte"][i]),
                                        "sum of DEPARTMENT TOTALs (" + "; ".join(n for n, _ in ps) + ")", round(sum(tot), 2), bt["page"])
        for sname, s in subs.items():
            e, d = s["esum"].get("esum_done"), s["pdet"].get("pdet_done")
            if e and d and detail_col is not None:
                ck = colkey(e["cols"])
                key = (detail_col.year, detail_col.basis)
                if key in ck and e["vals"][ck[key]] is not None and d["vals"][3] is not None:
                    sink.xcheck("tier3", "PROGRAM DETAIL DEPARTMENT TOTAL = EXPENDITURE SUMMARY DEPARTMENT TOTAL (adopted column)",
                                f"{sname} | {key[0]} {key[1]}", "program detail total", num0(d["vals"][3]),
                                "expenditure summary total", num0(e["vals"][ck[key]]), d["page"])
    # group level against the citywide statements
    if stm and "all_funds" in stm:
        bygroup = defaultdict(list)
        for sec in sections:
            if sec["btype"] and sec["group"]:
                bygroup[sec["group"]].append(sec)
        stat = {g: {norm_name(lab): (lab, vals, p) for lab, vals, p in stm[g]["rows"]} for g in stm}
        cols = stm["all_funds"]["cols"]
        sk = colkey(cols)
        for grp, secs in bygroup.items():
            key = norm_name(grp)
            hit = stat["all_funds"].get(key)
            for ck_, ci in sk.items():
                tot = [s["btype"]["total"][colkey(s["btype"]["cols"])[ck_]] for s in secs if ck_ in colkey(s["btype"]["cols"])]
                tot = [num0(t) for t in tot]
                gf = 0
                for s in secs:
                    bf = s["bfund"]
                    if bf and ck_ in colkey(bf["cols"]):
                        for lab, vals in bf["rows"]:
                            if lab == "General Fund" and vals[colkey(bf["cols"])[ck_]] is not None:
                                gf += num0(vals[colkey(bf["cols"])[ck_]])
                pages = [s["btype"]["page"] for s in secs]
                if hit is None:
                    sink.cross.append(dict(tier="tier2", check="sum of by_type totals for the department group = citywide all-funds statement row",
                                           key=f"{grp} | {ck_[0]} {ck_[1]}", a="by_type totals", a_value=sum(tot), b="(no statement row with this name)",
                                           b_value=None, diff=None, status="not_in_citywide_statements", page=pages[0]))
                    continue
                sink.xcheck("tier2", "sum of by_type totals for the department group = citywide all-funds statement row",
                            f"{grp} | {ck_[0]} {ck_[1]}", "sum of by_type totals (p." + ",".join(map(str, pages)) + ")", sum(tot),
                            f"statement all_funds '{hit[0]}'", num0(hit[1][ci]), hit[2])
                if len(secs) > 1:
                    first = num0(secs[0]["btype"]["total"][colkey(secs[0]["btype"]["cols"])[ck_]]) if ck_ in colkey(secs[0]["btype"]["cols"]) else 0
                    sink.xcheck("tier2", "FIRST by_type section of a multi-section group alone = citywide all-funds statement row "
                                         "(groups where a later section re-prints a sub-department already inside the first)",
                                f"{grp} | {ck_[0]} {ck_[1]}", f"by_type total p.{secs[0]['btype']['page']}", first,
                                f"statement all_funds '{hit[0]}'", num0(hit[1][ci]), hit[2])
                g_hit = stat["general_fund"].get(key)
                gv = num0(g_hit[1][ci]) if g_hit else 0
                sink.xcheck("tier2", "sum of by_fund 'General Fund' rows for the department group = citywide general-fund statement row",
                            f"{grp} | {ck_[0]} {ck_[1]}", "sum of by_fund General Fund rows", gf,
                            f"statement general_fund '{g_hit[0]}'" if g_hit else "statement general_fund (no row)", gv,
                            g_hit[2] if g_hit else None)
        # all sections together vs statement grand total
        if stm["all_funds"]["total"]:
            tv, tp = stm["all_funds"]["total"]
            for ck_, ci in sk.items():
                tot = sum(num0(s["btype"]["total"][colkey(s["btype"]["cols"])[ck_]]) for s in sections
                          if s["btype"] and ck_ in colkey(s["btype"]["cols"]))
                sink.xcheck("tier2", "sum of all department by_type totals = citywide all-funds statement total",
                            f"{ck_[0]} {ck_[1]}", "sum of by_type totals", tot, "statement Total Expenditures", num0(tv[ci]), tp)


def citywide_checks(sink, stm, funds, rev):
    if not funds:
        return
    fs, bt, rf = funds["fund_source"], funds["by_type"], funds["rev_by_fund"]
    sk = colkey(stm["all_funds"]["cols"]) if stm and "all_funds" in stm else {}
    def tot_of(g):
        return stm[g]["total"][0] if stm and g in stm and stm[g]["total"] else None
    if fs:
        kf = colkey(fs["cols"])
        for key, i in kf.items():
            if key in sk:
                ci = sk[key]
                for g, lab in (("all_funds", "fund-source total = statement all-funds total"),):
                    t = tot_of(g)
                    if t and fs["total"] and fs["total"][1][i] is not None:
                        sink.xcheck("tier1", "Total Expenditures by Fund Source total = department statement total", f"{key[0]} {key[1]}",
                                    "fund source total", num0(fs["total"][1][i]), "statement all-funds total", num0(t[ci]), fs["page"])
                gfrow = next((v for lab, v in fs["rows"] if lab == "General Fund"), None)
                t = tot_of("general_fund")
                if gfrow and t:
                    sink.xcheck("tier1", "fund source 'General Fund' = department statement general-fund total", f"{key[0]} {key[1]}",
                                "fund source General Fund", num0(gfrow[i]), "statement general-fund total", num0(t[ci]), fs["page"])
        byname = {norm_name(lab): v for lab, v in fs["rows"]}
        for fund, rec in bt.items():
            hit = byname.get(norm_name(fund))
            for key, i in colkey(rec["cols"]).items():
                if hit is not None and key in kf and rec["total"] and rec["total"][1][i] is not None:
                    sink.xcheck("tier1", "Total Expenditures by Type table total = Fund Source row", f"{fund} | {key[0]} {key[1]}",
                                "by-type table total", num0(rec["total"][1][i]), "fund source row", num0(hit[kf[key]]), rec["page"])
                elif hit is None and i == 0:
                    sink.cross.append(dict(tier="tier1", check="Total Expenditures by Type table total = Fund Source row", key=fund,
                                           a="by-type table", a_value=None, b="(no fund-source row of this name)", b_value=None, diff=None,
                                           status="not_in_citywide_statements", page=rec["page"]))
        if bt:
            for key, ci in sk.items():
                s = sum(num0(rec["total"][1][colkey(rec["cols"])[key]]) for rec in bt.values() if rec["total"] and key in colkey(rec["cols"]))
                t = tot_of("all_funds")
                sink.xcheck("tier1", "sum of the per-fund Total Expenditures by Type tables = department statement all-funds total",
                            f"{key[0]} {key[1]}", "sum of by-type tables", s, "statement all-funds total", num0(t[ci]), None)
    if rf and rev and rev.get("gf"):
        kr = colkey(rf["cols"])
        gv, gcols, gp = rev["gf"]
        for key, i in colkey(gcols).items():
            if key in kr:
                row = next((v for lab, v in rf["rows"] if lab == "General Fund"), None)
                if row:
                    sink.xcheck("tier1", "Total Revenues by Fund 'General Fund' = revenue statement Total General Fund", f"{key[0]} {key[1]}",
                                "Total Revenues by Fund", num0(row[kr[key]]), "statement Total General Fund", num0(gv[i]), rf["page"])
    if rf and rev and rev.get("gf") and rev.get("other"):
        gv, gcols, gp = rev["gf"]
        ov, ocols, op_ = rev["other"]
        for key, i in colkey(gcols).items():
            if key in colkey(rf["cols"]) and rf["total"] and rf["total"][1][colkey(rf["cols"])[key]] is not None:
                sink.xcheck("tier1", "Total Revenues by Fund total = revenue statement GF + Other Funds", f"{key[0]} {key[1]}",
                            "Total Revenues by Fund total", num0(rf["total"][1][colkey(rf["cols"])[key]]), "statement GF + Other",
                            num0(gv[i]) + num0(ov[colkey(ocols)[key]]), rf["page"])


# --------------------------------------------------------------------------- writers
def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def fmt_amount(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def fmt_for_search(v, decimal=False):
    if decimal:
        return f"{v:.2f}".lstrip("0") if v < 1 else f"{v:.2f}"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.2f}".lstrip("0") if abs(v) < 1 else f"{abs(v):.2f}"
    return f"{abs(int(v)):,}"


def spot_checks(sink, cfg, n_each=8):
    """Re-read seeded-random output cells with pdfplumber (a different engine from the PyMuPDF word
    clustering used above): the number must appear on a text line that also carries the row label."""
    import pdfplumber
    rng = random.Random(cfg.seed)
    sets = [("department_expenditures", sink.dept_exp, lambda r: r["dept_name"], False),
            ("revenues", sink.revenues, lambda r: r["line_name"], False),
            ("department_detail", sink.detail, lambda r: r["dim1"], False),
            ("positions", sink.positions, lambda r: r["position_title"] or r["program"], True),
            ("workyears_citywide", sink.workyears, lambda r: r["dept_name"], False),
            ("citywide_by_fund", sink.by_fund, lambda r: r["line_name"] or r["fund_name"], False)]
    out = []
    with pdfplumber.open(str(cfg.pdf)) as pdf:
        cache = {}
        for name, rows, labf, dec in sets:
            for r in rng.sample(rows, min(n_each, len(rows))):
                pg = r["page"]
                if pg not in cache:
                    cache[pg] = [clean(x) for x in (pdf.pages[pg - 1].extract_text() or "").split("\n")]
                amt = r["amount"] if "amount" in r else r["fte"]
                needle = fmt_for_search(amt, dec or (isinstance(amt, float) and not float(amt).is_integer()))
                lab = labf(r)
                nl = re.sub(r"[^a-z0-9]", "", lab.lower())
                hits = [ln for ln in cache[pg] if needle in ln and nl and nl in re.sub(r"[^a-z0-9]", "", ln.lower())]
                if not hits and nl:      # label may wrap onto lines above/below the numbers: use label words in +-4 neighbour lines
                    lines = cache[pg]
                    words = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in lab.split()]
                    words = [t for t in words if len(t) >= 3] or [nl]
                    for i, ln in enumerate(lines):
                        if needle in ln:
                            ctx = "".join(re.sub(r"[^a-z0-9]", "", lines[j].lower()) for j in range(max(0, i - 4), min(len(lines), i + 5)))
                            if all(t in ctx for t in words):
                                hits.append(ln + "  [label on neighbouring line]")
                                break
                out.append(dict(file=name, page=pg, fiscal_year=r["fiscal_year"], basis=r["basis"], label=lab,
                                amount=amt, found=bool(hits), matched_line=hits[0] if hits else None))
    return out


def write_outputs(sink, cfg, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for lst in (sink.dept_exp, sink.revenues, sink.detail, sink.totals, sink.workyears, sink.by_fund):
        for r in lst:
            if "amount" in r:
                r["amount"] = fmt_amount(r["amount"])
    write_csv(out_dir / "department_expenditures.csv", sink.dept_exp,
              "book_id fiscal_year basis fund_group dept_code dept_name amount page".split())
    write_csv(out_dir / "revenues.csv", sink.revenues,
              "book_id fiscal_year basis fund_group fund_name category line_name amount page".split())
    write_csv(out_dir / "department_detail.csv", sink.detail,
              "book_id fiscal_year basis dept_code dept_name table dim1 dim2 amount page".split())
    write_csv(out_dir / "positions.csv", [dict(r, fte=f"{r['fte']:.2f}") for r in sink.positions],
              "book_id fiscal_year basis dept_code dept_name program fund position_title pay_grade fte page".split())
    write_csv(out_dir / "workyears_citywide.csv", sink.workyears,
              "book_id fiscal_year basis fund_group dept_name fte page".split())
    write_csv(out_dir / "citywide_by_fund.csv", sink.by_fund,
              "book_id fiscal_year basis table fund_name line_name amount page".split())
    write_csv(out_dir / "printed_totals.csv", sink.totals, "book_id fiscal_year basis scope label amount page".split())


def write_checks(sink, cfg, out_dir):
    tiers = defaultdict(Counter)
    for c in sink.checks:
        tiers[c["tier"]][c["status"]] += 1
    cross = defaultdict(Counter)
    for c in sink.cross:
        cross[c["tier"]][c["status"]] += 1
    out = OrderedDict()
    out["book_id"] = sink.book_id
    out["row_counts"] = dict(department_expenditures=len(sink.dept_exp), revenues=len(sink.revenues),
                             department_detail=len(sink.detail), positions=len(sink.positions),
                             workyears_citywide=len(sink.workyears), citywide_by_fund=len(sink.by_fund),
                             printed_totals=len(sink.totals))
    out["tier_definitions"] = {
        "tier1": "citywide statements: revenues by source, department expenditures (general / non-general / all funds), fund tables",
        "tier2": "department summaries by expenditure type and by funding source (Budget Summary / Expenditure by Type / Expenditures by Funding Source pages)",
        "tier3": "program-level detail (EXPENDITURE SUMMARY, PROGRAM DETAIL)",
        "tier4": "positions / FTE (PERSONNEL SUMMARY pages, citywide personnel schedule)"}
    out["year_columns_captured"] = {
        name: sorted({f"{r['fiscal_year']} {r['basis']}" for r in rows})
        for name, rows in (("department_expenditures", sink.dept_exp), ("revenues", sink.revenues),
                           ("department_detail", sink.detail), ("positions", sink.positions),
                           ("workyears_citywide", sink.workyears), ("citywide_by_fund", sink.by_fund),
                           ("printed_totals", sink.totals))}
    out["printed_total_checks_summary"] = {t: dict(v) for t, v in sorted(tiers.items())}
    out["cross_checks_summary"] = {t: dict(v) for t, v in sorted(cross.items())}
    out["mismatches"] = [c for c in sink.checks if c["status"] == "mismatch"] + [c for c in sink.cross if c["status"] == "mismatch"]
    out["printed_total_checks"] = sink.checks
    out["cross_checks"] = sink.cross
    sc = spot_checks(sink, cfg)
    out["spot_checks_summary"] = dict(sampled=len(sc), found=sum(1 for x in sc if x["found"]))
    out["spot_checks"] = sc
    out["duplicate_pages_skipped"] = sink.duplicate_pages
    out["not_attempted"] = sink.not_attempted
    out["unparsed"] = sink.unparsed
    out["notes"] = sink.notes
    out["pages_located"] = sink.pages
    out["page_types_after_citywide_statements"] = Counter(sink.page_types.values())
    with open(out_dir / "checks.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=lambda o: dict(o) if isinstance(o, Counter) else str(o))
    return out


# --------------------------------------------------------------------------- main
def run(cfg):
    out_dir = ROOT / "data/extracted" / cfg.book_id
    doc = Doc(cfg.pdf)
    sink = Sink(cfg.book_id)
    # first department page
    first_dept = next((p for p in range(60, doc.n + 1) if (classify(doc, p) or {}).get("kind") in ("btype", "bfund", "pdet", "esum", "pers")), None)
    rev_pages = [p for p in range(1, doc.n + 1)
                 if re.match(r"^Statements? of Revenues$", (first_rows_text(doc, p, 60)[0].text if first_rows_text(doc, p, 60) else ""))]
    rf_page = next((p for p in range(1, doc.n + 1)
                    if (first_rows_text(doc, p, 100) or [None])[0] and first_rows_text(doc, p, 100)[0].text == "Total Revenues by Fund"),
                   rev_pages[0])
    sink.pages["first_department_page"] = first_dept
    rev = parse_revenues(doc, sink)
    stm = parse_dept_statements(doc, sink, rev_pages[-1] + 1, first_dept - 1)
    funds = parse_fund_tables(doc, sink, rf_page, first_dept - 1)
    parse_personnel_schedule(doc, sink, rev_pages[-1] + 1, first_dept - 1)
    citywide_checks(sink, stm, funds, rev)
    sections, detail_col = parse_sections(doc, sink, first_dept)
    section_checks(sink, sections, detail_col, stm)
    write_outputs(sink, cfg, out_dir)
    res = write_checks(sink, cfg, out_dir)
    print(json.dumps({k: res[k] for k in ("row_counts", "printed_total_checks_summary", "cross_checks_summary", "spot_checks_summary")}, indent=1))
    print("unparsed:", len(sink.unparsed), "duplicates:", len(sink.duplicate_pages))
    return sink, res


CFG = Config(pdf=ROOT / "data/raw/nola-gov/2023/2023-Budget-Book-City-of-New-Orleans-Compiled-Budget-Book.pdf",
             book_id="2023-operating-adopted", seed=2023)


def main():
    run(CFG)


if __name__ == "__main__":
    main()
