#!/usr/bin/env python3
"""Extract a City of New Orleans Questica/OpenBook operating budget book into the shared
long-format CSVs described in data/extracted/SCHEMA.md.

Usage (project root):
    .venv/bin/python scripts/extract/extract_2026.py                       # 2026 adopted (default)
    .venv/bin/python scripts/extract/extract_2026.py <book_dir> [--out DIR] [--book-id ID]
                                                     [--no-pdf-check] [--pdf-samples N]

<book_dir> is a folder under data/raw/questica/<year>/operating/<kind>_<book>/ that holds
version.json, pages/<pageId>.json and the PDF of the book.  Nothing under data/raw is modified.
book_id is derived from the folder name (e.g. 2026-operating-adopted); output goes to
data/extracted/<book_id>/ :
    department_expenditures.csv revenues.csv department_detail.csv positions.csv
    printed_totals.csv checks.json

Everything is parsed from the `publishedMarkup` HTML of each page (pivot tables whose row
hierarchy is carried by `levelN` CSS classes).  No numbers are typed in by hand: every
amount comes from a printed cell, every check compares printed cells with printed cells.
This script replaces scripts/parse_questica_tables.py (which flattened tables without
building the row tree and so could not foot totals).
"""
import argparse
import csv
import difflib
import glob
import html
import json
import os
import random
import re
import sys
import unicodedata
from collections import OrderedDict, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEFAULT_BOOK = os.path.join(ROOT, "data", "raw", "questica", "2026", "operating",
                            "adopted_2026-operating-budget-book-adopted")

# --------------------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------------------

