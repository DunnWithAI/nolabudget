#!/usr/bin/env python
"""Extract the City of New Orleans 2021 Adopted Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2021.py

Columns printed by this book (all four are captured): FY19 Actual, FY20 Adopted, FY21 Proposed, FY21 Adopted.
The FY20 'Adopted' column is the book's record of the adopted 2020 budget (the city never posted an adopted 2020
book); the book labels its columns 'as of' 6/01/2020 (FY20 Adopted), 10/31/2020 (FY21 Proposed) and 11/29/2020
(FY21 Adopted).  Basis labels are exactly the printed header words; the as-of dates are recorded in checks.json.

Outputs (data/extracted/2021-operating-adopted/): department_expenditures.csv, revenues.csv,
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
PDF = ROOT / "data/raw/nola-gov/2021/City-of-New-Orleans-2021-Adopted-Budget-Book.pdf"
BOOK_ID = "2021-operating-adopted"
OUT = ROOT / "data/extracted" / BOOK_ID

BASES = {"actual": "actual", "adopted": "adopted", "proposed": "proposed", "projected": "projected",
         "estimate": "estimate", "estimated": "estimate"}
DASH = object()          # a printed dash / blank-equivalent cell
NUM_RE = re.compile(r"^\$?\(?-?\$?[\d,]*\.?\d+\)?$")
DASH_RE = re.compile(r"^\$?-$")
YEAR_RE = re.compile(r"^(?:FY)?(20[0-3]\d)$|^FY(\d\d)$")


def year_of(tok):
    m = YEAR_RE.match(tok)
    return int(m.group(1)) if m.group(1) else 2000 + int(m.group(2))


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
            cols.append((year_of(w.t), best[1] if best else None, w.cx))
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
                g["cols"] = sorted(g["cols"] + [(year_of(w.t), best[1], w.cx)], key=lambda c: c[2])
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


def parse_dept_statement(doc, pages, sink, fund_group):
    """A 'Total Departmental Expenditures' statement (one fund group) printed over one or more pages.  The printed
    total's label ('Total Expenditures') sits on its own line above the numbers."""
    pages = [pages] if isinstance(pages, int) else list(pages)
    rows, total = [], None
    for p in pages:
        prev = None
        for l in assign_cells(doc.lines(p)):
            if re.match(r"^Page\b", l.text):
                continue
            if not l.cells:
                prev = l if l.label else None
                continue
            lab = l.label
            if not lab and prev is not None and l.y - prev.y < 16:
                lab = prev.label
            prev = None
            if not lab:
                continue
            if lab.lower() in ("total", "total expenditures"):
                total = (l, p)
            else:
                rows.append((lab, l, p))
    cols = rows[0][1].hdr["cols"]
    recs = []
    for lab, l, p in rows:
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        recs.append((lab, vals))
        for ci, (yr, basis, _) in enumerate(cols):
            if vals[ci] is not None:
                sink.dept_exp.append(dict(book_id=BOOK_ID, fiscal_year=yr, basis=basis, fund_group=fund_group,
                                          dept_code="", dept_name=lab, amount=vals[ci], page=p))
    tl, tp = total
    tvals = [None] * len(cols)
    for ci, v, _ in tl.cells:
        tvals[ci] = None if v is DASH else v
    for ci, (yr, basis, _) in enumerate(cols):
        if tvals[ci] is not None:
            sink.total(yr, basis, f"department_expenditures/{fund_group}", "Total Expenditures", tvals[ci], tp)
            s = sum((r[1][ci] or 0) for r in recs)
            add_check(sink, "tier1", f"department_expenditures/{fund_group}", "Total Expenditures", tp, yr, basis,
                      tvals[ci], s, "sum of all department rows on the statement's pages")
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
JUNK_RE = re.compile(r"^(Statements? of Revenues|General Fund|Other Funds|Source|Page \d+|As of.*|\d{1,2}/\d{2}/\d{4}.*|Actual|Adopted|"
                     r"Proposed|\d{4})( .*)?$", re.I)


