"""Read-only access to the extraction evidence behind the "not explained" and "method" pages.

The extraction scripts wrote, for every budget book, the rows they read, every total the book
prints, and a list of the places where a printed total did not match the rows beneath it or
another table. This module loads those files and answers questions about them. It never
changes anything, and it does not choose figures (that is `data.py`).

    data/extracted/<book_id>/{department_expenditures,revenues,positions,printed_totals}.csv
    data/extracted/<book_id>/checks.json
    data/combined/{spending,revenue}_observations.csv   every printing of every figure
    data/crosswalk/department_aliases_*.csv, corrections.csv
"""
import collections
import csv
import json
import os

from .data import COMBINED, CROSSWALK, DATA, read_csv

EXTRACTED = os.path.join(DATA, "extracted")

FUNDS = ("general_fund", "other_funds", "all_funds")


def book_year(book_id):
    return int(book_id[:4])


def book_stage(book_id):
    return book_id.rsplit("-", 1)[1]


def book_order(book_id):
    """Order books in the order they were published: by year, the proposed book before the adopted one."""
    return (book_year(book_id), 0 if book_stage(book_id) == "proposed" else 1)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Evidence(object):
    def __init__(self):
        self.books = sorted(
            (d for d in os.listdir(EXTRACTED) if os.path.exists(os.path.join(EXTRACTED, d, "checks.json"))),
            key=book_order)
        self._cache = {}
        self._aliases = None

    # ----------------------------------------------------------------- files
    def _csv(self, book, name):
        key = (book, name)
        if key not in self._cache:
            path = os.path.join(EXTRACTED, book, name + ".csv")
            self._cache[key] = read_csv(path) if os.path.exists(path) else []
        return self._cache[key]

    def checks(self, book):
        key = (book, "checks")
        if key not in self._cache:
            with open(os.path.join(EXTRACTED, book, "checks.json"), encoding="utf-8") as f:
                self._cache[key] = json.load(f)
        return self._cache[key]

    def printed_totals(self, book):
        key = (book, "pt")
        if key not in self._cache:
            rows = []
            for r in self._csv(book, "printed_totals"):
                r = dict(r)
                r["fiscal_year"] = int(r["fiscal_year"])
                r["amount"] = num(r["amount"])
                rows.append(r)
            self._cache[key] = rows
        return self._cache[key]

    def dept_rows(self, book):
        """Citywide expenditure rows with the crosswalk's dept_id added (None when unmatched)."""
        key = (book, "dr")
        if key not in self._cache:
            al = self.statement_aliases()
            rows = []
            for r in self._csv(book, "department_expenditures"):
                r = dict(r)
                r["fiscal_year"] = int(r["fiscal_year"])
                r["amount"] = num(r["amount"])
                r["dept_id"] = al.get((book, r["dept_name"]))
                rows.append(r)
            self._cache[key] = rows
        return self._cache[key]

    def revenue_rows(self, book):
        key = (book, "rr")
        if key not in self._cache:
            rows = []
            for r in self._csv(book, "revenues"):
                r = dict(r)
                r["fiscal_year"] = int(r["fiscal_year"])
                r["amount"] = num(r["amount"])
                rows.append(r)
            self._cache[key] = rows
        return self._cache[key]

    def position_rows(self, book):
        return self._csv(book, "positions")

    def detail_rows(self, book):
        return self._csv(book, "department_detail")

    # -------------------------------------------------------------- crosswalk
    def statement_aliases(self):
        """(book_id, printed name) -> dept_id for the citywide statements."""
        if self._aliases is None:
            self._aliases = {}
            for r in read_csv(os.path.join(CROSSWALK, "department_aliases_statement.csv")):
                self._aliases[(r["book_id"], r["printed_name"])] = r["dept_id"]
        return self._aliases

    def alias_rows(self, which="statement"):
        key = ("alias", which)
        if key not in self._cache:
            self._cache[key] = read_csv(os.path.join(CROSSWALK, "department_aliases_%s.csv" % which))
        return self._cache[key]

    def corrections(self):
        return read_csv(os.path.join(CROSSWALK, "corrections.csv"))

    # ----------------------------------------------------------- observations
    def spending_observations(self):
        """{(fiscal_year, basis, fund, dept_id): {book_id: amount}}, several printed lines summed."""
        if "so" not in self._cache:
            out = collections.defaultdict(lambda: collections.defaultdict(float))
            pages = collections.defaultdict(collections.Counter)
            for r in read_csv(os.path.join(COMBINED, "spending_observations.csv")):
                k = (int(r["fiscal_year"]), r["basis"], r["fund_group"], r["dept_id"])
                out[k][r["book_id"]] += float(r["amount"])
                pages[(k, r["book_id"])][r["page"]] += 1
            self._cache["so"] = out
            self._cache["so_pages"] = pages
        return self._cache["so"]

    def spending_page(self, key, book):
        """The page a department's figure was read from in a book (the commonest, if it was read from several)."""
        self.spending_observations()
        c = self._cache["so_pages"].get((key, book))
        return c.most_common(1)[0][0] if c else ""

    def column_page(self, fy, basis, fund, book):
        """The page of the citywide statement a whole column was read from."""
        self.spending_observations()
        c = collections.Counter()
        for (k, bk), pages in self._cache["so_pages"].items():
            if bk == book and k[:3] == (fy, basis, fund):
                c.update(pages)
        return c.most_common(1)[0][0] if c else ""

    def revenue_observations(self):
        """{(fiscal_year, basis, fund, rev_id): {book_id: amount}}"""
        if "ro" not in self._cache:
            out = collections.defaultdict(lambda: collections.defaultdict(float))
            pages = collections.defaultdict(collections.Counter)
            for r in read_csv(os.path.join(COMBINED, "revenue_observations.csv")):
                k = (int(r["fiscal_year"]), r["basis"], r["fund_group"], r["rev_id"])
                out[k][r["book_id"]] += float(r["amount"])
                pages[(k, r["book_id"])][r["page"]] += 1
            self._cache["ro"] = out
            self._cache["ro_pages"] = pages
        return self._cache["ro"]

    def revenue_page(self, key, book):
        self.revenue_observations()
        c = self._cache["ro_pages"].get((key, book))
        return c.most_common(1)[0][0] if c else ""

    def spending_column_totals(self):
        """{(fiscal_year, basis, fund): {book_id: total}} summed over every department."""
        if "sct" not in self._cache:
            out = collections.defaultdict(lambda: collections.defaultdict(float))
            for (fy, basis, fund, dept), books in self.spending_observations().items():
                for b, v in books.items():
                    out[(fy, basis, fund)][b] += v
            self._cache["sct"] = out
        return self._cache["sct"]

    # ------------------------------------------------------- printed totals
    def printed_total(self, book, fy, basis, fund):
        """(amount, page) of the grand total printed under the citywide expenditure statement for a fund."""
        found = [(r["amount"], r["page"]) for r in self.printed_totals(book)
                 if r["scope"] == "department_expenditures/%s" % fund and r["fiscal_year"] == fy and r["basis"] == basis]
        return found[0] if found else (None, None)

    def dept_sums(self, book, fy, basis, fund):
        return sum(r["amount"] for r in self.dept_rows(book)
                   if r["fiscal_year"] == fy and r["basis"] == basis and r["fund_group"] == fund)

    def dept_gaps(self, book, fy, basis):
        """Per department: all funds minus (General Fund + other funds), a missing figure counting as nothing."""
        d = collections.defaultdict(dict)
        for r in self.dept_rows(book):
            if r["fiscal_year"] == fy and r["basis"] == basis:
                d[r["dept_name"]][r["fund_group"]] = r["amount"]
        out = []
        for name, v in d.items():
            gap = v.get("all_funds", 0) - v.get("general_fund", 0) - v.get("other_funds", 0)
            out.append({"name": name, "gap": gap, "gf": v.get("general_fund"), "of": v.get("other_funds"),
                        "af": v.get("all_funds")})
        return out

    # ------------------------------------------------------- check summaries
    def check_counts(self, book):
        """Normalised check totals for a book: {'printed': {...}, 'cross': {...}} with keys
        total, exact, within_1, mismatch. The three eras of extraction scripts summarise differently."""
        c = self.checks(book)
        printed = collections.Counter()
        cross = collections.Counter()

        def add(target, d):
            for k, v in d.items():
                if k == "total" or k.startswith("of_which") or not isinstance(v, int):
                    continue
                if k == "exact":
                    target["exact"] += v
                elif k == "within_1":
                    target["within_1"] += v
                elif k == "mismatch":
                    target["mismatch"] += v
                else:
                    target["other"] += v

        if "check_summary_by_tier" in c:                 # 2008-2018
            for tier in c["check_summary_by_tier"].values():
                add(printed, tier.get("printed_total", {}))
                add(cross, tier.get("cross_check", {}))
        elif "printed_total_checks_summary" in c:        # 2019-2024
            for tier in c["printed_total_checks_summary"].values():
                add(printed, tier)
            for tier in c["cross_checks_summary"].values():
                add(cross, tier)
        else:                                            # 2025-2027
            s = c["summary_by_tier"]
            add(printed, s.get("grand_total", {}))
            add(printed, s.get("subtotal", {}))
            add(cross, s.get("cross_table", {}))
        for t in (printed, cross):
            t["total"] = sum(t[k] for k in ("exact", "within_1", "mismatch", "other"))
        return {"printed": printed, "cross": cross}

    def mismatches(self, book):
        """checks.json mismatches with one shape: tier, scope, label, fy, basis, printed, rows, diff, page, raw."""
        out = []
        for m in self.checks(book).get("mismatches", []):
            diff = num(m.get("diff", m.get("difference")))
            printed = num(m.get("printed", m.get("a_value")))
            rows = num(m.get("extracted_sum", m.get("extracted", m.get("b_value"))))
            out.append({"tier": m.get("tier"), "kind": m.get("kind", ""), "scope": str(m.get("scope") or m.get("check") or ""),
                        "label": m.get("label") or m.get("key") or "", "fy": m.get("fiscal_year"), "basis": m.get("basis"),
                        "printed": printed, "rows": rows, "diff": diff, "page": m.get("page"), "raw": m})
        return out