def norm_ws(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def parse_num(s):
    """'$1,234.00' '-$5.00' '(1,293,164.00)' '3,804.23' -> Decimal; blank/dash -> None."""
    s = norm_ws(s)
    if s in ("", "-", "--", "—", "–", "n/a", "N/A"):
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or s.startswith("$-")
    t = re.sub(r"[,$()\s%-]", "", s)
    try:
        v = Decimal(t)
    except Exception:
        return None
    return -v if neg else v


def to_int(d):
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fmt_amount(d, kind):
    if d is None:
        return ""
    if kind == "fte":
        return "%.2f" % d
    return str(to_int(d))


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


BASIS = {"adopted": "adopted", "proposed": "proposed", "estimate": "estimate", "estimated": "estimate",
         "projected": "projected", "actual": "actual", "actuals": "actual"}


def colspec(label):
    """column header -> ('year', fiscal_year, basis) | ('skip',) | ('unknown',)"""
    l = norm_ws(label)
    m = re.match(r"^(Adopted|Proposed|Estimated?|Projected)\s+(\d{4})$", l, re.I)
    if m:
        return ("year", int(m.group(2)), BASIS[m.group(1).lower()])
    m = re.match(r"^(\d{4})\s+(Actuals?)$", l, re.I)
    if m:
        return ("year", int(m.group(1)), "actual")
    if re.search(r"(dollar|percent|percentage|fte)\s+change|^change$|% change", l, re.I):
        return ("skip",)
    if re.match(r"^Budget Year(\s+Budget Year)?$", l, re.I):
        # 2025 books: one-column chart-feed tables under 'Expenditure by Funding Source' whose header carries
        # neither a fiscal year nor a basis (and whose values match no other table).  Cannot be dated: skipped.
        return ("chart_undated",)
    return ("unknown",)


# --------------------------------------------------------------------------------------
# HTML table parsing
# --------------------------------------------------------------------------------------

class _TP(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.cur, self.row, self.cell, self.depth = [], None, None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.cur = {"rows": []}
        elif self.cur is not None:
            if tag == "tr":
                self.row = {"cls": a.get("class", ""), "cells": []}
            elif tag in ("td", "th"):
                self.cell = {"tag": tag, "cls": a.get("class", ""), "text": "",
                             "rowspan": int(a.get("rowspan") or 1), "colspan": int(a.get("colspan") or 1)}

    def handle_endtag(self, tag):
        if tag == "table":
            if self.depth == 1 and self.cur is not None:
                self.tables.append(self.cur)
                self.cur = None
            self.depth -= 1
        elif self.cur is not None:
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row["cells"].append(self.cell)
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.cur["rows"].append(self.row)
                self.row = None

    def handle_data(self, d):
        if self.cell is not None:
            self.cell["text"] += d


def page_blocks(markup):
    """Ordered content blocks of a page: ('table', rawtable) for PivotTable blocks and
    (blocktype, plaintext) for everything else.  Block boundaries are the grid items
    (`data-content-type=`); this lets us see the heading text sitting above each table."""
    parts = re.split(r'data-content-type="(\w+)"', markup)
    out = []
    for i in range(1, len(parts), 2):
        btype, body = parts[i], parts[i + 1]
        cut = body.find('<div class="content-container')
        seg = body if cut < 0 else body[:cut]
        if btype == "PivotTable":
            p = _TP()
            p.feed(seg)
            for tb in p.tables:
                out.append(("table", tb))
        else:
            txt = re.sub(r"<[^>]+>", " ", seg)
            txt = html.unescape(txt)
            txt = re.sub(r'^\s*gs-x="\d+" gs-y="\d+" gs-w="\d+" gs-h="\d+">', "", txt)
            out.append((btype, norm_ws(txt)))
    return out


class Node:
    __slots__ = ("label", "level", "raw", "vals", "is_total_cls", "parent", "children", "ridx", "path")

    def __init__(self, label, level, raw, is_total_cls, ridx):
        self.label, self.level, self.raw, self.is_total_cls, self.ridx = label, level, raw, is_total_cls, ridx
        self.vals = [parse_num(x) for x in raw]
        self.parent, self.children, self.path = None, [], []

    @property
    def is_grand(self):
        return self.level == 0 and self.label.lower() in ("grand total", "total")


class Table:
    """A pivot table: dims (row hierarchy names), cols (value-column headers), nodes with tree."""

    def __init__(self, raw, page_title, anomalies):
        rows = raw["rows"]
        hdr = [r for r in rows if "pivot-table-header-row" in r["cls"]]
        self.ok = bool(hdr)
        self.page_title = page_title
        self.dims, self.cols, self.nodes, self.specs = [], [], [], []
        self.n_anom = 0
        if not self.ok:
            return
        self.dims = [norm_ws(r["cells"][0]["text"]) for r in hdr if r["cells"]]
        self.cols = [norm_ws(c["text"]) for c in hdr[-1]["cells"][1:]]
        self.specs = [colspec(c) for c in self.cols]
        body = [r for r in rows if "pivot-table-header-row" not in r["cls"]]
        for ri, r in enumerate(body):
            cells = r["cells"]
            if not cells:
                continue
            first = next((c for c in cells if "rowHeaderCell" in c["cls"]), None)
            if first is None:
                anomalies.append(dict(page=page_title, issue="body row without rowHeaderCell", row=ri))
                continue
            vals = [c for c in cells if c is not first]
            m = re.search(r"level(\d+)", first["cls"])
            lvl = int(m.group(1)) if m else 0
            n = Node(norm_ws(first["text"]), lvl, [norm_ws(c["text"]) for c in vals],
                     any("grandTotalCell" in c["cls"] for c in vals), ri)
            if len(n.raw) != len(self.cols):
                anomalies.append(dict(page=page_title, issue="value count != header count", row=n.label,
                                      n_values=len(n.raw), n_cols=len(self.cols)))
            self.nodes.append(n)
        stack = []
        for n in self.nodes:
            while stack and stack[-1].level >= n.level:
                stack.pop()
            n.parent = stack[-1] if stack else None
            if n.parent is not None:
                n.parent.children.append(n)
                if n.parent.level != n.level - 1:
                    anomalies.append(dict(page=page_title, issue="row level skips a level (missing intermediate row)",
                                          row=n.label, level=n.level, parent=n.parent.label))
                    self.n_anom += 1
            elif n.level > 0:
                anomalies.append(dict(page=page_title, issue="indented row with no parent", row=n.label, level=n.level))
                self.n_anom += 1
            lv = [""] * (n.level + 1)
            p = n
            while p is not None:
                if p.level < len(lv):
                    lv[p.level] = p.label
                p = p.parent
            n.path = lv
            stack.append(n)

    def leaves(self):
        return [n for n in self.nodes if not n.children and not n.is_grand]

    def parents(self):
        return [n for n in self.nodes if n.children]

    def grand(self):
        g = [n for n in self.nodes if n.is_grand]
        return g[-1] if g else None


# --------------------------------------------------------------------------------------
# name matching (department page title <-> citywide agency description)
# --------------------------------------------------------------------------------------
_STOP = {"department", "dept", "of", "the", "office", "orleans", "parish", "new", "city", "and", "for", "a"}


def norm_name(s):
    s = strip_accents(s).lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return norm_ws(s)


def tokens(s):
    return [t for t in norm_name(s).split() if t not in _STOP]


def name_score(a, b):
    na, nb = norm_name(a), norm_name(b)
    if na == nb:
        return 1.0
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / float(len(ta | tb))
    cont = len(ta & tb) / float(min(len(ta), len(tb)))
    seq = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(0.5 * jac + 0.5 * seq, 0.9 * cont * (0.5 + 0.5 * seq))


def match_names(left, right, threshold=0.6):
    """one-to-one greedy assignment by descending score.  Returns {left: (right, score)}"""
    cand = []
    for l in left:
        for r in right:
            s = name_score(l, r)
            if s >= threshold:
                cand.append((s, l, r))
    cand.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_l, used_r, out = set(), set(), {}
    for s, l, r in cand:
        if l in used_l or r in used_r:
            continue
        out[l] = (r, round(s, 3))
        used_l.add(l)
        used_r.add(r)
    return out


# --------------------------------------------------------------------------------------
# main extractor
# --------------------------------------------------------------------------------------
TYPE_DIMS = {"Expenditure Type", "Appropriation Type"}
FUND_DIMS = {"Fund Grouping", "Funding Source", "Fund"}


def classify_detail_table(t):
    d = t.dims
    if "Position Name" in d:
        return "positions"
    if len(d) == 1 and d[0] in TYPE_DIMS:
        return "by_type"
    if len(d) == 1 and d[0] in FUND_DIMS:
        return "by_fund"
    if len(d) == 3 and d[0] in TYPE_DIMS and d[1] in FUND_DIMS and d[2] == "Costing Center Name":
        return "by_program"
    if len(d) == 2 and d[0] in TYPE_DIMS and d[1] in FUND_DIMS:
        return "type_fund"
    if d == ["Costing Center Name"]:
        return "cc_flat"
    return None


class Extractor:
    def __init__(self, book_dir, book_id):
        self.book_dir, self.book_id = book_dir, book_id
        self.dept_exp, self.revenues, self.detail, self.positions, self.totals = [], [], [], [], []
        self.checks = []
        self.anomalies = []
        self.unparsed = []
        self.notes = []
        self.pages = []          # (page_id, title, blocks)
        self.page_tables = {}    # title -> n pivot tables
        self.cityw = {}          # (fund_group) -> list of Table
        self.pages_used = defaultdict(list)

    # ---------------- loading ----------------
    def load(self):
        files = sorted(glob.glob(os.path.join(self.book_dir, "pages", "*.json")))
        for f in files:
            d = json.load(open(f))
            title = norm_ws(d.get("title", ""))
            markup = d.get("publishedMarkup") or ""
            blocks = page_blocks(markup)
            n_all = markup.count("<table")
            n_pivot = sum(1 for b in blocks if b[0] == "table")
            self.pages.append(dict(id=d.get("id"), title=title, blocks=blocks, n_all=n_all, n_pivot=n_pivot))
        self.n_pages = len(self.pages)

    # ---------------- generic emit helpers ----------------
    def year_cols(self, t):
        out = []
        for j, sp in enumerate(t.specs):
            if sp[0] == "year":
                out.append((j, sp[1], sp[2]))
            elif sp[0] == "unknown":
                self.unparsed.append(dict(page=t.page_title, what="column header not recognised, column skipped",
                                          header=t.cols[j]))
            elif sp[0] == "chart_undated":
                self.unparsed.append(dict(page=t.page_title, what="undated chart-feed column ('%s': no fiscal year or basis in header) not extracted" % t.cols[j],
                                          header=t.cols[j]))
        return out

    def add_total(self, scope, label, fy, basis, val, page, kind="usd"):
        self.totals.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, scope=scope, label=label,
                                amount=val, page=page, _kind=kind))

    def foot(self, t, scope_prefix, page, kind="usd", emit_totals=True, include_leaves=False, tier_prefix=""):
        """Print every parent / grand-total row of table t into printed_totals and test it
        against the rows it covers (tier 'subtotal' = parent vs its children, tier
        'grand_total' = Grand Total vs sum of every leaf row)."""
        ycols = self.year_cols(t)
        leaves = t.leaves()
        for n in t.nodes:
            is_parent = bool(n.children)
            if not (is_parent or n.is_grand or include_leaves):
                continue
            anc = [x for x in n.path[:-1] if x != ""] if not n.is_grand else []
            scope = scope_prefix + ("/" + "/".join(anc) if anc else "")
            for j, fy, basis in ycols:
                v = n.vals[j] if j < len(n.vals) else None
                if v is None:
                    continue
                if emit_totals:
                    self.add_total(scope, n.label, fy, basis, v, page, kind)
                if is_parent or n.is_grand:
                    if n.is_grand:
                        src = [x for x in leaves if j < len(x.vals) and x.vals[j] is not None]
                        tier = "grand_total"
                    else:
                        src = [x for x in n.children if j < len(x.vals) and x.vals[j] is not None]
                        tier = "subtotal"
                    s = sum((x.vals[j] for x in src), Decimal(0))
                    self.checks.append(self.mk_check(tier, scope, n.label, fy, basis, v, s, len(src), page, kind))

    def mk_check(self, tier, scope, label, fy, basis, printed, summed, nrows, page, kind="usd", extra=None):
        diff = printed - summed
        c = dict(tier=tier, scope=scope, label=label, fiscal_year=fy, basis=basis, page=page,
                 printed=str(printed), extracted_sum=str(summed), difference=str(diff), n_rows=nrows,
                 status="exact" if diff == 0 else "mismatch", kind=kind)
        if extra:
            c.update(extra)
        return c

    # ---------------- page handlers ----------------
    def run(self):
        self.load()
        detail_pages, citywide_done = [], set()
        for pg in self.pages:
            title, tl = pg["title"], pg["title"].lower()
            tabs = [Table(b[1], title, self.anomalies) for b in pg["blocks"] if b[0] == "table"]
            if pg["n_all"] != pg["n_pivot"]:
                self.unparsed.append(dict(page=title, what="non-pivot <table> elements in text blocks (layout/narrative), not parsed",
                                          count=pg["n_all"] - pg["n_pivot"]))
            if not tabs:
                continue
            if tl == "total expenditures by department":
                self.h_citywide(pg, tabs, "all_funds")
            elif tl.startswith("total departmental expenditures"):
                self.h_citywide(pg, tabs, "other_funds" if re.search(r"non[\s-]*ge", tl) else "general_fund")
            elif tl == "statement of revenues":
                self.h_revenues(pg)
            elif tl == "overview of expenditures":
                self.h_overview(pg, tabs, "overview_of_expenditures")
            elif tl == "overview of revenues":
                self.h_overview(pg, tabs, "overview_of_revenues")
            elif tl.startswith("detailed personnel schedule"):
                self.h_personnel(pg, tabs)
            else:
                self.h_detail(pg, tabs)
        self.cross_checks()

    # ---- citywide department tables ----
    def h_citywide(self, pg, tabs, fund_group):
        title = pg["title"]
        for t in tabs:
            if t.dims != ["Agency Description"]:
                self.unparsed.append(dict(page=title, what="citywide table with unexpected row dimension", dims=t.dims))
                continue
            ycols = self.year_cols(t)
            for n in t.nodes:
                if n.is_grand:
                    continue
                for j, fy, basis in ycols:
                    v = n.vals[j] if j < len(n.vals) else None
                    if v is None:
                        continue
                    self.dept_exp.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, fund_group=fund_group,
                                              dept_code="", dept_name=n.label, amount=v, page=title,
                                              _raw=n.raw, _col=j, _node=n, _tbl=t))
            self.foot(t, "department_expenditures/" + fund_group, title)
            self.pages_used["department_expenditures"].append(title)
            self.cityw.setdefault(fund_group, []).append(t)

    # ---- statement of revenues ----
    def h_revenues(self, pg):
        title = pg["title"]
        section, last_text, cat_tables, summary = "", "", OrderedDict(), []
        for btype, c in pg["blocks"]:
            if btype != "table":
                if btype == "TitleText" and re.match(r"^(general fund|other funds)$", c, re.I):
                    section, last_text = c, ""
                elif c:
                    last_text = c
                continue
            t = Table(c, title, self.anomalies)
            if not t.ok:
                self.unparsed.append(dict(page=title, what="revenue table without header rows"))
                continue
            ycols = self.year_cols(t)
            if t.dims == ["Object Name"]:
                cat = last_text
                if not section.lower().startswith("general"):
                    self.unparsed.append(dict(page=title, what="Object Name table outside General Fund section", category=cat))
                fund_group, fund_name = "general_fund", "General Fund"
                for n in t.leaves():
                    for j, fy, basis in ycols:
                        v = n.vals[j] if j < len(n.vals) else None
                        if v is not None:
                            self.revenues.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, fund_group=fund_group,
                                                      fund_name=fund_name, category=cat, line_name=n.label, amount=v,
                                                      page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                self.foot(t, "revenues/General Fund/" + cat, title)
                cat_tables[cat] = t
            elif t.dims == ["Object Category"]:
                # 'General Fund Grand Totals': re-print of every category total plus overall total
                summary.append(t)
                self.foot(t, "revenues/General Fund/summary_by_category", title, include_leaves=True)
            elif t.dims and t.dims[0] in FUND_DIMS and len(t.dims) == 1:
                for n in t.leaves():
                    for j, fy, basis in ycols:
                        v = n.vals[j] if j < len(n.vals) else None
                        if v is not None:
                            self.revenues.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, fund_group="other_funds",
                                                      fund_name=n.label, category="", line_name=n.label, amount=v,
                                                      page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                self.foot(t, "revenues/Other Funds", title)
                self.other_rev_table = t
            else:
                self.unparsed.append(dict(page=title, what="revenue table with unexpected dims", dims=t.dims))
        self.rev_cat_tables, self.rev_summary = cat_tables, summary
        self.pages_used["revenues"].append(title)

    # ---- overview pages ----
    def h_overview(self, pg, tabs, key):
        title = pg["title"]
        for t in tabs:
            if not t.ok:
                continue
            self.foot(t, key, title, include_leaves=True)
            self.overview = getattr(self, "overview", {})
            self.overview.setdefault(key, []).append(t)
        self.pages_used["printed_totals"].append(title)

    # ---- citywide personnel schedule ----
    def h_personnel(self, pg, tabs):
        title, tl = pg["title"], pg["title"].lower()
        kind = "non_general" if "non-general" in tl or "non general" in tl else ("general" if "general fund" in tl else "all")
        for t in tabs:
            if len(t.dims) != 1:
                self.unparsed.append(dict(page=title, what="personnel schedule with unexpected dims", dims=t.dims))
                continue
            self.foot(t, "positions/citywide_schedule/" + kind, title, kind="fte", include_leaves=True)
            self.sched = getattr(self, "sched", {})
            self.sched.setdefault(kind, []).append(t)
        self.pages_used["printed_totals"].append(title)

    # ---- department pages ----
    def h_detail(self, pg, tabs):
        title = pg["title"]
        any_ok = False
        by_class = defaultdict(list)
        seen_sig = set()
        for t in tabs:
            if t.ok:
                sig = (tuple(t.dims), tuple(t.cols), tuple((n.level, n.label, tuple(n.raw)) for n in t.nodes))
                if sig in seen_sig:
                    self.anomalies.append(dict(page=title, issue="identical table printed twice on the page (source repeats it under both chart headings); second copy skipped",
                                               dims=t.dims, cols=t.cols))
                    continue
                seen_sig.add(sig)
            cl = classify_detail_table(t) if t.ok else None
            if cl is None:
                self.unparsed.append(dict(page=title, what="table not recognised (dims %s, cols %s)" % (t.dims, t.cols)))
                continue
            by_class[cl].append(t)
        if not by_class:
            return
        # 2025 books: under 'Expenditure by Type' the FY2023 actuals table is sometimes bound to the Fund
        # dimension instead of Expenditure Type, so a page can print its by-fund actuals twice (once as 'Fund',
        # once as 'Fund Grouping') with the same Grand Total.  Keep one (prefer 'Fund Grouping'), never both.
        bf = by_class.get("by_fund", [])
        if len(bf) > 1:
            def yb_totals(tb):
                gt_ = tb.grand()
                return {(fy, b): (gt_.vals[j] if gt_ is not None else None) for j, fy, b in self.year_cols_quiet(tb)}
            keep = []
            for t in sorted(bf, key=lambda x: 0 if x.dims[0] == "Fund Grouping" else 1):
                sig = yb_totals(t)
                dup = next((k for k in keep if any(v is not None and yb_totals(k).get(yb) == v for yb, v in sig.items())), None)
                if dup is not None:
                    self.anomalies.append(dict(page=title, issue="second by-fund table with the same year column and Grand Total as another by-fund table on the page; skipped to avoid double counting",
                                               skipped_dims=t.dims, kept_dims=dup.dims, cols=t.cols))
                else:
                    keep.append(t)
            by_class["by_fund"] = [t for t in bf if any(t is k for k in keep)]
        self.dept_pages = getattr(self, "dept_pages", OrderedDict())
        self.dept_pages[title] = by_class
        for cl, ts in by_class.items():
            for t in ts:
                ycols = self.year_cols(t)
                if cl in ("by_type", "by_fund"):
                    for n in t.leaves():
                        for j, fy, basis in ycols:
                            v = n.vals[j] if j < len(n.vals) else None
                            if v is not None:
                                self.detail.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, dept_code="",
                                                        dept_name=title, table=cl, dim1=n.label, dim2="", amount=v,
                                                        page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                    self.foot(t, "department_detail/%s/%s" % (title, cl), title)
                elif cl == "by_program":
                    for n in t.leaves():
                        p = n.path + [""] * (3 - len(n.path))
                        if n.level != 2:
                            self.anomalies.append(dict(page=title, issue="program table leaf not at cost-centre level", row=n.label, level=n.level))
                        cc = n.label if n.level == 2 else ""
                        dim2 = "%s | %s" % (p[0], p[1] if n.level >= 1 else "")
                        if n.level != 2:
                            dim2 = "%s | %s" % (p[0], n.label if n.level == 1 else "")
                        for j, fy, basis in ycols:
                            v = n.vals[j] if j < len(n.vals) else None
                            if v is not None:
                                self.detail.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, dept_code="",
                                                        dept_name=title, table="by_program", dim1=cc, dim2=dim2, amount=v,
                                                        page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                    self.foot(t, "department_detail/%s/by_program" % title, title)
                elif cl == "type_fund":
                    # not emitted (schema has no home); footed only.  Marginals are in by_type / by_fund.
                    self.foot(t, "department_detail/%s/type_fund_not_emitted" % title, title, emit_totals=False)
                    # ...except that its level-0 rows ARE the page's Expenditure-by-Type subtotals: where the page
                    # has no by_type table for a year column (2025 books: FY2023 actuals), emit those as by_type.
                    have = {(fy, b) for tt in by_class.get("by_type", []) for _, fy, b in self.year_cols_quiet(tt)}
                    need = [(j, fy, b) for j, fy, b in ycols if (fy, b) not in have]
                    if need:
                        g = t.grand()
                        lvl0 = [n for n in t.nodes if n.level == 0 and not n.is_grand]
                        for n in lvl0:
                            for j, fy, basis in need:
                                v = n.vals[j] if j < len(n.vals) else None
                                if v is not None:
                                    self.detail.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, dept_code="",
                                                            dept_name=title, table="by_type", dim1=n.label, dim2="", amount=v,
                                                            page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                        if g is not None:
                            for j, fy, basis in need:
                                v = g.vals[j] if j < len(g.vals) else None
                                if v is None:
                                    continue
                                self.add_total("department_detail/%s/by_type" % title, g.label, fy, basis, v, title)
                                s_ = sum((n.vals[j] for n in lvl0 if n.vals[j] is not None), Decimal(0))
                                self.checks.append(self.mk_check("grand_total", "department_detail/%s/by_type" % title, g.label, fy, basis, v, s_,
                                                                 len([n for n in lvl0 if n.vals[j] is not None]), title,
                                                                 extra=dict(note="by_type rows taken from the level-0 rows of the Expenditure Type x Funding Source table (page has no stand-alone by_type table for this year)")))
                        self.unparsed.append(dict(page=title, what="type x fund table (%s): level-0 (expenditure type) rows emitted as by_type for %s; type x fund split itself not emitted (schema has no table for it)"
                                                  % (t.dims[0], ", ".join("%s/%s" % (fy, b) for j, fy, b in need)), cols=t.cols))
                    else:
                        self.unparsed.append(dict(page=title, what="type x fund table (%s) footed but not emitted: schema has no table for it" % t.dims[0],
                                                  cols=t.cols))
                elif cl == "cc_flat":
                    self.unparsed.append(dict(page=title, what="flat costing-centre table not emitted (duplicates by_program)", cols=t.cols))
                elif cl == "positions":
                    for n in t.leaves():
                        p = n.path + [""] * (4 - len(n.path))
                        fund, prog, pos, cls = p[0], p[1], p[2], (p[3] if n.level == 3 else "")
                        if n.level == 2:
                            pos = n.label
                        if n.level < 2:
                            self.anomalies.append(dict(page=title, issue="position leaf above position level", row=n.label, level=n.level))
                            pos = ""
                        for j, fy, basis in ycols:
                            v = n.vals[j] if j < len(n.vals) else None
                            if v is not None:
                                self.positions.append(dict(book_id=self.book_id, fiscal_year=fy, basis=basis, dept_code="",
                                                           dept_name=title, program=prog, fund=fund, position_title=pos,
                                                           pay_grade=cls, fte=v, page=title, _raw=n.raw, _col=j, _node=n, _tbl=t))
                    self.foot(t, "positions/%s" % title, title, kind="fte")
        self.pages_used["department_detail"].append(title)

    # ---------------- cross checks ----------------
    def cross_checks(self):
        self.xc = OrderedDict()
        P = self.checks

        def agg(rows, keyf):
            d = defaultdict(lambda: Decimal(0))
            cnt = defaultdict(int)
            for r in rows:
                d[keyf(r)] += r["amount"]
                cnt[keyf(r)] += 1
            return d, cnt

        # 1. general + other = all, per agency / year / basis
        by = defaultdict(dict)
        for r in self.dept_exp:
            by[(r["dept_name"], r["fiscal_year"], r["basis"])][r["fund_group"]] = r["amount"]
        for (dn, fy, b), fg in sorted(by.items()):
            if "all_funds" not in fg:
                self.checks.append(dict(tier="cross_table", scope="gf_plus_other_eq_all", label=dn, fiscal_year=fy, basis=b,
                                        status="not_checkable", note="agency has no all_funds row; general=%s other=%s" % (fg.get("general_fund"), fg.get("other_funds")),
                                        page="department_expenditures", kind="usd"))
                continue
            g, o, a = fg.get("general_fund"), fg.get("other_funds"), fg["all_funds"]
            s = (g or Decimal(0)) + (o or Decimal(0))
            extra = {"missing_component": [k for k in ("general_fund", "other_funds") if k not in fg]}
            self.checks.append(self.mk_check("cross_table", "gf_plus_other_eq_all", dn, fy, b, a, s, 2 - len(extra["missing_component"]),
                                             "department_expenditures", extra=extra))
        # also: totals of each citywide table vs sum of rows are already tier grand_total; GF + other vs all grand totals
        gt = {}
        for fg, ts in self.cityw.items():
            for t in ts:
                g = t.grand()
                if g is None:
                    continue
                for j, sp in enumerate(t.specs):
                    if sp[0] == "year":
                        gt[(fg, sp[1], sp[2])] = g.vals[j]
        for (fg, fy, b), v in sorted(gt.items()):
            if fg == "all_funds":
                g, o = gt.get(("general_fund", fy, b)), gt.get(("other_funds", fy, b))
                if g is None or o is None:
                    continue
                self.checks.append(self.mk_check("cross_table", "gf_plus_other_eq_all/printed_grand_totals", "Grand Total", fy, b, v, g + o, 2,
                                                 "department_expenditures"))

        # 2. department pages vs citywide figures (groups of pages <-> one citywide agency)
        self.build_groups()
        cw = defaultdict(dict)
        for r in self.dept_exp:
            cw[(r["dept_name"], r["fiscal_year"], r["basis"])][r["fund_group"]] = r["amount"]
        self.cw = cw
        for grp, pages in self.groups.items():
            for cl in ("by_type", "by_fund", "by_program"):
                tot = defaultdict(lambda: Decimal(0))
                gfs = defaultdict(lambda: Decimal(0))
                has = defaultdict(list)
                for pg in pages:
                    for t in self.dept_pages.get(pg, {}).get(cl, []):
                        g = t.grand()
                        if g is None:
                            continue
                        for j, fy, b in self.year_cols_quiet(t):
                            if g.vals[j] is not None:
                                tot[(fy, b)] += g.vals[j]
                                has[(fy, b)].append(pg)
                            if cl == "by_fund":
                                for n in t.nodes:
                                    if n.level == 0 and not n.is_grand and re.match(r"^(\d+(-\w+)?\s*-\s*)?general fund", n.label.lower()) and n.vals[j] is not None:
                                        gfs[(fy, b)] += n.vals[j]
                for (fy, b), v in sorted(tot.items()):
                    agency = self.agency_for(grp, b)
                    sc = "dept_%s_vs_citywide_all_funds" % cl + ("_inferred_link" if self.group_link.get(grp) else "")
                    ref = cw.get((agency, fy, b), {}).get("all_funds") if agency else None
                    extra = dict(citywide_agency=agency, pages=has[(fy, b)],
                                 note="printed=citywide all_funds figure, extracted_sum=Grand Total of the department page table(s)")
                    if ref is None:
                        self.checks.append(dict(tier="cross_table", scope=sc, label=grp, fiscal_year=fy, basis=b, page=grp, status="not_checkable",
                                                kind="usd", printed=None, extracted_sum=str(v),
                                                note="no citywide all_funds row could be matched by name (agency=%s)" % agency, pages=has[(fy, b)]))
                    elif self.partial_group_not_checkable and len(pages) > 1 and \
                            [pg for pg in pages if not self.page_has(pg, cl, fy, b)]:
                        # some pages of the group print no table of this kind at all (2025 books bind the
                        # 'Expenditure by Type' / 'Funding Source' chart to another dimension on some pages),
                        # so the group sum is incomplete by construction: not a like-for-like comparison.
                        self.checks.append(dict(tier="cross_table", scope=sc, label=grp, fiscal_year=fy, basis=b, page=grp, status="not_checkable",
                                                kind="usd", printed=str(ref), extracted_sum=str(v), citywide_agency=agency, pages=has[(fy, b)],
                                                note="pages of the group without a %s table for this year/basis: %s" % (cl, [pg for pg in pages if not self.page_has(pg, cl, fy, b)])))
                    else:
                        ck = self.mk_check("cross_table", sc, grp, fy, b, ref, v, len(has[(fy, b)]), grp, extra=extra)
                        if ck["status"] == "mismatch":
                            diff = ref - v
                            for pg in pages:
                                for t in self.dept_pages.get(pg, {}).get("by_fund", []):
                                    for j, f2, b2 in self.year_cols_quiet(t):
                                        if (f2, b2) != (fy, b):
                                            continue
                                        for n in t.nodes:
                                            if not n.is_grand and n.vals[j] is not None and n.vals[j] != 0 and abs(n.vals[j]) == abs(diff):
                                                ck.setdefault("difference_equals_page_row", []).append("%s: by_fund '%s'" % (pg, n.label))
                        self.checks.append(ck)
                    if cl == "by_fund":
                        refg = cw.get((agency, fy, b), {}).get("general_fund") if agency else None
                        if refg is not None:
                            self.checks.append(self.mk_check("cross_table", "dept_by_fund_general_fund_vs_citywide_general_fund", grp, fy, b,
                                                             refg, gfs.get((fy, b), Decimal(0)), len(has[(fy, b)]), grp,
                                                             extra=dict(citywide_agency=agency, pages=has[(fy, b)])))
        # sum of every department page vs citywide Grand Total
        for fy_b in sorted({k for g in self.groups for cl in ("by_type",) for pg in self.groups[g] for t in self.dept_pages.get(pg, {}).get(cl, [])
                            for k in [(fy, b) for j, fy, b in self.year_cols_quiet(t)]}):
            fy, b = fy_b
            s_ = Decimal(0)
            n_ = 0
            for pg in self.dept_pages:
                for t in self.dept_pages[pg].get("by_type", []):
                    g = t.grand()
                    for j, f2, b2 in self.year_cols_quiet(t):
                        if (f2, b2) == fy_b and g is not None and g.vals[j] is not None:
                            s_ += g.vals[j]
                            n_ += 1
            ref = self.printed_grand("all_funds", fy, b)
            if ref is not None:
                self.checks.append(self.mk_check("cross_table", "sum_of_all_dept_page_by_type_vs_citywide_grand_total", "Grand Total", fy, b, ref, s_, n_,
                                                 "all department pages", extra=dict(note="includes component-unit / enterprise pages that have a by_type table (none in 2026)")))
        # 3. FTE
        self.fte_checks()
        # 4. overview / statement cross-tables
        self.overview_checks()

    # ---- grouping of department pages (TOC nesting) and page -> citywide agency matching ----
    # Page titles that name a citywide agency differently.  Keys/values are names only (never numbers);
    # every other link is made by TOC nesting or fuzzy name matching.  A wrong alias would show up as a
    # mismatch, not hide one.
    ALIASES = {
        "Finance Director's Office": "Department of Finance",
        "Fire Administration": "New Orleans Fire Department",
        "Office of Superintendent": "New Orleans Police Department",
        "OCD - Grants": "Office of Housing and Community Development",
        "Health": "Department of Health",
        "Health - EMS": "Department of Health",
        "Law Department": "City Attorney's Office - Law Department",
    }

    # Links that were NOT visible from page titles or the table of contents: these four pages have no citywide
    # line of their own, and the only unclaimed non-pass-through citywide line is 'Miscellaneous'.  The link was
    # found by comparing totals, so the resulting checks are confirmatory only; they are reported under their own
    # scope name (suffix _inferred_link) and counted separately.
    INFERRED_ALIASES = {
        "Chief Administrative Office": "Miscellaneous",
        "Office of Police Secondary Employment": "Miscellaneous",
        "Risk Management": "Miscellaneous",
        "New Orleans Recreation Development Commission": "Miscellaneous",
    }

    # 2025 books: the table of contents is flat (no parent/sub-page nesting) and several citywide lines
    # (Mayor's Office, Department of Finance, Department of Health, Miscellaneous) are the sum of several
    # department pages.  Name-only links; the dept-page-vs-citywide checks then confirm or refute them
    # (Mayor's Office = 5 Mayor pages, Finance = 6 pages, Health = 2 pages and Miscellaneous = CAO + Police
    # Secondary Employment + Risk Management + NORDC all foot exactly).
    ALIASES_2025 = {
        "Mayor's Office": "Mayor's Office",
        "Mayor - Criminal Justice Coordination": "Mayor's Office",
        "Mayor's Office Homeland Security": "Mayor's Office",
        "Mayor - Community & Economic Development": "Mayor's Office",
        "Mayor's Office of Resilience & Sustainability": "Mayor's Office",
        "Finance Director's Office": "Department of Finance",
        "Bureau of Accounting": "Department of Finance",
        "Bureau of Purchasing": "Department of Finance",
        "Bureau of Retirement": "Department of Finance",
        "Bureau of Revenue": "Department of Finance",
        "Bureau of Treasury": "Department of Finance",
        "Fire Administration": "New Orleans Fire Department",
        "Office of Superintendent": "New Orleans Police Department",
        "Health": "Department of Health",
        "Health - EMS": "Department of Health",
        "Law Department": "City Attorney's Office - Law Department",
        "Office of Community Development": "Office of Housing and Community Development",
        "Library": "New Orleans Public Library",
        "Mosquito Control Board": "Mosquito, Termite and Rodent Control Board",
    }
    INFERRED_ALIASES_2025 = {
        "Chief Administrative Office": "Miscellaneous",
        "Office of Police Secondary Employment": "Miscellaneous",
        "Risk Management": "Miscellaneous",
        "New Orleans Recreation Development Commission": "Miscellaneous",
    }
    # pages with no citywide line of their own that fuzzy matching would otherwise attach to a wrong agency
    NO_CITYWIDE_LINE_2025 = {"Chief Administrative Office - ITI", "FEMA Public Assistance Grants"}

    # 2025 proposed book: different page titles, and its table of contents does nest (CAO > ITI, FEMA;
    # Mayor's Office > 4 pages; Finance > 5 bureaus), so only the title variants need linking.
    ALIASES_2025P = {
        "Chief Administrative Office": "Chief Administrative Office - Core",
        "Department of Health - Emergency Medical Services": "Department of Health",
        "Health": "Department of Health",
        "Law Department": "City Attorney's Office - Law Department",
        "Office of Community Development": "Office of Housing and Community Development",
        "Code Enforcement": "Department of Code Enforcment",
        # the all-funds table prints the Miscellaneous line (GF) under the name of the Non-GF line
        # 'Office of Police Secondary Employment': the four pages below foot to that line exactly
        "Miscellaneous: Chief Administrative Office": "Office of Police Secondary Employment",
        "New Orleans Recreation Development Commission": "Office of Police Secondary Employment",
        "Risk Management": "Office of Police Secondary Employment",
        "Office of Police Secondary Employment": "Office of Police Secondary Employment",
    }
    partial_group_not_checkable = False

    def build_groups(self):
        self.partial_group_not_checkable = self.book_id.startswith("2025-")
        if self.book_id == "2025-operating-proposed":
            self.ALIASES, self.INFERRED_ALIASES = self.ALIASES_2025P, {}
            self.no_line = set()
        elif self.book_id == "2026-operating-proposed":
            # in this book the CAO page group foots to 'Chief Administrative Office - Core', not to Miscellaneous
            self.ALIASES = dict(self.ALIASES, **{"Chief Administrative Office": "Chief Administrative Office - Core"})
            self.INFERRED_ALIASES = {k: v for k, v in self.INFERRED_ALIASES.items() if k != "Chief Administrative Office"}
            self.no_line = set()
        elif self.book_id.startswith("2025-"):
            self.ALIASES, self.INFERRED_ALIASES = self.ALIASES_2025, self.INFERRED_ALIASES_2025
            self.no_line = self.NO_CITYWIDE_LINE_2025
        else:
            self.no_line = set()
        id2title = {p["id"]: p["title"] for p in self.pages}
        group_of = {}
        try:
            toc = json.load(open(os.path.join(self.book_dir, "version.json")))["tableOfContents"]["tableOfContentsItems"]
        except Exception:
            toc = []

        def collect(item, leader, out):
            pid = item.get("digitalBudgetBookPageId")
            t = id2title.get(pid)
            if t:
                out[t] = leader
            for c in item.get("tableOfContentItems") or []:
                collect(c, leader, out)
        for top in toc:
            if norm_ws(top.get("title", "")).lower() == "departmental budget summaries":
                for c in sorted(top.get("tableOfContentItems") or [], key=lambda x: x.get("sort", 0)):
                    lt = id2title.get(c.get("digitalBudgetBookPageId"))
                    if lt:
                        collect(c, lt, group_of)
        self.group_of = {}
        self.groups = OrderedDict()
        for pg in self.dept_pages:
            g = group_of.get(pg, pg)
            # a page may be aliased to an agency under a different grouping (e.g. Health - EMS -> Department of Health)
            self.group_of[pg] = g
        # alias-merge groups that share an alias target
        tgt = {}
        self.group_link = {}
        # an alias target that is itself a page title names its own group (keeps e.g. "Mayor's Office" as the group name)
        for a in list(self.ALIASES.values()) + list(self.INFERRED_ALIASES.values()):
            if a in self.group_of:
                tgt.setdefault(a, self.group_of[a])
        for pg, g in list(self.group_of.items()):
            a = self.ALIASES.get(pg) or self.ALIASES.get(g)
            inferred = False
            if not a and (pg in self.INFERRED_ALIASES or g in self.INFERRED_ALIASES):
                a, inferred = self.INFERRED_ALIASES.get(pg) or self.INFERRED_ALIASES.get(g), True
            if a:
                tgt.setdefault(a, g)
                self.group_of[pg] = tgt[a]
                if inferred:
                    self.group_link[tgt[a]] = "inferred"
        for pg, g in self.group_of.items():
            self.groups.setdefault(g, []).append(pg)
        cur_names = list(OrderedDict.fromkeys(r["dept_name"] for r in self.dept_exp if r["fund_group"] == "all_funds" and r["basis"] != "actual"))
        act_names = list(OrderedDict.fromkeys(r["dept_name"] for r in self.dept_exp if r["fund_group"] == "all_funds" and r["basis"] == "actual"))
        self.agency_map = {}
        for kind, names in (("current", cur_names), ("actual", act_names)):
            q = {}
            for g, pgs in self.groups.items():
                al = next((self.ALIASES[p] for p in [g] + pgs if p in self.ALIASES), None) or \
                    next((self.INFERRED_ALIASES[p] for p in [g] + pgs if p in self.INFERRED_ALIASES), None)
                q[g] = al if al else g
                if g in self.no_line:
                    q.pop(g)
            direct = {g: a for g, a in q.items() if a in names}
            rest_q = [g for g in q if g not in direct]
            used = set(direct.values())
            fz = match_names([q[g] for g in rest_q], [n for n in names if n not in used])
            m = OrderedDict()
            for g in q:
                if g in direct:
                    m[g] = (direct[g], 1.0, "alias/exact")
                elif q[g] in fz:
                    m[g] = (fz[q[g]][0], fz[q[g]][1], "fuzzy")
            self.agency_map[kind] = m
        self.unmatched_pages = {k: [g for g in self.groups if g not in m] for k, m in self.agency_map.items()}
        self.unmatched_agencies = {k: [n for n in nm if n not in {v[0] for v in self.agency_map[k].values()}]
                                   for k, nm in (("current", cur_names), ("actual", act_names))}

    def page_has(self, pg, cl, fy, b):
        return any((fy, b) in {(f2, b2) for _, f2, b2 in self.year_cols_quiet(t)} for t in self.dept_pages.get(pg, {}).get(cl, []))

    def agency_for(self, group, basis):
        m = self.agency_map["actual" if basis == "actual" else "current"].get(group)
        return m[0] if m else None

    def year_cols_quiet(self, t):
        return [(j, sp[1], sp[2]) for j, sp in enumerate(t.specs) if sp[0] == "year"]

    def fte_checks(self):
        pos = self.positions
        # per department page: positions sum vs page Grand Total is tier grand_total.  Here: vs citywide schedules.
        sched = getattr(self, "sched", {})
        sched_rows = {}
        for kind, ts in sched.items():
            for t in ts:
                for n in t.nodes:
                    for j, fy, b in self.year_cols_quiet(t):
                        sched_rows[(kind, n.label, fy, b)] = n.vals[j]
        by_dept = defaultdict(lambda: Decimal(0))
        by_dept_gf = defaultdict(lambda: Decimal(0))
        by_fund = defaultdict(lambda: Decimal(0))
        tot_gf = defaultdict(lambda: Decimal(0))
        tot_all = defaultdict(lambda: Decimal(0))
        for r in pos:
            k = (r["dept_name"], r["fiscal_year"], r["basis"])
            by_dept[k] += r["fte"]
            tot_all[(r["fiscal_year"], r["basis"])] += r["fte"]
            if r["fund"].lower().startswith("general fund"):
                by_dept_gf[k] += r["fte"]
                tot_gf[(r["fiscal_year"], r["basis"])] += r["fte"]
            by_fund[(r["fund"], r["fiscal_year"], r["basis"])] += r["fte"]
        grp_sum, grp_sum_gf = defaultdict(lambda: Decimal(0)), defaultdict(lambda: Decimal(0))
        for (dn, fy, b), v in by_dept.items():
            grp_sum[(self.group_of.get(dn, dn), fy, b)] += v
        for (dn, fy, b), v in by_dept_gf.items():
            grp_sum_gf[(self.group_of.get(dn, dn), fy, b)] += v
        by_dept, by_dept_gf = grp_sum, grp_sum_gf
        for (dn, fy, b), s in sorted(by_dept.items()):
            ag = self.agency_for(dn, b)
            ref = sched_rows.get(("all", ag, fy, b)) if ag else None
            sc = "fte_dept_positions_vs_schedule_all_funds"
            if ref is None:
                self.checks.append(dict(tier="cross_table", scope=sc, label=dn, fiscal_year=fy, basis=b, status="not_checkable",
                                        note="no schedule row matched (agency=%s)" % ag, printed=None, kind="fte", page="positions"))
            else:
                self.checks.append(self.mk_check("cross_table", sc, dn, fy, b, ref, s, 1, "positions", "fte"))
            refg = sched_rows.get(("general", ag, fy, b)) if ag else None
            sg = by_dept_gf.get((dn, fy, b), Decimal(0))
            if refg is not None or sg != 0:
                sc = "fte_dept_positions_general_fund_vs_schedule_general_fund"
                if refg is None:
                    self.checks.append(self.mk_check("cross_table", sc, dn, fy, b, Decimal(0), sg, 1, "positions", "fte",
                                                     extra=dict(note="positions show General Fund FTE but no GF schedule row found")))
                else:
                    self.checks.append(self.mk_check("cross_table", sc, dn, fy, b, refg, sg, 1, "positions", "fte"))
        # 2025 books: the non-general schedule is labelled by agency (as the other two schedules are), not by fund.
        # Then check agencies one by one (positions of the page group minus its General Fund positions), and
        # report every schedule agency that no department page group could be matched to.
        agency_names = {x["dept_name"] for x in self.dept_exp if x["fund_group"] == "all_funds"}
        ng_labels = {k[1] for k in sched_rows if k[0] == "non_general" and k[1].lower() != "grand total"}
        agency_sched = bool(ng_labels) and len(ng_labels & agency_names) * 2 >= len(ng_labels)
        if agency_sched:
            a2g = {}
            for kind_ in ("current", "actual"):
                for g_, v_ in self.agency_map[kind_].items():
                    a2g.setdefault(v_[0], g_)
            for (kind, label, fy, b), v in sorted(sched_rows.items(), key=lambda x: str(x[0])):
                if label.lower() == "grand total" or v is None:
                    continue
                g_ = a2g.get(label)
                if g_ is None:
                    self.checks.append(dict(tier="cross_table", scope="fte_schedule_agency_vs_positions_" + kind, label=label, fiscal_year=fy, basis=b,
                                            status="not_checkable", printed=str(v), kind="fte", page="positions",
                                            note="no department page group is linked to this schedule agency"))
                    continue
                if kind == "non_general":
                    s_ = by_dept.get((g_, fy, b), Decimal(0)) - by_dept_gf.get((g_, fy, b), Decimal(0))
                    self.checks.append(self.mk_check("cross_table", "fte_dept_positions_non_general_vs_schedule_non_general", label, fy, b, v, s_, 1, "positions", "fte"))
                elif (g_, fy, b) not in by_dept and v != 0:
                    self.checks.append(dict(tier="cross_table", scope="fte_schedule_agency_vs_positions_" + kind, label=label, fiscal_year=fy, basis=b,
                                            status="not_checkable", printed=str(v), kind="fte", page="positions",
                                            note="schedule agency linked to page group %s but that group has no position rows" % g_))
        # GF schedule rows with no positions at all
        for (kind, label, fy, b), v in sorted(sched_rows.items(), key=lambda x: str(x[0])):
            if kind == "non_general" and agency_sched:
                continue
            if kind == "non_general" and not (label.lower() in ("grand total",)):
                s = by_fund.get((label, fy, b))
                sc = "fte_fund_positions_vs_schedule_non_general"
                if v is None:
                    continue
                if s is None:
                    self.checks.append(dict(tier="cross_table", scope=sc, label=label, fiscal_year=fy, basis=b, status="not_checkable",
                                            note="fund not found in department position tables", printed=str(v), kind="fte", page="positions"))
                else:
                    self.checks.append(self.mk_check("cross_table", sc, label, fy, b, v, s, 1, "positions", "fte"))
            if label.lower() == "grand total":
                if kind == "general":
                    s = tot_gf.get((fy, b))
                    sc = "fte_total_positions_vs_schedule_general_fund_grand_total"
                elif kind == "all":
                    s = tot_all.get((fy, b))
                    sc = "fte_total_positions_vs_schedule_all_funds_grand_total"
                else:
                    s = (tot_all.get((fy, b), Decimal(0)) - tot_gf.get((fy, b), Decimal(0))) if (fy, b) in tot_all else None
                    sc = "fte_total_positions_vs_schedule_non_general_grand_total"
                if v is not None and s is not None:
                    self.checks.append(self.mk_check("cross_table", sc, "Grand Total", fy, b, v, s, 1, "positions", "fte"))
        # schedule GF + NonGF = All grand totals
        for (kind, label, fy, b), v in list(sched_rows.items()):
            if kind == "all" and label.lower() == "grand total":
                g = sched_rows.get(("general", label, fy, b))
                n = sched_rows.get(("non_general", label, fy, b))
                if g is not None and n is not None and v is not None:
                    self.checks.append(self.mk_check("cross_table", "fte_schedule_general_plus_non_general_eq_all", "Grand Total", fy, b, v, g + n, 2,
                                                     "positions", "fte"))
        self.fte_totals = {"%s/%s" % k: str(v) for k, v in tot_all.items()}
        self.fte_totals_gf = {"%s/%s" % k: str(v) for k, v in tot_gf.items()}

    def overview_checks(self):
        ov = getattr(self, "overview", {})
        sumrows = defaultdict(lambda: Decimal(0))
        for r in self.dept_exp:
            sumrows[(r["fund_group"], r["fiscal_year"], r["basis"])] += r["amount"]
        ex = ov.get("overview_of_expenditures", [])
        for t in ex:
            ycols = self.year_cols_quiet(t)
            if not ycols:
                continue
            gen = next((n for n in t.nodes if n.level == 0 and n.label.lower() == "general fund"), None)
            g = t.grand()
            for j, fy, b in ycols:
                if g is not None and g.vals[j] is not None:
                    allv = sumrows.get(("all_funds", fy, b))
                    if allv is not None:
                        self.checks.append(self.mk_check("cross_table", "overview_expenditures_grand_total_vs_sum_of_dept_all_funds", "Grand Total", fy, b,
                                                         g.vals[j], allv, 1, "Overview of Expenditures"))
                    pg = self.printed_grand("all_funds", fy, b)
                    if pg is not None:
                        self.checks.append(self.mk_check("cross_table", "overview_expenditures_grand_total_vs_printed_total_expenditures_by_department",
                                                         "Grand Total", fy, b, g.vals[j], pg, 1, "Overview of Expenditures"))
                if gen is not None and gen.vals[j] is not None:
                    gv = sumrows.get(("general_fund", fy, b))
                    if gv is not None:
                        self.checks.append(self.mk_check("cross_table", "overview_expenditures_general_fund_vs_sum_of_dept_general_fund", "General Fund", fy, b,
                                                         gen.vals[j], gv, 1, "Overview of Expenditures"))
                    if g is not None and g.vals[j] is not None:
                        ov_other = g.vals[j] - gen.vals[j]
                        ov_ = sumrows.get(("other_funds", fy, b))
                        if ov_ is not None:
                            self.checks.append(self.mk_check("cross_table", "overview_expenditures_non_general_vs_sum_of_dept_other_funds", "Non-General Fund", fy, b,
                                                             ov_other, ov_, 1, "Overview of Expenditures",
                                                             extra=dict(note="printed = overview Grand Total minus overview General Fund")))
        # revenue statement vs overview of revenues, and revenue vs expenditure
        rev_by = defaultdict(lambda: Decimal(0))
        for r in self.revenues:
            rev_by[(r["fund_group"], r["fiscal_year"], r["basis"])] += r["amount"]
        for t in ov.get("overview_of_revenues", []):
            ycols = self.year_cols_quiet(t)
            if len(t.dims) != 1:
                continue
            for n in t.nodes:
                for j, fy, b in ycols:
                    if n.vals[j] is None:
                        continue
                    if n.is_grand:
                        s = rev_by.get(("general_fund", fy, b), Decimal(0)) + rev_by.get(("other_funds", fy, b), Decimal(0))
                        if ("general_fund", fy, b) in rev_by or ("other_funds", fy, b) in rev_by:
                            self.checks.append(self.mk_check("cross_table", "overview_revenues_grand_total_vs_statement_of_revenues_all_funds", "Grand Total", fy, b,
                                                             n.vals[j], s, 1, "Overview of Revenues"))
                    else:
                        lab = n.label
                        if lab.lower() == "general fund":
                            s = rev_by.get(("general_fund", fy, b))
                        else:
                            s = sum((r["amount"] for r in self.revenues if r["fund_group"] == "other_funds" and r["fund_name"] == lab
                                     and r["fiscal_year"] == fy and r["basis"] == b), Decimal(0))
                            if not any(r["fund_name"] == lab and r["fiscal_year"] == fy and r["basis"] == b for r in self.revenues):
                                s = None
                        if s is None:
                            self.checks.append(dict(tier="cross_table", scope="overview_revenues_fund_vs_statement_of_revenues", label=lab, fiscal_year=fy, basis=b,
                                                    status="not_checkable", note="fund not in statement of revenues for this year/basis", printed=str(n.vals[j]),
                                                    kind="usd", page="Overview of Revenues"))
                        else:
                            self.checks.append(self.mk_check("cross_table", "overview_revenues_fund_vs_statement_of_revenues", lab, fy, b, n.vals[j], s, 1,
                                                             "Overview of Revenues"))
        # statement of revenues summary table vs per-category tables
        cats = getattr(self, "rev_cat_tables", {})
        for st in getattr(self, "rev_summary", []):
            for n in st.nodes:
                for j, fy, b in self.year_cols_quiet(st):
                    if n.vals[j] is None:
                        continue
                    if n.is_grand:
                        s = sum((tb.grand().vals[k] for tb in cats.values() if tb.grand() is not None
                                 for k, f2, b2 in self.year_cols_quiet(tb) if (f2, b2) == (fy, b) and tb.grand().vals[k] is not None), Decimal(0))
                        sc, lab = "revenues_summary_grand_total_vs_sum_of_category_grand_totals", "Grand Total"
                    else:
                        tb = cats.get(n.label)
                        if tb is None or tb.grand() is None:
                            self.checks.append(dict(tier="cross_table", scope="revenues_summary_category_vs_category_table", label=n.label, fiscal_year=fy, basis=b,
                                                    status="not_checkable", note="no category table with this heading", printed=str(n.vals[j]), kind="usd",
                                                    page="Statement of Revenues"))
                            continue
                        s = next((tb.grand().vals[k] for k, f2, b2 in self.year_cols_quiet(tb) if (f2, b2) == (fy, b)), None)
                        sc, lab = "revenues_summary_category_vs_category_table", n.label
                    if s is not None:
                        self.checks.append(self.mk_check("cross_table", sc, lab, fy, b, n.vals[j], s, 1, "Statement of Revenues"))
            summ_labels = {n.label for n in st.nodes if not n.is_grand}
            for c in cats:
                if c not in summ_labels:
                    self.anomalies.append(dict(page="Statement of Revenues", issue="category table has no row in summary table", category=c))
        # balanced budget: revenue grand total vs expenditure grand total
        for (fg, fy, b), v in sorted(sumrows.items()):
            if fg == "all_funds":
                r = rev_by.get(("general_fund", fy, b), Decimal(0)) + rev_by.get(("other_funds", fy, b), Decimal(0))
                if ("general_fund", fy, b) in rev_by:
                    self.checks.append(self.mk_check("cross_table", "revenues_vs_expenditures_all_funds", "all funds", fy, b, v, r, 1,
                                                     "Statement of Revenues", extra=dict(note="printed=sum of dept expenditure rows, extracted_sum=sum of revenue rows")))
            if fg == "general_fund" and ("general_fund", fy, b) in rev_by:
                self.checks.append(self.mk_check("cross_table", "revenues_vs_expenditures_general_fund", "General Fund", fy, b, v, rev_by[("general_fund", fy, b)], 1,
                                                 "Statement of Revenues", extra=dict(note="printed=sum of GF dept expenditure rows, extracted_sum=sum of GF revenue rows")))

    def printed_grand(self, fg, fy, b):
        for t in self.cityw.get(fg, []):
            g = t.grand()
            if g is None:
                continue
            for j, sp in enumerate(t.specs):
                if sp[0] == "year" and (sp[1], sp[2]) == (fy, b):
                    return g.vals[j]
        return None


