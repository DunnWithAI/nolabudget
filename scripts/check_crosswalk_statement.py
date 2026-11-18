#!/usr/bin/env python
"""Verify data/crosswalk/department_aliases_statement.csv against the citywide statements.

Run: .venv/bin/python scripts/check_crosswalk_statement.py [--verbose]

Exit status 1 if coverage is incomplete (1) or totals are not conserved (2).
Everything else is evidence that is printed for a person to read:

  1. coverage      every (book_id, dept_name) in every data/extracted/*/department_expenditures.csv
                   is in the alias table exactly once; no alias row without data; dept_ids exist
                   in departments.csv.
  2. conservation  for each book/year/basis/fund_group: sum over dept_id == sum over printed names.
  3. statement-line years per dept_id (which years have a line at all, which a non-zero one).
  4. overlap, basis=actual   same (dept_id, fiscal_year, fund_group) printed in >= 2 books
                   (amounts summed over printed names within a book) must be identical.
  5. overlap, basis=adopted  same comparison; genuine revisions expected, counted and ranked.
  6. composition   statement line == sum of department-section pages (department_detail.csv,
                   by_type) for the hard cases (Miscellaneous, Health, Finance, CAO, OCD, General
                   Services = Inspector General, the FY2024-actuals table of the 2026 book).
  7. name links    printed names whose amounts equal another name's amounts in another book:
                   consistent (same dept_id) vs conflicting (different dept_id).

data/crosswalk/corrections.csv rows for department_expenditures (mislabelled year/basis columns)
are applied before comparing, as scripts/build_combined.py does.
"""
import csv, glob, os, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERBOSE = "--verbose" in sys.argv
fails = []


