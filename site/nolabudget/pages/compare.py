# ABOUTME: The city comparisons page, /compare/.
# ABOUTME: New Orleans spending per resident on seven services against similar-size U.S. and Louisiana cities.
"""/compare/: how New Orleans compares. Figures come from nolabudget/compare.py."""
from .. import compare


def build(site):
    rows = compare.load()
    sections = []
    for f in compare.FUNCTIONS:
        key = f["key"]
        peer = compare.summary(rows, key, "peer")
        la = compare.summary(rows, key, "louisiana") if any(
            r[key + "_per"] for r in rows if "louisiana" in r["groups"] and not r["is_nola"]) else None
        peer["svg"] = compare.dot_strip(peer)
        peer["svg_narrow"] = compare.dot_strip(peer, w=360)
        sections.append({"key": key, "label": f["label"], "fn": f, "peer": peer, "la": la})
    nola = next(r for r in rows if r["is_nola"])
    site.publish("/compare/", **{f["key"] + "_per": nola[f["key"] + "_per"] for f in compare.FUNCTIONS})
    site.page("/compare/", "compare.html", "How New Orleans compares", sections=sections, nola=nola,
              year=compare.YEAR, source=compare.SOURCE, rows=rows,
              combined=[r for r in rows if r["combined"]], overview=compare.overview(rows))
