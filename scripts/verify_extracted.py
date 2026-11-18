"""Independent check of an extracted book: sums the department rows and, for PDF books,
looks for a random sample of amounts on the pages they cite.

    .venv/bin/python scripts/verify_extracted.py <book_id> [pdf_path] [--n 15]
"""
import collections
import csv
import random
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 15
book = args[0]
pdf = args[1] if len(args) > 1 else None
d = f"data/extracted/{book}/"


def load(name):
    try:
        return list(csv.DictReader(open(d + name)))
    except FileNotFoundError:
        return []


rows = load("department_expenditures.csv")
s = collections.Counter()
for r in rows:
    s[(r["fiscal_year"], r["basis"], r["fund_group"])] += int(float(r["amount"]))
print(f"== {book}: {len(rows)} dept rows")
for y, b in sorted({(k[0], k[1]) for k in s}):
    g, o, a = (s.get((y, b, f), 0) for f in ("general_fund", "other_funds", "all_funds"))
    flag = "" if g + o == a else f"  GF+other-all={g + o - a:,}"
    print(f"  {y} {b:9} GF={g:>14,} other={o:>14,} all={a:>14,}{flag}")

rev = load("revenues.csv")
rs = collections.Counter()
for r in rev:
    if r["fund_name"].strip().lower() == "general fund":
        rs[(r["fiscal_year"], r["basis"])] += int(float(r["amount"]))
print("  GF revenue lines:", {f"{k[0]} {k[1]}": f"{v:,}" for k, v in sorted(rs.items())})

pos = load("positions.csv")
fs = collections.Counter()
for r in pos:
    try:
        fs[(r["fiscal_year"], r["basis"])] += float(r["fte"])
    except ValueError:
        pass
print("  FTE:", {f"{k[0]} {k[1]}": round(v, 2) for k, v in sorted(fs.items())})
print("  detail rows:", len(load("department_detail.csv")), "positions:", len(pos))

if pdf:
    import fitz

    doc = fitz.open(pdf)
    random.seed(11)
    for name in ("department_expenditures.csv", "department_detail.csv", "revenues.csv"):
        pool = [r for r in load(name) if abs(int(float(r["amount"]))) > 999 and r["page"].isdigit()]
        smp = random.sample(pool, min(n, len(pool)))
        miss = []
        for r in smp:
            a = f"{abs(int(float(r['amount']))):,}"
            if a not in doc[int(r["page"]) - 1].get_text():
                miss.append((r["page"], a))
        print(f"  spot {name}: {len(smp) - len(miss)}/{len(smp)} found on cited page", miss[:5])
