#!/usr/bin/env python3
"""Build derived context tables in data/raw/context from files fetched by fetch_open_data.py.
Outputs orleans_parish_population_2000_2025.csv and cpi_u_*_annual_avg_*.csv. Run: python3 -I scripts/build_context_series.py"""
import csv, glob, json, os, collections
C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data/raw/context")
pop = {}
for r in csv.DictReader(open(f"{C}/co-est00int-tot.csv", encoding="latin-1")):
    if r["STATE"] == "22" and r["COUNTY"] == "71":
        for y in range(2000, 2011): pop[y] = (int(r[f"POPESTIMATE{y}"]), "2000-2010 intercensal (co-est00int-tot)")
for fn, yrs, src in [("co-est2020-alldata.csv", range(2011, 2020), "vintage2020 (2010-2020 estimates)"),
                     ("co-est2025-alldata.csv", range(2020, 2026), "vintage2025 (2020-2025 estimates)")]:
    for r in csv.DictReader(open(f"{C}/{fn}", encoding="latin-1")):
        if r["STATE"] == "22" and r["COUNTY"] == "071":
            for y in yrs: pop[y] = (int(r[f"POPESTIMATE{y}"]), src)
with open(f"{C}/orleans_parish_population_2000_2025.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["year", "population_july1", "source_vintage"])
    for y in sorted(pop): w.writerow([y, *pop[y]])
for sid, name in [("CUUR0000SA0", "us_city_avg"), ("CUUR0300SA0", "south_region")]:
    m = collections.defaultdict(dict)
    for fn in glob.glob(f"{C}/bls_{sid}_*.json"):
        for s in json.load(open(fn))["Results"]["series"]:
            for d in s["data"]:
                if d["period"].startswith("M") and d["period"] != "M13" and d["value"] not in ("-", ""):
                    m[int(d["year"])][int(d["period"][1:])] = float(d["value"])
    with open(f"{C}/cpi_u_{name}_annual_avg_{sid}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["year", "annual_avg_index_1982-84=100", "months_available"])
        for y in sorted(m): w.writerow([y, round(sum(m[y].values()) / len(m[y]), 3), len(m[y])])
