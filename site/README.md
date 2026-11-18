# NOLA Budget: the site

A static site that explains the City of New Orleans budget. Python builds every page
from `data/combined/*.csv` into `dist/`. No Node, no JS framework, no requests at runtime.

    .venv/bin/python site/build.py          # build into dist/, then run site/checks.py
    .venv/bin/python -m http.server 4321 --directory dist    # preview at http://localhost:4321

The build fails if `site/checks.py` finds an error (wrong headline totals, broken
internal links, "nan"/"None"/"undefined" or unrendered template tags in a page).

`dist/` sits in a Dropbox folder, so the build writes files in place (only when changed)
and then deletes anything it did not write. Never `rm -rf dist` while Dropbox is
syncing: that is what creates "conflicted copy" files.

## Layout

    site/build.py              entry point: runs each page type, copies static files and CSVs, runs checks
    site/checks.py             correctness gate
    site/nolabudget/data.py    Budget: every lookup, the rules for choosing figures, citations
    site/nolabudget/fmt.py     number formats (also the Jinja filters)
    site/nolabudget/charts.py  inline SVG line charts (wide and narrow drawings)
    site/nolabudget/views.py   chart figures with switchable views, each with its table
    site/nolabudget/detail.py  department breakdowns from department_detail.csv
    site/nolabudget/common.py  shared computations (the moment, per-department changes)
    site/nolabudget/site.py    Site: content, templates, notes, page writer
    site/nolabudget/seo.py     robots.txt, sitemap.xml, llms.txt, _headers (see docs/seo.md)
    site/nolabudget/pages/     one module per page type (home, departments, budgets, changes, revenue, about)
    site/templates/            Jinja templates; partials/macros.html has the shared pieces
    site/static/               style.css, site.js, favicon.svg
    site/content/              hand-written text: site.yml, departments.yml, glossary.yml,
                               notes.yml, revenue_categories.yml

## Adding a page type

1. Write `site/nolabudget/pages/<name>.py` with a `build(site)` function. Get numbers
   from `site.budget` (never read the CSVs directly) and call
   `site.page(url, template, title, **context)` once per page. URLs start and end with `/`.
2. Write `site/templates/<name>.html`, extending `base.html` and importing
   `partials/macros.html as m`. Give it a `{% block description %}` (one plain sentence
   for search results) and, below Home, a breadcrumb trail as
   `{% set crumbs = [("/", "Home"), (url, "Label")] %}`; base.html draws the trail and
   its structured data. Search basics (titles, canonical URLs, sitemap, noindex) are
   explained in [docs/seo.md](../docs/seo.md).
3. Add the module to `PAGES` in `site/nolabudget/pages/__init__.py`.
4. If the page states headline figures, call `site.publish(url, name=value, ...)` and add
   a comparison against `spending.csv` (or `revenue.csv`) in `site/checks.py`.
5. Run the build. Fix every check failure; then look at the page in light and dark and
   at phone width.

Three page types exist only as minimal first versions, so links resolve. Replace them,
keeping their URLs:

- **Year pages** `/budgets/<year>/` (`pages/budgets.py`, `year.html`): currently a
  department table and sources. Room for: revenue for the year, proposed vs adopted vs
  actual, what changed from the year before.
- **Changes** `/changes/` (`pages/changes.py`, `changes.html`): currently every
  department's change, General Fund and all funds. Room for: program-level changes,
  proposed vs adopted, what moved between departments (only where the books say so).
- **Revenue** `/revenue/` and `/revenue/<slug>/` (`pages/revenue.py`): currently the
  proposal year vs the year in force. Room for: history over time (use `comparable_id`
  in revenue.csv to follow a line across books; actuals run 2004 to 2022), charts with
  `views.money_figure`, per-line descriptions from `data/crosswalk/revenue_sources.csv`.
  The nine category slugs are fixed in `content/revenue_categories.yml`.

## Conventions

**Words.** Follow `/VOICE.md`. Plain words, short sentences, no jargon, no hype, no
opinion. Say what the data shows and stop. If the books do not say why something
changed, say that.

**The moment.** `content/site.yml` sets `proposal_year` (2027), `in_force_year`
(2026), `adoption_deadline` and `stage` (which step of the yearly cycle is current). Never hard-code those years in templates or code; read `site.config`.

**General Fund first.** Every page leads with the General Fund and offers all funds
second. Explain "all funds" by linking to `/#general-fund`; do not re-explain it.

**Numbers.**
- Prose: `{{ x|money }}` gives "$799.8 million" (always millions above $1 million,
  so $1,592.4 million, not billions). The only exception is the home page hero number,
  `{{ x|money_hero }}` ("$1.47 billion").
- Tables: full dollars with `{{ x|full }}`, caption or note says "in dollars". The
  home page's long table uses `|millions` and says "in millions of dollars".
