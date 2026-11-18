"""/about/ : what this is, where the data comes from, its limits, downloads."""
import os

from ..data import COMBINED

MAX_DOWNLOAD = 25 * 1024 * 1024


def downloads():
    out = []
    for name in sorted(os.listdir(COMBINED)):
        if not name.endswith(".csv"):
            continue
        size = os.path.getsize(os.path.join(COMBINED, name))
        if size <= MAX_DOWNLOAD:
            out.append({"name": name, "size": size, "href": "/data/%s" % name})
    return out


DESCRIPTIONS = {
    "spending.csv": "Spending by department, year, basis (proposed, adopted, revised, actual) and fund group, with the source book and page for every figure.",
    "spending_observations.csv": "Every printing of every spending figure, in every book, before one was chosen for each year.",
    "revenue.csv": "Income by source, year and basis, with source book and page.",
    "revenue_observations.csv": "Every printing of every income figure.",
    "positions_by_department.csv": "Budgeted positions (full-time equivalents) by department and year.",
    "positions.csv": "Budgeted positions at the finest level each book prints (job title, program, fund).",
    "department_detail.csv": "Each department's own tables: by type of expense, by source of money and by program.",
    "context.csv": "Orleans Parish population and consumer prices (U.S. and South region), by year.",
}


def build(site):
    b = site.budget
    used = sorted({c.book for c in b.S.values()})
    books = [b.book(x) for x in used]
    files = downloads()
    for f in files:
        f["text"] = DESCRIPTIONS.get(f["name"], "")
    site.downloads = files
    site.page("/about/", "about.html", "About the data", books=books, files=files,
              notes=site.notes, n_books=len(used), dataset=dataset(site.config, files))


def dataset(config, files):
    """schema.org Dataset describing the CSV downloads, for search engines' dataset
    listings. No figures: only what the files are, their years and where they live."""
    if not files:
        return None
    base = config["base_url"]
    author = {"@type": "Person", "name": config["author"], "url": config["author_url"]}
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "City of New Orleans budget data, %d to %d" % (config["first_year"], config["proposal_year"]),
        "description": ("The data behind NOLA Budget, as CSV files: City of New Orleans spending by "
                        "department, income by source and budgeted positions, by year, from the city's "
                        "budget books, plus the other figures the site uses (population, prices and "
                        "Census Bureau city comparisons). Each spending and income row names the book "
                        "and page it came from. "
                        "An independent project, not published by the City of New Orleans."),
        "url": base + "/about/#downloads",
        "creator": author,
        "isAccessibleForFree": True,
        "inLanguage": "en-US",
        "temporalCoverage": "%d/%d" % (config["first_year"], config["proposal_year"]),
        "spatialCoverage": {"@type": "Place", "name": "New Orleans, Louisiana"},
        "distribution": [dict({"@type": "DataDownload", "name": f["name"], "encodingFormat": "text/csv",
                               "contentUrl": base + f["href"]}, **({"description": f["text"]} if f["text"] else {}))
                         for f in files],
    }
