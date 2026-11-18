# ABOUTME: City comparisons from data/combined/city_comparison.csv (Census Bureau, 2022).
# ABOUTME: Per-resident spending on seven everyday services for New Orleans, similar-size U.S. cities and Louisiana cities.
"""How New Orleans compares with other cities.

The data are each city government's own 2022 spending as reported to the Census Bureau
(scripts/build_city_comparison.py), divided by Census's population for that government.
They are not budget-book figures and must never be mixed with them on a page without
saying so: Census years, populations and definitions differ from the city's books.
"""
import csv
import os
from xml.sax.saxutils import escape

from . import fmt
from .data import COMBINED

NEW_ORLEANS = "222071166094"
YEAR = 2022
SOURCE = {
    "text": "U.S. Census Bureau, 2022 Census of Governments, individual unit file",
    "short": "Census Bureau, 2022",
    "href": "https://www.census.gov/data/datasets/2022/econ/local/public-use-datasets.html",
    "computed": False, "title": "", "book": "",
}
# The services compared, in page order. `on` finishes "spent $X per resident ...";
# `note` explains a known difference between Census's figure and New Orleans's budget book.
# `strict` services are run by every city government, so a zero is never "someone else
# runs it" (the build script stops on one); for the rest, a zero leaves that city out.
FUNCTIONS = [
    {"key": "police", "label": "Police", "on": "on police", "strict": True, "note": ""},
    {"key": "fire", "label": "Fire", "on": "on fire protection", "strict": True, "note": ""},
    {"key": "streets", "label": "Streets", "on": "on streets and roads", "strict": False,
     "note": "Streets and roads: building and maintaining them, including street lights and drainage along them."},
    {"key": "trash", "label": "Trash and recycling", "on": "on trash and recycling", "strict": False,
     "note": "Census counts New Orleans's trash spending lower than the Department of Sanitation's budget, "
             "which also pays for work Census counts elsewhere, such as cleaning streets."},
    {"key": "parks", "label": "Parks and recreation", "on": "on parks and recreation", "strict": False,
     "note": "Census counts park and recreation spending wherever it sits in city government, so its figure for "
             "New Orleans is larger than the budget's parks department alone."},
    {"key": "libraries", "label": "Libraries", "on": "on libraries", "strict": False, "note": ""},
    {"key": "health", "label": "Public health", "on": "on public health", "strict": False,
     "note": "New Orleans runs its ambulance service (EMS) inside its Health Department, and Census's health figure "
             "for New Orleans is close to that department's whole budget. In many cities ambulances are run by the "
             "fire department and counted under fire, which may explain part of this difference."},
]
BY_KEY = {f["key"]: f for f in FUNCTIONS}


