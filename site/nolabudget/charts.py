"""Inline SVG charts, drawn at build time.

Two chart forms: `sparkline`, a tiny trend line beside a figure in a list, and a line
chart over fiscal years, with up to
three series (two lines and one "proposed" point). Each chart is drawn twice, in a wide
and a narrow layout, and CSS shows the one that fits; the text in an SVG scales with
its width, so one drawing cannot be legible on both a phone and a desktop.

Colours come from CSS custom properties (--s1, --s2, --s3, --grid, --axis, --ink-2,
--surface) set in static/style.css, so light and dark themes need no change here.

Every chart must be paired with a table of the same numbers (see the `figure` macro
in templates/partials/macros.html). The hover layer is progressive enhancement:
static/site.js reads the `data-tips` attribute on the <svg>.
"""
import json
import math
from xml.sax.saxutils import escape

LAYOUTS = {
    # width, height, left, right, top, bottom, year tick step, font size
    "wide": dict(W=680, H=290, l=62, r=78, t=18, b=30, step=2, fs=12),
    "narrow": dict(W=360, H=250, l=50, r=66, t=16, b=28, step=5, fs=11.5),
}


def nice_ticks(vmin, vmax, n=4):
    """Round tick values covering [vmin, vmax], always including 0."""
    vmin = min(0.0, vmin)
    vmax = max(0.0, vmax)
    if vmax == vmin:
        vmax = vmin + 1
    raw = (vmax - vmin) / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    ticks = []
    v = lo
    while v <= hi + step / 1e6:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _attr(s):
    return escape(str(s), {'"': "&quot;"})


def line_chart(series, years, yfmt, tipfmt=None, layout="wide", label="", desc=""):
    """Draw a line chart.

    series: list of dicts
        key    css class suffix: "s1", "s2", "s3"
        label  name shown in labels, legend and tooltip
        values {year: value or None}
        kind   "line" (default) or "point" (an open marker; for proposed figures)
    years:  the x-axis years, in order
    yfmt:   function value -> axis tick text
    tipfmt: function value -> tooltip text (default yfmt)
    """
    tipfmt = tipfmt or yfmt
    L = LAYOUTS[layout]
    W, H, l, r, t, b = L["W"], L["H"], L["l"], L["r"], L["t"], L["b"]
    pw, ph = W - l - r, H - t - b
    vals = [v for s in series for v in s["values"].values() if v is not None]
    ticks = nice_ticks(min(vals) if vals else 0, max(vals) if vals else 1)
    y0, y1 = ticks[0], ticks[-1]
    n = len(years)

    def X(year):
        i = years.index(year)
        return l + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v):
        return t + ph - (v - y0) / float(y1 - y0) * ph

    out = []
    out.append('<svg class="chart chart-%s" viewBox="0 0 %d %d" role="img" aria-label="%s"%s>' % (
        layout, W, H, _attr(label), ""))
    if desc:
        out.append("<desc>%s</desc>" % escape(desc))
    # grid and y ticks
    out.append('<g class="grid">')
    for v in ticks:
        y = Y(v)
        cls = "axis" if v == 0 else "gridline"
        out.append('<line class="%s" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (cls, l, W - r, y, y))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end" dy="0.32em" font-size="%s">%s</text>' % (
            l - 8, y, L["fs"], escape(yfmt(v))))
    out.append("</g>")
    # x ticks
    out.append('<g class="xaxis">')
    for yr in years:
        if yr % L["step"] == 0 or (L["step"] == 2 and False):
            out.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle" font-size="%s">%d</text>' % (
                X(yr), H - b + 18, L["fs"], yr))
    out.append("</g>")
    # series
    ends = []
    for s in series:
        key = s["key"]
        pts = [(yr, s["values"].get(yr)) for yr in years]
        if s.get("kind") == "point":
            for yr, v in pts:
                if v is not None:
                    out.append('<circle class="mark-point %s" cx="%.1f" cy="%.1f" r="5"/>' % (key, X(yr), Y(v)))
                    ends.append((s, yr, v, "point"))
            continue
        segs, cur = [], []
        for yr, v in pts:
            if v is None:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append((yr, v))
        if cur:
            segs.append(cur)
        for seg in segs:
            if len(seg) == 1:
                yr, v = seg[0]
                out.append('<circle class="mark-dot %s" cx="%.1f" cy="%.1f" r="3.5"/>' % (key, X(yr), Y(v)))
            else:
                d = "M" + " L".join("%.1f %.1f" % (X(yr), Y(v)) for yr, v in seg)
                out.append('<path class="mark-line %s" pathLength="1" d="%s"/>' % (key, d))
        if segs:
            yr, v = segs[-1][-1]
            out.append('<circle class="mark-end %s" cx="%.1f" cy="%.1f" r="4"/>' % (key, X(yr), Y(v)))
            ends.append((s, yr, v, "line"))
    # direct labels at series ends, placed away from the other series
    placed = []
    for s, yr, v, kind in ends:
        x, y = X(yr), Y(v)
        others = [o for o in series if o is not s]
        above = True
        for o in others:
            ov = o["values"].get(yr)
            if ov is None and o.get("kind") != "point":
                # nearest value of the other series
                near = [(abs(yy - yr), vv) for yy, vv in o["values"].items() if vv is not None]
                ov = min(near)[1] if near else None
            if ov is not None and ov > v:
                above = False
        ty = y - 10 if above else y + 18
        ty = max(t + 10, min(t + ph - 4, ty))
        for py in placed:
            if abs(py - ty) < L["fs"] + 2:
                ty = py + (L["fs"] + 3) * (1 if ty >= py else -1)
        placed.append(ty)
        if kind == "point":
            out.append('<text class="label" x="%.1f" y="%.1f" text-anchor="start" font-size="%s">%s</text>' % (
                x + 9, y + 4, L["fs"], escape(s.get("short", s["label"]) if layout == "wide" else s.get("tiny", "Proposed"))))
        # Lines are identified by the legend above the chart (and the tooltip and table);
        # end labels on converging lines read as noise, so only points are labelled.
    # hover layer
    tips = []
    out.append('<g class="hits">')
    colw = pw / float(max(n - 1, 1))
    for i, yr in enumerate(years):
        rows = []
        for s in series:
            v = s["values"].get(yr)
            if v is not None:
                rows.append([s["key"], s["label"], tipfmt(v)])
        tips.append({"x": round(X(yr), 1), "year": yr, "rows": rows})
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" data-i="%d"/>' % (
            X(yr) - colw / 2, t, colw, ph, i))
    out.append("</g>")
    out.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%d" visibility="hidden"/>' % (t, t + ph))
    out.append("</svg>")
    svg = "".join(out)
    svg = svg.replace("<svg ", '<svg data-tips="%s" ' % _attr(json.dumps(tips, separators=(",", ":"))), 1)
    return svg


