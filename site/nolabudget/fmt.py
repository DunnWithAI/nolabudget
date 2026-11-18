"""Number formatting. Registered as Jinja filters in build.py.

House style:
  prose      "$799.8 million", "$1,592.4 million", "$412,000"
  tables     full dollars, no $ sign (the caption says "in dollars"): "799,839,000"
  millions   "799.8" for tables captioned "in millions of dollars"
  changes    "up $5.1 million (3.1%)" / "down ..." / "no change"
A missing value is never printed as 0, "nan" or "None": use `dash` in tables.
"""
MINUS = "−"


def money(x, digits=1):
    """Prose money: $799.8 million / $412,000 / $950."""
    if x is None:
        return "not printed"
    neg = x < 0
    a = abs(x)
    if a >= 1e6:
        s = "${:,.{d}f} million".format(a / 1e6, d=digits)
    elif a >= 1e4:
        s = "${:,.0f}".format(round(a, -3))
    else:
        s = "${:,.0f}".format(a)
    return (MINUS if neg else "") + s


def money_short(x):
    """Compact money for chart axes and tight spots: $800M, $1.2B, $1,250."""
    if x is None:
        return ""
    a = abs(x)
    sign = MINUS if x < 0 else ""
    if a >= 1e9:
        return "%s$%sB" % (sign, ("%.2f" % (a / 1e9)).rstrip("0").rstrip("."))
    if a >= 1e6:
        v = a / 1e6
        return "%s$%sM" % (sign, ("%.0f" % v) if v >= 10 or v == int(v) else ("%.1f" % v))
    if a >= 1e4:
        return "%s$%sK" % (sign, "%.0f" % (a / 1e3))
    return "%s$%s" % (sign, "{:,.0f}".format(a))


def full(x):
    """Full dollars for tables: 799,839,000 (minus sign for negatives)."""
    if x is None:
        return ""
    s = "{:,.0f}".format(abs(round(x)))
    return (MINUS + s) if round(x) < 0 else s


def millions(x, digits=1):
    if x is None:
        return ""
    s = "{:,.{d}f}".format(abs(x) / 1e6, d=digits)
    return (MINUS + s) if x < 0 and s.strip("0.,") else s


def number(x, digits=0):
    if x is None:
        return ""
    s = "{:,.{d}f}".format(abs(x), d=digits)
    return (MINUS + s) if x < 0 and s.strip("0.,") else s


def pct(x, digits=1):
    if x is None:
        return ""
    s = "{:.{d}f}%".format(abs(x) * 100, d=digits)
    return (MINUS + s) if x < 0 and s.strip("0.%") else s


def signed_pct(x, digits=1):
    if x is None:
        return ""
    s = "{:.{d}f}%".format(abs(x) * 100, d=digits)
    if not s.strip("0.%"):
        return s
    return ("+" if x > 0 else MINUS) + s


def signed_full(x):
    if x is None:
        return ""
    if round(x) == 0:
        return "0"
    return ("+" if x > 0 else MINUS) + "{:,.0f}".format(abs(round(x)))


def signed_millions(x, digits=1):
    if x is None:
        return ""
    s = "{:,.{d}f}".format(abs(x) / 1e6, d=digits)
    if not s.strip("0.,"):
        return s
    return ("+" if x > 0 else MINUS) + s


def change(new, old, unit="money"):
    """Plain change phrase: 'up $5.1 million (3.1%)'."""
    if new is None or old is None:
        return ""
    d = new - old
    if abs(d) < 0.5:
        return "no change"
    word = "up" if d > 0 else "down"
    amt = money(abs(d)) if unit == "money" else number(abs(d), 1 if abs(d) < 10 else 0)
    if old:
        return "%s %s (%s)" % (word, amt, pct(abs(d) / abs(old)))
    return "%s %s" % (word, amt)


def share(x):
    """A share of a whole: '35%', and '<1%' rather than '0%' for small non-zero parts."""
    if x is None:
        return ""
    if 0 < abs(x) < 0.005:
        return "<1%"
    return pct(x, 0)


def money_hero(x):
    """One big headline figure: "$1.47 billion" or "$763 million". Prose keeps `money`."""
    if x is None:
        return "not printed"
    if abs(x) >= 1e9:
        return "$%.2f billion" % (x / 1e9)
    return "$%.0f million" % (x / 1e6)


def per_hundred(amounts):
    """Whole dollars out of every $100 for each amount, adding to exactly 100
    (largest remainder), so "of every $100" pictures never sum to 99 or 101."""
    total = sum(amounts)
    if not total:
        return [0 for _ in amounts]
    raw = [a * 100.0 / total for a in amounts]
    out = [int(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: -(raw[i] - out[i]))
    for i in order[:100 - sum(out)]:
        out[i] += 1
    return out


def per100(share):
    """A share as money out of every $100: "$22", "$4.70", "$0.40", "less than 1 cent"."""
    if share is None:
        return ""
    v = share * 100
    if v >= 9.5:
        return "$%.0f" % v
    if v >= 0.01:
        return "$%.2f" % v
    return "less than 1 cent"


def ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


FILTERS = {
    "money": money, "money_short": money_short, "full": full, "millions": millions,
    "number": number, "pct": pct, "signed_pct": signed_pct, "signed_full": signed_full,
    "signed_millions": signed_millions, "share": share, "money_hero": money_hero, "per100": per100,
}