def load():
    with open(os.path.join(COMBINED, "city_comparison.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["population"] = int(r["population"])
        r["groups"] = r["groups"].split()
        r["is_nola"] = r["census_id"] == NEW_ORLEANS
        for fn in FUNCTIONS:
            k = fn["key"]
            v = float(r[k]) if r[k] else None
            r[k + "_none"] = v == 0 and not fn["strict"]   # reported none of its own
            r[k] = None if r[k + "_none"] else v
            r[k + "_per"] = r[k] / r["population"] if r[k] is not None else None
    return rows


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else None


def summary(rows, key, group):
    """Cities in `group` with a figure for `key`, highest per resident first, plus where
    New Orleans ranks and the median of the other cities."""
    cities = sorted([r for r in rows if group in r["groups"] and r[key + "_per"] is not None],
                    key=lambda r: -r[key + "_per"])
    nola = next(r for r in cities if r["is_nola"])
    others = [r[key + "_per"] for r in cities if not r["is_nola"]]
    med = median(others)
    top = cities[0][key + "_per"]
    return {
        "key": key, "group": group, "cities": cities, "nola": nola, "median": med,
        "rank": cities.index(nola) + 1, "n": len(cities),
        "higher_than": sum(1 for v in others if v < nola[key + "_per"]),
        "ratio": nola[key + "_per"] / med if med else None,
        "pct_diff": round(abs(nola[key + "_per"] / med - 1) * 100) if med else None,
        "bars": [{"r": r, "width": r[key + "_per"] / top} for r in cities],
        "excluded": [r for r in rows if group in r["groups"] and r[key + "_per"] is None and r["note"]],
        "none": [r for r in rows if group in r["groups"] and r[key + "_none"]],
        "fn": BY_KEY[key],
    }


def overview(rows):
    """One row per service: New Orleans against the median similar-size city, as a bar
    from the median to how much more or less New Orleans spent. Most more first."""
    out = []
    for fn in FUNCTIONS:
        s = summary(rows, fn["key"], "peer")
        out.append({"fn": fn, "s": s, "diff": s["ratio"] - 1})
    out.sort(key=lambda o: -o["diff"])
    biggest = max(abs(o["diff"]) for o in out) or 1
    for o in out:
        o["width"] = abs(o["diff"]) / biggest
    return out


def dot_strip(s, w=680):
    """Every city as a dot on one per-resident scale, New Orleans marked and labelled under
    the axis, the median as a dashed line labelled above the dots. Dots that would overlap
    stack upward; the drawing is as tall as the tallest stack. Each dot has a <title>."""
    key = s["key"]
    vals = [r[key + "_per"] for r in s["cities"]]
    hi = max(vals) * 1.05
    l, r_, rad = 16, 16, 5.5

    def X(v):
        return l + (w - l - r_) * v / hi

    # Stack dots first (measured upward from the axis), then size the drawing.
    placed, dots = [], []
    for city in sorted(s["cities"], key=lambda c: c[key + "_per"]):
        x, up = X(city[key + "_per"]), 0.0
        while any(abs(px - x) < rad * 2 and abs(pu - up) < rad * 2 for px, pu in placed):
            up += rad * 2 - 1
        placed.append((x, up))
        dots.append((city, x, up))
    top_pad, below = 26, 44
    tallest = max(u for _, _, u in dots) + rad * 2
    h = int(top_pad + tallest + below)
    axis = top_pad + tallest + 2

    out = ['<svg class="strip" viewBox="0 0 %d %d" role="img" aria-label="%s">' % (
        w, h, escape("%s spending per resident in %d cities; New Orleans %s, median %s" % (
            s["fn"]["label"], s["n"], fmt.money(s["nola"][key + "_per"]), fmt.money(s["median"]))))]
    out.append('<line class="strip-axis" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (l, w - r_, axis, axis))
    step = _tick_step(hi, w)
    t = 0
    while t <= hi:
        out.append('<text class="strip-tick" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (X(t), h - 4, fmt.money(t)))
        t += step
    mx = X(s["median"])
    out.append('<line class="strip-median" x1="%.1f" x2="%.1f" y1="16" y2="%.1f"/>' % (mx, mx, axis))
    out.append('<text class="strip-label" x="%.1f" y="12" text-anchor="middle">Median %s</text>' % (mx, fmt.money(s["median"])))
    for city, x, up in dots:
        nola = city["is_nola"]
        out.append('<circle class="%s" cx="%.1f" cy="%.1f" r="%.1f"><title>%s, %s: %s per resident</title></circle>' % (
            "strip-dot nola" if nola else "strip-dot", x, axis - rad - 2 - up, rad + (1.5 if nola else 0),
            escape(city["name"]), city["state"], fmt.money(city[key + "_per"])))
        if nola:
            nx = x
    out.append('<line class="strip-pointer" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>' % (nx, nx, axis, axis + 8))
    out.append('<text class="strip-label nola" x="%.1f" y="%.1f" text-anchor="middle">New Orleans %s</text>' % (
        nx, axis + 21, fmt.money(s["nola"][key + "_per"])))
    out.append("</svg>")
    return "".join(out)


def _tick_step(hi, w):
    """A round dollar step giving about 3 to 5 axis labels for this width."""
    want = 3 if w < 500 else 5
    for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if hi / step <= want:
            return step
    return 1000