def both(series, years, yfmt, tipfmt=None, label="", desc=""):
    """The wide and narrow drawings of one chart."""
    return {
        "wide": line_chart(series, years, yfmt, tipfmt, "wide", label, desc),
        "narrow": line_chart(series, years, yfmt, tipfmt, "narrow", label, desc),
    }


def sparkline(values, years, w=132, h=30):
    """A tiny trend line for a list row: {year: value} drawn over `years`, from zero to
    the largest value, with a gap where a year has no figure and a dot on the last one.
    Decorative (aria-hidden): the row always prints the figures it summarises."""
    vals = [values.get(y) for y in years]
    known = [v for v in vals if v is not None]
    if len(known) < 2:
        return ""
    top = max(max(known), 1)
    pad = 3
    n = len(years) - 1 or 1

    def X(i):
        return pad + (w - 2 * pad) * i / n

    def Y(v):
        return h - pad - (h - 2 * pad) * max(v, 0) / top

    segs, cur = [], []
    for i, v in enumerate(vals):
        if v is None:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append("%.1f,%.1f" % (X(i), Y(v)))
    if cur:
        segs.append(cur)
    out = ['<svg class="spark" viewBox="0 0 %d %d" width="%d" height="%d" aria-hidden="true" focusable="false">' % (w, h, w, h),
           '<line class="spark-base" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (pad, w - pad, h - pad + 0.5, h - pad + 0.5)]
    for sg in segs:
        if len(sg) == 1:
            x, y = sg[0].split(",")
            out.append('<circle class="spark-dot" cx="%s" cy="%s" r="1.6"/>' % (x, y))
        else:
            out.append('<polyline class="spark-line" pathLength="1" points="%s"/>' % " ".join(sg))
    last = max(i for i, v in enumerate(vals) if v is not None)
    out.append('<circle class="spark-end" cx="%.1f" cy="%.1f" r="2.6"/>' % (X(last), Y(vals[last])))
    out.append("</svg>")
    return "".join(out)
