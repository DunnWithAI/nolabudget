# The data pipeline

How the city's budget books become `data/combined/*.csv`, the only data the site reads.
You only need this to add a new budget book or fix a number. Building the site does not
run any of it.

Install the extra packages first:

    .venv/bin/pip install -r requirements-extract.txt

Run every command from the project root.

## Stages, in order

### 1. Get the source files (writes `data/raw/`)

`data/raw/` is read-only once downloaded. Most stages skip files that already exist.

| Command | What it does |
| --- | --- |
| `python3 -I scripts/fetch_open_data.py [--force]` | Downloads data.nola.gov datasets, Census population and finance files, BLS inflation (CPI) and City Council files. Writes `data/raw/open-data/`, `data/raw/context/`. |
| `python3 -I scripts/extract_census_new_orleans.py` | Pulls New Orleans rows out of the Census finance downloads. |
| `python3 -I scripts/build_context_series.py` | Writes the population and inflation series that `build_combined.py` reads. |
| `python3 -I scripts/finalize_manifest.py` | Adds non-data.nola.gov files to `data/raw/open-data/manifest.json`. |
| `python3 -I scripts/fetch_questica.py [--no-pdf] [--only <id>]` | Downloads the 2025-2027 books from the city's OpenBook (Questica) site into `data/raw/questica/`. |

Not scripted:

- **2011-2024 PDFs** in `data/raw/nola-gov/` were downloaded by hand. Only the manifest
  records where they came from.
- **Pre-2011 books** came from one-off scrapers in `scripts/pre-2011/` (Wayback Machine,
  New Orleans Public Library, Legislative Auditor). They depend on working files that
  are no longer here, must run from inside that folder, and some still hold an old
  absolute path (`/Users/andrewdunn/Library/CloudStorage/Dropbox/...`). Treat them as a
  record of what was done, not something to re-run.
- `data/raw/pre-2011/manifest.json` and `data/raw/acfr/manifest.json` store that same
  old absolute path for each file. The other manifests use relative paths.

### 2. OCR the 2008 book (macOS only, already done)

The 2008 book is a scan. Its OCR is cached in `data/interim/ocr/2008/`, so you only need
this if that cache is deleted.

    swiftc -O scripts/extract/ocr_vision.swift -o scripts/extract/bin/ocr_vision
    .venv/bin/python scripts/extract/ocr_2008.py [first_page last_page]

### 3. Extract each book (writes `data/extracted/<book_id>/`)

One script per book. The output shape is in [data/extracted/SCHEMA.md](../data/extracted/SCHEMA.md);
the rules are in [scripts/extract/README.md](../scripts/extract/README.md). In short:
never type a number in by hand, never loosen a tolerance, and when the book itself is
wrong, record the mismatch instead of fixing it.

    .venv/bin/python scripts/extract/extract_2019.py        # 2008, 2009, 2011-2024 each have one
    .venv/bin/python scripts/extract/extract_2026.py [<questica book dir>] [--book-id ID]

- `extract_2026.py` handles every OpenBook (Questica) book. With no argument it does
  2026 adopted. For 2025 adopted and proposed, 2026 proposed and 2027 proposed, pass the
  book's folder under `data/raw/questica/`; each book's `checks.json` records the folder
  used.
- `extract_2022.py` and `extract_2024.py` reuse the engine in `extract_2023.py`.
- Each run writes `checks.json`: every printed total against the sum of extracted rows.
- Optional spot check against the PDF: `.venv/bin/python scripts/verify_extracted.py <book_id>`.
- `scripts/parse_questica_tables.py` is an earlier approach that `extract_2026.py` replaced.

### 4. Match names across years (`data/crosswalk/`)

Books print the same department under different names. The crosswalk gives each one
stable ID.

| File | Made by |
| --- | --- |
| `departments.csv` | Hand. One row per department: ID, name, group. |
| `department_aliases_statement.csv` | Hand. (book, printed name) to department ID, for the citywide tables. |
| `department_aliases_detail.csv` | Hand. Same, for department detail and positions. A blank ID means skip. |
| `corrections.csv` | Hand. Fixes column labels that a book itself prints wrongly. |
| `program_groups.csv` | Hand. Program code to a plain group and name, for the "Inside the department" section. Read by the site build only. |
| `revenue_sources.csv`, `revenue_aliases.csv`, `revenue_rollups.csv` | `.venv/bin/python scripts/build_crosswalk_revenue.py`, from rules in that script. |

