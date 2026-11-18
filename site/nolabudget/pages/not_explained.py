"""/not-explained/ : what the budget books do not explain.

Every figure on this page is computed here from the extraction evidence (see evidence.py):
the totals each book prints, the rows beneath them, and every printing of every figure across
books. Only the explanatory sentences are written by hand, in site/content/not_explained.yml.
Those sentences hold no figures of their own: they are Jinja templates over the computed
`f` facts below, so a figure cannot be typed into the page by mistake (checks.py enforces this).

Each item states what the documents show ("the book prints X on page A and Y on page B") and
stops there. Nothing here says why; the books do not, and a table in a large document put
together under deadline can disagree with another for many ordinary reasons.
"""
import collections
import os
import re

from markupsafe import Markup

from .. import fmt
from ..data import CROSSWALK, RAW, read_csv
from ..evidence import Evidence, book_order, book_stage, book_year

MILLION = 1_000_000
BIG = MILLION            # a difference of at least this is listed by name
ENTERPRISE_WORDS = ("AVIATION", "FRENCH MARKET", "YACHT", "BUILDING CORP", "CANAL ST", "RIVERGATE",
                    "NEIGHBORHOOD HOUSING", "ECONOMIC DEVELOPMENT FUND", "DELGADO", "PONTALBA", "PIAZZA", "AUDUBON")


class Figures(object):
    """Formats every number that appears on the page and remembers what it printed.

    checks.py compares this set with the dollar figures and comma-grouped numbers in the HTML,
    so anything on the page that did not pass through here is reported."""

    def __init__(self):
        self.used = set()

    def _add(self, s):
        self.used.add(s.lstrip("+−-"))
        return s

    def full(self, x):
        return self._add(fmt.full(x)) if x is not None else ""

    def signed(self, x):
        return self._add(fmt.signed_full(x)) if x is not None else ""

    def exact(self, x):
        """Whole dollars with a dollar sign: $11,956,645."""
        self._add("{:,.0f}".format(abs(x)))
        return self._add("${:,.0f}".format(abs(x)))

    def money(self, x):
        return self._add(fmt.money(x))

    def fte(self, x):
        return self._add("{:,.2f}".format(x)) if x is not None else ""

    def signed_fte(self, x):
        if x is None:
            return ""
        self._add("{:,.2f}".format(abs(x)))
        return ("+" if x > 0 else "\u2212" if x < 0 else "") + "{:,.2f}".format(abs(x))

    def count(self, n):
        return self._add("{:,}".format(n))


def _label(book_id, basis=None):
    """'2025 adopted' / '2026 proposed' for a column."""
    return "%d %s" % (book_year(book_id), book_stage(book_id))


def build(site):
    b = site.budget
    ev = Evidence()
    fig = Figures()
    text = site.content["not_explained"]
    ctx = Context(site, b, ev, fig)

    sections = [
        ctx.section("add-up", [ctx.all_funds_parts, ctx.total_vs_rows, ctx.overview_total, ctx.own_page]),
        ctx.section("spent", [ctx.other_funds_spent]),
        ctx.section("no-section", [ctx.intergovernmental, ctx.transfers_in]),
        ctx.section("reprinted", [ctx.reprinted_totals, ctx.reprinted_depts, ctx.restated_revenue]),
        ctx.section("revenue-lines", [ctx.revenue_lines]),
        ctx.section("labels", [ctx.label_2018, ctx.label_stale, ctx.label_secondary, ctx.label_general_services,
                               ctx.label_titles]),
        ctx.section("positions", [ctx.position_totals, ctx.position_columns, ctx.position_enterprise]),
        ctx.section("missing", [ctx.missing]),
    ]
    tail = ctx.small_differences()
    site.publish("/not-explained/", figures=fig.used)
    site.page("/not-explained/", "not_explained.html", "What the budget books don't explain",
              sections=sections, tail=tail, intro=ctx.render(text["intro"], {"n_books": len(ev.books)}), report=ctx.render(text["report"], {}),
              n_books=len(ev.books))


