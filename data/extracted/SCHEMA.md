# Extracted budget data: shared shape

One folder per budget book: `data/extracted/<book_id>/`, where `book_id` is
`<fiscal year>-operating-<adopted|proposed>` (e.g. `2019-operating-adopted`).

All files are long-format UTF-8 CSV with a header row. Amounts are whole dollars
as integers (negatives with a leading `-`; printed parentheses mean negative).
Blank or dash cells in the book are omitted, not written as 0, unless the book prints `0`.
Names are kept exactly as printed (trimmed, internal whitespace collapsed); do not
normalise or merge departments across years. `page` is the 1-based PDF page index
(for OpenBook books, the page title instead).

Every book reports several fiscal years side by side (prior-year actuals, current-year
adopted, next-year proposed/adopted). Capture every column, one row per cell.

Common columns on every file:

| column | meaning |
|---|---|
| `book_id` | as above |
| `fiscal_year` | the year the amount is for (not the book's year) |
| `basis` | `actual`, `adopted`, `proposed`, `estimate` or `projected`, from the column header |
| `page` | where the number is printed |

## department_expenditures.csv  (citywide statements)

One row per department per fund group per year column.

`book_id, fiscal_year, basis, fund_group, dept_code, dept_name, amount, page`

- `fund_group`: `general_fund`, `other_funds` or `all_funds`, matching the three citywide statements.
- `dept_code`: the printed department/agency number if any, else blank.
- Printed total rows are NOT included here; they go to `printed_totals.csv`.

## revenues.csv  (citywide statement of revenues)

`book_id, fiscal_year, basis, fund_group, fund_name, category, line_name, amount, page`

- `fund_name`: printed fund name (`General Fund`, or the specific other fund).
- `category`: the printed section heading the line sits under (e.g. `Taxes`, `Licenses & Permits`); blank if none.
- Subtotal and total rows go to `printed_totals.csv`, not here.

## department_detail.csv  (each department's own section)

`book_id, fiscal_year, basis, dept_code, dept_name, table, dim1, dim2, amount, page`

- `table`: one of
  - `by_type` — expenditure by type (dim1 = e.g. `Personal Services`, `Other Operating`, `Debt Service`)
  - `by_fund` — expenditure by funding source (dim1 = fund name)
  - `by_program` — program / cost-centre detail (dim1 = program code + name as printed, dim2 = fund or expenditure type if the table is split that way, else blank)
  - `by_program_type` — program detail split by expenditure type (dim1 = program, dim2 = `fund | type`)
  - `dept_summary_by_type`, `dept_summary_by_fund` — a department-level summary page that sits
    above sub-sections which have their own `by_type` / `by_fund` (e.g. Mayor vs Mayor- Core).
    Never sum these together with the sub-section rows: that double counts.
- Totals go to `printed_totals.csv`.
- `dept_code` is blank in practice: the 2012, 2019 and 2026 books print no department numbers.
- A book prints the same department under different names in different tables. Names stay as
  printed here; unifying them is the job of a separate crosswalk, not the extraction.

## workyears_citywide.csv  (optional)

The citywide multi-year position table where a book has one (2012: 2004–2012).

## positions.csv  (if the book has position / FTE tables)

`book_id, fiscal_year, basis, dept_code, dept_name, program, fund, position_title, pay_grade, fte, page`

Use the finest level the book prints; leave unavailable columns blank. `fte` is a decimal.

## printed_totals.csv  (every total the book itself prints)

`book_id, fiscal_year, basis, scope, label, amount, page`

- `scope`: which file and slice it totals, e.g. `department_expenditures/general_fund`,
  `revenues/General Fund/Taxes`, `department_detail/<dept_name>/by_type`.
- `revenues/General Fund/summary_by_category` (2025 books on): the book's summary of
  General Fund income, one row per printed category plus `Grand Total`. A category's
  figure here can differ from the sum of its lines.

## checks.json

Written by the extraction script on every run: for each printed total, the sum of the
extracted rows it covers, the printed figure, and the difference. Also cross-checks:
general_fund + other_funds = all_funds per department and year; each department's
`by_type` total = its citywide `all_funds` figure for the same year and basis.
Include counts of rows per file and a list of pages or tables that could not be parsed.
