#!/usr/bin/env python3
"""Extract City of New Orleans rows from Census Annual Survey of State & Local Government Finances
individual-unit files downloaded by fetch_open_data.py. Output (long format, $ thousands):
  data/raw/open-data/census-gov-finances/new_orleans_city_finances_long.csv  [year, source, variable_or_item_code, value]
Run: python3 -I scripts/extract_census_new_orleans.py
"""
import csv, io, os, re, zipfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
D = os.path.join(ROOT, "data/raw/open-data/census-gov-finances")
rows = []
NO_IDS = {"192036001"}  # City of New Orleans (pre-2008 govs ID); later years matched by name
# --- 1967-2012: wide CSV files (a,b,c per year), one row per government
z = zipfile.ZipFile(os.path.join(D, "IndFin_1967-2012.zip"))
for n in sorted(z.namelist()):
    m = re.match(r"IndFin(\d\d)([abc])\.Txt", n, re.I)
    if not m: continue
    yy = int(m.group(1)); yr = 2000 + yy if yy < 30 else 1900 + yy
    rd = csv.reader(io.StringIO(z.read(n).decode("latin-1")))
    hdr = next(rd)
    idx_id = hdr.index("ID") if "ID" in hdr else 3
    idx_name = hdr.index("Name") if "Name" in hdr else None
    for r in rd:
        if len(r) < len(hdr): continue
        if r[idx_id].strip()[:9] in NO_IDS or (idx_name is not None and r[idx_name].upper().startswith("NEW ORLEANS CITY")):
            for h, v in zip(hdr, r):
                if h in ("SortCode", "ID", "Name"): continue
                rows.append((yr, f"IndFin{yy:02d}{m.group(2)}", h, v.strip()))
# --- 2013-2024: fixed-width item-code files
for y in range(2013, 2025):
    zz = zipfile.ZipFile(os.path.join(D, f"indunit_{y}.zip"))
    datf = [n for n in zz.namelist() if re.search(r"FinEstDAT.*\.txt$", n, re.I)][0]
    pidf = [n for n in zz.namelist() if re.search(r"Fin_[PG]ID_\d{4}\.txt$", n, re.I)][0]
    gid = None
    idlen = 14 if "_GID_" in pidf.upper() else 12   # 2013-2016 use 14-char IDs; later 12-char
    for l in zz.read(pidf).decode("latin-1").splitlines():
        if "NEW ORLEANS CITY" in l.upper(): gid = l[:idlen]; break
    for l in zz.read(datf).decode("latin-1").splitlines():
        if l.startswith(gid):
            rows.append((y, os.path.basename(datf), l[idlen:idlen+3], l[idlen+3:idlen+15].strip()))
with open(os.path.join(D, "new_orleans_city_finances_long.csv"), "w", newline="") as f:
    w = csv.writer(f); w.writerow(["year", "source_file", "variable_or_item_code", "value_thousands_usd_or_raw"]); w.writerows(rows)
print(len(rows), "rows; years", sorted({r[0] for r in rows}))