class Context(object):
    def __init__(self, site, b, ev, fig):
        self.site, self.b, self.ev, self.fig = site, b, ev, fig
        self.text = site.content["not_explained"]
        self.dept_names = {k: v["name"] for k, v in b.depts.items()}

    # ---------------------------------------------------------------- helpers
    def render(self, s, facts):
        if s is None:
            return None
        return Markup(self.site.env.from_string(s).render(f=facts, fig=self.fig))

    def cite(self, book, page=""):
        return self.b.cite(book, "" if page is None else str(page))

    def position_column(self, book, fy, basis):
        """A personnel column's year and basis after the corrections for stale page headers (corrections.csv)."""
        for c in self.ev.corrections():
            if c["file"] == "positions" and c["book_id"] == book and int(c["match_fiscal_year"]) == fy and c["match_basis"] == basis:
                return int(c["set_fiscal_year"]), c["set_basis"]
        return fy, basis

    def dept_link(self, dept_id):
        d = self.b.depts.get(dept_id)
        if not d:
            return dept_id
        return Markup('<a href="%s">%s</a>') % (d["url"], d["name"])

    def section(self, sid, builders):
        meta = self.text["sections"][sid]
        items = []
        for fn in builders:
            it = fn()
            if it:
                items.append(it)
        facts = {"n_items": len(items)}
        return {"id": sid, "title": meta["title"], "intro": self.render(meta.get("intro"), facts), "entries": items}

    def item(self, iid, facts, table=None, cites=None, extra=None):
        """One finding. Hand-written sentences come from the yaml file by id."""
        t = self.text["items"][iid]
        facts = dict(facts, n_books=len(self.ev.books))
        d = {"id": iid, "title": self.render(t["title"], facts), "says": self.render(t.get("says"), facts),
             "after": self.render(t.get("after"), facts), "facts": facts,
             "table": table, "cites": cites or [], "extra": extra}
        if table is not None and t.get("caption"):
            table["caption"] = self.render(t["caption"], facts)
        d["note"] = self.render(t["note"], facts) if t.get("note") else None
        return d

    def table(self, cols, rows, sort=None):
        return {"cols": cols, "rows": rows, "caption": ""}

    def money_cell(self, x):
        return self.fig.full(x)

    # ----------------------------------------------------------------- A: totals
    def _books_with_printed_totals(self):
        return self.ev.books

    def all_funds_parts(self):
        """Columns where the all-funds total differs from General Fund + other funds, in one book."""
        ev, fig = self.ev, self.fig
        rows = {}
        for book in ev.books:
            seen = set()
            for r in ev.printed_totals(book):
                if r["scope"] != "department_expenditures/all_funds":
                    continue
                key = (r["fiscal_year"], r["basis"])
                if key in seen:
                    continue
                seen.add(key)
                fy, basis = key
                af, af_page = r["amount"], r["page"]
                gf, _ = ev.printed_total(book, fy, basis, "general_fund")
                of, _ = ev.printed_total(book, fy, basis, "other_funds")
                if None in (gf, of) or abs(af - gf - of) < BIG:
                    continue
                gaps = sorted(ev.dept_gaps(book, fy, basis), key=lambda g: -abs(g["gap"]))
                k = (fy, basis, gf, of, af)
                if k in rows:
                    rows[k]["cites"].append(self.cite(book, af_page))
                    rows[k]["books"].append(book)
                    continue
                rows[k] = {"fy": fy, "basis": basis, "gf": gf, "of": of, "af": af, "gap": af - gf - of,
                           "cites": [self.cite(book, af_page)], "books": [book], "top": gaps[0] if gaps else None}
        rows = sorted(rows.values(), key=lambda r: -abs(r["gap"]))
        if not rows:
            return None
        trs = []
        for r in rows:
            top = r["top"]
            trs.append([
                {"cites": r["cites"]}, "%d %s" % (r["fy"], r["basis"]), fig.full(r["gf"]), fig.full(r["of"]),
                fig.full(r["af"]), fig.signed(r["gap"]),
                "%s (%s)" % (top["name"], fig.signed(top["gap"])) if top else ""])
        cols = [("Book and page", "cite"), ("Column", "text"), ("General Fund", "num"), ("Other funds", "num"),
                ("All funds", "num"), ("All funds minus the other two", "num"), ("Largest single line (same difference)", "text")]
        # the headline example: the newest adopted book's own-year column
        own = [r for r in rows if r["basis"] == "adopted" and book_year(r["books"][0]) == r["fy"]]
        ex = max(own, key=lambda r: r["fy"]) if own else rows[0]
        f = {"n_rows": len(rows), "ex_book": _label(ex["books"][0]), "ex_col": "%d %s" % (ex["fy"], ex["basis"]),
             "ex_parts": fig.money(ex["gf"] + ex["of"]), "ex_all": fig.money(ex["af"]), "ex_gap": fig.money(abs(ex["gap"])),
             "ex_dept": ex["top"]["name"] if ex["top"] else "", "ex_dept_gap": fig.money(abs(ex["top"]["gap"])) if ex["top"] else ""}
        noaf = [r for r in rows if r["top"] and r["top"]["af"] is None and abs(abs(r["top"]["gap"]) - abs(r["gap"])) < 1]
        f["no_af"] = bool(noaf)
        if noaf:
            f.update(no_af_dept=noaf[0]["top"]["name"], no_af_col="%d %s" % (noaf[0]["fy"], noaf[0]["basis"]),
                     no_af_book=_label(noaf[0]["books"][0]))
        return self.item("all-funds-parts", f, self.table(cols, trs))

    def total_vs_rows(self):
        """Printed totals that differ from the rows beneath them."""
        ev, fig = self.ev, self.fig
        out = []
        for book in ev.books:
            for fund in ("general_fund", "all_funds"):
                seen = set()
                for r in ev.printed_totals(book):
                    if r["scope"] != "department_expenditures/%s" % fund:
                        continue
                    key = (r["fiscal_year"], r["basis"])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows_sum = ev.dept_sums(book, key[0], key[1], fund)
                    if abs(r["amount"] - rows_sum) < BIG:
                        continue
                    top = None
                    if fund == "all_funds":
                        gaps = sorted(ev.dept_gaps(book, key[0], key[1]), key=lambda g: -abs(g["gap"]))
                        top = gaps[0] if gaps else None
                    out.append({"book": book, "fy": key[0], "basis": key[1], "fund": fund, "printed": r["amount"],
                                "rows": rows_sum, "page": r["page"], "top": top})
        out.sort(key=lambda r: -abs(r["printed"] - r["rows"]))
        if not out:
            return None
        trs = []
        for r in out:
            top = r["top"]
            trs.append([{"cites": [self.cite(r["book"], r["page"])]}, "%d %s, %s" % (r["fy"], r["basis"], r["fund"].replace("_", " ")),
                        fig.full(r["printed"]), fig.full(r["rows"]), fig.signed(r["printed"] - r["rows"]),
                        "%s: all-funds table %s; General Fund and other-funds tables %s" % (
                            top["name"], fig.full(top["af"]), fig.full((top["gf"] or 0) + (top["of"] or 0)))
                        if top and r["fund"] == "all_funds" else ""])
        cols = [("Book and page", "cite"), ("Column", "text"), ("Printed total", "num"), ("The rows add to", "num"),
                ("Printed minus rows", "num"), ("Largest single line", "text")]
        ex = [r for r in out if r["fund"] == "all_funds" and r["top"]]
        ex = ex[0] if ex else out[0]
        f = {"n_rows": len(out), "ex_book": _label(ex["book"]), "ex_col": "%d %s" % (ex["fy"], ex["basis"]),
             "ex_printed": fig.money(ex["printed"]), "ex_rows": fig.money(ex["rows"]),
             "ex_dept": ex["top"]["name"] if ex["top"] else "",
             "ex_dept_all": fig.money(ex["top"]["af"]) if ex["top"] else "",
             "ex_dept_gf": fig.money(ex["top"]["gf"]) if ex["top"] else "",
             "ex_dept_of": fig.money(ex["top"]["of"]) if ex["top"] else ""}
        eq = [r for r in out if r["fund"] == "general_fund" and any(
            m["scope"] == "revenues/General Fund" and m["fy"] == r["fy"] and m["basis"] == r["basis"] and m["printed"] is not None
            and abs(m["printed"] - r["rows"]) < 1 for m in ev.mismatches(r["book"]))]
        f["rev_eq"] = bool(eq)
        if eq:
            f.update(rev_book=_label(eq[0]["book"]), rev_col="%d %s" % (eq[0]["fy"], eq[0]["basis"]), rev_rows=fig.money(eq[0]["rows"]),
                     rev_total=fig.money(eq[0]["rows"]), rev_printed=fig.money(eq[0]["printed"]))
        return self.item("total-vs-rows", f, self.table(cols, trs))

    def overview_total(self):
        """The overview page's grand total against the department tables, with the fund that explains the gap."""
        ev, fig = self.ev, self.fig
        found = collections.OrderedDict()
        for book in ev.books:
            if book_year(book) < 2025:
                continue
            for m in ev.mismatches(book):
                if m["scope"] == "overview_expenditures_grand_total_vs_sum_of_dept_all_funds" and abs(m["diff"]) >= BIG:
                    found.setdefault((m["fy"], m["basis"], m["printed"], m["rows"]), []).append(book)
        if not found:
            return None
        rows = sorted(found.items(), key=lambda kv: -abs(kv[0][2] - kv[0][3]))
        trs = []
        others = []
        for (fy, basis, printed, tables), books in rows:
            # a fund line some other book prints on its overview page for the same column, equal to the gap
            other = None
            for bk in ev.books:
                for r in ev.printed_totals(bk):
                    if r["scope"] == "overview_of_expenditures" and r["fiscal_year"] == fy and r["basis"] == basis \
                            and r["label"] != "Grand Total" and r["amount"] is not None and abs(r["amount"] - abs(printed - tables)) < 1:
                        other = (bk, r)
            others.append(other)
            trs.append([{"cites": [self.cite(bk, "Overview of Expenditures") for bk in books]}, "%d %s" % (fy, basis),
                        fig.full(printed), fig.full(tables), fig.signed(printed - tables)])
        cols = [("Book and page", "cite"), ("Column", "text"), ("Overview page total", "num"),
                ("Department tables total", "num"), ("Difference", "num")]
        (fy, basis, printed, tables), books = rows[0]
        other = others[0]
        f = {"col": "%d %s" % (fy, basis), "books": " and ".join(_label(bk) for bk in books),
             "overview": fig.money(printed), "tables": fig.money(tables), "gap": fig.money(abs(printed - tables)),
             "has_other": bool(other), "other_label": other[1]["label"] if other else "",
             "other_book": _label(other[0]) if other else "", "other_amount": fig.exact(other[1]["amount"]) if other else ""}
        extra = {"cite": self.cite(other[0], other[1]["page"])} if other else None
        return self.item("overview-total", f, self.table(cols, trs), extra=extra)

    def own_page(self):
        """A body's own section against its line in the citywide table (2026 adopted book, 2026 column)."""
        ev, fig = self.ev, self.fig
        book = "2026-operating-adopted"
        rows = []
        for m in ev.mismatches(book):
            if m["scope"] == "dept_by_program_vs_citywide_all_funds" and m["fy"] == 2026 and m["basis"] == "adopted" \
                    and abs(m["diff"]) >= BIG:
                rows.append(m)
        if not rows:
            return None
        rows.sort(key=lambda m: -abs(m["diff"]))
        city_page = "Total Expenditures by Department"
        trs = []
        for m in rows:
            trs.append([m["label"].replace(" - Component Unit", ""), {"cites": [self.cite(book, city_page), self.cite(book, m["page"])]},
                        fig.full(m["printed"]), fig.full(m["rows"]), fig.signed(m["printed"] - m["rows"])])
        cols = [("Body", "text"), ("Citywide table, then own page", "cite"), ("Citywide table", "num"),
                ("Own page", "num"), ("Difference", "num")]
        f = {"n": len(rows), "names": [m["label"].replace(" - Component Unit", "") for m in rows],
             "book": _label(book)}
        return self.item("own-page", f, self.table(cols, trs))

    # -------------------------------------------------------------- C: spent
    def other_funds_spent(self):
        b, fig = self.b, self.fig
        years = [y for y in b.years if b.total(y, "adopted", "other_funds") and b.total(y, "actual", "other_funds")]
        if not years:
            return None
        trs = []
        below = 0
        ratios = []
        for y in years:
            ad, ac = b.total(y, "adopted", "other_funds"), b.total(y, "actual", "other_funds")
            gad, gac = b.total(y, "adopted", "general_fund"), b.total(y, "actual", "general_fund")
            if ac < ad:
                below += 1
            ratios.append((ac / ad, y))
            trs.append([str(y), fig.full(ad), fig.full(ac), fmt.pct(ac / ad, 0),
                        fmt.pct(gac / gad, 0) if gad and gac else "",
                        {"cites": [b.total_source(y, "adopted", "other_funds"), b.total_source(y, "actual", "other_funds")]}])
        cols = [("Year", "text"), ("Other funds, adopted", "num"), ("Other funds, actual", "num"),
                ("Actual as a share of adopted", "num"), ("Same for the General Fund", "num"), ("Sources (adopted, then actual)", "cite")]
        low = min(ratios)
        lo2 = sorted(ratios)[1]
        above = [y for r, y in ratios if r > 1]
        f = {"n_years": len(years), "n_below": below, "first": years[0], "last": years[-1],
             "median": fmt.pct(sorted(r for r, _ in ratios)[len(ratios) // 2], 0),
             "low_pct": fmt.pct(low[0], 0), "low_year": low[1], "above_years": above,
             "above_text": ", ".join(str(y) for y in above)}
        return self.item("other-funds-spent", f, self.table(cols, trs))

    # ----------------------------------------------------- B: lines with no section
    def intergovernmental(self):
        ev, b, fig = self.ev, self.b, self.fig
        alias = {k: v for k, v in ev.statement_aliases().items() if v == "intergovernmental"}
        names = collections.defaultdict(set)
        for (book, name), d in alias.items():
            names[book].add(name)
        by_year = {}
        for book in ev.books:
            if not names.get(book):
                continue
            sections = {r["dept_name"].strip().lower() for r in ev.detail_rows(book)}
            if names[book] & {n.lower() for n in sections} or any(n.lower() in sections for n in names[book]):
                continue   # this book has a department section for it
            fy = book_year(book)
            basis = "adopted" if book_stage(book) == "adopted" else "proposed"
            amt = sum(r["amount"] for r in ev.dept_rows(book) if r["dept_name"] in names[book] and r["fiscal_year"] == fy
                      and r["basis"] == basis and r["fund_group"] == "all_funds")
            page = next((r["page"] for r in ev.dept_rows(book) if r["dept_name"] in names[book] and r["fiscal_year"] == fy
                         and r["basis"] == basis and r["fund_group"] == "all_funds"), "")
            if amt and (fy not in by_year or book_stage(book) == "adopted"):
                by_year[fy] = {"book": book, "fy": fy, "basis": basis, "amt": amt, "page": page}
        rows = [by_year[y] for y in sorted(by_year)]
        if not rows:
            return None
        trs = [[{"cites": [self.cite(r["book"], r["page"])]}, "%d %s" % (r["fy"], r["basis"]), fig.full(r["amt"])] for r in rows]
        cols = [("Book and page", "cite"), ("Column", "text"), ("Intergovernmental, all funds", "num")]
        lo = min(rows, key=lambda r: r["amt"])
        hi = max(rows, key=lambda r: r["amt"])
        f = {"n": len(rows), "first": rows[0]["book"][:4], "last": rows[-1]["book"][:4], "low": fig.money(lo["amt"]),
             "low_year": lo["fy"], "high": fig.money(hi["amt"]), "high_year": hi["fy"],
             "n_100": sum(1 for r in rows if round(r["amt"]) == 100 * MILLION), "hundred": fig.money(100 * MILLION)}
        of_only = True
        for r in rows:
            k = (r["fy"], r["basis"], "general_fund", "intergovernmental")
            gf = ev.spending_observations().get(k, {}).get(r["book"], 0)
            if abs(gf) > 0.5:
                of_only = False
        f["gf_zero"] = of_only
        first_year = rows[0]["fy"]
        f["had_before"] = any(book_year(bk) < first_year and names.get(bk) and any(
            r["dept_name"].strip().lower() in {n.lower() for n in names[bk]} for r in ev.detail_rows(bk)) for bk in ev.books)
        return self.item("intergovernmental", f, self.table(cols, trs))

    def transfers_in(self):
        ev, b, fig = self.ev, self.b, self.fig
        book = "2026-operating-adopted"
        line = "Transfer From Other Funds"
        rows = [r for r in ev.revenue_rows(book) if r["line_name"] == line and r["fund_group"] == "general_fund"]
        if not rows:
            return None
        by = {(r["fiscal_year"], r["basis"]): r for r in rows}
        cur = by.get((2026, "adopted"))
        if not cur or not cur["amount"]:
            return None
        total = b.total(2026, "adopted", "general_fund")
        again = [r for r in ev.revenue_rows("2027-operating-proposed") if r["line_name"] == line and r["fiscal_year"] == 2026
                 and r["basis"] == "adopted"]
        trs = []
        for key in ((2025, "adopted"), (2026, "proposed"), (2026, "adopted")):
            r = by.get(key)
            if r:
                trs.append([{"cites": [self.cite(book, r["page"])]}, "%d %s" % key, fig.full(r["amount"])])
        if again:
            trs.append([{"cites": [self.cite("2027-operating-proposed", again[0]["page"])]}, "2026 adopted, as reprinted in the 2027 book",
                        fig.full(again[0]["amount"])])
        cols = [("Book and page", "cite"), ("Column", "text"), ("General Fund, line 'Transfer From Other Funds'", "num")]
        f = {"amount": fig.money(cur["amount"]), "share": fmt.pct(cur["amount"] / total, 0), "gf_total": fig.money(total),
             "book": _label(book), "year": 2026, "prev": 2025, "again": bool(again)}
        return self.item("transfers-in", f, self.table(cols, trs))

    # ------------------------------------------------------ E: reprinted figures
    def _first_and_later(self, books_vals, min_diff, key):
        """Given {book: value-vector}, the first book and the first later book whose vector differs by min_diff."""
        order = sorted(books_vals, key=book_order)
        first = order[0]
        for later in order[1:]:
            if any(abs(books_vals[later][k] - books_vals[first][k]) >= min_diff for k in key):
                return first, later
        return None

    def reprinted_totals(self):
        ev, b, fig = self.ev, self.b, self.fig
        cols_tot = ev.spending_column_totals()
        rows = []
        for fy, basis in sorted({(k[0], k[1]) for k in cols_tot if k[1] in ("adopted", "proposed")}):
            vec = collections.defaultdict(dict)
            for fund in ("general_fund", "other_funds", "all_funds"):
                for bk, v in cols_tot.get((fy, basis, fund), {}).items():
                    vec[bk][fund] = v
            vec = {bk: v for bk, v in vec.items() if len(v) == 3}
            if len(vec) < 2:
                continue
            pair = self._first_and_later(vec, BIG, ("general_fund", "other_funds", "all_funds"))
            if not pair:
                continue
            a, c = pair
            rows.append({"fy": fy, "basis": basis, "a": a, "c": c, "va": vec[a], "vc": vec[c]})
        if not rows:
            return None
        rows.sort(key=lambda r: -max(abs(r["vc"][k] - r["va"][k]) for k in r["va"]))
        trs = []
        for r in rows:
            va, vc = r["va"], r["vc"]
            trs.append(["%d %s" % (r["fy"], r["basis"]),
                        {"cites": [self.cite(r["a"], ev.column_page(r["fy"], r["basis"], "all_funds", r["a"]))]},
                        {"cites": [self.cite(r["c"], ev.column_page(r["fy"], r["basis"], "all_funds", r["c"]))]},
                        fig.full(va["general_fund"]), fig.full(vc["general_fund"]),
                        fig.signed(vc["general_fund"] - va["general_fund"]), fig.signed(vc["other_funds"] - va["other_funds"]),
                        fig.signed(vc["all_funds"] - va["all_funds"])])
        cols = [("Column", "text"), ("First printed in", "cite"), ("Reprinted in", "cite"),
                ("General Fund as first printed", "num"), ("General Fund as reprinted", "num"),
                ("Change, General Fund", "num"), ("Change, other funds", "num"), ("Change, all funds", "num")]
        ex = next((r for r in rows if r["basis"] == "adopted" and r["fy"] == 2015), rows[0])
        gf_only = [r for r in rows if abs(r["vc"]["all_funds"] - r["va"]["all_funds"]) < BIG <= abs(r["vc"]["general_fund"] - r["va"]["general_fund"])]
        f = {"n": len(rows), "ex_col": "%d %s" % (ex["fy"], ex["basis"]), "ex_a": _label(ex["a"]), "ex_c": _label(ex["c"]),
             "ex_gf_a": fig.money(ex["va"]["general_fund"]), "ex_gf_c": fig.money(ex["vc"]["general_fund"]),
             "ex_af_a": fig.money(ex["va"]["all_funds"]), "ex_af_c": fig.money(ex["vc"]["all_funds"]),
             "n_gf_only": len(gf_only), "gf_only_years": ", ".join(str(r["fy"]) for r in sorted(gf_only, key=lambda r: r["fy"])),
             "ex_af_same": abs(ex["vc"]["all_funds"] - ex["va"]["all_funds"]) < BIG}
        prop = [r for r in rows if r["basis"] == "proposed"]
        f["has_prop"] = bool(prop)
        if prop:
            p = prop[0]
            f.update(prop_col="%d" % p["fy"], prop_a=_label(p["a"]), prop_c=_label(p["c"]),
                     prop_gf_a=fig.money(p["va"]["general_fund"]), prop_gf_c=fig.money(p["vc"]["general_fund"]),
                     prop_af_a=fig.money(p["va"]["all_funds"]), prop_af_c=fig.money(p["vc"]["all_funds"]))
        return self.item("reprinted-totals", f, self.table(cols, trs))

    def reprinted_depts(self):
        ev, b, fig = self.ev, self.b, self.fig
        so = ev.spending_observations()
        diffs = []
        for (fy, basis, fund, dept), books in so.items():
            if basis not in ("adopted", "proposed") or fund not in ("general_fund", "other_funds") or dept == "intergovernmental":
                continue
            if len(books) < 2:
                continue
            pair = self._first_and_later({bk: {"v": v} for bk, v in books.items()}, 10 * MILLION, ("v",))
            if not pair:
                continue
            a, c = pair
            diffs.append({"fy": fy, "basis": basis, "fund": fund, "dept": dept, "a": a, "c": c, "va": books[a], "vc": books[c],
                          "d": books[c] - books[a]})
        if not diffs:
            return None
        diffs.sort(key=lambda r: -abs(r["d"]))
        trs = []
        for r in diffs:
            trs.append(["%d %s" % (r["fy"], r["basis"]), self.dept_link(r["dept"]), r["fund"].replace("_", " "),
                        {"cites": [self.cite(r["a"], ev.spending_page((r["fy"], r["basis"], r["fund"], r["dept"]), r["a"]))]},
                        fig.full(r["va"]),
                        {"cites": [self.cite(r["c"], ev.spending_page((r["fy"], r["basis"], r["fund"], r["dept"]), r["c"]))]},
                        fig.full(r["vc"]), fig.signed(r["d"])])
        cols = [("Column", "text"), ("Department", "text"), ("Fund", "text"), ("First printed in", "cite"), ("Amount", "num"),
                ("Reprinted in", "cite"), ("Amount", "num"), ("Change", "num")]
        # pairs of departments whose changes nearly cancel (money moved from one line to another)
        swaps = []
        by = collections.defaultdict(list)
        for r in diffs:
            by[(r["fy"], r["basis"], r["fund"], r["a"], r["c"])].append(r)
        for k, lst in by.items():
            for i in range(len(lst)):
                for j in range(i + 1, len(lst)):
                    x, y = lst[i], lst[j]
                    if x["d"] * y["d"] < 0 and abs(x["d"] + y["d"]) <= 0.05 * max(abs(x["d"]), abs(y["d"])):
                        swaps.append((x, y))
        swaps.sort(key=lambda p: -abs(p[0]["d"]))
        sw = []
        for x, y in swaps:
            sw.append({"col": "%d %s, %s" % (x["fy"], x["basis"], x["fund"].replace("_", " ")),
                       "a": _label(x["a"]), "c": _label(x["c"]),
                       "d1": self.dept_link(x["dept"]), "d2": self.dept_link(y["dept"]),
                       "a1": fig.money(x["va"]), "c1": fig.money(x["vc"]), "a2": fig.money(y["va"]), "c2": fig.money(y["vc"])})
        # the 2026 proposal column: how much of the book-to-book difference sits in one department
        prop = [r for r in diffs if r["basis"] == "proposed" and r["fund"] == "general_fund"]
        f = {"n": len(diffs), "swaps": sw, "n_swaps": len(sw), "threshold": fig.money(10 * MILLION)}
        if prop:
            p = prop[0]
            tot = ev.spending_column_totals().get((p["fy"], "proposed", "general_fund"), {})
            if p["a"] in tot and p["c"] in tot:
                f.update(prop_col="%d proposed" % p["fy"], prop_dept=self.dept_link(p["dept"]),
                         prop_dept_gap=fig.money(abs(p["d"])), prop_total_gap=fig.money(abs(tot[p["c"]] - tot[p["a"]])),
                         prop_a=_label(p["a"]), prop_c=_label(p["c"]),
                         prop_all=abs(abs(p["d"]) - abs(tot[p["c"]] - tot[p["a"]])) < 1)
        return self.item("reprinted-depts", f, self.table(cols, trs))

    def restated_revenue(self):
        ev, fig = self.ev, self.fig
        ro = ev.revenue_observations()
        lines = []
        n_both = 0
        for (fy, basis, fund, rid), books in ro.items():
            if fy != 2006 or basis != "actual" or fund != "general_fund" or len(books) < 2:
                continue
            n_both += 1
            order = sorted(books, key=book_order)
            first = order[0]
            change = next((bk for bk in order if round(books[bk]) != round(books[first])), None)
            if change is None:
                continue
            prev = order[order.index(change) - 1]
            lines.append({"rid": rid, "a": prev, "c": change, "va": books[prev], "vc": books[change]})
        if not lines:
            return None
        lines.sort(key=lambda r: -abs(r["vc"] - r["va"]))
        top = lines[:6]
        names = {k: v.get("name", k) for k, v in self.b.rev_lines.items()}
        key = lambda r: (2006, "actual", "general_fund", r["rid"])
        trs = [[names.get(r["rid"], r["rid"]), {"cites": [self.cite(r["a"], ev.revenue_page(key(r), r["a"]))]}, fig.full(r["va"]),
                {"cites": [self.cite(r["c"], ev.revenue_page(key(r), r["c"]))]},
                fig.full(r["vc"]), fig.signed(r["vc"] - r["va"])] for r in top]
        cols = [("Income line, General Fund", "text"), ("Printed in", "cite"), ("2006 actual", "num"), ("Reprinted in", "cite"),
                ("2006 actual", "num"), ("Change", "num")]
        f = {"n_changed": len(lines), "n_both": n_both, "n_shown": len(top), "a": _label(top[0]["a"]), "c": _label(top[0]["c"])}
        return self.item("restated-revenue", f, self.table(cols, trs))

    # --------------------------------------------------------- D: revenue lines
    def revenue_lines(self):
        ev, fig = self.ev, self.fig
        found = {}
        for book in ev.books:
            ms = ev.mismatches(book)
            cats = collections.defaultdict(list)
            for m in ms:
                if m["scope"] == "revenues_summary_category_vs_category_table" and m["diff"] and abs(m["diff"]) >= 1000:
                    cats[(m["fy"], m["basis"])].append(m)
                elif m["scope"].startswith("revenues/General Fund/") and m["scope"].count("/") == 2 and m["diff"] \
                        and abs(m["diff"]) >= 1000:
                    cats[(m["fy"], m["basis"])].append(m)
            for m in ms:
                total = (m["scope"] == "revenues_summary_grand_total_vs_sum_of_category_grand_totals") or m["scope"] == "revenues/General Fund"
                if not total or m["diff"] is None or abs(m["diff"]) < 1000:
                    continue
                if m["scope"] == "revenues/General Fund" and book_year(book) != m["fy"]:
                    continue
                key = (m["fy"], m["basis"], m["printed"], m["rows"])
                where = [(c["label"] or c["scope"].rsplit("/", 1)[-1].title(), c["diff"]) for c in cats.get((m["fy"], m["basis"]), [])]
                if key in found:
                    found[key]["cites"].append(self.cite(book, "Statement of Revenues" if book_year(book) >= 2025 else m["page"]))
                    found[key]["books"].append(book)
                else:
                    found[key] = {"fy": m["fy"], "basis": m["basis"], "printed": m["printed"], "rows": m["rows"], "books": [book],
                                  "page": m["page"], "where": where,
                                  "cites": [self.cite(book, "Statement of Revenues" if book_year(book) >= 2025 else m["page"])]}
        rows = sorted(found.values(), key=lambda r: -abs(r["printed"] - r["rows"]))
        if not rows:
            return None
        trs = []
        for r in rows:
            where = "; ".join("%s (%s)" % (lab, fig.full(abs(d))) for lab, d in sorted(r["where"], key=lambda x: -abs(x[1]))[:3])
            trs.append([{"cites": r["cites"]}, "%d %s" % (r["fy"], r["basis"]), fig.full(r["printed"]), fig.full(r["rows"]),
                        fig.signed(r["printed"] - r["rows"]), where])
        cols = [("Book and page", "cite"), ("Column", "text"), ("General Fund total, printed", "num"), ("The lines add to", "num"),
                ("Printed minus lines", "num"), ("Where the gap sits", "text")]
        big = rows[0]
        limit = 2 * MILLION
        f = {"n": len(rows), "big_col": "%d %s" % (big["fy"], big["basis"]), "big_book": " and ".join(_label(x) for x in big["books"]),
             "big_gap": fig.money(abs(big["printed"] - big["rows"])), "small_limit": fig.money(limit),
             "n_small": sum(1 for r in rows if abs(r["printed"] - r["rows"]) < limit)}
        return self.item("revenue-lines", f, self.table(cols, trs))

    # ----------------------------------------------------------- F: labels
    def label_2018(self):
        ev, fig = self.ev, self.fig
        corr = [c for c in ev.corrections() if c["book_id"] == "2018-operating-adopted" and c["file"] == "department_expenditures"]
        if not corr:
            return None
        c = corr[0]
        tot = ev.spending_column_totals()
        fy = int(c["match_fiscal_year"])
        held = tot[(fy, "actual", "all_funds")].get(c["book_id"])
        adopted = tot[(fy, "adopted", "all_funds")]
        a16 = adopted.get("2016-operating-adopted")
        a17 = adopted.get("2017-operating-adopted")
        if None in (held, a16, a17):
            return None
        f = {"book": _label(c["book_id"]), "fy": fy, "held": fig.money(held), "a16": fig.money(a16), "a17": fig.money(a17)}
        return self.item("label-2018", f, None, cites=[self.cite(c["book_id"], "")])

    def label_stale(self):
        ev, fig = self.ev, self.fig
        out = []
        for c in ev.corrections():
            if c["file"] != "positions" or "stale" not in c["reason"].lower():
                continue
            if c["book_id"] not in [o[0] for o in out]:
                out.append((c["book_id"], c["reason"]))
        res = []
        for book, _ in out:
            pages = collections.defaultdict(set)
            for r in ev.position_rows(book):
                pages[r["page"]].add(int(r["fiscal_year"]))
            lo = collections.Counter(min(v) for v in pages.values())
            stale_first = min(lo)             # the stale template's earliest year
            res.append({"book": book, "stale": lo[stale_first], "other": sum(n for y, n in lo.items() if y != stale_first),
                        "stale_year": stale_first, "years": sorted(lo)})
        if not res:
            return None
        f = {"rows": [{"book": _label(r["book"]), "stale": r["stale"], "other": r["other"], "sy": r["stale_year"],
                       "cite": self.cite(r["book"], "")} for r in res]}
        f["b1"], f["b2"] = f["rows"][0], f["rows"][-1]
        return self.item("label-stale", f, None, cites=[self.cite(r["book"], "") for r in res])

    def label_secondary(self):
        ev = self.ev
        books = sorted({r["book_id"] for r in ev.alias_rows("statement") if r["note"].startswith("MISLABELLED")}, key=book_order)
        if not books:
            return None
        labels = [_label(b) for b in books]
        f = {"n": len(books), "books": labels, "books_text": ", ".join(labels[:-1]) + (" and " if len(labels) > 1 else "") + labels[-1]}
        return self.item("label-secondary", f, None, cites=[self.cite(b, "") for b in books])

    def label_general_services(self):
        ev = self.ev
        rows = [r for r in ev.alias_rows("statement") if r["printed_name"] == "General Services" and r["dept_id"] == "inspector-general"]
        books = sorted({r["book_id"] for r in rows}, key=book_order)
        if not books:
            return None
        f = {"n": len(books), "first": book_year(books[0]), "last": book_year(books[-1])}
        return self.item("label-general-services", f, None, cites=[self.cite(books[0], ""), self.cite(books[-1], "")])

    def label_titles(self):
        ev = self.ev
        hits = []
        for book in ev.books:
            seen = set()
            for r in ev.position_rows(book):
                if "AVIATION" in r["dept_name"].upper() and "LAW" in r["program"].upper() and r["page"] not in seen:
                    seen.add(r["page"])
                    hits.append((book, r["page"]))
        if not hits:
            return None
        f = {"pages": [{"book": _label(bk), "page": pg, "cite": self.cite(bk, pg)} for bk, pg in hits]}
        return self.item("label-titles", f, None, cites=[self.cite(bk, pg) for bk, pg in hits])

    # --------------------------------------------------------- G: positions
    def _enterprise(self, name):
        n = name.upper()
        return any(w in n for w in ENTERPRISE_WORDS)

    def position_totals(self):
        ev, fig = self.ev, self.fig
        found = {}
        for book in ev.books:
            for m in ev.mismatches(book):
                sc = m["scope"]
                if not sc.startswith("positions/") or m["diff"] is None or abs(m["diff"]) < 10:
                    continue
                if m["kind"] not in ("fte_sum_vs_fund_total", "fte_sum_vs_dept_total") and not str(m["tier"]).startswith("tier4"):
                    continue
                if m["kind"] == "" and "printed program totals" not in str(m["raw"].get("method", "")) \
                        and "printed fund totals" not in str(m["raw"].get("method", "")):
                    continue
                parts = sc.split("/")
                if len(parts) > 3 or self._enterprise(parts[1]):
                    continue
                fund = parts[2] if len(parts) > 2 else (re.sub(r"\s+Total$", "", m["label"]) if "fund" in m["label"].lower() else "")
                key = (book, parts[1], fund, m["printed"], m["rows"])
                rec = found.setdefault(key, {"book": book, "dept": parts[1], "fund": fund,
                                             "printed": m["printed"], "rows": m["rows"], "page": m["page"], "years": set()})
                if m["fy"]:
                    rec["years"].add(self.position_column(book, m["fy"], m["basis"])[0])
        rows = sorted(found.values(), key=lambda r: (book_order(r["book"]), r["dept"]))
        # a department total and its fund total are the same finding
        best = {}
        for r in rows:
            k = (r["book"], r["dept"], tuple(sorted(r["years"])))
            if k not in best or abs(r["printed"] - r["rows"]) > abs(best[k]["printed"] - best[k]["rows"]):
                best[k] = r
        rows = sorted(best.values(), key=lambda r: (book_order(r["book"]), r["dept"]))
        if not rows:
            return None
        pretty = lambda s: s.title() if s.isupper() else s
        trs = []
        for r in rows:
            ys = sorted(r["years"])
            yrs = ("%d" % ys[0] if len(ys) == 1 else "%d to %d" % (ys[0], ys[-1])) if ys else ""
            trs.append([{"cites": [self.cite(r["book"], r["page"])]}, pretty(r["dept"]),
                        pretty(re.sub(r"^\d+\s+", "", r["fund"])) if r["fund"] else "Whole department", yrs, fig.fte(r["printed"]),
                        fig.fte(r["rows"]), fig.signed_fte(r["printed"] - r["rows"])])
        cols = [("Book and page", "cite"), ("Department", "text"), ("Total for", "text"), ("Years", "text"),
                ("Printed total, positions", "num"), ("The rows add to", "num"), ("Printed minus rows", "num")]
        ex = next((r for r in rows if r["book"] == "2018-operating-adopted"), rows[0])
        f = {"n": len(rows), "ex_book": _label(ex["book"]), "ex_dept": pretty(ex["dept"]), "ex_printed": fig.fte(ex["printed"]),
             "ex_rows": fig.fte(ex["rows"]), "ex_page": ex["page"]}
        return self.item("position-totals", f, self.table(cols, trs))

    def position_columns(self):
        ev, fig = self.ev, self.fig
        al = {}
        for r in ev.alias_rows("detail"):
            if r["source_file"] == "positions":
                al[(r["book_id"], r["printed_name"].strip().upper())] = r["dept_id"]
        groups = {k: v["group"] for k, v in self.b.depts.items()}
        stats = {}
        for book in ev.books:
            tot = collections.defaultdict(lambda: collections.defaultdict(float))
            for r in ev.position_rows(book):
                d = al.get((book, r["dept_name"].strip().upper()))
                if d is None or groups.get(d) == "Enterprise bodies":
                    continue
                tot[d][(int(r["fiscal_year"]), r["basis"])] += float(r["fte"] or 0)
            n = same = 0
            for d, v in tot.items():
                if len(v) >= 2:
                    n += 1
                    if len({round(x, 2) for x in v.values()}) == 1:
                        same += 1
            stats[book] = (same, n)
        b18 = "2018-operating-adopted"
        if b18 not in stats or not stats[b18][1]:
            return None
        before = max((bk for bk in stats if book_order(bk) < book_order(b18) and stats[bk][1]), key=book_order)
        after = min((bk for bk in stats if book_order(bk) > book_order(b18) and stats[bk][1]), key=book_order)
        f = {"same": stats[b18][0], "n": stats[b18][1], "before": _label(before), "before_same": stats[before][0],
             "before_n": stats[before][1], "after": _label(after), "after_same": stats[after][0], "after_n": stats[after][1],
             "book": _label(b18)}
        return self.item("position-columns", f, None, cites=[self.cite(b18, "")])

    def position_enterprise(self):
        ev, fig = self.ev, self.fig
        rows = {}
        french = set()
        for book in ev.books:
            for m in ev.mismatches(book):
                sc = m["scope"]
                if not sc.startswith("positions/") or m["diff"] is None or abs(m["diff"]) < 10:
                    continue
                if m["kind"] not in ("fte_sum_vs_dept_total", "fte_sum_vs_fund_total") or sc.count("/") > 2:
                    continue
                name = sc.split("/")[1]
                if "FRENCH MARKET" in name.upper():
                    french.add(book)
                if "AVIATION" in name.upper():
                    fy2, basis2 = self.position_column(book, m["fy"], m["basis"])
                    rows[(book, fy2, basis2)] = {"book": book, "fy": fy2, "basis": basis2, "printed": m["printed"],
                                                 "rows": m["rows"], "page": m["page"]}
        rows = sorted(rows.values(), key=lambda r: (book_order(r["book"]), r["fy"]))
        # one line per book: the column whose printed total is furthest from its rows
        best = {}
        for r in rows:
            if r["book"] not in best or abs(r["printed"] - r["rows"]) > abs(best[r["book"]]["printed"] - best[r["book"]]["rows"]):
                best[r["book"]] = r
        rows = [best[b] for b in sorted(best, key=book_order)]
        if not rows:
            return None
        trs = [[{"cites": [self.cite(r["book"], r["page"])]}, "%d %s" % (r["fy"], r["basis"]), fig.fte(r["printed"]),
                fig.fte(r["rows"])] for r in rows]
        cols = [("Book and page", "cite"), ("Column", "text"), ("Printed total, positions", "num"), ("The job rows add to", "num")]
        f = {"n": len(rows), "first": book_year(rows[0]["book"]), "last": book_year(rows[-1]["book"]), "n_french": len(french),
             "all_below": all(r["printed"] < r["rows"] for r in rows)}
        return self.item("position-enterprise", f, self.table(cols, trs))

    # ----------------------------------------------------------- H: missing
    def missing(self):
        ev = self.ev
        adopted = sorted(book_year(b) for b in ev.books if book_stage(b) == "adopted")
        gaps = [y for y in range(adopted[0], adopted[-1] + 1) if y not in adopted]
        import json
        pre = json.load(open(os.path.join(RAW, "pre-2011", "manifest.json")))
        docs_2010 = [d for d in pre if d.get("fiscal_year") == 2010]
        has_2010_book = any(d.get("doc_type") == "adopted_operating_budget" for d in docs_2010)
        nola = json.load(open(os.path.join(RAW, "nola-gov", "manifest.json")))
        has_2020_adopted = any(str(d.get("year")) == "2020" and d.get("kind") == "operating-adopted" for d in nola)
        unparsed = ev.checks("2020-operating-proposed").get("unparsed", [])
        n_unread = sum(len(u["pages"]) for u in unparsed if "organizational" not in u["what"])
        q = json.load(open(os.path.join(RAW, "questica", "manifest.json")))
        host = re.sub(r"^https?://", "", q.get("source", ""))
        last_year = max(book_year(b) for b in ev.books)
        f = {"gaps": gaps, "gap_text": " and ".join(str(y) for y in gaps), "has_2010": 2010 in gaps and not has_2010_book,
             "n_2010_docs": len(docs_2010), "has_2020": 2020 in gaps and not has_2020_adopted, "n_unread": self.fig.count(n_unread),
             "host": host, "first_open": min(book_year(b) for b in ev.books if book_year(b) >= 2025),
             "last_year": last_year, "first_book": book_year(ev.books[0]),
             "n_pre2025": sum(1 for b in ev.books if book_year(b) < 2025)}
        return self.item("missing", f, None)

    # ---------------------------------------------------------------- tail
    def small_differences(self):
        ev = self.ev
        within1 = 0
        small = 0
        for book in ev.books:
            cc = ev.check_counts(book)
            within1 += cc["printed"]["within_1"] + cc["cross"]["within_1"]
            for m in ev.mismatches(book):
                if m["diff"] is None or m["scope"].startswith("positions") or "fte" in m["kind"] or "fte" in m["scope"]:
                    continue
                if 1 < abs(m["diff"]) <= 5:
                    small += 1
        f = {"within1": self.fig.count(within1), "small": self.fig.count(small), "n_books": len(ev.books)}
        return self.render(self.text["small"], f)
