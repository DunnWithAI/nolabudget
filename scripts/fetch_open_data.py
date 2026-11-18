#!/usr/bin/env python3
"""Fetch City of New Orleans open-data (Socrata) datasets relevant to budget/finance,
plus context series (population, CPI). Public data, no keys. Writes
data/raw/open-data/<slug>/{rows.csv,meta.json}, data/raw/context/*, and manifest.json.
Run: python3 -I scripts/fetch_open_data.py   (re-runnable; skips existing files unless --force)
"""
import csv, hashlib, json, os, sys, time, urllib.request, urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OD = os.path.join(ROOT, "data/raw/open-data")
CTX = os.path.join(ROOT, "data/raw/context")
UA = "Mozilla/5.0 (compatible; civic-budget-research/1.0)"
FORCE = "--force" in sys.argv
MAX_BYTES = 300 * 1024 * 1024

# slug, id, relevance, year_col (column name to derive years from, optional)
DATASETS = [
 ("sales-tax-revenue-2003", "qx7d-6vrr", "Annual General Fund sales/motor-vehicle/hotel-motel tax revenue 2003-present from CAFR. Revenue context series.", "Year"),
 ("resultsnola-historic", "jqyq-axtx", "ResultsNOLA KPIs (performance measures by department/strategy). No dollars; useful for service-level context.", "Year"),
 ("city-data-inventory", "dhg4-3vbu", "Catalog of City datasets incl. which finance/budget data exist internally but are unpublished.", None),
 ("roadwork-dpw-projects", "u5mk-y9z4", "Capital road projects with Total_Cost, phase, contractor (current snapshot).", None),
 ("roadwork-swb-projects", "bsnu-de3a", "SWBNO/DPW capital program project segments (geo; no costs).", None),
 ("adjudication-fines-fees", "f5zm-xkyr", "Fines/fees levied in adjudication hearings since 2018 (Amount, D_Paid): revenue-side micro data.", None),
 ("city-jobs-hiring", "wx2k-rsac", "Civil Service hire actions since 2019 by department/job title (no salaries).", "Start_Date"),
 ("city-jobs-requisitions", "m99x-bsvt", "Position requisitions by department (no salaries).", "Received_Date"),
 ("hud-grants", "rtej-a36y", "HUD-funded OCD program locations (no dollar amounts).", None),
 ("municipal-building-energy", "dm3n-f3pu", "Monthly energy use of city buildings (context for utilities costs).", None),
]

