"""Chart figures with switchable views (General Fund / all funds x dollars / 2025 dollars /
per resident), each paired with its table. Used by the home page and department pages.

A figure is a dict the `figure` macro (templates/partials/macros.html) renders:
    {id, title, controls: {funds: [...], units: [...]}, legend: [...], views: [...]}
Each view: {id, fund, unit, heading, svg: {wide, narrow}, note, columns, rows}
"""
from . import charts, fmt

UNITS = [("dollars", "Dollars"), ("real", "%(base)d dollars"), ("resident", "Per resident")]
FUND_NAMES = {"general_fund": "General Fund", "all_funds": "All funds", "other_funds": "Other funds"}


def unit_notes(budget, unit, years):
    site = budget.content["site"]
    last = max(years)
    if unit == "real":
        months = budget.cpi_months.get(budget.real_base, "12")
        held = [y for y in years if y > budget.last_cpi_year]
        s = ("Adjusted for inflation to %d dollars using consumer prices for the South region "
             "(the annual average%s)." % (budget.real_base, "" if months == "12" else
                                         " of the %s months published for %d" % (months, budget.real_base)))
        if held:
            s += " %s not adjusted, because full-year prices are not yet available." % (
                "%d is" % held[0] if len(held) == 1 else _years(held) + " are")
        return s
    if unit == "resident":
        held = [y for y in years if y > budget.last_pop_year]
        s = "Divided by the population of Orleans Parish (Census estimates for July 1 of each year)."
        if held:
            s += " %s the %d population, the latest available." % (
                "%d uses" % held[0] if len(held) == 1 else _years(held) + " use", budget.last_pop_year)
        if min(years) <= 2005 < last:
            s += (" After Hurricane Katrina the population fell from %s in 2005 to %s in 2006, "
                  "so amounts per resident jump that year." % (
                      fmt.number(budget.population(2005)), fmt.number(budget.population(2006))))
        return s
    return "Not adjusted for inflation."


def _years(ys):
    return "%d and %d" % (ys[0], ys[1]) if len(ys) == 2 else "%d to %d" % (ys[0], ys[-1])


def convert(budget, unit, value, year):
    if value is None:
        return None
    if unit == "real":
        return budget.real(value, year)
    if unit == "resident":
        return budget.per_resident(value, year)
    return value


def money_figure(budget, fig_id, title, years, getter, funds=("general_fund", "all_funds"),
                 series_spec=None, subject=""):
    """Build a figure.

    getter(fund, basis, year) -> nominal dollars or None.
    series_spec: list of (key, basis, label, short, kind, only_years) - defaults to
        adopted line, actual line, proposed point for the proposal year.
    """
    site = budget.content["site"]
    py = site["proposal_year"]
    if series_spec is None:
        series_spec = [
            ("s1", "adopted", "Adopted budget", "Adopted", "line", None),
            ("s2", "actual", "Actual spending", "Actual", "line", None),
            ("s3", "proposed", "%d proposal" % py, "%d proposed" % py, "point", [py]),
        ]
    views = []
    for fund in funds:
        for unit, ulabel in UNITS:
            ulabel = ulabel % {"base": budget.real_base}
            series = []
            for key, basis, label, short, kind, only in series_spec:
                vals = {}
                for y in years:
                    if only and y not in only:
                        continue
                    vals[y] = convert(budget, unit, getter(fund, basis, y), y)
                if any(v is not None for v in vals.values()):
                    series.append({"key": key, "label": label, "short": short, "kind": kind, "values": vals,
                                   "basis": basis})
            allv = [v for s in series for v in s["values"].values() if v is not None]
            small = unit == "resident" and allv and max(abs(v) for v in allv) < 20
            if unit == "resident":
                tick = (lambda v, small=small: "$" + fmt.number(v, 2 if small and v != int(v) else 0))
                cellf = (lambda v, small=small: fmt.number(v, 2 if small else 0))
                tipf = (lambda v, small=small: "$" + fmt.number(v, 2 if small else 0))
            else:
                tick = fmt.money_short
                cellf = fmt.full
                tipf = fmt.money
            heading = "%s, %s" % (FUND_NAMES[fund], ulabel[0].lower() + ulabel[1:] if unit != "real" else "in " + ulabel)
            if unit == "dollars":
                heading = "%s, in dollars" % FUND_NAMES[fund]
            elif unit == "resident":
                heading = "%s, dollars per resident" % FUND_NAMES[fund]
            desc = describe(series, subject, heading, tipf)
            svg = charts.both(series, years, tick, tipf, label="%s: %s" % (title, heading), desc=desc)
            rows = []
            for y in years:
                cells = [s["values"].get(y) for s in series]
                if any(c is not None for c in cells):
                    rows.append({"year": y, "cells": [cellf(c) if c is not None else None for c in cells]})
            views.append({
                "id": "%s-%s" % (fund, unit), "fund": fund, "unit": unit, "heading": heading,
                "svg": svg, "note": unit_notes(budget, unit, years), "empty": not series, "_series": series,
                "columns": [s["label"] for s in series], "rows": rows,
                "unit_caption": "dollars per resident" if unit == "resident" else (
                    "%d dollars" % budget.real_base if unit == "real" else "dollars"),
            })
    present = {s_["key"] for v in views for s_ in v.pop("_series")}
    legend = [{"key": key, "label": label, "kind": kind}
              for key, basis, label, short, kind, only in series_spec if key in present]
    return {
        "id": fig_id, "title": title, "views": views,
        "funds": [(f, FUND_NAMES[f]) for f in funds],
        "units": [(u, l % {"base": budget.real_base}) for u, l in UNITS],
        "legend": legend,
    }


def describe(series, subject, heading, f):
    """A one-paragraph text alternative for a chart."""
    parts = []
    for s in series:
        pts = [(y, v) for y, v in sorted(s["values"].items()) if v is not None]
        if not pts:
            continue
        if len(pts) == 1:
            parts.append("%s: %s in %d." % (s["label"], f(pts[0][1]), pts[0][0]))
            continue
        hi = max(pts, key=lambda p: p[1])
        parts.append("%s from %d to %d, starting at %s and ending at %s; highest %s in %d." % (
            s["label"], pts[0][0], pts[-1][0], f(pts[0][1]), f(pts[-1][1]), f(hi[1]), hi[0]))
    return "%s%s. %s Full figures are in the table below the chart." % (
        (subject + ", ") if subject else "", heading, " ".join(parts))


def simple_figure(budget, fig_id, title, heading, years, series, tick, cellf, tipf, note=""):
    """A single-view figure (no switch), e.g. share of the General Fund, or positions."""
    desc = describe(series, "", heading, tipf)
    svg = charts.both(series, years, tick, tipf, label="%s: %s" % (title, heading), desc=desc)
    rows = []
    for y in years:
        cells = [s["values"].get(y) for s in series]
        if any(c is not None for c in cells):
            rows.append({"year": y, "cells": [cellf(c) if c is not None else None for c in cells]})
    return {
        "id": fig_id, "title": title, "funds": [], "units": [],
        "legend": [{"key": s["key"], "label": s["label"], "kind": s.get("kind", "line")} for s in series],
        "views": [{"id": "only", "heading": heading, "svg": svg, "note": note, "empty": not rows,
                   "columns": [s["label"] for s in series], "rows": rows, "unit_caption": ""}],
    }
