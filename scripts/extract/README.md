# Extracting a New Orleans operating budget book

Each book gets one deterministic script, `scripts/extract/extract_<year>.py`, that reads the
raw book and writes `data/extracted/<book_id>/` in the shape defined in
`data/extracted/SCHEMA.md`. Run with the project venv: `.venv/bin/python scripts/extract/extract_<year>.py`.

## Rules

- `data/raw` is read-only. No hand-typed numbers in scripts or outputs.
- Start from the pilot script whose book looks most like yours; copy it to a new
  `extract_<year>.py` and adapt. Do not edit another year's script (other agents are working
  in parallel on other years) — if you want shared code, import from a pilot script read-only.
  - `extract_2012.py` — 2012 adopted PDF (statements with 2004–2012 columns, program-number tables)
  - `extract_2019.py` — 2019 adopted PDF (title-located pages, "DEPARTMENTAL BUDGET SUMMARY" pages)
  - `extract_2026.py` — OpenBook page JSON (book folder is a parameter)
- Names stay exactly as printed. Do not unify department names across tables or years.
- Tiers, in priority order. Finish and check each before the next:
  1. citywide statements: revenues by source, expenditures by department (general fund, other funds, all funds), every year column
  2. each department's summary by expenditure type and by fund
  3. program-level detail
  4. positions / FTE
- If part of a tier cannot be parsed reliably, extract what is reliable and list the rest
  under `unparsed` in `checks.json`. Never guess.

## Known pitfalls (from the pilot)

- Plain text order scrambles columns. Use word coordinates; assign a number to a column by
  x-position against the header, not by sequence.
- "Page N" footers can fall inside a column and be read as an amount. Strip them.
- Row labels wrap above or below the numeric line; kerning splits words ("Parkw ays").
- Totals sometimes carry no "TOTAL" word; fund headings can be lost at page breaks.
- Some books repeat pages verbatim (2019 pp.143–145 = 146–148). Detect and skip, or FTEs double.
- Locate tables by title anywhere on the page, not by fixed page number alone.
- Negatives are in parentheses. Dashes/blank cells are omitted; a printed 0 is kept.

## Checks (the gate)

`checks.json` must contain, per tier: every printed total vs the sum of extracted rows it
covers (exact / within $1 / mismatch, with the figures), and cross-checks: general + other =
all funds per department and year; department section totals vs the citywide statement;
FTE totals vs printed. Then spot-check at least 15 random cells per book against the page
using a different text engine than the extractor (pymupdf vs pdfplumber) or the page image.

A mismatch that is the book's own error stays a mismatch and is reported. Do not loosen
tolerances or special-case values to make checks pass.

## Report format (per book)

Rows per file; printed-total reconciliation per tier (exact / within $1 / mismatch);
adopted General Fund and all-funds expenditure totals and General Fund revenue total, as
printed, with page; year columns captured; what is unparsed and why; inconsistencies inside
the book; anything that deviates from SCHEMA.md.