def get(url, dest=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            if dest:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                open(dest, "wb").write(data)
            return data
        except Exception as e:
            print("  retry", url, e); time.sleep(3 * (i + 1))
    return None

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def years_in(path, col):
    if not col: return ""
    ys = set()
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            v = (row.get(col) or "")
            for tok in (v[:4], v[-4:]):
                if tok.isdigit() and 1990 <= int(tok) <= 2030: ys.add(int(tok)); break
    return f"{min(ys)}-{max(ys)}" if ys else ""

def main():
    manifest = []
    for slug, did, notes, ycol in DATASETS:
        d = os.path.join(OD, slug)
        meta_p, csv_p = os.path.join(d, f"{did}.json"), os.path.join(d, f"{slug}.csv")
        if FORCE or not os.path.exists(meta_p):
            print("meta", did); get(f"https://data.nola.gov/api/views/{did}.json", meta_p); time.sleep(1)
        meta = json.load(open(meta_p))
        if FORCE or not os.path.exists(csv_p):
            print("csv ", did); get(f"https://data.nola.gov/api/views/{did}/rows.csv?accessType=DOWNLOAD", csv_p); time.sleep(1)
        with open(csv_p, newline="", encoding="utf-8", errors="replace") as f:
            rd = csv.reader(f); cols = next(rd); n = sum(1 for _ in rd)
        import datetime
        upd = meta.get("rowsUpdatedAt")
        upd = datetime.datetime.utcfromtimestamp(upd).strftime("%Y-%m-%d") if upd else ""
        manifest.append(dict(slug=slug, title=meta.get("name"), source_url=f"https://data.nola.gov/d/{did}", dataset_id=did,
            path=os.path.relpath(csv_p, ROOT), meta_path=os.path.relpath(meta_p, ROOT), bytes=os.path.getsize(csv_p), sha256=sha(csv_p),
            rows=n, columns=cols, years_covered=years_in(csv_p, ycol), updated_at=upd, relevance_notes=notes))
    json.dump(manifest, open(os.path.join(OD, "manifest.json"), "w"), indent=1)
    print("manifest written", len(manifest))

    # ---- context series (no API keys needed) ----
    W = "https://www2.census.gov/programs-surveys/popest/datasets"
    for name, url in [
        ("co-est00int-tot.csv", f"{W}/2000-2010/intercensal/county/co-est00int-tot.csv"),
        ("co-est2020-alldata.csv", f"{W}/2010-2020/counties/totals/co-est2020-alldata.csv"),
        ("co-est2025-alldata.csv", f"{W}/2020-2025/counties/totals/co-est2025-alldata.csv"),
    ]:
        p = os.path.join(CTX, name)
        if FORCE or not os.path.exists(p):
            print("ctx", name); get(url, p); time.sleep(1)
    # BLS public API v1 (no key; max 10 yrs per request). CPI-U all items NSA: US city avg and South region
    import urllib.request as ur
    for sid in ("CUUR0000SA0", "CUUR0300SA0"):
        for s0 in (1980, 1990, 2000, 2010, 2020):
            e0 = min(s0 + 9, 2026)
            p = os.path.join(CTX, f"bls_{sid}_{s0}_{e0}.json")
            if FORCE or not os.path.exists(p):
                req = ur.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                    data=json.dumps({"seriesid": [sid], "startyear": str(s0), "endyear": str(e0)}).encode(),
                    headers={"User-Agent": UA, "Content-type": "application/json"})
                open(p, "wb").write(ur.urlopen(req, timeout=60).read()); time.sleep(1)
    # derived annual tables (population by year; CPI annual averages) are built by scripts/build_context_series.py

    # ---- Census Annual Survey of State & Local Government Finances: individual unit files (incl. City of New Orleans) ----
    CG = "https://www2.census.gov/programs-surveys/gov-finances"
    cdir = os.path.join(OD, "census-gov-finances")
    units = {
        2024: "tables/2024/2024_Individual_Unit_Files.zip", 2023: "tables/2023/2023_Individual_Unit_Files.zip",
        2022: "tables/2022/2022_Individual_Unit_File.zip", 2021: "tables/2021/2021_Individual_Unit_File.zip",
        2020: "tables/2020/2020_Individual_Unit_File.zip", 2019: "tables/2019/2019_Individual_Unit_File.zip",
        2018: "tables/2018/2018_Individual_Unit_File.zip", 2017: "tables/2017/2017_Individual_Unit_File.zip",
        2016: "datasets/2016/public-use-datasets/2016_Individual_Unit_file.zip",
        2015: "datasets/2015/public-use-datasets/2015-individual-unit-file.zip",
        2014: "datasets/2014/public-use-datasets/2014-individual-unit-file.zip",
        2013: "tables/2013/summary-tables/2013_Individual_Unit_file.zip",
    }
    for y, rel in units.items():
        p = os.path.join(cdir, f"indunit_{y}.zip")
        if FORCE or not os.path.exists(p):
            print("census", y); get(f"{CG}/{rel}", p); time.sleep(1)
    p = os.path.join(cdir, "IndFin_1967-2012.zip")
    if FORCE or not os.path.exists(p):
        print("census hist"); get(f"{CG}/datasets/historical/_IndFin_1967-2012.zip", p)
    # Council Budget/Audit/BOR committee workbooks (2020 COVID-era FTE + expense templates)
    for f in ("Expenses-Template.xlsx", "FTE-Tracking-Spreadsheet.xlsx"):
        p = os.path.join(OD, "council-budget-committee-xlsx", f)
        if FORCE or not os.path.exists(p):
            get(f"https://council.nola.gov/council/media/Assets/Committees/Budget/{f}", p); time.sleep(1)
    # Legistar Web API (cityofno): budget-related matters (Oct 2024 onward only)
    lp = os.path.join(OD, "council-legistar-budget-matters", "matters_budget.json")
    if FORCE or not os.path.exists(lp):
        import urllib.parse as up
        q = up.urlencode({"$filter": "substringof('Budget',MatterTitle) or substringof('Millage',MatterTitle) or substringof('Tax Levy',MatterTitle)",
                          "$orderby": "MatterIntroDate asc", "$top": "1000"})
        get("https://webapi.legistar.com/v1/cityofno/matters?" + q, lp)

if __name__ == "__main__":
    main()
