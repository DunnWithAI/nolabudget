"""Correctness gate, run at the end of build.py. Any error fails the build.

1. Headline totals published by pages (site.publish) equal sums computed directly from
   data/combined/spending.csv, independently of nolabudget/data.py.
2. The home page's lead sentence prints those totals.
3. Every department page's latest figures equal spending.csv.
4. Every internal link (href/src) in dist/ resolves to a file, including #fragments.
5. No page contains "nan", "None", "undefined" or an unrendered template tag.
6. Every department in the crosswalk has a description.
7. Search basics (seo_checks): every page has one h1, a unique title, a description, a
   canonical URL and parseable JSON-LD that matches the page; sitemap.xml lists every
   page; robots.txt, _headers and the robots meta tag agree with `noindex`.
"""
import collections
import csv
import html as htmllib
import json
import os
import re
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPENDING = os.path.join(ROOT, "data", "combined", "spending.csv")


def raw_spending():
    """{(dept, year, basis, fund): amount} straight from the CSV, applying only the one
    documented substitution (2020 adopted all funds = GF + other funds)."""
    S = {}
    with open(SPENDING, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            S[(r["dept_id"], int(r["fiscal_year"]), r["basis"], r["fund_group"])] = float(r["amount"])
    depts = {k[0] for k in S}
    for d in depts:
        g = S.get((d, 2020, "adopted", "general_fund"))
        o = S.get((d, 2020, "adopted", "other_funds"))
        S.pop((d, 2020, "adopted", "all_funds"), None)
        if g is not None or o is not None:
            S[(d, 2020, "adopted", "all_funds")] = (g or 0) + (o or 0)
    return S


class Links(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        for k in ("href", "src"):
            if k in a and a[k]:
                self.links.append(a[k])


BAD = [
    (re.compile(r"\bnan\b", re.I), "nan"),
    (re.compile(r"\bNone\b"), "None"),
    (re.compile(r"\bundefined\b"), "undefined"),
    (re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}"), "template tag"),
]


def visible_text(html):
    html = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style\b.*?</style>", " ", html, flags=re.S)
    return html


def close(a, b, tol=0.5):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def money(x):
    return "${:,.1f} million".format(x / 1e6)


def run(site, dist):
    errors = []
    S = raw_spending()
    T = collections.defaultdict(float)
    for (d, y, b, f), v in S.items():
        T[(y, b, f)] += v
    py, fy = site.config["proposal_year"], site.config["in_force_year"]

    # 1-2. home page headline totals
    expect = {
        "gf_new": T[(py, "proposed", "general_fund")], "all_new": T[(py, "proposed", "all_funds")],
        "gf_now": T[(fy, "adopted", "general_fund")], "all_now": T[(fy, "adopted", "all_funds")],
    }
    got = site.facts.get("/", {})
    for k, v in expect.items():
        if not close(got.get(k), v):
            errors.append("home: %s is %r, spending.csv sums to %r" % (k, got.get(k), v))
    home = open(os.path.join(dist, "index.html"), encoding="utf-8").read()
    lead = re.search(r'<p class="lead">(.*?)</p>', home, re.S)
    lead = re.sub(r"<[^>]+>", "", lead.group(1)) if lead else ""
    for k in ("gf_new", "gf_now"):
        if money(expect[k]) not in lead:
            errors.append("home: lead sentence does not print %s (%s)" % (k, money(expect[k])))
    for k in ("all_new", "all_now"):
        if money(expect[k]) not in home:
            errors.append("home: page does not print %s (%s)" % (k, money(expect[k])))

    # 3. department pages
    for d in site.budget.depts:
        url = "/departments/%s/" % d
        got = site.facts.get(url)
        if got is None:
            errors.append("%s: page not built" % url)
            continue
        for k, key in (("gf_new", (d, py, "proposed", "general_fund")), ("gf_now", (d, fy, "adopted", "general_fund")),
                       ("all_new", (d, py, "proposed", "all_funds")), ("all_now", (d, fy, "adopted", "all_funds"))):
            if not close(got.get(k), S.get(key)):
                errors.append("%s: %s is %r, spending.csv has %r" % (url, k, got.get(k), S.get(key)))
        if not site.budget.depts[d].get("about") or not site.budget.depts[d].get("short"):
            errors.append("%s: no description in site/content/departments.yml" % d)
        html = open(os.path.join(dist, "departments", d, "index.html"), encoding="utf-8").read()
        v = S.get((d, py, "proposed", "general_fund"))
        if v:
            full = "{:,.0f}".format(round(v))
            if full not in html:
                errors.append("%s: %s proposed General Fund (%s) not printed" % (url, py, full))

    # 4-5. links and bad strings
    files = {}
    for dirpath, dirnames, filenames in os.walk(dist):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            files["/" + os.path.relpath(p, dist).replace(os.sep, "/")] = p
    ids = {}
    parsed = {}
    for url, p in files.items():
        if not url.endswith(".html"):
            continue
        html = open(p, encoding="utf-8").read()
        lp = Links()
        lp.feed(html)
        ids[url] = lp.ids
        parsed[url] = lp.links
        text = visible_text(html)
        for rx, name in BAD:
            m = rx.search(text)
            if m:
                ctx = text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ")
                errors.append("%s: contains %s: ...%s..." % (url, name, ctx))
    for url, links in parsed.items():
        for href in links:
            if re.match(r"^(https?:|mailto:|data:)", href):
                continue
            path, _, frag = href.partition("#")
            path = path.partition("?")[0]   # asset fingerprints (?v=...) are not part of the file
            if not path:
                target = url
            else:
                if not path.startswith("/"):
                    base = url.rsplit("/", 1)[0] + "/"
                    path = os.path.normpath(base + path).replace(os.sep, "/")
                target = path + "index.html" if path.endswith("/") else path
            if target not in files:
                errors.append("%s: broken link %s" % (url, href))
                continue
            if frag and target.endswith(".html") and frag not in ids.get(target, set()):
                errors.append("%s: link %s points to a missing #%s" % (url, href, frag))
    errors.extend(revenue_checks(site, dist))
    errors.extend(budget_checks(site, dist))
    errors.extend(not_explained_checks(site, dist))
    errors.extend(program_checks(site))
    errors.extend(compare_checks(site, dist))
    errors.extend(seo_checks(site, dist))
    errors.extend(source_checks(dist))
    return errors


# ---------------------------------------------------------------------------- search
def plain(html):
    """Text of an HTML fragment with tags dropped and whitespace collapsed."""
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def seo_checks(site, dist):
    errors = []
    cfg = site.config
    base = cfg["base_url"].rstrip("/")
    noindex = bool(cfg.get("noindex"))
    titles = collections.defaultdict(list)
    for url, _ in site.pages:
        path = os.path.join(dist, url.strip("/"), "index.html")
        html = open(path, encoding="utf-8").read()
        head, _, body = html.partition("</head>")
        if not re.search(r'<html lang="en">', html):
            errors.append("%s: <html> has no lang attribute" % url)
        if len(re.findall(r"<h1[\s>]", body)) != 1:
            errors.append("%s: should have exactly one <h1>" % url)
        levels = [int(x) for x in re.findall(r"<h([1-6])[\s>]", body)]
        for a, b in zip(levels, levels[1:]):
            if b > a + 1:
                errors.append("%s: heading jumps from h%d to h%d" % (url, a, b))
                break
        m = re.search(r"<title>(.*?)</title>", head)
        if m:
            titles[m.group(1)].append(url)
        else:
            errors.append("%s: no <title>" % url)
        m = re.search(r'<meta name="description" content="([^"]*)">', head)
        if not m or len(m.group(1).strip()) < 50:
            errors.append("%s: missing or very short meta description" % url)
        for pattern in (r'<link rel="canonical" href="([^"]*)">', r'<meta property="og:url" content="([^"]*)">'):
            m = re.search(pattern, head)
            if not m or m.group(1) != base + url:
                errors.append("%s: %s is %r, expected %r" % (url, pattern.split('"')[1], m and m.group(1), base + url))
        has_noindex = '<meta name="robots" content="noindex' in head
        if has_noindex != noindex:
            errors.append("%s: robots noindex tag is %s but site.yml noindex is %s" % (url, has_noindex, noindex))
        for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
            try:
                data = json.loads(raw)
            except ValueError as e:
                errors.append("%s: JSON-LD does not parse: %s" % (url, e))
                continue
            kind = data.get("@type")
            if kind == "BreadcrumbList":
                nav = re.search(r'<nav class="crumbs".*?</nav>', body, re.S)
                shown = re.findall(r'<li>(?:<a href="([^"]*)">|<span aria-current="page">)(.*?)</(?:a|span)></li>', nav.group(0)) if nav else []
                want = [(base + (h or url), plain(t)) for h, t in shown]
                got = [(i["item"], i["name"]) for i in data["itemListElement"]]
                if got != want:
                    errors.append("%s: breadcrumb JSON-LD %r does not match the visible trail %r" % (url, got, want))
            elif kind == "FAQPage":
                squash = lambda t: re.sub(r"\s+", "", t)   # tags leave spaces before punctuation
                text = squash(plain(visible_text(body)))
                for q in data["mainEntity"]:
                    if squash(q["name"]) not in text or squash(q["acceptedAnswer"]["text"]) not in text:
                        errors.append("%s: FAQ JSON-LD question %r is not printed on the page word for word" % (url, q["name"]))
    for t, urls in titles.items():
        if len(urls) > 1:
            errors.append("pages share the title %r: %s" % (t, ", ".join(urls)))
    # sitemap.xml, robots.txt, _headers, llms.txt, 404
    read = lambda name: open(os.path.join(dist, name), encoding="utf-8").read() if os.path.exists(os.path.join(dist, name)) else None
    sitemap = read("sitemap.xml") or ""
    listed = set(re.findall(r"<loc>(.*?)</loc>", sitemap))
    pages = {base + u for u, _ in site.pages}
    if listed != pages:
        errors.append("sitemap.xml: missing %s; extra %s" % (sorted(pages - listed)[:5], sorted(listed - pages)[:5]))
    robots = read("robots.txt") or ""
    groups = re.split(r"\n\s*\n", robots)   # blocking named crawlers is fine; blocking every crawler is not
    if any(re.search(r"(?m)^User-agent: \*\s*$", g) and re.search(r"(?m)^Disallow: /\s*$", g) for g in groups):
        errors.append("robots.txt blocks the whole site, which hides the noindex tags from search engines")
    if ("Sitemap:" in robots) == noindex:
        errors.append("robots.txt %s a Sitemap line while noindex is %s" % ("has" if noindex else "lacks", noindex))
    headers = read("_headers")
    if noindex and (not headers or "X-Robots-Tag: noindex" not in headers):
        errors.append("_headers: noindex is on but responses do not send X-Robots-Tag: noindex")
    if not noindex and headers and "noindex" in headers:
        errors.append("_headers: sends noindex while site.yml noindex is false")
    llms = read("llms.txt")
    if not llms:
        errors.append("llms.txt not written")
    else:
        for href in re.findall(r"\]\((%s[^)]*)\)" % re.escape(base), llms):
            path = href[len(base):]
            target = path + "index.html" if path.endswith("/") else path
            if not os.path.exists(os.path.join(dist, target.lstrip("/"))):
                errors.append("llms.txt: broken link %s" % href)
    nf = read("404.html") or ""
    if 'rel="canonical"' in nf or '<meta name="robots" content="noindex' not in nf:
        errors.append("404.html: should carry noindex and no canonical URL")
    return errors


# ---------------------------------------------------------------------------- revenue
def revenue_category_gaps(rows):
    """{(year, basis, category): gap} for General Fund columns whose one source book prints a
    category total that differs from its lines (data/combined/revenue_category_totals.csv).
    The site counts the gap in that category, so its totals match what the book prints."""
    src = collections.defaultdict(set)
    for r in rows:
        if r["fund_group"] == "general_fund":
            src[(int(r["fiscal_year"]), r["basis"])].add(r["source_book"])
    gaps = collections.defaultdict(float)
    with open(os.path.join(ROOT, "data", "combined", "revenue_category_totals.csv"), newline="", encoding="utf-8") as f:
        printed = list(csv.DictReader(f))
    for (y, basis), books in src.items():
        if len(books) != 1:
            continue
        book = next(iter(books))
        for g in printed:
            if (g["book_id"] == book and int(g["fiscal_year"]) == y and g["place"]
                    and g["basis"] == ("adopted" if basis == "revised" else basis)):
                gaps[(y, basis, g["place"])] += float(g["gap"])
    return gaps


def revenue_checks(site, dist):
    """7. Revenue pages: the index and category totals equal sums from revenue.csv, every
    General Fund line with data has a page that prints its figures, one-off events quote
    amounts that are in the data, and every rollup has its plain-words explanation."""
    errors = []
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    rows = []
    with open(os.path.join(ROOT, "data", "combined", "revenue.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["amount"] = float(r["amount"])
            r["fiscal_year"] = int(r["fiscal_year"])
            rows.append(r)

    gaps = revenue_category_gaps(rows)

    def total(year, basis, fund="general_fund", **where):
        vals = [r["amount"] for r in rows if r["fiscal_year"] == year and r["basis"] == basis and r["fund_group"] == fund
                and all(r[k] == v for k, v in where.items())]
        if not vals:
            return None
        if fund == "general_fund" and "rev_id" not in where:
            vals += [g for (y, bs, c), g in gaps.items() if y == year and bs == basis and where.get("category", c) == c]
        return sum(vals)

    def read(url):
        p = os.path.join(dist, url.strip("/"), "index.html")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else None

    def full(v):
        return "{:,.0f}".format(round(v))

    cats = site.content["revenue_categories"]
    slugs = {c["slug"] for c in cats}
    ids = {r["rev_id"] for r in rows if r["fund_group"] == "general_fund"}
    for c in {r["category"] for r in rows if r["fund_group"] == "general_fund"}:
        if c not in {x["category"] for x in cats}:
            errors.append("revenue: category %r has no slug in content/revenue_categories.yml" % c)
    for i in sorted(ids & slugs):
        errors.append("revenue: rev_id %r is also a category slug, so two pages would share a URL" % i)

    # index
    got = site.facts.get("/revenue/", {})
    want = {"gf_new": total(py, "proposed"), "gf_now": total(fy, "adopted"),
            "of_new": total(py, "proposed", "other_funds"), "of_now": total(fy, "adopted", "other_funds")}
    for c in cats:
        want["new:" + c["slug"]] = total(py, "proposed", category=c["category"])
        want["now:" + c["slug"]] = total(fy, "adopted", category=c["category"])
    for k, v in want.items():
        if not close(got.get(k), v):
            errors.append("revenue index: %s is %r, revenue.csv sums to %r" % (k, got.get(k), v))
    html = read("/revenue/") or ""
    for k, v in want.items():
        if v is not None and full(v) not in html:
            errors.append("revenue index: %s (%s) not printed" % (k, full(v)))
    # the category amounts add up to the total
    if want["gf_new"] is not None and not close(sum(v for k, v in want.items() if k.startswith("new:") and v), want["gf_new"]):
        errors.append("revenue index: category amounts do not add up to the %d total" % py)

    # categories
    for c in cats:
        url = "/revenue/%s/" % c["slug"]
        got = site.facts.get(url)
        if got is None:
            errors.append("%s: page not built" % url)
            continue
        for k, v in (("new", want["new:" + c["slug"]]), ("now", want["now:" + c["slug"]])):
            if not close(got.get(k), v):
                errors.append("%s: %s is %r, revenue.csv sums to %r" % (url, k, got.get(k), v))
            if v is not None and full(v) not in (read(url) or ""):
                errors.append("%s: %s (%s) not printed" % (url, k, full(v)))

    # every General Fund line with data has a page that prints its current figures
    for i in sorted(ids):
        url = "/revenue/%s/" % i
        got = site.facts.get(url)
        html = read(url)
        if got is None or html is None:
            errors.append("%s: no page for revenue line %s" % (url, i))
            continue
        for k, year, basis in (("new", py, "proposed"), ("now", fy, "adopted")):
            v = total(year, basis, rev_id=i)
            if not close(got.get(k), v):
                errors.append("%s: %s is %r, revenue.csv has %r" % (url, k, got.get(k), v))
            if v is not None and full(v) not in html:
                errors.append("%s: %s (%s) not printed" % (url, k, full(v)))
        # the line's latest year-by-year figure is in its table
        mine = [r for r in rows if r["rev_id"] == i and r["fund_group"] == "general_fund"]
        last = max(mine, key=lambda r: (r["fiscal_year"], r["basis"]))
        if full(last["amount"]) not in html:
            errors.append("%s: %s %s figure (%s) not printed" % (url, last["fiscal_year"], last["basis"], full(last["amount"])))

    # one-off events quote figures that are in the data
    ev = site.content["revenue_events"]
    for e in ev["events"]:
        v = e.get("verify")
        if v:
            have = total(v["year"], v["basis"], rev_id=v["rev_id"])
            if have is None or abs(have - v["amount"]) > v.get("tol", 1):
                errors.append("revenue event %s: revenue.csv has %r for %s %s %s, text says %r" % (
                    e["id"], have, v["rev_id"], v["year"], v["basis"], v["amount"]))
    # every rollup is explained in plain words
    groups = collections.defaultdict(set)
    for r in rows:
        if r["fund_group"] == "general_fund":
            groups[r["comparable_id"]].add(r["rev_id"])
    for comp, members in groups.items():
        if len(members) > 1 and comp not in ev["rollups"]:
            errors.append("revenue: comparable series %s combines %s but has no text in content/revenue_events.yml" % (
                comp, sorted(members)))
    return errors


# ------------------------------------------------------------------ year and changes pages
def budget_checks(site, dist):
    """8. Year pages: every published total, and every department figure, equals the sum
    or value in spending.csv, and the page prints it; every document link is an absolute
    http(s) URL. 9. Changes pages: every department's old, new and change equal the two
    columns in spending.csv (and likewise for income by category and for positions)."""
    errors = []
    S = raw_spending()
    T = collections.defaultdict(float)
    have = set()
    for (d, y, b, f), v in S.items():
        T[(y, b, f)] += v
        have.add((y, b, f))
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    years = sorted({k[0] for k in have})

    def read(url):
        p = os.path.join(dist, url.strip("/"), "index.html")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else None

    def full(v):
        return "{:,.0f}".format(round(v))

    def signed(v):
        return ("+" if v > 0 else "\u2212") + "{:,.0f}".format(abs(round(v)))

    shown = ("proposed", "adopted", "actual")
    funds = ("general_fund", "all_funds")

    # year pages
    for y in years:
        url = "/budgets/%d/" % y
        got, html = site.facts.get(url), read(url)
        if got is None or html is None:
            errors.append("%s: page not built" % url)
            continue
        for b in shown:
            for f in funds:
                want = T[(y, b, f)] if (y, b, f) in have else None
                if not close(got.get("%s_%s" % (b, f)), want):
                    errors.append("%s: %s %s total is %r, spending.csv sums to %r" % (url, b, f, got.get("%s_%s" % (b, f)), want))
                if want is not None and full(want) not in html:
                    errors.append("%s: %s %s total (%s) not printed" % (url, b, f, full(want)))
        dv = got.get("dept_vals", {})
        sums = collections.defaultdict(float)
        for k, v in dv.items():
            d, b, f = k.split("|")
            sums[(b, f)] += v
            if not close(v, S.get((d, y, b, f))):
                errors.append("%s: %s is %r, spending.csv has %r" % (url, k, v, S.get((d, y, b, f))))
        for (d, yy, b, f), v in S.items():
            if yy == y and b in shown and f in funds and "%s|%s|%s" % (d, b, f) not in dv:
                errors.append("%s: %s %s %s (%s) is in spending.csv but not on the page" % (url, d, b, f, full(v)))
        for (b, f), v in sums.items():
            if not close(v, T[(y, b, f)], 1):
                errors.append("%s: department rows for %s %s add to %r, total is %r" % (url, b, f, v, T[(y, b, f)]))
        # every document link is an absolute http(s) URL
        for href in re.findall(r'<a class="doc-link" href="([^"]*)"', html):
            if not re.match(r"^https?://[^/\s]+", href):
                errors.append("%s: document link is not an absolute http(s) URL: %s" % (url, href))
        if len(re.findall(r'class="doc-link"', html)) != len(got.get("docs", [])):
            errors.append("%s: document links on the page do not match the documents listed" % url)
        for u in got.get("docs", []):
            if not re.match(r"^https?://[^/\s]+", u) or " " in u:
                errors.append("%s: document URL is not an absolute http(s) URL: %r" % (url, u))
    # the index prints each year's headline General Fund figures
    idx = read("/budgets/") or ""
    for y in years:
        for b in ("adopted", "actual"):
            if (y, b, "general_fund") in have and "{:,.1f}".format(T[(y, b, "general_fund")] / 1e6) not in idx:
                errors.append("/budgets/: %d %s General Fund not printed" % (y, b))

    # revenue and positions straight from their CSVs
    rev = collections.defaultdict(float)
    with open(os.path.join(ROOT, "data", "combined", "revenue.csv"), newline="", encoding="utf-8") as f:
        rev_rows = list(csv.DictReader(f))
    for r in rev_rows:
        if r["fund_group"] == "general_fund":
            rev[(int(r["fiscal_year"]), r["basis"], r["category"])] += float(r["amount"])
    for k, g in revenue_category_gaps(rev_rows).items():
        rev[k] += g
    pos = {}
    with open(os.path.join(ROOT, "data", "combined", "positions_by_department.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pos[(r["dept_id"], int(r["fiscal_year"]), r["basis"])] = float(r["fte"])

    # changes pages: the proposal against the budget in force, and each earlier year's
    # adopted budget against the year before's
    want_pages = {"/changes/": ((py, "proposed"), (fy, "adopted"))}
    for y in years:
        if y != py and (y, "adopted", "general_fund") in have and (y - 1, "adopted", "general_fund") in have:
            want_pages["/changes/%d/" % y] = ((y, "adopted"), (y - 1, "adopted"))
    for url, (new, old) in want_pages.items():
        got, html = site.facts.get(url), read(url)
        if got is None or html is None:
            errors.append("%s: page not built" % url)
            continue
        if tuple(got["new_key"]) != new or tuple(got["old_key"]) != old:
            errors.append("%s: compares %r with %r, expected %r with %r" % (url, got["new_key"], got["old_key"], new, old))
            continue
        for f in funds:
            g = got["funds"][f]
            for tag, key in (("total_new", new), ("total_old", old)):
                want = T[key + (f,)] if key + (f,) in have else None
                if not close(g[tag], want):
                    errors.append("%s: %s %s is %r, spending.csv sums to %r" % (url, f, tag, g[tag], want))
            rows = g["rows"]
            for d in {k[0] for k in S}:
                a, b = S.get((d, new[0], new[1], f)), S.get((d, old[0], old[1], f))
                if (a or b) and d not in rows:
                    errors.append("%s: %s %s is in spending.csv but not on the page" % (url, d, f))
            for d, (o, n, diff) in rows.items():
                a, b = S.get((d, new[0], new[1], f)), S.get((d, old[0], old[1], f))
                if not close(n, a) or not close(o, b):
                    errors.append("%s: %s %s has old %r new %r, spending.csv has %r and %r" % (url, d, f, o, n, b, a))
                    continue
                if a is not None and b is not None:
                    if not close(diff, a - b):
                        errors.append("%s: %s %s change is %r, the two figures differ by %r" % (url, d, f, diff, a - b))
                    if abs(a - b) >= 0.5 and round(a - b) != 0 and signed(a - b) not in html:
                        errors.append("%s: %s %s change %s not printed" % (url, d, f, signed(a - b)))
                    if a - b != 0 and (full(a) not in html or full(b) not in html):
                        errors.append("%s: %s %s figures not printed" % (url, d, f))
                elif diff is not None:
                    errors.append("%s: %s %s has a change but only one figure" % (url, d, f))
        # income by category
        r = got["rev"]
        cats = {k[2] for k in rev}
        for c, (o, n) in r["rows"].items():
            wo, wn = rev.get((old[0], old[1], c)), rev.get((new[0], new[1], c))
            if not close(o, wo) or not close(n, wn):
                errors.append("%s: income %s has %r and %r, revenue.csv has %r and %r" % (url, c, o, n, wo, wn))
        for tag, key in (("new", new), ("old", old)):
            want = sum(v for k, v in rev.items() if k[:2] == key)
            if not close(r[tag], want):
                errors.append("%s: income total %s is %r, revenue.csv sums to %r" % (url, tag, r[tag], want))
        # positions
        p = got["pos"]
        for d, (o, n) in p["rows"].items():
            wo, wn = pos.get((d, p["old_key"][0], p["old_key"][1])), pos.get((d, p["new_key"][0], p["new_key"][1]))
            if not close(o, wo, 0.001) or not close(n, wn, 0.001):
                errors.append("%s: positions %s are %r and %r, positions_by_department.csv has %r and %r" % (url, d, o, n, wo, wn))
        for tag, key in (("total_new", p["new_key"]), ("total_old", p["old_key"])):
            vals = [v for (d, y, b), v in pos.items() if (y, b) == tuple(key)]
            if p[tag] is not None and not close(p[tag], sum(vals), 0.01):
                errors.append("%s: positions %s is %r, csv sums to %r" % (url, tag, p[tag], sum(vals)))
    return errors


# ------------------------------------------------------------------ not explained
def not_explained_checks(site, dist):
    """8. /not-explained/: every dollar figure and every number with a thousands comma on the
    page was produced by code (nolabudget/pages/not_explained.py records each one it formats),
    none is typed into site/content/not_explained.yml, and four headline figures recomputed
    here straight from the extracted CSVs are on the page."""
    import html as htmllib
    errors = []
    url = "/not-explained/"
    facts = site.facts.get(url)
    path = os.path.join(dist, "not-explained", "index.html")
    if facts is None or not os.path.exists(path):
        return ["%s: page not built" % url]
    used = facts["figures"]
    with open(path, encoding="utf-8") as f:
        page = f.read()
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", visible_text(page)))
    money_rx = r"\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?: million)?"
    plain_rx = r"(?<![\w.,$])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)"
    for tok in sorted(set(re.findall(money_rx + "|" + plain_rx, text))):
        if tok not in used:
            errors.append("%s: figure %s was not produced by code" % (url, tok))
    if len(used) < 50:
        errors.append("%s: only %d computed figures on the page" % (url, len(used)))
    # no figures typed into the hand-written sentences
    with open(os.path.join(ROOT, "site", "content", "not_explained.yml"), encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.lstrip().startswith("#"):
                continue
            if "$" in line or re.search(r"\d,\d{3}", line):
                errors.append("site/content/not_explained.yml:%d: a figure is typed into the text: %s" % (n, line.strip()[:70]))

    def rows(book, name):
        with open(os.path.join(ROOT, "data", "extracted", book, name + ".csv"), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    want = []
    # 1. 2025 adopted book: General Fund + other funds against all funds, 2025 adopted column
    tot = {r["scope"].split("/")[1]: float(r["amount"]) for r in rows("2025-operating-adopted", "printed_totals")
           if r["scope"].startswith("department_expenditures/") and r["fiscal_year"] == "2025" and r["basis"] == "adopted"}
    gap = tot["all_funds"] - tot["general_fund"] - tot["other_funds"]
    want.append(("2025 adopted all-funds gap", money(abs(gap)).replace(" million", "") + " million"))
    # 2. the 2026 adopted General Fund transfer from other funds
    tr = [float(r["amount"]) for r in rows("2026-operating-adopted", "revenues")
          if r["line_name"] == "Transfer From Other Funds" and r["fund_group"] == "general_fund"
          and r["fiscal_year"] == "2026" and r["basis"] == "adopted"]
    if tr:
        want.append(("2026 transfer from other funds", "{:,.0f}".format(tr[0])))
    # 3. other funds, adopted and actual, 2024, summed from spending.csv
    sums = collections.defaultdict(float)
    with open(SPENDING, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["fiscal_year"] == "2024" and r["fund_group"] == "other_funds" and r["basis"] in ("adopted", "actual"):
                sums[r["basis"]] += float(r["amount"])
    for basis, v in sums.items():
        want.append(("2024 other funds, %s" % basis, "{:,.0f}".format(v)))
    # 4. the 2023 book's printed all-funds total for its proposed column
    pt = [float(r["amount"]) for r in rows("2023-operating-adopted", "printed_totals")
          if r["scope"] == "department_expenditures/all_funds" and r["fiscal_year"] == "2023" and r["basis"] == "proposed"]
    if pt:
        want.append(("2023 printed all-funds total", "{:,.0f}".format(pt[0])))
    for label, s in want:
        if s not in text:
            errors.append("%s: %s (%s) is not on the page" % (url, label, s))
    return errors



def program_checks(site):
    """Inside-the-department figures: for every department mapped in program_groups.csv, the
    proposal year's programs add up to its all-funds figure, and every program with money
    in that year is mapped (not left in "Other programs" by accident). For every department
    in position_ranks.yml, ranks add up to its total positions in every year shown."""
    from nolabudget import programs
    b = site.budget
    py, fy = site.config["proposal_year"], site.config["in_force_year"]
    years = list(range(site.config["first_year"], py + 1))
    errors = []
    for dept in programs.mapped_depts():
        ins = programs.inside(b, dept, years, py, fy)
        if ins is None:
            errors.append("programs: %s has a crosswalk but no %d program pages" % (dept, py))
            continue
        t = b.amount(dept, py, "proposed", "all_funds")
        if t is None or abs(ins["total"] - t) > 1000:
            errors.append("programs: %s %d programs add up to %r, all funds is %r" % (dept, py, ins["total"], t))
        cw = programs.mapped(dept)
        for g in ins["groups"]:
            for p in g["programs"]:
                if g["name"] == programs.OTHER and p["code"] not in cw and abs(p["amount"]) >= 100000:
                    errors.append("programs: %s %d program %r (%s) is not in program_groups.csv" % (dept, py, p["printed"], p["code"]))
    for dept, rules in (site.content.get("position_ranks") or {}).items():
        for y, v in programs.ranks(b, dept, rules, years, py).items():
            basis = "proposed" if y == py else "adopted"
            want = b.positions(dept, y, basis)
            if want is not None and abs(v["total"] - want) > 0.5:
                errors.append("ranks: %s %d ranks add up to %.2f, positions total is %.2f" % (dept, y, v["total"], want))
    return errors


def compare_checks(site, dist):
    """/compare/: New Orleans's figure per resident for every service, recomputed straight from
    city_comparison.csv, match what the page published and appear on the page."""
    errors = []
    path = os.path.join(ROOT, "data", "combined", "city_comparison.csv")
    with open(path, newline="", encoding="utf-8") as f:
        nola = [r for r in csv.DictReader(f) if r["census_id"] == "222071166094"]
    if len(nola) != 1:
        return ["compare: New Orleans is not in city_comparison.csv exactly once"]
    got = site.facts.get("/compare/", {})
    page = open(os.path.join(dist, "compare", "index.html"), encoding="utf-8").read()
    from nolabudget import compare
    for k in [f["key"] for f in compare.FUNCTIONS]:
        want = float(nola[0][k]) / int(nola[0]["population"])
        if not close(got.get(k + "_per"), want, 0.01):
            errors.append("compare: %s per resident is %r, the CSV gives %r" % (k, got.get(k + "_per"), want))
        shown = "$%s" % "{:,.0f}".format(want)
        if shown not in page:
            errors.append("compare: page does not print New Orleans %s per resident (%s)" % (k, shown))
    return errors


def source_checks(dist):
    """Every part of a page that shows a dollar figure also shows where it comes from: a
    "Source:" or "Calculated from:" line, a citation marker, or (class="source") a pointer to
    the table that cites each figure. Sections with no budget figures in them (a glossary
    example such as "$1 of tax") opt out with class="src-exempt".

    Citation markers ("[3]", class="cite") link to the page's "Notes and sources" list
    (class="pagerefs"). That list is taken out before sections are checked, so its own
    entries never count as a source for the section above it, and every marker must point
    to an entry in it that names a source."""
    errors = []
    for root, _, files in os.walk(dist):
        for fn in files:
            if not fn.endswith(".html"):
                continue
            path = os.path.join(root, fn)
            url = "/" + os.path.relpath(path, dist).replace(os.sep, "/").replace("index.html", "")
            html = open(path, encoding="utf-8").read()
            main = html.split("<main", 1)[-1].split("</main>")[0]
            m = re.search(r'<section class="pagerefs".*?</section>', main, re.S)
            listed = m.group(0) if m else ""
            main = main.replace(listed, "")
            # Notes printed in place under their chart (m.footnotes) are entries too.
            entries = dict(re.findall(r'<li id="(src-\d+)"[^>]*>(.*?)</li>', listed + main, re.S))
            for target, label in re.findall(r'<a class="(?:cite|fn)" href="#(src-\d+)"[^>]*>([^<]*)</a>', main):
                if target not in entries:
                    errors.append("%s: marker %s points to no entry in Notes and sources" % (url, label))
            for target in re.findall(r'<a class="cite" href="#(src-\d+)"', main):
                if not re.sub(r"<[^>]+>|↑", "", entries.get(target, "")).strip():
                    errors.append("%s: source %s has no text in Notes and sources" % (url, target))
            # Footnote sections (class="notes") belong to the table above them, so they are not split off.
            for chunk in re.split(r'(?=<section class="block|<header class="lede")', main):
                if "src-exempt" in chunk.split(">", 1)[0]:
                    continue
                text = re.sub(r"<[^>]+>", " ", chunk)
                if re.search(r"\$\d", text) and 'class="source"' not in chunk and 'class="cite"' not in chunk:
                    m = re.search(r"<h[12][^>]*>(.*?)</h[12]>", chunk, re.S)
                    title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else chunk[:60]
                    errors.append("%s: a section shows figures with no source (%s)" % (url, title[:60]))
    return errors
