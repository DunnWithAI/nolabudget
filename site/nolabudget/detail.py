"""What a department's money is spent on, from its own section of a budget book
(data/combined/department_detail.csv).

Rules (see data/extracted/SCHEMA.md):
* One table per breakdown, from ONE book: the latest book that prints the department's
  own section, for that book's own year and stage (e.g. 2027 proposed in the 2027
  proposed book).
* `dept_summary_*` tables are never used: they sit above sub-sections that have their
  own tables, and adding them would double count.
* Where a department has sub-units (e.g. Miscellaneous = CAO misc + NORDC + ...), each
  sub-unit is shown as a part of the whole.
* If a book prints no `by_type` / `by_fund` table, the split is read from the
  program table's type/fund columns instead (still one table).
"""
import collections
import re

PROGRAMS_SHOWN = 12
# Plain names for sub-units whose printed section title would mislead.
SUB_LABELS = {"cao-misc": "Citywide costs (printed in the Chief Administrative Office's pages)"}
TYPE_ORDER = ["Pay and benefits", "Supplies, contracts and other running costs", "Loan repayments",
              "Grants, contributions and transfers to other funds", "Money held in reserve"]


def clean(label):
    s = (label or "").strip()
    if not s or s == "(blank)":
        return "Not specified"
    s = re.sub(r"\s+-\s+\d{3,5}$", "", s)        # "Field Operations - 2780"
    s = re.sub(r"^\d{3,5}\s+", "", s)             # "5801 PARK AND RECREATION ADMIN"
    if s.isupper() and len(s) > 3:
        small = {"And", "Of", "The", "For", "To", "In", "On"}
        words = [w.capitalize() for w in s.lower().split()]
        s = " ".join(w.lower() if (i and w in small) else w for i, w in enumerate(words))
    return s


def _pick_rows(rows):
    """Rows for the latest book that prints this department's own section."""
    books = sorted({r["book_id"] for r in rows if not r["table"].startswith("dept_summary")}, reverse=True)
    for book in books:
        fy = int(book[:4])
        stage = book.rsplit("-", 1)[1]
        rs = [r for r in rows if r["book_id"] == book and r["fiscal_year"] == fy and r["basis"] == stage
              and not r["table"].startswith("dept_summary")]
        if rs and any(r["amount"] for r in rs):
            return book, fy, stage, rs
    return None, None, None, []


def _group(rows, keyf):
    parts = collections.OrderedDict()
    for r in rows:
        sub = r["sub_unit"] or ""
        k = keyf(r)
        parts.setdefault(sub, collections.OrderedDict())
        parts[sub][k] = parts[sub].get(k, 0) + r["amount"]
    return parts


