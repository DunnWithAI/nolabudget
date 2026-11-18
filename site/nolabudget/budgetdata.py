"""Helpers for the year pages and changes pages: positions, revenue columns, and the
list of original documents for each year (from the manifests in data/raw/)."""
import collections
import json
import os

from .data import RAW

REVENUE_BASES = ("proposed", "adopted", "actual")


# ---------------------------------------------------------------- positions
def positions_by_dept(b, y, basis):
    return {d: c.amount for (d, yy, bs), c in b.P.items() if yy == int(y) and bs == basis}


def positions_total(b, y, basis):
    v = positions_by_dept(b, y, basis)
    return sum(v.values()) if v else None


def positions_cite(b, y, basis):
    cells = [c for (d, yy, bs), c in b.P.items() if yy == int(y) and bs == basis]
    if not cells:
        return None
    book = collections.Counter(c.book for c in cells).most_common(1)[0][0]
    return b.cite(book, "")


# ------------------------------------------------------------------ revenue
def revenue_cite(b, y, basis):
    rows = b.revenue_rows(y, basis)
    if not rows:
        return None
    book = collections.Counter(r["source_book"] for r in rows).most_common(1)[0][0]
    pages = collections.Counter(r["source_page"].split(";")[0] for r in rows if r["source_book"] == book and r["source_page"])
    return b.cite(book, pages.most_common(1)[0][0] if pages else "")


# ---------------------------------------------------------------- documents
KIND_LABEL = {
    "operating-adopted": "Adopted budget book (operating)",
    "operating-proposed": "Proposed budget book (operating)",
    "capital-adopted": "Adopted capital budget",
    "capital-proposed": "Proposed capital budget",
    "presentation": "Presentation to the City Council",
    "speech": "Mayor's remarks on the budget",
    "other": "Other budget document",
}
KIND_ORDER = list(KIND_LABEL)
PRE2011_KIND = {
    "adopted_operating_budget": "operating-adopted",
    "proposed_capital_budget": "capital-proposed",
    "budget_presentation": "presentation",
    "departmental_budget_presentation": "presentation",
    "press_release": "speech",
}
PRE2011_LABEL = {  # kinds that need a plainer label than the generic one
    "departmental_budget_presentation": "Department's budget hearing presentation",
    "budget_presentation": "Budget presentation",
    "press_release": "Mayor's statement on the budget",
    "budget_supporting": "Supporting budget paper",
    "budget_summary": "Budget handout",
    "revenue_estimate": "Revenue Estimating Conference report",
    "secondary_bgr_report": "Independent review by the Bureau of Governmental Research",
    "mayors_annual_report": "Mayor's annual report",
    "capital_bond_program": "Bond program overview",
    "capital_budget_supporting": "Capital budget spreadsheet",
}


def host_label(url):
    if "web.archive.org" in url:
        return "Internet Archive copy of the city's old website"
    if "nola.gov" in url:
        return "City of New Orleans website"
    if "questica" in url or "opendatastorage" in url:
        return "the city's online budget books (OpenBook)"
    if "bgr.org" in url:
        return "Bureau of Governmental Research website"
    if "nolalibrary" in url:
        return "New Orleans Public Library city archives"
    return "the publisher's website"


def clean_url(u):
    u = u.strip().split(" (")[0].split()[0]
    return u.replace(" ", "%20")


def load_documents():
    """{year: [doc]} where doc = {kind, label, title, url, host}. Absolute URLs only."""
    docs = collections.defaultdict(list)
    seen = set()

    def add(year, kind, label, title, url):
        url = clean_url(url)
        if not url.startswith(("http://", "https://")) or (year, url) in seen:
            return
        seen.add((year, url))
        docs[int(year)].append({"kind": kind, "label": label, "title": title, "url": url, "host": host_label(url)})

    for it in json.load(open(os.path.join(RAW, "nola-gov", "manifest.json"))):
        if str(it.get("ok")) != "True":
            continue
        k = it["kind"] if it["kind"] in KIND_LABEL else "other"
        add(it["year"], k, KIND_LABEL[k], it["title"], it["url"])
    q = json.load(open(os.path.join(RAW, "questica", "manifest.json")))
    for it in q["files"]:
        d = it.get("description") or ""
        if not (it.get("url") and d.startswith("Full PDF budget book")):
            continue
        k = "%s-%s" % (it["type"], it["stage"])
        if k not in KIND_LABEL:
            continue
        add(it["year"], k, KIND_LABEL[k], d.split(":", 1)[-1].strip(), it["url"])
    for it in json.load(open(os.path.join(RAW, "pre-2011", "manifest.json"))):
        y = it.get("fiscal_year")
        if y is None or y < 2004 or y > 2010:
            continue
        t = it["doc_type"]
        k = PRE2011_KIND.get(t, "other")
        label = PRE2011_LABEL.get(t) or KIND_LABEL[k]
        if t == "adopted_operating_budget":
            label = KIND_LABEL[k]
        add(y, k, label, it["title"], it["url"])
    for y in docs:
        docs[y].sort(key=lambda d: (KIND_ORDER.index(d["kind"]), d["title"]))
    return docs