def is_junk_label(lab):
    if not lab:
        return True
    if JUNK_RE.match(lab) and not lab.upper().startswith(("GENERAL FUND TOTAL",)):
        return True
    toks = lab.split()
    return all(t.lower() in BASES or YEAR_RE.match(t) or t.lower() in ("as", "of", "source") or
               re.match(r"^\d{1,2}/\d\d/\d{4}$", t) or not re.search(r"[A-Za-z0-9]", t) or
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


def last_heading_group(pend):
    """Consecutive heading lines: an indented line continues the previous heading; a line at the same indent starts a
    new heading (the earlier one had no rows, e.g. 'N.O. Regional Business Park').  Returns the last heading's lines."""
    groups = []
    for x in pend:
        if groups and x.words[0].x0 > groups[-1][-1].words[0].x0 + 4:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [x.label for x in groups[-1]] if groups else []


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
        # a wrapped label is printed centred on its numbers line: label-only lines within 9.5pt above/below a
        # numbers-only line belong to that line; farther label-only lines are headings
        attach, consumed = defaultdict(list), set()
        for n in lines:
            if n.cells and not n.label:
                for L in lines:
                    if (not L.cells and L.label and id(L) not in consumed and not is_junk_label(L.label)
                            and L.label != "Other Funds" and abs(L.y - n.y) <= 9.5):
                        attach[id(n)].append(L)
                        consumed.add(id(L))
        for l in lines:
            lab = l.label
            if l.text == "Other Funds" or (not l.cells and lab == "Other Funds"):
                fund_group, cat, heading = "other_funds", "", []
                continue
            if not l.cells:
                if is_junk_label(lab) or id(l) in consumed:
                    continue
                pend.append(l)
                continue
            if is_junk_label(lab) and not pend and not attach.get(id(l)):
                continue
            # a line with cells; label may be on this line, on label-only lines around it, or on pending headings above
            if not lab and attach.get(id(l)):
                lab = clean(" ".join(x.label for x in sorted(attach[id(l)], key=lambda x: x.y)))
                if fund_group == "general_fund":
                    for x in pend:
                        cat = x.label
                else:
                    heading.extend(last_heading_group(pend))
                pend = []
                merged_from_heading = False
            elif not lab and pend:
                lab = clean(" ".join(x.label for x in pend))
                pend = []
                merged_from_heading = True
            else:
                merged_from_heading = False
                # pending label-only lines are headings
                if fund_group == "general_fund":
                    for x in pend:
                        cat = x.label
                else:
                    heading.extend(last_heading_group(pend))
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


HEADER_WORDS = {"change", "fy18-19", "fy19-20", "fy20-21", "fy20-fy21", "variance", "actual", "adopted", "proposed", "dollar", "percent"}


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


def parse_by_type_or_fund(doc, p, sink, kind):
    pat = BYTYPE_RE.pattern if kind == "by_type" else r"^Expenditures by Funding Source\s*-\s*"
    name = page_title(doc, p, pat)
    rows, cols = simple_table(doc, p)
    out = []
    table = "by_type" if kind == "by_type" else "by_fund"
    for lab, vals, c, l in rows:
        if re.match(r"^Total\b", lab):
            for ci, v in enumerate(vals):
                if v is not None:
                    sink.total(cols[ci][0], cols[ci][1], f"department_detail/{name}/{table}", lab, v, p)
            out.append(("TOTAL", lab, vals, cols))
        elif re.match(r"^Department FTEs", lab):
            out.append(("FTE", lab, vals, cols))          # FTEs, not dollars: used only for the FTE cross-check
        else:
            emit_detail(sink, name, table, lab, "", vals, cols, p)
            out.append(("ROW", lab, vals, cols))
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
# the PROGRAM DETAIL tables carry no year header: they hold the book year (FY21 Adopted); proven by the DEPARTMENTAL TOTAL cross-check
PD_YEAR, PD_BASIS = 2021, "adopted"
PROG_RE = re.compile(r"^(\d{3,5})\s+(.*)$")


def label_state(state, lines, i, title_x0, lab, l, key_fund="fund", key_rows="fund_rows"):
    """Shared handling of label-only lines in the program tables.  Returns True when the line was consumed.
    Fund headings start at the left margin (x of the page title), program names are indented; headings and
    totals wrap onto a second line, and a long program name wraps above the line that carries its numbers."""
    fund_like = l.words[0].x0 < title_x0 + 8
    nxt = lines[i + 1] if i + 1 < len(lines) else None
    if fund_like and FUND_HDR_RE.match(lab):
        if (nxt is not None and nxt.cells and re.search(r"\bTOT(?:A|AL)?$", nxt.label, re.I) and not FUND_HDR_RE.match(nxt.label)
                and not nxt.label.upper().startswith("DEPARTMENT")):
            state["pend_total"] = lab                        # first line of a wrapped fund TOTAL label
            return True
        state[key_fund] = lab
        state[key_rows] = []
        state["last_fund_hdr"] = True
        return True
    if fund_like and state.get("last_fund_hdr"):
        state[key_fund] = clean(state.get(key_fund, "") + " " + lab)     # wrapped fund heading
        return True
    if (not fund_like) and PROG_RE.match(lab):
        state["pend_prog"] = lab
        state["last_fund_hdr"] = False
        return True
    return False


def parse_program_exp(doc, p, sink, dept_name, state):
    """state carries current fund and per-fund/program stacks across continuation pages."""
    lines = assign_cells(doc.lines(p))
    groups = header_groups(lines)
    if not groups:
        sink.unparsed.append(dict(page=p, what="program expenditure page without year header", dept=dept_name))
        return
    ytab = max(g["y"] for g in groups)
    cols = max(groups, key=lambda g: g["y"])["cols"]
    title_x0 = lines[0].words[0].x0
    for i, l in enumerate(lines):
        if l.y <= ytab + 1 or re.match(r"^Page \d+$", l.text):
            continue
        lab = l.label
        if not l.cells:
            if not lab:
                continue
            if label_state(state, lines, i, title_x0, lab, l):
                continue
            sink.unparsed.append(dict(page=p, what=f"label-only line in program table ignored: {lab!r}", dept=dept_name))
            continue
        state["last_fund_hdr"] = False
        if state.get("pend_total") and re.search(r"\bTOT(?:A|AL)?$", lab, re.I) and not FUND_HDR_RE.match(lab):
            lab = state.pop("pend_total") + " " + lab
        if state.get("pend_prog") and not PROG_RE.match(lab):
            lab = state.pop("pend_prog") + " " + lab
        state.pop("pend_prog", None)
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        if re.search(r"\bTOT(?:A|AL)?$", lab, re.I):
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
    yp = next((w.cy for l in lines for w in l.words if w.t == "Personal" and l.y < 140), None)
    hdr = [w for l in lines for w in l.words if w.t in ("Personal", "Services", "Other", "Operating", "Debt", "Service", "Total")
           and l.y < 140 and yp is not None and abs(w.cy - yp) <= 22]
    # column centre = mean centre of the header words of the column (works for one-line and two-line headers)
    pairs = {"Personal Services": ("Personal", "Services"), "Other Operating": ("Other", "Operating"),
             "Debt Service": ("Debt", "Service"), "Total": ("Total",)}
    centers = {}
    for nm, toks in pairs.items():
        ws_ = [next((w for w in hdr if w.t == t), None) for t in toks]
        if all(ws_):
            centers[nm] = sum(w.cx for w in ws_) / len(ws_)
    if len(centers) < 4:
        sink.unparsed.append(dict(page=p, what="program detail header columns not found", dept=dept_name))
        return
    names = [(nm, centers[nm]) for nm in ("Personal Services", "Other Operating", "Debt Service", "Total")]
    ytab = max(w.cy for w in hdr)
    title_x0 = lines[0].words[0].x0

    class R:                                   # a table line with its numbers already assigned to the four columns
        pass
    rows_ = []
    for l in lines:
        if l.y <= ytab + 14 or re.match(r"^Page \d+$", l.text):
            continue
        r = R()
        r.words, r.y, cells = l.words, l.y, {}
        lab_words = []
        for w in l.words:
            v = parse_num(w.t)
            if v is not None and w.cx > names[0][1] - 40:
                cells[min(range(4), key=lambda k: abs(names[k][1] - w.cx))] = v
            else:
                lab_words.append(w)
        r.cells, r.label = cells, clean(" ".join(w.t for w in lab_words))
        rows_.append(r)
    for i, r in enumerate(rows_):
        l, cells, lab = r, r.cells, r.label
        if not cells:
            if lab:
                label_state(state, rows_, i, title_x0, lab, r, "pd_fund", "pd_rows")
            continue
        state["last_fund_hdr"] = False
        if state.get("pend_total") and re.search(r"\bTOT(?:A|AL)?$", lab, re.I) and not FUND_HDR_RE.match(lab):
            lab = state.pop("pend_total") + " " + lab
        if state.get("pend_prog") and not PROG_RE.match(lab):
            lab = state.pop("pend_prog") + " " + lab
        state.pop("pend_prog", None)
        vals = [None if cells.get(i) is DASH else cells.get(i) for i in range(4)]
        if re.search(r"\bTOT(?:A|AL)?$", lab, re.I):
            is_dept = lab.upper().startswith("DEPARTMENT")
            scope = f"department_detail/{dept_name}/by_program_type" + ("" if is_dept else f"/{state.get('pd_fund')}")
            for i in range(4):
                if vals[i] is not None:
                    sink.total(PD_YEAR, PD_BASIS, scope + f"/{names[i][0]}", lab, vals[i], p)
                    if is_dept:
                        s = sum(t[i] or 0 for t in state.get("pd_fund_totals", []))
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, PD_YEAR, PD_BASIS, vals[i], s,
                                  "sum of the fund TOTAL rows in this program-detail table")
                    else:
                        s = sum((r[i] or 0) for r in state["pd_rows"])
                        add_check(sink, "tier3", scope + f"/{names[i][0]}", lab, p, PD_YEAR, PD_BASIS, vals[i], s,
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
                sink.detail.append(dict(book_id=BOOK_ID, fiscal_year=PD_YEAR, basis=PD_BASIS, dept_code="", dept_name=dept_name,
                                        table="by_program_type", dim1=lab, dim2=f"{state.get('pd_fund','')} | {names[i][0]}",
                                        amount=vals[i], page=p))
        if vals[3] is not None:
            add_check(sink, "tier3", f"department_detail/{dept_name}/by_program_type/row", lab, p, PD_YEAR, PD_BASIS,
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
    title_x0 = lines[0].words[0].x0
    for l in lines:
        if l.y <= ytab + 1 or re.match(r"^Page \d+$", l.text):
            continue
        lab = l.label
        if not l.cells:
            if not lab:
                continue
            m = FUND_HDR_RE.match(lab)
            fund_like = l.words[0].x0 < title_x0 + 8          # fund headings start at the left margin, program headings are indented
            nxt = lines[lines.index(l) + 1] if lines.index(l) + 1 < len(lines) else None
            wrapped_total = (nxt is not None and nxt.cells and re.search(r"\bTOT(?:A|AL)?$", nxt.label, re.I)
                             and not FUND_HDR_RE.match(nxt.label) and not nxt.label.upper().startswith("DEPARTMENT"))
            if fund_like and state.get("last_fund_hdr") and not m:
                state["fund"] = clean(state.get("fund", "") + " " + lab)       # wrapped fund heading
                continue
            state["last_fund_hdr"] = bool(m and fund_like and not wrapped_total)
            if wrapped_total and fund_like:
                state["pend_total"] = lab
            elif PROG_RE.match(lab) and not fund_like:
                state["program"] = lab
            elif m and fund_like:
                state["fund"] = lab
            elif PROG_RE.match(lab):
                state["program"] = lab
            else:
                state["pending_title"] = (state.get("pending_title", "") + " " + lab).strip()
            continue
        state["last_fund_hdr"] = False
        if state.get("pend_total") and re.search(r"\bTOT(?:A|AL)?$", lab, re.I) and not FUND_HDR_RE.match(lab):
            lab = state.pop("pend_total") + " " + lab
        vals = [None] * len(cols)
        for ci, v, _ in l.cells:
            vals[ci] = None if v is DASH else v
        if re.search(r"\bTOT(?:A|AL)?$", lab, re.I):
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
    ("New Orleans General Fund Projections", "multi-year General Fund projections (projected columns; not in the schema)"),
    ("FY2021 Revenue Overview", "narrative / summary chart pages"),
    ("2021 Millage Rates, Special Millages, and Special Fees", "millage table (rates, not dollars)"),
    ("Special Fees", "fee schedule"),
    ("City Debt", "debt statement, bond ratings, pension/OPEB notes (pp.102-117)"),
    ("2021 Capital Budget Summary", "capital budget summary (separate book)"),
]


def note_not_attempted(doc, sink):
    for title, why in NOT_ATTEMPTED_TITLES:
        pgs = [p for p in range(1, doc.n + 1) if any(l.text.startswith(title) for l in doc.lines(p)[:6])]
        sink.not_attempted.append(dict(title=title, pages=pgs, reason=why))
    narrative = [p for p in sorted(sink.section_types_all_pages) if p not in sink.section_types]
    sink.not_attempted.append(dict(title="department narrative pages (overview, org chart, accomplishments, priorities, section dividers)",
                                   pages=narrative, reason="free text / org charts; no budget tables"))


def statement_pages(doc, title):
    """Pages of a citywide statement: the titled page plus continuation pages up to the one holding 'Total Expenditures'."""
    start = find_pages(doc, lambda d, p: d.first_text(p, 1) == title, 60, 110)
    if not start:
        return []
    pages = [start[0]]
    while not any(re.match(r"^Total Expenditures [\d$(-]", l.text) for l in doc.lines(pages[-1])) and len(pages) < 4:
        pages.append(pages[-1] + 1)
    return pages


def parse_fte_schedule(doc, pages, sink, scope):
    """'Detailed Personnel Schedule by Department': whole-number FTEs, FY20 Adopted / FY21 Adopted."""
    rows, total = [], None
    for p in pages:
        for l in assign_cells(doc.lines(p)):
            if not l.cells or not l.label or re.match(r"^Page\b", l.text):
                continue
            vals = [None] * len(l.hdr["cols"])
            for ci, v, _ in l.cells:
                vals[ci] = None if v is DASH else v
            cols = l.hdr["cols"]
            if l.label.lower() == "total":
                total = (vals, p)
            else:
                rows.append((l.label, vals, p))
    for ci, (yr, basis, _) in enumerate(cols):
        if total and total[0][ci] is not None:
            sink.total(yr, basis, f"fte_schedule/{scope}", "Total", total[0][ci], total[1])
            add_check(sink, "tier4", f"fte_schedule/{scope}", "Total", total[1], yr, basis, total[0][ci],
                      sum((r[1][ci] or 0) for r in rows), "sum of the department rows of the schedule")
    return rows, cols


def run_all(doc, sink):
    # ---- tier 1a: citywide department statements -------------------------------------------
    pg = {}
    pg["gf"] = statement_pages(doc, "Total Departmental Expenditures - General Fund")
    pg["of"] = statement_pages(doc, "Total Departmental Expenditures - Non-General Fund")
    pg["all"] = statement_pages(doc, "Total Expenditures by Department")
    for k, v in pg.items():
        if not v:
            sink.unparsed.append(dict(page=None, what=f"locator found no page for {k}"))
    stm = {}
    for key, fg in (("gf", "general_fund"), ("of", "other_funds"), ("all", "all_funds")):
        if pg[key]:
            stm[fg] = parse_dept_statement(doc, pg[key], sink, fg)
    sink.pages = pg
    if len(stm) == 3:
        gf, of, al = stm["general_fund"], stm["other_funds"], stm["all_funds"]
        gd, od = dict(gf[0]), dict(of[0])
        for lab, va in al[0]:
            for ci, (yr, basis, _) in enumerate(al[1]):
                gci = next(i for i, c in enumerate(gf[1]) if (c[0], c[1]) == (yr, basis))
                oci = next(i for i, c in enumerate(of[1]) if (c[0], c[1]) == (yr, basis))
                g_ = (gd.get(lab) or [None] * len(gf[1]))[gci]
                o_ = (od.get(lab) or [None] * len(of[1]))[oci]
                if va[ci] is None and g_ is None and o_ is None:
                    continue
                add_cross(sink, "tier1", "general_fund + other_funds = all_funds", f"{lab} {yr} {basis}",
                          "general+other", (g_ or 0) + (o_ or 0), "all_funds", va[ci] or 0, pg["all"][-1])
        for nm, rows_ in (("general_fund", gf[0]), ("other_funds", of[0])):
            for lab, v in rows_:
                if lab not in dict(al[0]):
                    sink.unparsed.append(dict(page=pg["all"][0], what=f"{nm} statement row {lab!r} has no row of that name in the all-funds statement"))
    # ---- tier 1b: revenues ----------------------------------------------------------------
    rev = parse_revenues(doc, sink)
    if len(stm) == 3:
        for ci, (yr, basis, _) in enumerate(rev["gf_total"][1]):
            t = next((t for t in sink.totals if t["scope"] == "department_expenditures/general_fund" and (t["fiscal_year"], t["basis"]) == (yr, basis)), None)
            if t and rev["gf_total"][0][ci] is not None:
                add_cross(sink, "tier1", "BOOK BALANCE: statement of revenues TOTAL GENERAL FUND = General Fund expenditures total", f"{yr} {basis}",
                          "revenue statement", rev["gf_total"][0][ci], "expenditure statement", t["amount"], rev["gf_total"][2])
            t = next((t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == (yr, basis)), None)
            if t:
                ra = (rev["gf_total"][0][ci] or 0) + (rev["other_total"][0][ci] or 0)
                add_cross(sink, "tier1", "BOOK BALANCE: statement of revenues TOTAL ALL FUNDS = Total Expenditures (all funds)", f"{yr} {basis}",
                          "revenue statement", ra, "expenditure statement", t["amount"], rev["other_total"][2])
    parse_fund_summaries(doc, sink, stm, rev)
    type_by_fund_pages(doc, sink)
    # ---- FTE schedules (whole numbers) ---------------------------------------------------------------
    sink.fte_sched = {}
    for key, title, scope in (("gf", "Detailed Personnel Schedule by Department - General Fund", "general_fund"),
                              ("of", "Detailed Personnel Schedule by Department - Non-General Fund", "other_funds"),
                              ("all", "Total Detailed Personnel Schedule by Department", "all_funds")):
        pp = find_pages(doc, lambda d, p: d.first_text(p, 1) == title, 60, 120)
        if not pp:
            sink.unparsed.append(dict(page=None, what=f"FTE schedule {title!r} not located"))
            continue
        pages = [pp[0]]
        while not any(l.text == "Total" or l.text.startswith("Total ") and re.search(r"\d", l.text) for l in doc.lines(pages[-1])) and len(pages) < 3:
            pages.append(pages[-1] + 1)
        sink.fte_sched[scope] = parse_fte_schedule(doc, pages, sink, scope) + (pages,)
    # ---- tier 2/3/4: department sections ---------------------------------------------------------
    sections(doc, sink, stm, None)
    fte_schedule_check(doc, sink, stm)
    first = sink.first_section_page
    sink.section_types_all_pages = list(range(first, doc.n + 1))
    note_not_attempted(doc, sink)


def parse_fund_summaries(doc, sink, stm, rev):
    """p.72 'Total Expenditures by Fund Source', p.60 'Total Revenues by Fund'."""
    p72 = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Expenditures by Fund Source"), 70, 110)
    sink.fund_summary = {}
    if p72:
        p = p72[0]
        rows, cols = simple_table(doc, p, wrap=True, pick="first", stop_re=r"^Total Expenditures")
        tot, body = None, []
        for lab, vals, c, l in rows:
            if re.match(r"^Total Expenditures", lab):
                tot = vals
            else:
                body.append((lab, vals))
        sink.fund_summary["p72"] = (body, tot, cols, p)
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
    for key, ttl, scope in (("p60", "Overview of Revenues and Expenditures Total Revenues by Fund", "revenue_summary/by_fund_p60"),):
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
    for key in ("p60",):
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
    """pp.73-84 'Total Expenditures by Type - <fund>': one small table per fund.  Stored as printed totals
    (they are fund-level aggregates) and summed over funds to cross-check the department sections' by-type lines."""
    pages = find_pages(doc, lambda d, p: d.first_text(p, 1).startswith("Total Expenditures by Type -"), 60, 110)
    agg = defaultdict(lambda: defaultdict(int))
    for p in pages:
        fund = re.sub(r"^Total Expenditures by Type\s*-\s*", "", doc.first_text(p, 1))
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
    """'Detailed Personnel Schedule by Department' (whole-number FTEs, FY20 Adopted / FY21 Adopted; General Fund,
    Non-General Fund, Total) vs the FTEs summed from the position pages, per department family (the department rows
    are matched to the family by the same name as the all-funds expenditure statement row)."""
    if len(sink.fte_sched) < 3:
        return
    fte, ftegf = defaultdict(float), defaultdict(float)
    for r in sink.positions:
        k = (r["dept_name"], r["fiscal_year"], r["basis"])
        fte[k] += r["fte"]
        if r["fund"].upper().endswith("GENERAL FUND"):
            ftegf[k] += r["fte"]
    sched = {sc: dict((lab, v) for lab, v, _ in rows_) for sc, (rows_, cols_, pages_) in sink.fte_sched.items()}
    cols = sink.fte_sched["all_funds"][1]
    pg = sink.fte_sched["all_funds"][2][0]
    used = set()
    for fam in sink.families:
        lab = fam["label"]
        names = set(fam["names"])
        used.add(lab)
        for ci, (yr, basis, _) in enumerate(cols):
            t = sum(fte[(n, yr, basis)] for n in names)
            gfv = sum(ftegf[(n, yr, basis)] for n in names)
            for sc, tv in (("all_funds", t), ("general_fund", gfv)):
                row = sched[sc].get(lab)
                pv = row[ci] if row else None
                add_cross(sink, "tier4", f"FTE from position rows (rounded) = whole-number personnel schedule ({sc})", f"{lab} {yr} {basis}",
                          "positions.csv (rounded)", round(tv), "schedule", pv or 0, sink.fte_sched[sc][2][0])
    for lab, v in sched["all_funds"].items():
        if lab not in used and any(v):
            sink.cross.append(dict(tier="tier4", check="schedule row with non-zero FTE has no department section group", key=lab,
                                   a="positions.csv", a_value=None, b="schedule", b_value=v, diff=None, status="no_matching_group", page=pg))


BYTYPE_RE = re.compile(r"^(Budget Summary|Expenditure by Type)\s*-\s*")


def page_title(doc, p, pat):
    """Printed title of a department table page: the first line after the 'X - ' prefix, plus a wrapped second line
    (a short line with no digits that is not table-header furniture)."""
    ls = doc.lines(p)
    i = next((k for k, l in enumerate(ls[:6]) if re.match(pat, l.text)), None)
    if i is None:
        return None
    t = re.sub(pat, "", ls[i].text)
    if i + 1 < len(ls):
        nx = ls[i + 1].text
        if (not re.search(r"\d", nx) and len(nx.split()) <= 3
                and not re.match(r"^(Expenditure|Funding|Dollar|Percent|Actual|Adopted|Proposed|Fund|Type|Change|Source)", nx)
                and ls[i + 1].y - ls[i].y < 22):
            t = clean(t + " " + nx)
    return t


def classify_pages(doc, first):
    types = {}
    for p in range(first, doc.n + 1):
        ls = doc.lines(p)
        if not ls:
            continue
        ft1 = doc.first_text(p, 1)
        if BYTYPE_RE.match(ft1):
            types[p] = "by_type"
        elif ft1.startswith("Expenditures by Funding Source -"):
            types[p] = "by_fund"
        else:
            head = ls[0].text if len(ls) < 2 else ls[0].text + " " + ls[1].text
            for key, tp in (("EXPENDITURE SUMMARY", "program_exp"), ("PROGRAM DETAIL", "program_detail"),
                            ("PERSONNEL SUMMARY", "program_pers")):
                if ls[0].text.endswith(key) or (key in head and ls[0].y < 100 and ls[0].text.upper() == ls[0].text.upper()
                                                and re.search(r"Component Unit|CORPORATION|BOARD|DISTRICT|BUILDING", ls[0].text) and p >= enterprise_start(doc, first)):
                    types[p] = tp
                    break
    return types


def enterprise_start(doc, first):
    return next((p for p in range(first, doc.n + 1) if doc.first_text(p, 2).startswith("(") is False and "Enterprise Fund Summaries" in doc.first_text(p, 3)), doc.n + 1)


def sections(doc, sink, stm, funds_src):
    first = next(p for p in range(100, 300) if BYTYPE_RE.match(doc.first_text(p, 1)))
    types = classify_pages(doc, first)
    sink.section_types = types
    sink.first_section_page = first
    groups, cur_sec, last_group, prev_kind = [], None, None, None
    ent_group, ent0 = None, enterprise_start(doc, first)
    for p in sorted(types):
        t = types[p]
        if t == "by_type":
            name = page_title(doc, p, BYTYPE_RE.pattern)
            cur_sec = dict(name=name, by_type=p, by_fund=None, orphan_funds=[])
            if last_group is None or prev_kind in ("program_exp", "program_detail", "program_pers"):
                last_group = dict(sections=[], program_pages=[])
                groups.append(last_group)
            last_group["sections"].append(cur_sec)
        elif t == "by_fund" and cur_sec:
            if cur_sec["by_fund"] is None:
                cur_sec["by_fund"] = p
            else:
                cur_sec["orphan_funds"].append(p)
        elif t in ("program_exp", "program_detail", "program_pers") and last_group is not None:
            if p >= ent0:
                if ent_group is None:
                    ent_group = dict(sections=[], program_pages=[], enterprise=True)
                    groups.append(ent_group)
                ent_group["program_pages"].append(p)
            else:
                last_group["program_pages"].append(p)
        prev_kind = t
    sink.groups = groups
    for g in groups:
        g["top"] = g["sections"][0]["name"] if g["sections"] else "Enterprise Fund Summaries"
        for s_ in g["sections"]:
            nm, out = parse_by_type_or_fund(doc, s_["by_type"], sink, "by_type")
            s_["by_type_rows"] = out
            s_["orphan_rows"] = []
            if s_["by_fund"]:
                nm2, outf = parse_by_type_or_fund(doc, s_["by_fund"], sink, "by_fund")
                s_["by_fund_rows"] = outf
                s_["by_fund_name"] = nm2
            else:
                sink.unparsed.append(dict(page=s_["by_type"], what="no 'Expenditures by Funding Source' page follows this section", dept=s_["name"]))
            for op in s_["orphan_funds"]:
                nm3, outo = parse_by_type_or_fund(doc, op, sink, "by_fund")
                s_["orphan_rows"].append((nm3, op, outo))
                sink.notes.append(f"p.{op}: second funding-source page {nm3!r} after section {s_['name']!r}; kept as by_fund rows under its printed title")
        program_pages(doc, g, sink)
        group_checks(doc, g, sink, stm)
    build_families(doc, sink, groups, stm)
    nested_checks(doc, sink, groups, stm)
    citywide_checks(doc, sink, groups, stm, funds_src)


def sec_total(s, kind="by_type_rows"):
    return next((o for o in s.get(kind, []) if o[0] == "TOTAL"), None)


def group_checks(doc, g, sink, stm):
    """Per section: by_type total = by_fund total; printed 'Department FTEs' = FTEs summed from the position rows."""
    for s_ in g["sections"]:
        tt, tf = sec_total(s_), sec_total(s_, "by_fund_rows")
        if tt and tf:
            for ci, (yr, basis, _) in enumerate(tt[3]):
                cf = next((i for i, c in enumerate(tf[3]) if (c[0], c[1]) == (yr, basis)), None)
                if cf is not None and tt[2][ci] is not None:
                    add_cross(sink, "tier2", "section by_type total = by_fund total", f"{s_['name']} {yr} {basis}",
                              "by_type", tt[2][ci], "by_fund", tf[2][cf] or 0, s_["by_type"])
        fte_row = next((o for o in s_["by_type_rows"] if o[0] == "FTE"), None)
        if fte_row:
            for ci, (yr, basis, _) in enumerate(fte_row[3]):
                v = fte_row[2][ci]
                if v is None:
                    continue
                nm = {s_["name"]} if g.get("use_sec_names", True) else set(g.get("dept_names", set()))
                tot = sum(r["fte"] for r in sink.positions if r["dept_name"] in nm and (r["fiscal_year"], r["basis"]) == (yr, basis))
                add_cross(sink, "tier4", "by_type page 'Department FTEs' (x100) = FTEs summed from the position rows", f"{s_['name']} {yr} {basis}",
                          "by_type page", round(v * 100), "positions.csv", round(tot * 100), s_["by_type"])


def build_families(doc, sink, groups, stm):
    """A 'family' = the contiguous run of department sections whose by_type totals add up to one row of the citywide
    all-funds statement (e.g. the Mayor sub-departments = 'Mayor').  Rows are matched by value: first on at least two
    of the four year columns, then (for the rows left over) on one; every year column is then checked and any
    disagreement is reported as a mismatch."""
    sink.families = []
    if "all_funds" not in stm:
        return
    al_rows, al_cols = stm["all_funds"]
    gf_rows, gf_cols = stm["general_fund"]
    gfd = dict(gf_rows)
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
    used, matched_rows = set(), {}
    for min_years in (2, 1):
        for ri, (lab, vals) in enumerate(al_rows):
            if not any(vals) or ri in matched_rows:
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
                    if m >= min_years and (best is None or (m, -(k - i)) > (best[0], -(best[2] - best[1]))):
                        best = (m, i, k, dict(acc))
            if best is None:
                continue
            m, i, k, acc = best
            matched_rows[ri] = True
            run = units[i:k + 1]
            used.update(range(i, k + 1))
            names_set = set()
            for g, s_ in run:
                names_set |= {s_["name"]} if g.get("use_sec_names", True) else set(g.get("dept_names", set()))
            fam = dict(label=lab, sections=[s_ for g, s_ in run], groups=[g for g, s_ in run], names=names_set, row=ri, years_matched=m)
            sink.families.append(fam)
            names = " + ".join(s_["name"] for g, s_ in run)
            for yb, ci in ycol.items():
                if yb in acc:
                    add_cross(sink, "tier2", "sum of the department sections' by_type totals = citywide all-funds statement row",
                              f"{lab} <- {names} {yb[0]} {yb[1]}", "sum of sections", acc[yb], "all-funds statement", vals[ci] or 0, stm_page(sink, "all"))
            gfs = defaultdict(int)
            for g in {id(g): g for g, s_ in run}.values():
                for st in g.get("prog_states", {}).get("exp", []):
                    if st.get("dept_name") not in names_set:
                        continue
                    for fund, d in st.get("fund_total_vals", {}).items():
                        if is_gf(fund):
                            for ci2, (v, c) in d.items():
                                gfs[(c[0], c[1])] += v
            for yb, ci in ycol.items():
                if yb in acc:
                    gci = next(i2 for i2, c in enumerate(gf_cols) if (c[0], c[1]) == yb)
                    add_cross(sink, "tier3", "sum of the General Fund fund TOTAL rows of the department's EXPENDITURE SUMMARY tables = General Fund statement row",
                              f"{lab} {yb[0]} {yb[1]}", "program tables", gfs.get(yb, 0), "general-fund statement",
                              (gfd.get(lab) or [None] * len(gf_cols))[gci] or 0, stm_page(sink, "gf"))
    for ri, (lab, vals) in enumerate(al_rows):
        if any(vals) and ri not in matched_rows:
            sink.cross.append(dict(tier="tier2", check="sum of a run of department sections' by_type totals = citywide all-funds statement row",
                                   key=lab, a="sections", a_value=None, b="statement", b_value={f"{k[0]} {k[1]}": vals[v] for k, v in ycol.items()},
                                   diff=None, status="no_matching_section_group", page=stm_page(sink, "all")))


def group_prog_totals(g):
    """{(year, basis): DEPARTMENT TOTAL} summed over the group's EXPENDITURE SUMMARY tables, and the General Fund totals"""
    tot, gf = defaultdict(int), defaultdict(int)
    for st in g.get("prog_states", {}).get("exp", []):
        if "dept_total" in st:
            vals, cols, pg = st["dept_total"]
            for ci, v in enumerate(vals):
                if v is not None:
                    tot[(cols[ci][0], cols[ci][1])] += v
        for fund, d in st.get("fund_total_vals", {}).items():
            if is_gf(fund):
                for ci2, (v, c) in d.items():
                    gf[(c[0], c[1])] += v
    return tot, gf


def nested_checks(doc, sink, groups, stm):
    """A 'by type' section can cover a department whose program tables are split over several sections (the 'Health'
    page covers Health-Core and Health-EMS; the EMS section has its own page but no row in the citywide statements).
    For a family followed by sections that belong to no family: family section total = program tables of the family
    plus those of the following unmatched sections."""
    in_fam = {id(s_) for f in sink.families for s_ in f["sections"]}
    gidx = {id(g): i for i, g in enumerate(groups)}
    gf_rows, gf_cols = stm["general_fund"]
    gfd = dict(gf_rows)
    for fam in sink.families:
        last = max(gidx[id(g)] for g in fam["groups"])
        nxt = []
        j = last + 1
        while j < len(groups) and groups[j]["sections"] and not any(id(s_) in in_fam for s_ in groups[j]["sections"]) and not groups[j].get("enterprise"):
            nxt.append(groups[j])
            j += 1
        if not nxt:
            continue
        own = {id(g): g for g in fam["groups"]}.values()
        own_tot = defaultdict(int)
        for g in own:
            for k, v in group_prog_totals(g)[0].items():
                own_tot[k] += v
        sec0 = defaultdict(int)
        for s_ in fam["sections"]:
            t = sec_total(s_)
            for ci, c in enumerate(t[3]):
                sec0[(c[0], c[1])] += t[2][ci] or 0
        if own_tot.get((PD_YEAR, PD_BASIS)) == sec0.get((PD_YEAR, PD_BASIS)):
            continue            # the family's own program tables already add up: the next section is a separate unmatched one
        tot, gf = defaultdict(int), defaultdict(int)
        for g in list(own) + nxt:
            t_, g_ = group_prog_totals(g)
            for k, v in t_.items():
                tot[k] += v
            for k, v in g_.items():
                gf[k] += v
        sec = defaultdict(int)
        for s_ in fam["sections"]:
            t = sec_total(s_)
            for ci, c in enumerate(t[3]):
                sec[(c[0], c[1])] += t[2][ci] or 0
        label = f"{fam['label']} (= {', '.join(g['top'] for g in own)} + nested {', '.join(g['top'] for g in nxt)})"
        for yb, v in sec.items():
            if yb in tot:
                add_cross(sink, "tier3", "family section total = program tables of the family + program tables of the following unmatched (nested) section(s)",
                          f"{label} {yb[0]} {yb[1]}", "section by_type total", v, "program tables", tot[yb], fam["sections"][0]["by_type"])
        for yb in gf:
            gci = next((i for i, c in enumerate(gf_cols) if (c[0], c[1]) == yb), None)
            if gci is not None and fam["label"] in gfd:
                add_cross(sink, "tier3", "General Fund statement row = General Fund fund TOTALs of the family's program tables + following unmatched (nested) section(s)",
                          f"{label} {yb[0]} {yb[1]}", "program tables", gf[yb], "general-fund statement", gfd[fam["label"]][gci] or 0, stm_page(sink, "gf"))
        # FTEs: the family's by-type 'Department FTEs' vs positions of the family and the nested sections
        for g in nxt:
            fam["names"] |= set(g.get("dept_names", set()))
        for s_ in fam["sections"]:
            fr = next((o for o in s_["by_type_rows"] if o[0] == "FTE"), None)
            if not fr:
                continue
            for ci, (yr, basis, _) in enumerate(fr[3]):
                if fr[2][ci] is None:
                    continue
                tot_f = sum(r["fte"] for r in sink.positions if r["dept_name"] in fam["names"] and (r["fiscal_year"], r["basis"]) == (yr, basis))
                add_cross(sink, "tier4", "family by_type page 'Department FTEs' (x100) = FTEs summed from the position rows of the family + nested section(s)",
                          f"{label} {yr} {basis}", "by_type page", round(fr[2][ci] * 100), "positions.csv", round(tot_f * 100), s_["by_type"])
        sink.notes.append(f"nested sections: {[g['top'] for g in nxt]} are covered by the by-type page of {fam['label']!r} (no separate row in the citywide statements)")


def is_gf(fund):
    return bool(fund) and fund.upper().endswith("GENERAL FUND")


def stm_page(sink, key):
    return sink.pages[key][0] if sink.pages.get(key) else None


def enterprise_sub(doc, p):
    """Component-unit / enterprise tables print 'UPPER-CASE NAMEMixed-case Name - Component U' (no gap) before the table
    title; the printed mixed-case name is used."""
    ls = doc.lines(p)
    h = top_and_sub(ls)
    if len(h) >= 3:
        return clean(h[1])
    t = ls[0].text
    t = re.sub(r"\s*(PROGRAM DETAIL|EXPENDITURE SUMMARY|EXPENDITURE|PERSONNEL SUMMARY)$", "", t).strip()
    parts = re.split(r"(?<=[A-Z])(?=[A-Z][a-z])", t, maxsplit=1)
    name = parts[1] if len(parts) == 2 else t
    return clean(name)


def program_pages(doc, g, sink):
    """Route this group's program pages (EXPENDITURE SUMMARY / PROGRAM DETAIL / PERSONNEL SUMMARY) to the parsers.
    Sub-departments are told apart by the sub-department text in each page's header and paired with the
    'Budget Summary' sections of the group by order of appearance."""
    types = sink.section_types
    secs = g["sections"]
    # sub-departments (tables with their own printed header) vs 'by type' sections: paired by order when the counts
    # agree, otherwise the printed sub-department names are used for the program tables
    sub_seq = []
    for p in g["program_pages"]:
        if types[p] == "program_exp" and doc.lines(p):
            h = top_and_sub(doc.lines(p))
            sb = h[1] if len(h) >= 2 else ""
            if not sub_seq or sub_seq[-1] != sb:
                sub_seq.append(sb)
    g["use_sec_names"] = len(sub_seq) == len(secs) and not g.get("enterprise")
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
        if g.get("enterprise"):
            sub = enterprise_sub(doc, p)
        if last_hdr.get(kind) != sub or kind not in cur:
            last_hdr[kind] = sub
            cur[kind] = dict(idx=len(allst[kind]), sub=sub, pages=[])
            allst[kind].append(cur[kind])
        st = cur[kind]
        st["pages"].append(p)
        i = st["idx"]
        if g.get("enterprise"):
            sub = enterprise_sub(doc, p)
            st["sub"] = sub
        if g["use_sec_names"] and i < len(secs):
            dept_name = secs[i]["name"]
        else:
            dept_name = sub or g["top"]
        st["dept_name"] = dept_name
        g.setdefault("dept_names", set()).add(dept_name)
        if kind == "exp":
            parse_program_exp(doc, p, sink, dept_name, st)
        elif kind == "det":
            parse_program_detail(doc, p, sink, dept_name, st)
        else:
            parse_personnel(doc, p, sink, dept_name, st)
    if not g["use_sec_names"] and not g.get("enterprise"):
        sink.notes.append(f"group {g['top']!r}: {len(sub_seq)} sub-department program table(s) {sub_seq} for {len(secs)} 'by type' section(s); "
                          f"program tables carry their printed sub-department names and are compared with the group total")
    g["prog_states"] = allst
    # --- cross-checks of program-level tables against the section's own summary
    sec_tot = {}
    for s_ in secs:
        t = sec_total(s_)
        if t:
            for ci, c in enumerate(t[3]):
                sec_tot[(c[0], c[1])] = sec_tot.get((c[0], c[1]), 0) + (t[2][ci] or 0)
    for kind in ("exp", "det"):
        sts = allst.get(kind, [])
        for st in sts:
            if kind == "exp" and "dept_total" not in st:
                sink.unparsed.append(dict(page=st["pages"][0], what="program expenditure table: no DEPARTMENT TOTAL row found", dept=st["dept_name"]))
        if g["use_sec_names"]:
            for st in sts:
                i = st["idx"]
                tt = sec_total(secs[i]) if i < len(secs) else None
                if tt is None:
                    continue
                if kind == "exp" and "dept_total" in st:
                    vals, cols, pg = st["dept_total"]
                    for ci, v in enumerate(vals):
                        if v is None:
                            continue
                        yb = (cols[ci][0], cols[ci][1])
                        ti = next((k for k, c in enumerate(tt[3]) if (c[0], c[1]) == yb), None)
                        if ti is not None:
                            add_cross(sink, "tier3", "by_program DEPARTMENT TOTAL = section by_type total", f"{secs[i]['name']} {yb[0]} {yb[1]}",
                                      "program table", v, "by_type total", tt[2][ti] or 0, pg)
                elif kind == "det" and st.get("pd_dept_total"):
                    dt = st["pd_dept_total"]
                    ti = next((k for k, c in enumerate(tt[3]) if (c[0], c[1]) == (PD_YEAR, PD_BASIS)), None)
                    if ti is not None:
                        add_cross(sink, "tier3", "PROGRAM DETAIL DEPARTMENTAL TOTAL (2021 adopted) = section by_type total", secs[i]["name"],
                                  "program detail", dt[0], "by_type total", tt[2][ti] or 0, dt[1])
        elif g.get("enterprise"):
            pass            # enterprise funds / component units: no 'by type' section, not part of the citywide statements
        else:
            # group level: the sub-department tables together = the group's sections together
            if kind == "exp":
                acc = defaultdict(int)
                for st in sts:
                    if "dept_total" in st:
                        vals, cols, pg = st["dept_total"]
                        for ci, v in enumerate(vals):
                            if v is not None:
                                acc[(cols[ci][0], cols[ci][1])] += v
                for yb, v in acc.items():
                    if yb in sec_tot:
                        add_cross(sink, "tier3", "sum of the sub-department DEPARTMENT TOTALs = sum of the group's section by_type totals",
                                  f"{g['top']} {yb[0]} {yb[1]}", "program tables", v, "by_type totals", sec_tot[yb], sts[0]["pages"][0])
            else:
                dts = [st["pd_dept_total"] for st in sts if st.get("pd_dept_total")]
                if dts and (PD_YEAR, PD_BASIS) in sec_tot:
                    add_cross(sink, "tier3", "sum of the sub-department PROGRAM DETAIL DEPARTMENTAL TOTALs (2021 adopted) = sum of the group's section by_type totals",
                              g["top"], "program detail", sum(d[0] for d in dts), "by_type totals", sec_tot[(PD_YEAR, PD_BASIS)], dts[0][1])
    for st in allst.get("pers", []):
        if "dept_total" not in st:
            sink.unparsed.append(dict(page=st["pages"][0], what="personnel table: no DEPARTMENT TOTAL row found", dept=st["dept_name"]))


def citywide_checks(doc, sink, groups, stm, funds_src):
    in_fam = {id(s_) for f in sink.families for s_ in f["sections"]}
    secs = [s_ for g in groups for s_ in g["sections"] if id(s_) in in_fam]
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
                add_cross(sink, "tier2", "sum of the family sections' by_type totals = citywide all-funds statement Total", f"{yb[0]} {yb[1]}",
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
                add_cross(sink, "tier2", "sum of the family sections' by_type totals + statement rows without a department section ("
                          + ", ".join(sink.unmatched_statement_rows) + ") = statement Total", f"{yb[0]} {yb[1]}",
                          "sections + unmatched rows", s0 + extra, "statement", r[0]["amount"], None)
    # sum over the fund tables of the printed fund totals = citywide all-funds statement total
    agg0 = getattr(sink, "type_agg", {}).get("__TOTAL__", {})
    for yb, v in agg0.items():
        r = [t for t in sink.totals if t["scope"] == "department_expenditures/all_funds" and (t["fiscal_year"], t["basis"]) == yb]
        if r:
            add_cross(sink, "tier1", "sum over the by-type fund tables of 'Total Expenditures' = citywide all-funds statement Total", f"{yb[0]} {yb[1]}",
                      "fund by-type tables", v, "statement", r[0]["amount"], None)
    # sum over pp.73-84 fund type tables of a type row = sum over sections of that type row
    agg = getattr(sink, "type_agg", {})
    for key, ycs in sums.items():
        if key == "TOTAL":
            continue
        for yb, s0 in ycs.items():
            a = agg.get(key, {}).get(yb)
            if a is None:
                sink.cross.append(dict(tier="tier2", check="sum over pp.73-84 fund tables of a type row = sum over department sections", key=f"{key} {yb}",
                                       a="pp73-84", a_value=None, b="sections", b_value=s0, diff=None, status="no_counterpart", page=None))
            else:
                add_cross(sink, "tier2", "sum over pp.73-84 fund tables of a type row = sum over department sections", f"{key} {yb[0]} {yb[1]}",
                          "pp73-84", a, "sections", s0, None)
    # p80 fund totals vs sum of fund TOTAL rows of program tables, matched by fund code order is not available: compare
    # the GF line of p80 with the sum of the General Fund totals
    s80 = sink.fund_summary.get("p72")
    gfsum = defaultdict(int)
    for g in groups:
        for st in g.get("prog_states", {}).get("exp", []):
            for fund, d in st.get("fund_total_vals", {}).items():
                if is_gf(fund):
                    for ci, (v, c) in d.items():
                        gfsum[(c[0], c[1])] += v
    if s80:
        body, totv, c80, p80 = s80
        gfl = next((vals for lab, vals in body if lab.lower().startswith("general fund")), None)
        if gfl:
            for ci, v in enumerate(gfl):
                yb = (c80[ci][0], c80[ci][1])
                if v is not None and yb in gfsum:
                    add_cross(sink, "tier2", "p72 General Fund line = sum of the General Fund fund TOTAL rows of all EXPENDITURE SUMMARY tables",
                              f"{yb[0]} {yb[1]}", "p72", v, "program tables", gfsum[yb], p80)


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


def spot_checks(sink, n_each=20, seed=2021):
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
                if not hits:       # a label wrapped above and/or below the line that carries the numbers
                    lo = re.sub(r"[^a-z]", "", lab.lower())
                    for k, ln in enumerate(cache[pg]):
                        if needle in ln:
                            around = " ".join(cache[pg][max(0, k - 1):k + 2])
                            if lo and lo in re.sub(r"[^a-z]", "", around.lower()):
                                hits = [ln]
                                break
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
        "tier1": "citywide statements: department expenditures (general/other/all funds), revenues, fund summaries (pp.60, 72, 73-84, 85-90)",
        "tier2": "department by-type (Budget Summary / Expenditure by Type pages) and by-fund (Expenditures by Funding Source pages)",
        "tier3": "program-level detail (EXPENDITURE SUMMARY, PROGRAM DETAIL)",
        "tier4": "positions / FTE (PERSONNEL SUMMARY, by-type Department FTEs, pp.92-94 schedules)"}
    out["column_basis_note"] = {
        "FY19 Actual": "fiscal_year 2019, basis actual",
        "FY20 Adopted (as of 6/01/2020)": "fiscal_year 2020, basis adopted: the book's record of the adopted 2020 budget",
        "FY21 Proposed (as of 10/31/2020)": "fiscal_year 2021, basis proposed",
        "FY21 Adopted (as of 11/29/2020)": "fiscal_year 2021, basis adopted",
        "PROGRAM DETAIL tables": "carry no year header; stored as 2021 adopted (proved by their DEPARTMENTAL TOTAL = by_type FY21 Adopted total)"}
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