Check coverage (each exits 1 if anything is unmatched):

    .venv/bin/python scripts/check_crosswalk_statement.py
    .venv/bin/python scripts/check_crosswalk_detail.py
    .venv/bin/python scripts/check_crosswalk_revenue.py

### 5. Combine (writes `data/combined/`)

    .venv/bin/python scripts/build_combined.py

Fails if any printed name has no mapping. `build_report.json` lists counts, corrections
applied and anything unmapped.

| File | What it is |
| --- | --- |
| `spending.csv` | One chosen figure per department, fund, year and basis (proposed, adopted, revised, actual). The site's main table. |
| `spending_observations.csv` | Every printing of each figure, across all books, before choosing. |
| `revenue.csv`, `revenue_observations.csv` | The same for income lines. `comparable_id` follows a line across years. |
| `department_detail.csv` | Each department's own tables: by type of cost, by fund, by program. |
| `positions.csv`, `positions_by_department.csv` | Budgeted jobs (FTE). |
| `city_comparison.csv` | Police and fire spending for comparison cities, from Census (stage 6). |
| `context.csv` | Population and inflation by year, for per-resident and inflation-adjusted views. |

Then rebuild the site: `.venv/bin/python site/build.py`.

### 6. City comparisons (writes `data/combined/city_comparison.csv`)

Separate from the budget books. Spending on seven everyday services (police, fire,
streets, trash and recycling, parks and recreation, libraries, public health) for New
Orleans, every U.S. city
government with 250,000 to 750,000 residents, and Louisiana cities with 50,000 or more,
from the Census Bureau's 2022 Census of Governments (public domain).

    # once: download and unzip into data/raw/census-finance-2022/
    # https://www2.census.gov/programs-surveys/gov-finances/tables/2022/2022_Individual_Unit_File.zip
    python3 -I scripts/build_city_comparison.py

- Spending on a service is current operations plus construction plus other capital
  (Census item codes E, F and G with the function code: 62 police, 24 fire, 44 streets,
  81 trash, 61 parks, 52 libraries, 32 health).
- Each service was checked against New Orleans's 2022 actual spending in the budget
  books before it went in. Police, fire, libraries, health and streets are close. Trash
  and parks differ for reasons the page explains. Housing ($301 million in Census against
  $60 million in the books) and courts ($105 million against about $14 million) could
  not be reconciled and are left out. Check any new service the same way.
- Police and fire are `STRICT`: every city runs them, so a zero stops the script. For
  the other services a zero usually means a county or district runs it; the site leaves
  those cities out of that comparison and names them.
- Per resident uses Census's own population for each government (389,476 for New
  Orleans), not the population used elsewhere on the site.
- `EXCLUDE` in the script lists cities left out of a comparison, with the reason the page
  prints (for example, Las Vegas police are a separate joint agency). The script stops if
  any city reports zero for a service, so every gap gets a reason.
- The Lincoln Institute's Fiscally Standardized Cities data would be a fairer comparison,
  but its terms don't allow republishing without written permission.
- When Census publishes the 2027 Census of Governments, download it, update the file
  names and `YEAR` (in `site/nolabudget/compare.py`), and rerun.

## Adding a new budget book

1. Download it into `data/raw/` (OpenBook books: `fetch_questica.py`).
2. Extract it with `extract_2026.py` (OpenBook) or a new extractor. Read `checks.json`.
3. Run the three crosswalk checks. Add alias rows by hand for every new printed name;
   add rules to `build_crosswalk_revenue.py` for new revenue lines.
4. Run `build_combined.py`, then the site build.
5. If the book starts a new budget year, update `site/content/site.yml`.

## Known gaps

- No 2010 book survives. 2010 adopted figures come from the 2011 book.
- 2020 adopted figures come from the 2021 book; the 2020 book on file is the proposal,
  and its PDF has broken text encoding.
- The 2011 book has no all-funds table, and the 2008 and 2009 books have no other-funds
  table. `build_combined.py` derives those totals and marks them as computed.