- Changes: `|signed_full`, `|signed_pct`, `fmt.change(new, old)` ("up $5.1 million (3.1%)").
- Shares: `|share` (gives "<1%" instead of "0%").
- A missing figure is `None`, never 0. In tables use `{{ m.num(value) }}`, which prints a
  dash with "not printed" for screen readers. Explain dashes: "not printed as its own
  line", never "zero".

**Sources.** Every figure needs a citation. Use `site.budget.cite(book_id, page, derived)`
or `site.budget.total_source(year, basis, fund)` and render with `{{ m.cite(c) }}` or
`{{ m.source([c, ...]) }}`. Rows with `derived` set are cited as "computed from".
Book links come from the manifests in `data/raw/`; 2025-2027 books cite a page title,
not a page number.

**Notes and sources list.** Each page has one numbered list, Wikipedia-style. `m.cite`
prints a small "[12]" marker linking to `#src-12`; `m.source` prints "Source: [3], [4]."
(plus any `note=` text); `m.refs` prints note markers the same way. The full citation
(linked to the PDF page) and the full note text are in the "Notes and sources" list that
`base.html` prints at the end of `<main>`. The same book page (same text and link) or the
same note gets the same number everywhere on a page; numbers follow first appearance.
The first marker for each entry gets `id="ref-12"`, and the entry's "↑" links back to it.
Markers carry an `aria-label` ("Source 12: 2021 adopted budget book, p. 367") and a `title`
with the full text. A computed figure keeps the words "computed from" before its marker,
and `m.source(cites, "Calculated from")` keeps that prefix, so a reader can tell derived
figures apart without opening the list. How it works: the macros register each entry in
`pagerefs` (`nolabudget/refs.py`, a Jinja global); base.html renders the list after the
page body, so every marker is registered by then; `Site.page()` clears it after each page.
A page that never calls these macros gets no list. One exception: notes that explain a
chart or table (`m.footnotes`) are printed in full right under it, because the reader
needs them there. They keep their number in the same sequence, "[n]" note markers link to
them in place, and the end-of-page list leaves them out (`pagerefs.listed()`).

