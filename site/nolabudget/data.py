"""Load the combined budget data into one read-only object.

Everything a page needs to look up comes through `Budget`. Pages never read the CSVs
themselves, so the rules about which figure to show (and how to cite it) live in one
place:

* `amount()` / `cell()`  one department, year, basis and fund group.
* `total()`              the citywide sum, with the same rules applied.
* `cite()`               a citation (text + link) for a source book and page.
* `real()` / `per_resident()`  the inflation and per-resident views.

Rules applied here (see site/content/notes.yml for the reader-facing explanations):

* 2020 adopted all funds: the printed figures are in error, so all funds is
  General Fund + other funds, marked as computed.
* Rows with `derived` set in spending.csv are computed, and cite "computed from".
* A department with no row for a year was not printed as its own line that year;
  lookups return None (never 0) so pages can say so.
"""
import collections
import csv
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data")
COMBINED = os.path.join(DATA, "combined")
CROSSWALK = os.path.join(DATA, "crosswalk")
RAW = os.path.join(DATA, "raw")

BASES = ("proposed", "adopted", "revised", "actual")
FUNDS = ("general_fund", "other_funds", "all_funds")
FUND_LABEL = {"general_fund": "General Fund", "other_funds": "Other funds", "all_funds": "All funds"}
BASIS_LABEL = {"proposed": "Proposed", "adopted": "Adopted", "revised": "Revised", "actual": "Actual"}

# Fiscal years whose printed all-funds figures are replaced by GF + other funds.
ALL_FUNDS_FROM_PARTS = {(2020, "adopted")}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Cell(object):
    """One figure, with where it came from."""
    __slots__ = ("amount", "book", "page", "derived", "name")

    def __init__(self, amount, book, page, derived="", name=""):
        self.amount = amount
        self.book = book
        self.page = page
        self.derived = derived
        self.name = name


