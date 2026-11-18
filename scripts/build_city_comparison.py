# ABOUTME: Builds data/combined/city_comparison.csv from the Census Bureau's 2022 local government finance file.
# ABOUTME: Spending on seven everyday services for New Orleans, similar-size U.S. cities and Louisiana cities.
"""City comparison data from the Census Bureau's 2022 Census of Governments.

    python3 -I scripts/build_city_comparison.py

Reads data/raw/census-finance-2022/2022_Individual_Unit_files/ (download and unzip
https://www2.census.gov/programs-surveys/gov-finances/tables/2022/2022_Individual_Unit_File.zip
there; record layouts are in the technical documentation PDF inside it).

Writes data/combined/city_comparison.csv: one row per city government in either group, with
its spending on each service in FUNCTIONS:
  - peer: U.S. municipal governments with 250,000 to 750,000 residents (Census's own count)
  - louisiana: Louisiana municipal governments with 50,000 or more residents
Amounts are dollars. Spending on a function is its direct spending: current operations (E)
plus construction (F) plus other capital outlay (G) for that function code. Census figures
are for each government's own 2022 fiscal year. Census data are in the public domain.
"""
import csv
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "raw", "census-finance-2022", "2022_Individual_Unit_files")
OUT = os.path.join(ROOT, "data", "combined", "city_comparison.csv")

PID = "Fin_PID_2022.txt"
DAT = "2022FinEstDAT_07152026modp.txt"
# Services compared, by Census function code. Each was checked against New Orleans's own
# budget books for 2022 (see docs/data-pipeline.md); services whose Census figure for New
# Orleans could not be reconciled with the books (housing, courts) are left out.
FUNCTIONS = {"police": "62", "fire": "24", "streets": "44", "trash": "81", "parks": "61",
             "libraries": "52", "health": "32"}
# Every city government runs these itself, so a zero means missing data and stops the
# script. For the others a zero usually means a county or district provides the service;
# the site leaves those cities out of that comparison and says how many.
STRICT = {"police", "fire"}
PEER_MIN, PEER_MAX, LA_MIN = 250000, 750000, 50000
NEW_ORLEANS = "222071166094"

STATES = {  # FIPS state codes, as in the first two characters of a Census government ID
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
}
# Census government names that the general rule below would get wrong.
NAMES = {
    "NASHVILLE-DAVIDSON COUNTY METROPOLITAN GOVERNMENT": "Nashville",
    "LEXINGTON-FAYETTE URBAN COUNTY": "Lexington",
    "BATON ROUGE-EAST BATON ROUGE CITY-PARISH": "Baton Rouge",
    "LAFAYETTE CITY-PARISH": "Lafayette",
    "DENVER CITY AND COUNTY": "Denver",
    "WASHINGTON DC CITY": "Washington, D.C.",
    "ANCHORAGE MUNICIPALITY": "Anchorage",
    "GILBERT TOWN": "Gilbert",
    "ST LOUIS CITY": "St. Louis",
    "ST PAUL CITY": "St. Paul",
    "ST PETERSBURG CITY": "St. Petersburg",
}


# Left out of a comparison, with the reason the page shows. Keyed by Census government ID.
EXCLUDE = {
    "112001124214": {"all": "Washington, D.C. also does the work of a state government."},
    "322003169988": {"police": "Las Vegas police are run jointly with Clark County, as a separate government."},
    "062059161229": {"fire": "Irvine's fire service is run by the Orange County Fire Authority."},
    "392095205133": {"all": "Toledo reported no figures for 2022."},
}
# City governments that are also their county or parish government.
COMBINED = {"082031194647", "472037175728", "212067165831", "022020194463", "222033166054", "222055166076",
            "222071166094"}


def plain_name(raw):
    for k, v in NAMES.items():
        if raw.startswith(k[:28]):
            return v
    name = re.sub(r" CITY$", "", raw)
    return " ".join(w.capitalize() for w in name.split())


def main():
    govs = {}
    with open(os.path.join(SRC, PID), encoding="latin-1") as f:
        for line in f:
            gid, raw, pop = line[0:12], line[12:76].strip(), line[116:125].strip()
            if gid[2] != "2" or not pop.isdigit():  # municipal governments only
                continue
            pop = int(pop)
            state = STATES[gid[:2]]
            groups = []
            if PEER_MIN <= pop <= PEER_MAX:
                groups.append("peer")
            if state == "LA" and pop >= LA_MIN:
                groups.append("louisiana")
            if groups:
                govs[gid] = {"census_id": gid, "name": plain_name(raw), "printed_name": raw, "state": state,
                             "population": pop, "groups": " ".join(groups)}
    if NEW_ORLEANS not in govs:
        raise SystemExit("New Orleans is missing from %s" % PID)
    for g in govs.values():
        for k in FUNCTIONS:
            g[k] = 0
    codes = {c: k for k, c in FUNCTIONS.items()}
    with open(os.path.join(SRC, DAT), encoding="latin-1") as f:
        for line in f:
            gid = line[0:12]
            if gid not in govs:
                continue
            item = line[12:15]
            if item[0] in "EFG" and item[1:] in codes:
                govs[gid][codes[item[1:]]] += int(line[15:27]) * 1000
    for gid, g in govs.items():
        ex = EXCLUDE.get(gid, {})
        g["note"] = " ".join(sorted(set(ex.values())))
        for k in FUNCTIONS:
            if "all" in ex or k in ex:
                g[k] = ""
        g["combined"] = "yes" if gid in COMBINED else ""
    fields = ["census_id", "name", "state", "population", "groups", "combined"] + list(FUNCTIONS) + ["note", "printed_name"]
    rows = sorted(govs.values(), key=lambda g: -g["population"])
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for g in rows:
            w.writerow({k: g[k] for k in fields})
    print("wrote %d cities to %s" % (len(rows), OUT))
    for k in STRICT:
        zero = [g["name"] for g in rows if g[k] == 0]
        if zero:
            raise SystemExit("%s is zero for %s: add them to EXCLUDE with a reason" % (k, ", ".join(zero)))


if __name__ == "__main__":
    main()