**Every section with a figure shows its source.** `checks.py` (`source_checks`) fails
the build if any page section showing a dollar figure has no "Source:" line, citation or
pointer. A citation marker counts only if it points to an entry in the page's "Notes
and sources" list; that list is taken out before sections are checked, so it never
stands in for a missing source. Use `m.source(cites)`; `m.source(cites, note="... calculated from these ...")`
for shares, per-resident figures and changes; `m.source(cites, "Calculated from")` when
everything shown is calculated; and `m.source_ref(text, href, link)` for a chart whose
figures are each cited in a table elsewhere (usually `#years`). A section with no budget
figures in it (such as the glossary's "$1 of tax") opts out with class `src-exempt`.

**Comparability notes.** Breaks in the data live in `content/notes.yml` (years, funds,
bases, optional depts). Get the ones that apply with
`site.notes_for(years, funds, bases, dept=None)`, mark rows with `{{ m.refs(notes, numbered) }}`
and point to them with `{{ m.footnotes(notes, heading) }}`, which prints one short line
("Notes on these figures: [5], [6].") under the table or chart. Notes share the page's
"Notes and sources" numbering; their text is in that list. (`numbered` is accepted and
ignored.) Add new site-wide breaks to `notes.yml`, not to templates.

**Charts.** Use `views.money_figure` (General Fund / all funds x dollars / 2025 dollars /
per resident) or `views.simple_figure`, and render with `{{ m.figure(f) }}`. Every chart
comes with its table and a text description; colours come from CSS tokens `--s1`
(adopted), `--s2` (actual), `--s3` (proposed point). Series keep these meanings
everywhere. Line charts start at zero. No dual axes.

**Pictures.** Each picture says one thing, with one plain sentence above it ("Out of
every $100 the General Fund spends, $22 goes to the Police Department"). Pieces to reuse:
- `{{ m.split_bar(parts, label) }}`: one whole split into at most six parts, printing
  "$35" (out of every $100) in each part, with a key. Build `parts` with `common.split()`
  (the five largest, then "Everything else"); dollar figures use
  `fmt.per_hundred`, which always adds to exactly 100. `unit="share"` prints "52%";
  `key=false` when the page names the parts right below.
- `{{ m.compare(old, new, old_label, new_label) }}`: then vs now. Two bars on one scale
  (the budget in force in grey, the proposal in blue) and an "Up $5.1 million (3.1%)"
  label. Leads every department, revenue category and revenue source page, inside
  `<div class="lede-visual">`. Draws nothing for a missing or negative figure.
- `{{ m.per100_stat(share, text) }}`: one big "$22" with a sentence ("of every $100 the
  General Fund spends would go here..."). `|per100` formats it ("$22", "$4.70",
  "$0.40", "less than 1 cent").
- `common.split(items, total)` builds parts for `m.split_bar` on any page; it returns
  [] when a picture would mislead (no total, or a negative amount).
- `{{ m.resident_stat(amount, year, text) }}` and `{{ m.resident_note(year) }}`: one big
  "$461 per resident" figure, and the note saying which population it divides by. Every
  per-resident figure needs that note nearby.
- `{{ m.bar(share) }}`: thin bar beside a ranked list (the ledgers).

**Inside a department** (`nolabudget/programs.py`, department pages). Programs come from
the `by_program` rows of `department_detail.csv`, using only each year's own book and own
column; later books repeat earlier columns and some repeats double count. Programs are
followed across years by their four-digit code, through
`data/crosswalk/program_groups.csv` (dept_id, code, name_pattern, group, plain_name). The
name check matters: a few codes were reused for unrelated programs, and a program whose
printed name fails it lands in "Other programs". Only departments listed in that file get
the section; to add one, map its codes and rebuild. Checks fail if the proposal year's
programs don't add up to the department's all-funds figure, or if a program over $100,000
has no mapping.

**How New Orleans compares** (`nolabudget/compare.py`, `/compare/`, and a box on the
matching department pages). Services, their wording and their caveats live in
`compare.FUNCTIONS`. Census Bureau figures for 2022, not budget-book figures: always
say so on the page, and never put the two in one sum or one chart. Peers are chosen by
rule (population 250,000 to 750,000), never by hand. `COMPARE_KEYS` in
`pages/departments.py` says which department gets which service's box. Checks recompute New Orleans's
figures from the CSV and look for them on the page.

**Who works there** (department pages). Job titles are sorted into ranks by patterns in
`content/position_ranks.yml`. Checks fail if ranks don't add up to the department's total
positions in any year. Years where more than 2% of positions have no title are left out of
the sworn trend. Always say these are budgeted positions, not people on the job.
- The steps list (`.steps`, home page): the yearly budget cycle with "We are here" set
  by `stage` in `content/site.yml`.
- Change bars (`.diverge`, home page): increases and decreases from a shared zero line,
  in HTML and CSS only. Each row also says "up" or "down" in words.

- The receipt (`.receipt`, home page hero): "Where every $100 of the General Fund goes",
  built by `split(..., n=RECEIPT_LINES)` in `pages/home.py`. Whole dollars only; never
  print cents the data does not have.

**Motion and hover.** Motion explains or delights; it never hides content.
- `base.html` adds `html.js` before the first paint. Every start state ("bar at zero",
  "row not yet printed") is written as `.js ...` inside
  `@media (prefers-reduced-motion: no-preference)`. So with JavaScript off, or for readers
  who ask for less motion, everything simply shows.
- `site.js` adds `.is-in` when an element scrolls into view, and CSS animates from that
  class. Opt in with `data-split`, `data-receipt`, `data-animate` or `.fig` (line charts
  draw themselves; `charts.py` gives lines `pathLength="1"`). Stagger with an inline
  `--i` index. After 2.5 seconds, anything on screen is shown regardless, because
  background tabs may never report scrolling.
- `data-count` on the hero figure counts it up from zero; the HTML already holds the
  final text.
- Split bars: hovering a part or its key row dims the rest and shows a tooltip with the
  name, amount and "$X of every $100". Ledger rows, change rows and step cards get a
  quiet hover state.
- Keep it fast and calm: 150-700 ms, ease-out, no bouncing, no loops (the "We are here"
  pulse runs three times, then stops).

Colours for parts of a whole are the tokens `--c1` to `--c5` plus `--c-other` (grey),
always in that order, each with a `--cN-ink` for text on it. Change bars use `--up` and
`--down`. These were checked with the dataviz palette validator in light and dark (worst
colour-blind separation 9.1 light, 8.4 dark). If you change a colour, re-run it. Light
mode slots 3-5 are below 3:1 against the page, so a part never relies on colour alone:
the key, the text and the table carry the names.

**Assets.** Link to files in `static/` with `{{ asset('style.css') }}`, never a bare
path. It adds a fingerprint of the file (`?v=1a2b3c4d`), so a browser or Cloudflare
never serves an old copy after a change.

**Design.** Serif for reading, system sans for numbers, tables and charts. One accent.
Tables right-align numbers with tabular figures. Wrap wide tables in
`<div class="table-scroll">` (add `wide` for tables that need the full page width).
Everything must read with JavaScript off; `static/site.js` only enhances.

**Content files.** `content/departments.yml` is drafted and needs review by someone who
knows City Hall. Add a `short` and `about` for any new `dept_id` in
`data/crosswalk/departments.csv`; the checks fail without them.