class Book(object):
    __slots__ = ("id", "year", "stage", "url", "label")

    def __init__(self, book_id, url):
        self.id = book_id
        self.year = int(book_id[:4])
        self.stage = book_id.rsplit("-", 1)[1]
        self.url = url
        self.label = "%d %s budget book" % (self.year, self.stage)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class Budget(object):
    def __init__(self, content):
        self.content = content
        self._load_books()
        self._load_departments()
        self._load_spending()
        self._load_revenue()
        self._load_positions()
        self._load_context()
        self._detail = None

    # ------------------------------------------------------------------ books
    def _load_books(self):
        urls = {}
        for it in json.load(open(os.path.join(RAW, "nola-gov", "manifest.json"))):
            if it.get("kind") in ("operating-adopted", "operating-proposed") and str(it.get("ok")) == "True":
                urls["%s-%s" % (it["year"], it["kind"])] = it["url"]
        q = json.load(open(os.path.join(RAW, "questica", "manifest.json")))
        for it in q["files"]:
            if it.get("type") == "operating" and (it.get("description") or "").startswith("Full PDF budget book"):
                urls["%s-operating-%s" % (it["year"], it["stage"])] = it["url"]
        for it in json.load(open(os.path.join(RAW, "pre-2011", "manifest.json"))):
            if it.get("doc_type") == "adopted_operating_budget" and it.get("fiscal_year") in (2008, 2009):
                urls["%s-operating-adopted" % it["fiscal_year"]] = it["url"]
        self.book_urls = urls
        self.books = {}

    def book(self, book_id):
        if book_id not in self.books:
            self.books[book_id] = Book(book_id, self.book_urls.get(book_id))
        return self.books[book_id]

    def cite(self, book_id, page="", derived=""):
        """Citation dict: text, href (may be None), and whether it is computed."""
        b = self.book(book_id)
        page = (page or "").split(";")[0].strip()
        short_book = "%d %s book" % (b.year, b.stage)
        c = {"href": b.url, "computed": False, "book": b.label, "title": "", "text": b.label, "short": short_book}
        if derived:
            c.update(text="computed from the %s" % b.label, short="computed from %s" % short_book, computed=True)
        elif page.isdigit():
            c.update(text="%s, p. %s" % (b.label, page), short="%s, p. %s" % (short_book, page),
                     href="%s#page=%s" % (b.url, page) if b.url else None)
        elif page:
            # 2025-2027 books from the city's online budget viewer are cited by the page's title
            c.update(text="%s, “%s”" % (b.label, page), title="Page titled “%s”" % page)
        return c

    # ------------------------------------------------------------ departments
    def _load_departments(self):
        desc = self.content["departments"]
        self.depts = collections.OrderedDict()
        for r in read_csv(os.path.join(CROSSWALK, "departments.csv")):
            d = dict(r)
            # the crosswalk's notes column is working notes for the pipeline, not reader text
            d["crosswalk_notes"] = d.pop("notes", "")
            d.update(desc.get(r["dept_id"], {}))
            if isinstance(d.get("notes"), str):
                d["notes"] = [d["notes"]]
            d["id"] = r["dept_id"]
            d["url"] = "/departments/%s/" % r["dept_id"]
            d.setdefault("notes", [])
            d.setdefault("see", [])
            self.depts[r["dept_id"]] = d
        self.groups = []
        for d in self.depts.values():
            if d["group"] not in self.groups:
                self.groups.append(d["group"])

    # --------------------------------------------------------------- spending
    def _load_spending(self):
        S = {}
        for r in read_csv(os.path.join(COMBINED, "spending.csv")):
            key = (r["dept_id"], int(r["fiscal_year"]), r["basis"], r["fund_group"])
            S[key] = Cell(float(r["amount"]), r["source_book"], r["source_page"], r["derived"], r["dept_name"])
        self.printed_all_funds = {}
        for (fy, basis) in ALL_FUNDS_FROM_PARTS:
            self.printed_all_funds[(fy, basis)] = sum(
                c.amount for (d, y, b, f), c in S.items() if y == fy and b == basis and f == "all_funds")
            for dept in self.depts:
                g = S.get((dept, fy, basis, "general_fund"))
                o = S.get((dept, fy, basis, "other_funds"))
                if g is None and o is None:
                    S.pop((dept, fy, basis, "all_funds"), None)
                    continue
                src = g or o
                S[(dept, fy, basis, "all_funds")] = Cell((g.amount if g else 0) + (o.amount if o else 0),
                                                         src.book, "", "general_fund + other_funds", src.name)
        self.S = S
        self.years = sorted({k[1] for k in S})
        self._totals = collections.defaultdict(float)
        self._has_total = set()
        for (dept, fy, basis, fund), c in S.items():
            self._totals[(fy, basis, fund)] += c.amount
            self._has_total.add((fy, basis, fund))

    def cell(self, dept, fy, basis, fund):
        return self.S.get((dept, int(fy), basis, fund))

    def amount(self, dept, fy, basis, fund):
        c = self.cell(dept, fy, basis, fund)
        return None if c is None else c.amount

    def total(self, fy, basis, fund):
        k = (int(fy), basis, fund)
        return self._totals[k] if k in self._has_total else None

    def total_source(self, fy, basis, fund):
        """Citation for a citywide column. The combine step picks one book per column."""
        cells = [c for (d, y, b, f), c in self.S.items() if y == int(fy) and b == basis and f == fund]
        if not cells:
            return None
        book = collections.Counter(c.book for c in cells).most_common(1)[0][0]
        pages = collections.Counter(c.page.split(";")[0] for c in cells if c.book == book and c.page)
        computed = any(c.derived for c in cells)
        return self.cite(book, pages.most_common(1)[0][0] if pages else "", "computed" if computed else "")

    def dept_years(self, dept):
        """Years where the department has any statement row with a non-zero amount."""
        return sorted({y for (d, y, b, f), c in self.S.items() if d == dept and c.amount})

    # ---------------------------------------------------------------- revenue
    def _load_revenue(self):
        self.R = collections.defaultdict(list)
        self.rev_lines = {}
        for r in read_csv(os.path.join(COMBINED, "revenue.csv")):
            r["amount"] = float(r["amount"])
            r["fiscal_year"] = int(r["fiscal_year"])
            self.R[(r["fiscal_year"], r["basis"], r["fund_group"])].append(r)
        for r in read_csv(os.path.join(CROSSWALK, "revenue_sources.csv")):
            self.rev_lines[r["rev_id"]] = r
        self._rev_gaps = []
        for r in read_csv(os.path.join(COMBINED, "revenue_category_totals.csv")):
            r["fiscal_year"], r["gap"], r["printed"] = int(r["fiscal_year"]), float(r["gap"]), float(r["printed"])
            if r["gap"] and r["place"]:
                self._rev_gaps.append(r)

    def revenue_rows(self, fy, basis, fund="general_fund"):
        return self.R.get((int(fy), basis, fund), [])

    def revenue_gaps(self, fy, basis, fund="general_fund"):
        """Where the book a General Fund income column comes from prints a category total
        that differs from the sum of its lines: [{place, gap, category_printed, printed, cite}].
        The site counts each gap in `place`, so totals match what the book prints."""
        rows = self.revenue_rows(fy, basis, fund)
        books = {r["source_book"] for r in rows}
        if fund != "general_fund" or len(books) != 1:
            return []
        book = books.pop()
        printed_basis = "adopted" if basis == "revised" else basis
        return [dict(g, cite=self.cite(g["book_id"], g["page"])) for g in self._rev_gaps
                if g["book_id"] == book and g["fiscal_year"] == int(fy) and g["basis"] == printed_basis]

    def revenue_by_category(self, fy, basis, fund="general_fund"):
        """Income by category; printed category totals where the book prints them."""
        out = collections.OrderedDict()
        for r in self.revenue_rows(fy, basis, fund):
            out[r["category"]] = out.get(r["category"], 0) + r["amount"]
        for g in self.revenue_gaps(fy, basis, fund):
            out[g["place"]] = out.get(g["place"], 0) + g["gap"]
        return out

    # -------------------------------------------------------------- positions
    def _load_positions(self):
        self.P = {}
        for r in read_csv(os.path.join(COMBINED, "positions_by_department.csv")):
            self.P[(r["dept_id"], int(r["fiscal_year"]), r["basis"])] = Cell(float(r["fte"]), r["source_book"], "", "", r["dept_name"])

    def positions(self, dept, fy, basis):
        c = self.P.get((dept, int(fy), basis))
        return None if c is None else c.amount

    # ---------------------------------------------------------------- context
    def _load_context(self):
        self.pop = {}
        self.cpi = {}
        for r in read_csv(os.path.join(COMBINED, "context.csv")):
            y = int(r["year"])
            if r["population"]:
                self.pop[y] = float(r["population"])
            if r["cpi_south"] and (r["cpi_months"] == "12" or y == self.content["site"]["real_base_year"]):
                self.cpi[y] = float(r["cpi_south"])
        self.cpi_months = {int(r["year"]): r["cpi_months"] for r in read_csv(os.path.join(COMBINED, "context.csv"))}
        self.last_pop_year = max(self.pop)
        self.real_base = self.content["site"]["real_base_year"]
        self.last_cpi_year = max(y for y in self.cpi if y <= self.real_base)

    def population(self, y):
        return self.pop[min(int(y), self.last_pop_year)]

    def price_index(self, y):
        return self.cpi[min(int(y), self.last_cpi_year)]

    def real(self, amount, y):
        if amount is None:
            return None
        return amount * self.cpi[self.real_base] / self.price_index(y)

    def per_resident(self, amount, y):
        if amount is None:
            return None
        return amount / self.population(y)

    # ----------------------------------------------------------------- detail
    @property
    def detail(self):
        """department_detail.csv grouped by dept_id (loaded on first use; it is large)."""
        if self._detail is None:
            d = collections.defaultdict(list)
            for r in read_csv(os.path.join(COMBINED, "department_detail.csv")):
                r["amount"] = float(r["amount"])
                r["fiscal_year"] = int(r["fiscal_year"])
                d[r["dept_id"]].append(r)
            self._detail = d
        return self._detail

    # ------------------------------------------------- revenue by line (revenue pages)
    def _rev_index(self):
        if getattr(self, "_RC", None) is None:
            self._RC = {}
            self._rev_order = collections.OrderedDict()
            self._rev_group = collections.defaultdict(list)
            self._rev_comp = {}
            for rows in self.R.values():
                for r in rows:
                    self._RC[(r["rev_id"], r["fiscal_year"], r["basis"], r["fund_group"])] = Cell(
                        r["amount"], r["source_book"], r["source_page"], "", r["rev_name"])
                    if r["fund_group"] == "general_fund":
                        self._rev_comp[r["rev_id"]] = r["comparable_id"]
            order = list(self.rev_lines)
            ids = {k[0] for k in self._RC if k[3] == "general_fund"}
            self._rev_order = [i for i in order if i in ids]
            for i in self._rev_order:
                self._rev_group[self._rev_comp[i]].append(i)

    def rev_ids(self):
        """General Fund revenue line ids with any figure, in crosswalk order."""
        self._rev_index()
        return list(self._rev_order)

    def rev_cell(self, rev_id, fy, basis, fund="general_fund"):
        self._rev_index()
        return self._RC.get((rev_id, int(fy), basis, fund))

    def rev_amount(self, rev_id, fy, basis, fund="general_fund"):
        c = self.rev_cell(rev_id, fy, basis, fund)
        return None if c is None else c.amount

    def rev_last_year(self, rev_id):
        """The latest fiscal year in which this General Fund line has any figure."""
        self._rev_index()
        ys = [k[1] for k in self._RC if k[0] == rev_id and k[3] == "general_fund"]
        return max(ys) if ys else None

    def rev_comparable(self, rev_id):
        """The comparable_id that follows this line across books."""
        self._rev_index()
        return self._rev_comp[rev_id]

    def rev_group(self, rev_id):
        """Every General Fund line that shares this line's comparable series (itself included)."""
        self._rev_index()
        return list(self._rev_group[self._rev_comp[rev_id]])

    def rev_series_amount(self, rev_id, fy, basis):
        """The comparable series for a line: the sum of every line in its group, for one year
        and basis. None when none of them has a figure."""
        vals = [self.rev_amount(i, fy, basis) for i in self.rev_group(rev_id)]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    def rev_total(self, fy, basis, fund="general_fund"):
        """Sum of the categories (printed category totals where the book prints them); None
        if the column has no lines at all."""
        rows = self.revenue_rows(fy, basis, fund)
        return sum(self.revenue_by_category(fy, basis, fund).values()) if rows else None

    def rev_category_amount(self, category, fy, basis, fund="general_fund"):
        rows = [r for r in self.revenue_rows(fy, basis, fund) if r["category"] == category]
        if not rows:
            return None
        return sum(r["amount"] for r in rows) + self.rev_category_gap(category, fy, basis, fund)

    def rev_category_gap(self, category, fy, basis, fund="general_fund"):
        """The part of a category's total that the book prints but its lines do not show."""
        return sum(g["gap"] for g in self.revenue_gaps(fy, basis, fund) if g["place"] == category)

    def rev_cite(self, rows):
        """One citation covering the pages the given revenue rows were read from."""
        by_book = collections.OrderedDict()
        for r in rows:
            by_book.setdefault(r["source_book"], set()).update(
                p.strip() for p in (r["source_page"] or "").split(";") if p.strip())
        if not by_book:
            return None
        book, pages = max(by_book.items(), key=lambda kv: len(kv[1]))
        nums = sorted(int(p) for p in pages if p.isdigit())
        if len(by_book) == 1 and len(nums) == len(pages) and len(nums) > 1:
            b = self.book(book)
            label = "%s, pp. %s" % (b.label, _page_span(nums))
            short = "%d %s book, pp. %s" % (b.year, b.stage, _page_span(nums))
            return {"href": "%s#page=%d" % (b.url, nums[0]) if b.url else None, "computed": False,
                    "book": b.label, "title": "", "text": label, "short": short}
        return self.cite(book, sorted(pages)[0] if pages else "")


def _page_span(nums):
    """[65, 66, 67, 68] -> '65-68'; [65, 68] -> '65, 68'."""
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else ("%d–%d" % (start, prev) if prev - start > 1 else "%d, %d" % (start, prev)))
        start = prev = n
    return ", ".join(out)
