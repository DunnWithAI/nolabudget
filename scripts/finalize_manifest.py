#!/usr/bin/env python3
"""Append non-Socrata entries to data/raw/open-data/manifest.json (idempotent: replaces entries with the same slug).
Run after fetch_open_data.py + extract_census_new_orleans.py + build_context_series.py:  python3 -I scripts/finalize_manifest.py"""
import csv, hashlib, json, os
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MP = os.path.join(ROOT, "data/raw/open-data/manifest.json")
def info(rel):
    p = os.path.join(ROOT, rel); h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return rel, os.path.getsize(p), h
def csvmeta(rel):
    with open(os.path.join(ROOT, rel), newline="", encoding="utf-8") as f:
        rd = csv.reader(f); cols = next(rd); n = sum(1 for _ in rd)
    return cols, n
E = []
def add(slug, title, url, rel, years, notes, did="", rows=None, cols=None, upd=""):
    path, b, h = info(rel)
    if rows is None and rel.endswith(".csv"): cols, rows = csvmeta(rel)
    E.append(dict(slug=slug, title=title, source_url=url, dataset_id=did, path=path, bytes=b, sha256=h, rows=rows, columns=cols or [], years_covered=years, updated_at=upd, relevance_notes=notes))
cg = "data/raw/open-data/census-gov-finances"
add("census-newor-city-finances-long", "Census Annual Survey of State & Local Govt Finances - City of New Orleans (extracted, long format)",
    "https://www.census.gov/programs-surveys/gov-finances/data/datasets.html", f"{cg}/new_orleans_city_finances_long.csv", "1967,1970-2024",
    "Derived by scripts/extract_census_new_orleans.py. FY1967-2012: variable names (wide->long); FY2013-2024: Census item codes (e.g. T01 property tax, T09 general sales tax, E62...). $ thousands. Consolidated city-parish function-level revenue/expenditure on one consistent GASB-ish Census classification; NOT budget (actuals, all funds, includes some component units per Census rules). 2017-2018 look imputed/rounded.")
add("census-indfin-1967-2012", "Census Historical Finances of Individual Governments 1967-2012 (all US local govts; zip)", "https://www.census.gov/programs-surveys/gov-finances/data/historical-data.html", f"{cg}/IndFin_1967-2012.zip", "1967,1970-2012", "Raw archive; New Orleans = ID 192036001 (type 2). 550+ variables/yr. 262 MB.", rows=None)
for y in range(2013, 2025):
    add(f"census-indunit-{y}", f"Census Annual Survey individual unit file FY{y} (zip)", "https://www.census.gov/programs-surveys/gov-finances/data/datasets.html", f"{cg}/indunit_{y}.zip", str(y), "Raw FinEstDAT fixed-width + PID/GID name lookup + technical doc PDF.")
cd = "data/raw/open-data/council-budget-committee-xlsx"
add("council-fte-tracking-2020", "Council Budget Committee FTE Tracking Spreadsheet (2020)", "https://council.nola.gov/council/media/Assets/Committees/Budget/FTE-Tracking-Spreadsheet.xlsx", f"{cd}/FTE-Tracking-Spreadsheet.xlsx", "2020", "Sheets: Current / 2020 Adopted / Current-Non-General Fund / 2020 Adopted-Non-General Fund. Positions by agency, org, job code, filled vs vacant. Single 2020 snapshot (COVID mid-year).", rows=None)
add("council-expenses-template-2020", "Council Budget Committee Expenses Template (2020)", "https://council.nola.gov/council/media/Assets/Committees/Budget/Expenses-Template.xlsx", f"{cd}/Expenses-Template.xlsx", "2020", "Blank department data-call template (org code/fund code/PO). Low value; shows BRASS org+fund coding.", rows=None)
lg = "data/raw/open-data/council-legistar-budget-matters"
add("council-legistar-budget-matters", "City Council Legistar (cityofno) matters with Budget/Millage/Tax Levy in title", "https://webapi.legistar.com/v1/cityofno/matters", f"{lg}/matters_budget.json", "2024-10..2026-11", "Budget ordinances + amendments metadata (title, status, dates, sponsors). Legistar only holds matters from Oct 2024. Attachments are PDFs of ordinance text.", rows=358)
add("council-legistar-budget-attachments", "Legistar attachment links for budget ordinances", "https://webapi.legistar.com/v1/cityofno/matters/{id}/attachments", f"{lg}/attachments_by_matter.json", "2024-10..2026-11", "Attachment hyperlinks per ordinance matter (PDF).", rows=187)
add("boo-2012-offers-zip", "FY2012 Budgeting for Outcomes Budget Offers (zip of 218 docx + 24 pdf)", "https://nola.gov/nola/media/Mayor-s-Office/Budget/2012/2012_Budgeting_For_Outcomes_Budget_Offers.zip", "data/raw/open-data/boo-2012-offers-zip/2012_Budgeting_For_Outcomes_Budget_Offers.zip", "2012", "Program-level offers (cost, funding, FTE, results) grouped by outcome area. Word docs, semi-structured; would need docx parsing. (nola.gov-hosted; PDF team may also have it.)", rows=248)
cx = "data/raw/context"
add("ctx-population-orleans", "Orleans Parish population estimates 2000-2025", "https://www2.census.gov/programs-surveys/popest/datasets/", f"{cx}/orleans_parish_population_2000_2025.csv", "2000-2025", "Census PEP July-1 estimates spliced from 3 vintages (breaks at 2010/2011 and 2019/2020). 2006-2009 post-Katrina collapse; 2010=347,858.")
add("ctx-cpi-us", "CPI-U US city average, all items, NSA, annual avg (BLS CUUR0000SA0)", "https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0", f"{cx}/cpi_u_us_city_avg_annual_avg_CUUR0000SA0.csv", "1980-2026", "Computed from monthly; 2025 has 11 months (Oct 2025 missing), 2026 partial (10 months).")
add("ctx-cpi-south", "CPI-U South region, all items, NSA, annual avg (BLS CUUR0300SA0)", "https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0300SA0", f"{cx}/cpi_u_south_region_annual_avg_CUUR0300SA0.csv", "1980-2026", "No New Orleans metro CPI published by BLS; South region is closest.")
m = [x for x in json.load(open(MP)) if x["slug"] not in {e["slug"] for e in E}]
json.dump(m + E, open(MP, "w"), indent=1); print(len(m + E), "entries")
