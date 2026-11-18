# CLAUDE.md

Notes for Claude sessions working on NOLA Budget, a static site that explains the City of
New Orleans budget to ordinary residents. Start with [README.md](README.md).

## Read first

- **[VOICE.md](VOICE.md)** before writing or editing any words a reader will see. Plain,
  short, specific. The audience is the general public, many of whom have never read a
  budget. Lead with the answer, then explain.
- **[site/README.md](site/README.md)** before changing the site: layout, conventions for
  numbers, citations, notes and charts.
- **[docs/data-pipeline.md](docs/data-pipeline.md)** before touching `scripts/` or `data/`.

## Commands

    .venv/bin/python site/build.py          # build + checks; must end "All checks passed"
    .venv/bin/python site/build.py --no-checks

Preview with the `nolabudget` server in `.claude/launch.json` (port 4321). After any
visible change, look at the page in light and dark and at phone width.

If `.venv/bin/python` is missing (Dropbox drops venv contents), recreate it:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Rules that are easy to break

- **Accuracy beats style.** Never type a figure into a template or YAML file. Every number
  comes from `site.budget` and every figure carries a citation. Don't claim a reason for
  a change unless the budget book states it.
- **Every section that shows a figure shows its source** (a citation, a "Calculated
  from" line, or a pointer to the cited table). The build fails otherwise; see "Every
  section with a figure shows its source" in site/README.md.
- **Don't hard-code the budget year.** Read `proposal_year`, `in_force_year`,
  `adoption_deadline` and `stage` from `site/content/site.yml`.
- **General Fund first**, all funds second.
- **Prose money is millions** (`|money` gives "$1,471.5 million"). The checks look for
  that exact form. The one exception is the home page hero number (`|money_hero`, "$1.47
  billion"); the millions form must still appear on the page.
- **Show, don't only tell.** Prefer a picture plus one plain sentence over a table. Every
  picture also has its numbers in text or a table, and works with JavaScript off.
  Reuse what exists: `m.split_bar` (parts of a whole, "out of every $100"),
  `m.compare` (then vs now), `m.per100_stat` (one big "$22 of every $100"), `m.bar`
  (ranked lists), `m.figure` (line charts over time), the steps list and change bars on
  the home page. Colours come from CSS tokens; see "Pictures" in site/README.md.
- **Motion is extra, never required.** Start states only under `.js` and
  `prefers-reduced-motion: no-preference`; see "Motion and hover" in site/README.md.
  Never hide text behind a scroll animation.
- **Census comparison figures are not budget figures.** Different year, population and
  definitions. Label them every time and keep them apart from budget numbers.
- **Never `rm -rf dist`.** The build prunes stale files itself.
- `data/raw/` is read-only. `data/extracted/` is written only by extractors.
- Git repo on GitHub (`andrewedunn/nolabudget`, public). Work on a branch and open a PR;
  `main` is what's live. Never commit secrets or `data/raw/` files (see `.gitignore`).

## Deploying

Merging to `main` deploys to nolabudget.dunn.us through Cloudflare Workers Builds; PRs get
a preview link. Only merge to `main` or run `wrangler deploy` by hand when Andrew asks.
See [docs/deploying.md](docs/deploying.md).