# --------------------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------------------
FILES = OrderedDict([
    ("department_expenditures", ["book_id", "fiscal_year", "basis", "fund_group", "dept_code", "dept_name", "amount", "page"]),
    ("revenues", ["book_id", "fiscal_year", "basis", "fund_group", "fund_name", "category", "line_name", "amount", "page"]),
    ("department_detail", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "table", "dim1", "dim2", "amount", "page"]),
    ("positions", ["book_id", "fiscal_year", "basis", "dept_code", "dept_name", "program", "fund", "position_title", "pay_grade", "fte", "page"]),
    ("printed_totals", ["book_id", "fiscal_year", "basis", "scope", "label", "amount", "page"]),
])


def write_csv(path, cols, rows, kindf):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            out = []
            for c in cols:
                v = r[c]
                if c == "amount":
                    out.append(fmt_amount(v, kindf(r)))
                elif c == "fte":
                    out.append(fmt_amount(v, "fte"))
                else:
                    out.append(v)
            w.writerow(out)


def sort_rows(rows, keys):
    return sorted(rows, key=lambda r: tuple(str(r[k]) for k in keys))


def pdf_spot_check(ex, pdf_path, n_samples, seed):
    """Pick random extracted cells (seeded) and look them up in the PDF text (pymupdf).  The
    row's printed value cells must appear as consecutive text lines on a PDF page that also
    contains the row label, and the cell in the extracted column must equal the CSV amount."""
    try:
        import fitz
    except ImportError:
        return dict(status="skipped", reason="pymupdf not installed")
    if not pdf_path or not os.path.exists(pdf_path):
        return dict(status="skipped", reason="no PDF found in book folder")
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(len(doc)):
        lines = [norm_ws(x) for x in doc[i].get_text().split("\n")]
        pages.append([x for x in lines if x != ""])
    rnd = random.Random(seed)
    pools = OrderedDict([
        ("department_expenditures", ex.dept_exp), ("revenues", ex.revenues),
        ("department_detail", ex.detail), ("positions", ex.positions)])
    per = max(1, (n_samples + len(pools) - 1) // len(pools))
    results = []
    for name, rows in pools.items():
        key = "fte" if name == "positions" else "amount"
        cand = [r for r in rows if r[key] != 0 and len(set(r["_raw"])) >= 1]
        picks = rnd.sample(cand, min(per, len(cand))) if cand else []
        for r in picks:
            seq = [x for x in r["_raw"]]
            label = {"department_expenditures": r.get("dept_name"), "revenues": r.get("line_name"),
                     "department_detail": r.get("dim1") or r.get("dim2"), "positions": r.get("pay_grade") or r.get("position_title")}[name]
            # context window: the sampled row plus up to 2 rows either side in the same table.  The PDF lists the
            # value cells of a table row after row in reading order, so the window must be contiguous in PDF text.
            nodes = r["_tbl"].nodes
            i0 = nodes.index(r["_node"])
            found, nmatch, win = None, 0, 0
            for w in (2, 1, 0):
                lo, hi = max(0, i0 - w), min(len(nodes), i0 + w + 1)
                flat = [x for nd in nodes[lo:hi] for x in nd.raw]
                off = sum(len(nd.raw) for nd in nodes[lo:i0])
                target = "\x00".join(flat)
                for pi, lines in enumerate(pages):
                    joined = "\x00".join(lines)
                    k = joined.find(target)
                    if k >= 0:
                        nmatch += joined.count(target)
                        if found is None:
                            idx = joined[:k].count("\x00")
                            found = (pi + 1, lines[idx + off: idx + off + len(seq)])
                if found:
                    win = w
                    break
            res = dict(file=name, fiscal_year=r["fiscal_year"], basis=r["basis"], label=label, page_title=r["page"],
                       csv_value=str(r[key]), raw_row=seq, column_index=r["_col"])
            if found:
                pv = parse_num(found[1][r["_col"]])
                ok = pv is not None and ((to_int(pv) == to_int(r[key])) if name != "positions" else pv == r[key])
                # in the PDF a table's row labels are printed as a separate block (often on the next page),
                # so the label is looked for on the matching page and the two following ones
                near = " ".join(" ".join(pages[q]) for q in range(found[0] - 1, min(found[0] + 2, len(pages))))
                res.update(pdf_page=found[0], pdf_cell=found[1][r["_col"]], status="match" if ok else "VALUE_MISMATCH",
                           window_rows_each_side=win, sequence_matches_in_pdf=nmatch, label_on_pages_p_to_p_plus_2=bool(label and label in near))
            else:
                res.update(status="not_found_in_pdf")
            results.append(res)
    n_ok = sum(1 for x in results if x["status"] == "match")
    return dict(status="done", seed=seed, n_checked=len(results), n_match=n_ok, samples=results)


def book_id_from_dir(book_dir):
    b = os.path.basename(os.path.abspath(book_dir))
    m = re.match(r"(adopted|proposed)_(\d{4})", b)
    if m:
        return "%s-operating-%s" % (m.group(2), m.group(1))
    m = re.search(r"(\d{4})", b)
    k = "proposed" if "proposed" in b else "adopted"
    return "%s-operating-%s" % (m.group(1) if m else "unknown", k)


def tier_summary(checks):
    out = OrderedDict()
    for c in checks:
        t = out.setdefault(c["tier"], OrderedDict(total=0, exact=0, mismatch=0, not_checkable=0))
        t["total"] += 1
        t[c["status"]] += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("book_dir", nargs="?", default=DEFAULT_BOOK)
    ap.add_argument("--out", default=None)
    ap.add_argument("--book-id", default=None)
    ap.add_argument("--no-pdf-check", action="store_true")
    ap.add_argument("--pdf-samples", type=int, default=40)
    a = ap.parse_args(argv)
    book_dir = os.path.abspath(a.book_dir)
    book_id = a.book_id or book_id_from_dir(book_dir)
    out_dir = a.out or os.path.join(ROOT, "data", "extracted", book_id)
    os.makedirs(out_dir, exist_ok=True)

    ex = Extractor(book_dir, book_id)
    ex.run()

    kinds = lambda r: r.get("_kind", "usd")
    write_csv(os.path.join(out_dir, "department_expenditures.csv"), FILES["department_expenditures"], ex.dept_exp, kinds)
    write_csv(os.path.join(out_dir, "revenues.csv"), FILES["revenues"], ex.revenues, kinds)
    write_csv(os.path.join(out_dir, "department_detail.csv"), FILES["department_detail"], ex.detail, kinds)
    write_csv(os.path.join(out_dir, "positions.csv"), FILES["positions"], ex.positions, kinds)
    write_csv(os.path.join(out_dir, "printed_totals.csv"), FILES["printed_totals"], ex.totals, kinds)

    pdfs = glob.glob(os.path.join(book_dir, "*.pdf"))
    pdfres = dict(status="skipped", reason="--no-pdf-check")
    if not a.no_pdf_check:
        pdfres = pdf_spot_check(ex, pdfs[0] if pdfs else None, a.pdf_samples, seed=book_id)

    counts = OrderedDict((k, len(getattr(ex, {"department_expenditures": "dept_exp", "revenues": "revenues",
                                              "department_detail": "detail", "positions": "positions",
                                              "printed_totals": "totals"}[k]))) for k in FILES)
    checks = ex.checks
    for c in checks:
        c.setdefault("status", "exact")
    summ = tier_summary(checks)
    unrec = [c for c in checks if c["status"] == "mismatch"]

    def totals_for(fg):
        d = defaultdict(lambda: Decimal(0))
        for r in ex.dept_exp:
            if r["fund_group"] == fg:
                d["%s/%s" % (r["fiscal_year"], r["basis"])] += r["amount"]
        return {k: str(v) for k, v in sorted(d.items())}

    def rev_totals(fg):
        d = defaultdict(lambda: Decimal(0))
        for r in ex.revenues:
            if fg is None or r["fund_group"] == fg:
                d["%s/%s" % (r["fiscal_year"], r["basis"])] += r["amount"]
        return {k: str(v) for k, v in sorted(d.items())}

    doc = OrderedDict([
        ("book_id", book_id),
        ("source_dir", os.path.relpath(book_dir, ROOT)),
        ("pages_in_book", ex.n_pages),
        ("row_counts", counts),
        ("check_tiers", OrderedDict([
            ("subtotal", "a printed parent/subtotal row vs the sum of its immediate child rows (hierarchical pivot tables)"),
            ("grand_total", "a table's printed Grand Total vs the sum of every leaf row extracted from that table"),
            ("cross_table", "a printed figure in one table vs a figure derived from another table (GF+other=all, dept page vs citywide, FTE, overview vs statements)")])),
        ("summary_by_tier", summ),
        ("mismatches", unrec),
        ("extracted_totals_from_rows", OrderedDict([
            ("department_expenditures", {fg: totals_for(fg) for fg in ("general_fund", "other_funds", "all_funds")}),
            ("revenues", {"general_fund": rev_totals("general_fund"), "other_funds": rev_totals("other_funds"), "all": rev_totals(None)}),
            ("positions_fte_all", getattr(ex, "fte_totals", {})),
            ("positions_fte_general_fund", getattr(ex, "fte_totals_gf", {}))])),
        ("department_name_matching", OrderedDict([
            ("method", "department pages are grouped by table-of-contents nesting (a parent page and its sub-pages roll up into one citywide agency), a small name-only ALIASES table, then exact/fuzzy name match (threshold 0.6, one-to-one); used only by cross_table checks"),
            ("groups", getattr(ex, "groups", {})),
            ("group_to_citywide_agency", {k: {g: dict(agency=v[0], score=v[1], how=v[2]) for g, v in m.items()} for k, m in getattr(ex, "agency_map", {}).items()}),
            ("groups_without_citywide_agency", getattr(ex, "unmatched_pages", {})),
            ("citywide_agencies_without_page_group", getattr(ex, "unmatched_agencies", {}))])),
        ("unparsed", ex.unparsed),
        ("anomalies", ex.anomalies),
        ("pdf_spot_check", pdfres),
        ("checks", checks),
    ])
    with open(os.path.join(out_dir, "checks.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)

    print("book_id:", book_id, "->", os.path.relpath(out_dir, ROOT))
    for k, v in counts.items():
        print("  %-26s %7d rows" % (k, v))
    for t, s in summ.items():
        print("  tier %-12s total %6d exact %6d mismatch %5d not_checkable %5d" % (t, s["total"], s["exact"], s["mismatch"], s["not_checkable"]))
    if pdfres.get("status") == "done":
        print("  pdf spot check: %d/%d match" % (pdfres["n_match"], pdfres["n_checked"]))
    print("  unparsed items: %d, anomalies: %d" % (len(ex.unparsed), len(ex.anomalies)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
