# NOLA Budget

A website that explains the City of New Orleans budget in plain words and pictures. It
reads the city's own budget books (2004 to the 2027 proposal), turns their tables into
clean data, and builds a static site where every budget figure is traced back to the book and page
it came from.

Live: https://nolabudget.dunn.us

## Quick start

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python site/build.py                              # build dist/ and run the checks
    .venv/bin/python -m http.server 4321 --directory dist       # preview at http://localhost:4321

The build stops with an error if `site/checks.py` finds a problem (wrong headline totals,
broken links, "nan" or "None" on a page). A clean build ends with "All checks passed".

## Deploying

The site is a Cloudflare Worker that serves static files (`wrangler.jsonc`). Cloudflare
Workers Builds watches the GitHub repo:

- **Push or merge to `main`:** Cloudflare runs the build and publishes the site.
- **Pull request:** Cloudflare runs the same build and posts a preview link on the PR.
  If the checks fail, the build fails and nothing is published.

Cloudflare's build command is
`pip install -r requirements.txt && python site/build.py`, and its deploy command is
`npx wrangler deploy`. See [docs/deploying.md](docs/deploying.md).

## How it fits together

    city budget PDFs ──> scripts/ ──> data/extracted/ ──> data/combined/*.csv ──> site/build.py ──> dist/
      (data/raw/)       extract      one folder per book   one clean table set     pages          static site
                                                     ▲
                                       data/crosswalk/ (names matched across years, by hand)

| Folder | What it holds |
| --- | --- |
| `site/` | The site builder: Python, Jinja templates, CSS, hand-written text. See [site/README.md](site/README.md). |
| `data/combined/` | The clean tables the site reads. The only data the site uses. |
| `data/crosswalk/` | How each printed department or revenue name maps to one ID across all years. |
| `data/extracted/` | Raw tables pulled from each budget book, as printed. See [data/extracted/SCHEMA.md](data/extracted/SCHEMA.md). |
| `data/raw/` | The source PDFs and downloads (about 2.4 GB). Read-only. Only the manifests are in git; `scripts/` re-fetches the files. |
| `scripts/` | Fetching, extraction and combining. See [docs/data-pipeline.md](docs/data-pipeline.md). |
| `dist/` | Built site. Generated; never edit by hand. |
| `docs/` | Notes on how and why things work. |
| `VOICE.md` | The writing guide for every word on the site. |

To change what the site says or shows, work in `site/`. To change the numbers, work in
`scripts/` and `data/crosswalk/`, then rebuild `data/combined/` and the site.

## Good to know

- The folder lives in Dropbox and is also a git repository (github.com/andrewedunn/nolabudget).
  Dropbox does not keep `.venv` contents reliably; if `.venv/bin/python` is missing,
  recreate it with the quick start.
- Never `rm -rf dist`. The build updates files in place and removes stale ones itself;
  deleting the folder while Dropbox syncs creates "conflicted copy" files.
- The site describes one moment in the budget year. `site/content/site.yml` sets the
  proposal year, the year in force, the adoption deadline and the current step
  (`stage`). Update those when the calendar moves on.