def rd(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def short(book):
    return book.split("-operating-")[0] + ("p" if "proposed" in book else "")


depts = {r["dept_id"]: r for r in rd(os.path.join(ROOT, "data/crosswalk/departments.csv"))}
aliases = rd(os.path.join(ROOT, "data/crosswalk/department_aliases_statement.csv"))
rows = []
for p in sorted(glob.glob(os.path.join(ROOT, "data/extracted/*/department_expenditures.csv"))):
    rows += rd(p)

# ---- corrections (relabel year/basis columns the books themselves mislabel)
corr_path = os.path.join(ROOT, "data/crosswalk/corrections.csv")
n_corr = defaultdict(int)
if os.path.exists(corr_path):
    corr = [c for c in rd(corr_path) if c["file"] == "department_expenditures"]
    for r in rows:
        for c in corr:
            if (r["book_id"] == c["book_id"] and r["fiscal_year"] == c["match_fiscal_year"]
                    and r["basis"] == c["match_basis"]
                    and (not c["match_depts"] or r["dept_name"] in c["match_depts"].split("|"))):
                r["fiscal_year"], r["basis"] = c["set_fiscal_year"] or r["fiscal_year"], c["set_basis"] or r["basis"]
                n_corr[(c["book_id"], c["match_fiscal_year"], c["match_basis"])] += 1
                break
    print("corrections applied (rows relabelled):", dict(n_corr) or "none")

# ---- 1. coverage
amap, dup = {}, []
for a in aliases:
    k = (a["book_id"], a["printed_name"])
    if k in amap:
        dup.append(k)
    amap[k] = a["dept_id"]
pairs = {(r["book_id"], r["dept_name"]) for r in rows}
uncovered = sorted(pairs - set(amap))
stale = sorted(set(amap) - pairs)
bad_id = sorted({a["dept_id"] for a in aliases} - set(depts))
print("\n== 1. coverage")
print(f"books={len({p[0] for p in pairs})} distinct printed names={len({p[1] for p in pairs})} "
      f"(book,name) pairs={len(pairs)} alias rows={len(aliases)}")
print(f"uncovered={len(uncovered)} duplicate alias rows={len(dup)} alias rows with no matching data={len(stale)} "
      f"unknown dept_ids={len(bad_id)}")
for lst, lab in ((uncovered, "UNCOVERED"), (dup, "DUPLICATE"), (stale, "STALE"), (bad_id, "UNKNOWN dept_id")):
    for x in lst:
        print("  ", lab, x)
if uncovered or dup or stale or bad_id:
    fails.append("coverage")
unused = sorted(set(depts) - set(amap.values()))
print("canonical dept_ids with no statement line in any book:", ", ".join(unused) or "none")

# ---- 2. conservation
by_name = defaultdict(int)
cell = defaultdict(lambda: defaultdict(int))  # (dept_id, fy, basis, fund) -> book -> amount
fan = defaultdict(set)
for r in rows:
    d = amap.get((r["book_id"], r["dept_name"]))
    if d is None:
        continue
    amt = int(r["amount"])
    by_name[(r["book_id"], r["fiscal_year"], r["basis"], r["fund_group"])] += amt
    cell[(d, r["fiscal_year"], r["basis"], r["fund_group"])][r["book_id"]] += amt
    fan[(d, r["book_id"], r["fiscal_year"], r["basis"], r["fund_group"])].add(r["dept_name"])
by_id = defaultdict(int)
for (d, fy, b, fg), bk in cell.items():
    for book, amt in bk.items():
        by_id[(book, fy, b, fg)] += amt
mism = [k for k in set(by_name) | set(by_id) if by_name[k] != by_id[k]]
print("\n== 2. conservation of totals (sum over dept_id == sum over printed names)")
print(f"book/year/basis/fund_group groups checked={len(by_name)} mismatches={len(mism)}")
for k in sorted(mism):
    print("  MISMATCH", k, by_name[k], by_id[k])
if mism:
    fails.append("conservation")
fanin = defaultdict(set)
for (d, book, fy, b, fg), names in fan.items():
    if len(names) > 1:
        fanin[(d, book)] |= names
print(f"(dept_id, book) pairs where several printed names sum into one dept_id: {len(fanin)}")
for (d, book), names in sorted(fanin.items()):
    print(f"   {d:26} {short(book):6} {' + '.join(sorted(names))}")


# ---- 3. statement-line years
def ranges(ys):
    ys = sorted(set(int(y) for y in ys))
    out, i = [], 0
    while i < len(ys):
        j = i
        while j + 1 < len(ys) and ys[j + 1] == ys[j] + 1:
            j += 1
        out.append(str(ys[i]) if i == j else f"{ys[i]}-{ys[j]}")
        i = j + 1
    return ",".join(out) or "-"


print("\n== 3. statement-line years per dept_id (any book, any fund group)")
print("   line = a printed row exists (even 0); nz = some printed amount is non-zero. act = basis actual; bud = adopted/proposed")
yl = defaultdict(lambda: {"act": set(), "bud": set(), "nz_act": set(), "nz_bud": set()})
for (d, fy, b, fg), bk in cell.items():
    k = "act" if b == "actual" else "bud"
    yl[d][k].add(fy)
    if any(v != 0 for v in bk.values()):
        yl[d]["nz_" + k].add(fy)
for d in depts:
    y = yl[d]
    s = f"  {d:28} act {ranges(y['act']):34} bud {ranges(y['bud'])}"
    if y["nz_act"] != y["act"] or y["nz_bud"] != y["bud"]:
        s += f"  | non-zero: act {ranges(y['nz_act'])} bud {ranges(y['nz_bud'])}"
    print(s)


# ---- 4/5. overlap
def compare(basis):
    n = ag = 0
    diffs = []
    for (d, fy, b, fg), bk in sorted(cell.items()):
        if b != basis or len(bk) < 2:
            continue
        n += 1
        if len(set(bk.values())) == 1:
            ag += 1
        else:
            diffs.append((max(bk.values()) - min(bk.values()), d, fy, fg, sorted(bk.items())))
    return n, ag, diffs


def fmt(vals):
    return "; ".join(f"{short(b)}={v:,}" for b, v in vals)


print("\n== 4. overlap, basis=actual: same dept_id/year/fund_group in >= 2 books must agree")
n, ag, diffs = compare("actual")
print(f"cells in >=2 books={n} identical={ag} ({100 * ag / max(n, 1):.2f}%) disagree={len(diffs)}")
for spread, d, fy, fg, vals in sorted(diffs, key=lambda x: (x[1], x[2], x[3])):
    print(f"  DIFF {d} FY{fy} {fg}: {fmt(vals)}")

print("\n== 5. overlap, basis=adopted: genuine revisions expected (not errors)")
n, ag, diffs = compare("adopted")
print(f"cells in >=2 books={n} identical={ag} ({100 * ag / max(n, 1):.2f}%) differ={len(diffs)}")
for spread, d, fy, fg, vals in sorted(diffs, reverse=True)[: (60 if VERBOSE else 12)]:
    print(f"  {d} FY{fy} {fg} spread={spread:,}: {fmt(vals)}")

# ---- 6. composition identities
detail = defaultdict(lambda: defaultdict(float))  # (book, fy, basis) -> casefolded page -> by_type total
detail_prog = defaultdict(lambda: defaultdict(float))  # same from by_program rows (pages that print no by_type)
for p in sorted(glob.glob(os.path.join(ROOT, "data/extracted/*/department_detail.csv"))):
    for r in rd(p):
        if r["table"] == "by_type":
            detail[(r["book_id"], r["fiscal_year"], r["basis"])][r["dept_name"].casefold()] += float(r["amount"])
        elif r["table"] == "by_program":
            detail_prog[(r["book_id"], r["fiscal_year"], r["basis"])][r["dept_name"].casefold()] += float(r["amount"])
stmt = defaultdict(int)
for r in rows:
    if r["fund_group"] == "all_funds":
        stmt[(r["book_id"], r["fiscal_year"], r["basis"], r["dept_name"])] += int(r["amount"])


def ident(book, fy, basis, lines, pages, label, prog=False):
    s = sum(stmt.get((book, fy, basis, n), 0) for n in lines)
    pg = (detail_prog if prog else detail)[(book, fy, basis)]
    miss = [x for x in pages if x.casefold() not in pg]
    t = round(sum(pg.get(x.casefold(), 0) for x in pages))
    if len(miss) == len(pages):
        print(f"  n/a  {short(book)} FY{fy} {basis[:3]} {label}: line {s:,}; page total for this year not extracted")
        return
    ok = abs(s - t) <= 3 and not miss
    print(f"  {'OK  ' if ok else 'DIFF'} {short(book)} FY{fy} {basis[:3]} {label}: line {s:,} vs pages {t:,}"
          + ("" if ok else f" (diff {s - t:,}; pages not found: {miss})"))


MISC_OLD = ["Miscellaneous", "Misc- NORD", "Misc- OPA", "Misc- Serv. & Innov."]
MISC_NEW = ["Chief Administrative Office", "New Orleans Recreation Development Commission", "Risk Management",
            "Office of Police Secondary Employment"]
BUREAUS = ["Bureau of Accounting", "Bureau of Purchasing", "Bureau of Retirement", "Bureau of Revenue", "Bureau of Treasury"]
MAYOR_PAGES = ["Mayor - Community & Economic Development", "Mayor - Criminal Justice Coordination",
               "Mayor's Office Homeland Security", "Mayor's Office"]
print("\n== 6. composition identities: statement line (all funds) vs sum of department pages (by_type totals)")
print("  -- Miscellaneous holds the former Recreation (NORDC) from FY2013; Health includes EMS; General Services = Inspector General")
for b, fy, bs in (("2019-operating-adopted", "2019", "adopted"), ("2020-operating-proposed", "2020", "proposed")):
    ident(b, fy, bs, ["Miscellaneous"], MISC_OLD, "Miscellaneous = Misc + Misc-NORD + Misc-OPA + Misc-Serv&Innov")
    ident(b, fy, bs, ["Health"], ["Health- Core", "EMS"], "Health = Health-Core + EMS")
    ident(b, fy, bs, ["General Services"], ["General Services"], "General Services = page whose programs are OIG 7101/7107/7108")
ident("2018-operating-adopted", "2018", "adopted", ["General Services"], ["OFFICE OF INSPECTOR GENERAL"], "General Services = OIG page")
ident("2017-operating-adopted", "2017", "adopted", ["General Services"], ["OFFICE OF INSPECTOR GENERAL"], "General Services = OIG page (ERB, IPM already own lines)")
ident("2021-operating-adopted", "2021", "adopted", ["General Services"], ["Office Of The Inspector General"], "General Services = OIG page")
for b, fy in (("2021-operating-adopted", "2021"), ("2022-operating-adopted", "2022"), ("2023-operating-adopted", "2023"),
              ("2024-operating-adopted", "2024"), ("2025-operating-adopted", "2025"), ("2026-operating-adopted", "2026"),
              ("2026-operating-proposed", "2026")):
    ident(b, fy, "adopted" if "adopted" in b else "proposed", ["Miscellaneous"], MISC_NEW,
          "Miscellaneous = CAO misc page + NORDC + Risk Mgmt + OPSE")
ident("2027-operating-proposed", "2027", "proposed", ["Miscellaneous"], MISC_NEW, "Miscellaneous = CAO misc page + NORDC + Risk Mgmt + OPSE")
for b, fy in (("2021-operating-adopted", "2021"), ("2022-operating-adopted", "2022"), ("2023-operating-adopted", "2023")):
    ident(b, fy, "adopted", ["Office of Comm. Dev."], ["OCD - Grants", "OCD - Code Enforcement"], "OCD line = OCD-Grants + OCD-Code Enforcement", prog=True)
ident("2024-operating-adopted", "2024", "adopted", ["Office of Comm. Dev."], ["OCD - Grants"], "OCD line = OCD-Grants (Code Enforcement now own line)", prog=True)
B26 = "2026-operating-adopted"
for fy in ("2025", "2026"):
    ident(B26, fy, "adopted", ["Department of Health"], ["Health", "Health - EMS"], "Health = Health + EMS")
    ident(B26, fy, "adopted", ["Department of Finance"], BUREAUS + ["Finance Director's Office"], "Finance = 5 bureaus + Director's Office")
    ident(B26, fy, "adopted", ["Chief Administrative Office - Core"],
          ["Chief Administrative Office - Core", "Chief Administrative Office - ITI", "FEMA Public Assistance Grants"], "CAO-Core = Core + ITI + FEMA PA")
    ident(B26, fy, "adopted", ["Mayor's Office"], MAYOR_PAGES + ["Office of Resilience & Sustainability"], "Mayor's Office = 5 mayor pages")
ident("2027-operating-proposed", "2027", "proposed", ["Department of Health"], ["Emergency Medical Services", "Health Department"], "Health = Health + EMS")
print("  -- FY2024 actuals table of 2026-operating-adopted (the 'different names')")
ident(B26, "2024", "actual", BUREAUS + ["Director's Office"], BUREAUS + ["Finance Director's Office"], "Finance = 5 bureaus + Director's Office")
ident(B26, "2024", "actual", ["Health Department", "EMS"], ["Health", "Health - EMS"], "Health Department + EMS")
ident(B26, "2024", "actual", ["EMS"], ["Health - EMS"], "EMS")
ident(B26, "2024", "actual", ["Chief Administrative Office", "Other Agencies"],
      ["Chief Administrative Office - Core", "Chief Administrative Office - ITI", "Risk Management",
       "New Orleans Recreation Development Commission"], "CAO + Other Agencies = CAO-Core + ITI + Risk + NORDC")
ident(B26, "2024", "actual", ["Office of the Mayor"], MAYOR_PAGES, "Office of the Mayor = 4 mayor pages (Resilience page has no 2024 actuals)")
for line, page in (("Law Administration", "Law Department"), ("Fire Department", "Fire Administration"),
                   ("Office of Community Development", "OCD - Grants"), ("OIG", "Office of the Inspector General"),
                   ("Orleans Parish Communications", "Orleans Parish Communication District"),
                   ("Municipal Traffic Court", "Municipal and Traffic Court"), ("Human Services", "Juvenile Justice Intervention Center"),
                   ("Mosquito Termite Control Board", "Mosquito Control Board"), ("Police", "Office of Superintendent"),
                   ("Library", "Library"), ("HDLC", "Historic District Landmarks Commission - Culture and Rec"),
                   ("Parks and Parkways", "Administration - Parks and Parkways"), ("Public Works", "Public Works"),
                   ("Sanitation", "Sanitation"), ("Safety and Permits", "Safety and Permits"),
                   ("Office of Police Secondary Employment", "Office of Police Secondary Employment"),
                   ("Department of Code Enforcement", "Code Enforcement"), ("Vieux Carre Commission", "Vieux Carré Commission")):
    ident(B26, "2024", "actual", [line], [page], f"{line} = {page}")
ident(B26, "2024", "actual", ["New Orleans Art Museum", "New Orleans Museum of Art"], ["New Orleans Museum of Art"],
      "NOMA is printed as two lines in that table")

# ---- 7. name links
print("\n== 7. name links: amounts (>= 100,000, same year/basis/fund) equal across books under different printed names")
cells_by_val = defaultdict(list)
for r in rows:
    a = int(r["amount"])
    if abs(a) >= 100_000:
        cells_by_val[(r["fiscal_year"], r["basis"], r["fund_group"], a)].append(r)
link = defaultdict(lambda: [0, 0, set()])  # (book,name) -> consistent, conflicting, conflicting depts
for lst in cells_by_val.values():
    for r in lst:
        for o in lst:
            if o["book_id"] != r["book_id"]:
                k = (r["book_id"], r["dept_name"])
                if amap[(o["book_id"], o["dept_name"])] == amap[k]:
                    link[k][0] += 1
                else:
                    link[k][1] += 1
                    link[k][2].add(amap[(o["book_id"], o["dept_name"])])
conf = {k: v for k, v in link.items() if v[1]}
print(f"printed names with a cross-book amount match under a different dept_id: {len(conf)} (coincidences on round placeholder amounts are possible)")
for k, v in sorted(conf.items()):
    if True:
        print(f"  {short(k[0])} {k[1]!r} -> {amap[k]}: consistent={v[0]} conflicting={v[1]} with {sorted(v[2])}")
nolink = sorted(k for k in pairs if link[k][0] == 0 and any(abs(int(r["amount"])) >= 100_000
                for r in rows if (r["book_id"], r["dept_name"]) == k))
print(f"printed names with non-trivial amounts but no identical cell in another book under the same dept_id: {len(nolink)}")
if VERBOSE:
    for k in nolink:
        print("   ", short(k[0]), repr(k[1]), "->", amap[k])

if fails:
    print("\nFAILED:", ", ".join(fails))
    sys.exit(1)
print("\nOK: coverage complete and totals conserved")