def breakdowns(budget, dept_id):
    rows = budget.detail.get(dept_id, [])
    book, fy, stage, rs = _pick_rows(rows)
    if not book:
        return None
    by = collections.defaultdict(list)
    for r in rs:
        by[r["table"]].append(r)
    sub_names = {}
    for r in rs:
        if r["sub_unit"] and r["sub_unit"] not in sub_names:
            sub_names[r["sub_unit"]] = r["printed_dept"]

    def split(r, i):
        parts = [p.strip() for p in (r["dim2"] or "").split("|")]
        return parts[i] if len(parts) > i else ""

    # Plain words for the books' own labels (site/content/plain_labels.yml); the printed
    # label is kept in `printed` so the page can show it underneath.
    labels = budget.content.get("plain_labels") or {}
    printed = {}

    def plain(kind, f):
        table = {k.lower(): v for k, v in (labels.get(kind) or {}).items()}

        def g(r):
            raw = f(r)
            new = table.get(raw.lower())
            if new and new != raw:
                printed[new] = raw
                return new
            return raw
        return g

    out = []
    # expense type
    if by.get("by_type"):
        out.append(("type", "By type of expense", "by_type", by["by_type"], lambda r: clean(r["dim1"])))
    elif by.get("by_program") and any("|" in r["dim2"] for r in by["by_program"]):
        out.append(("type", "By type of expense", "by_program", by["by_program"], lambda r: clean(split(r, 0))))
    elif by.get("by_program_type"):
        out.append(("type", "By type of expense", "by_program_type", by["by_program_type"], lambda r: clean(split(r, 1))))
    # funding source
    if by.get("by_fund"):
        out.append(("fund", "By source of money", "by_fund", by["by_fund"], lambda r: clean(r["dim1"])))
    elif by.get("by_program") and any("|" in r["dim2"] for r in by["by_program"]):
        out.append(("fund", "By source of money", "by_program", by["by_program"], lambda r: clean(split(r, 1))))
    # programs
    if by.get("by_program"):
        out.append(("program", "By program", "by_program", by["by_program"], lambda r: clean(r["dim1"])))
    elif by.get("by_program_type"):
        out.append(("program", "By program", "by_program_type", by["by_program_type"], lambda r: clean(r["dim1"])))

    result = []
    statement_all = budget.amount(dept_id, fy, stage, "all_funds")
    for kind, title, table, trs, keyf in out:
        if kind in ("type", "fund"):
            keyf = plain({"type": "expense_types", "fund": "funds"}[kind], keyf)
        grouped = _group(trs, keyf)
        parts = []
        for sub, items in grouped.items():
            its = [(k, v) for k, v in items.items() if round(v) != 0]
            if kind == "type":
                its.sort(key=lambda kv: (TYPE_ORDER.index(kv[0]) if kv[0] in TYPE_ORDER else 99, -kv[1]))
            else:
                its.sort(key=lambda kv: -kv[1])
            sub_pages = sorted({r["page"] for r in trs if (r["sub_unit"] or "") == sub}, key=lambda p: (len(p), p))
            parts.append({"sub": sub, "name": SUB_LABELS.get(sub, sub_names.get(sub, "")), "items": its, "total": sum(items.values()),
                          "cite": budget.cite(book, sub_pages[0] if sub_pages else "")})
        total = sum(p["total"] for p in parts)
        # Long program lists: show the largest, fold the rest into one row (full list in a details).
        n_items = sum(len(p["items"]) for p in parts)
        folded = False
        for p in parts:
            keep = PROGRAMS_SHOWN if len(parts) == 1 else 6
            if kind == "program" and len(p["items"]) > keep + 2:
                p["shown"] = p["items"][:keep] + [(None, sum(v for k, v in p["items"][keep:]))]
                folded = True
            else:
                p["shown"] = p["items"]
        pages = sorted({r["page"] for r in trs}, key=lambda p: (len(p), p))
        result.append({
            "kind": kind, "title": title, "table": table, "parts": parts, "total": total,
            "cite": budget.cite(book, pages[0] if pages else ""), "pages": pages,
            "many_parts": len([p for p in parts if p["items"]]) > 1,
            "folded": folded, "n_items": n_items,
        })
    return {
        "book": book, "year": fy, "basis": stage, "tables": result, "statement_all": statement_all, "printed": printed,
        "book_label": budget.book(book).label,
    }


def section_totals(budget, dept_id):
    """The adopted total printed in the department's own section for each year, for bodies
    that have no line (or only zeros) in the citywide tables. [(year, basis, total, cite)]

    A book's own-year figure is used first. Several books print the section with the new
    year's columns left at zero; when the next book prints a non-zero adopted figure for
    that same year, that figure is used instead. Years where every book prints zero are
    returned with total 0 so the page can say so."""
    rows = budget.detail.get(dept_id, [])
    books = sorted({r["book_id"] for r in rows})
    cands = {}  # year -> [(rank, row)]
    for book in books:
        by = int(book[:4])
        stage = book.rsplit("-", 1)[1]
        for fy, basis in sorted({(r["fiscal_year"], r["basis"]) for r in rows if r["book_id"] == book}):
            own = fy == by and basis == stage
            if not own and not (basis == "adopted" and fy == by - 1):
                continue
            rs = [r for r in rows if r["book_id"] == book and r["fiscal_year"] == fy and r["basis"] == basis]
            for table in ("by_type", "by_program", "by_program_type", "by_fund"):
                t = [r for r in rs if r["table"] == table]
                if t:
                    pages = sorted({r["page"] for r in t}, key=lambda p: (len(p), p))
                    rank = (0 if own and basis == "adopted" else 1 if basis == "adopted" else 2)
                    cands.setdefault(fy, []).append((rank, {
                        "year": fy, "basis": basis, "total": sum(r["amount"] for r in t),
                        "cite": budget.cite(book, pages[0])}))
                    break
    out = []
    for y in sorted(cands):
        cs = [c for _, c in sorted(cands[y], key=lambda x: x[0])]
        nz = [c for c in cs if c["total"]]
        out.append((nz or cs)[0])
    return out
