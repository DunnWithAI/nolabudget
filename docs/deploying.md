# Deploying

The site is a Cloudflare Worker that serves the static files in `dist/`
(`wrangler.jsonc`). Cloudflare Workers Builds is connected to the GitHub repo
`andrewedunn/nolabudget`, so the site is built on Cloudflare, not on a laptop.

## What happens when

| Event | What Cloudflare does |
| --- | --- |
| Push or merge to `main` | Builds the site and publishes it to nolabudget.dunn.us. |
| Push to any other branch, or a PR | Builds the site and uploads a preview version. The preview link shows up as a check on the PR. Production is not touched. |
| Build checks fail | `site/build.py` exits with an error, the build fails, and nothing is published. |

## Cloudflare settings

Workers & Pages → nolabudget → Settings → Build:

| Setting | Value |
| --- | --- |
| Git repository | `andrewedunn/nolabudget` |
| Production branch | `main` |
| Build command | `pip install -r requirements.txt && python site/build.py` |
| Deploy command | `npx wrangler deploy` |
| Preview command (other branches) | `npx wrangler preview` (Enable Preview builds is on) |
| Root directory | `/` |

`.python-version` pins the Python version the build uses.

## Why `dist/` and `data/raw/` aren't in git

- `dist/` is built from source on every deploy, so committing it would just create noise
  and conflicts.
- `data/raw/` holds about 2.4 GB of city PDFs and downloads, more than GitHub allows.
  Only the `manifest.json` files (the build reads them to link each figure to its source
  PDF) and the small BLS price-index files are committed. `scripts/` can re-fetch the rest;
  see [data-pipeline.md](data-pipeline.md).

## Deploying by hand

This still works and skips GitHub entirely. Use it only when you have to:

    .venv/bin/python site/build.py && wrangler deploy

A later push to `main` will replace whatever was deployed this way.

## Git inside Dropbox

The `.git` folder syncs through Dropbox like everything else. Running git on two machines
at once can produce "conflicted copy" files inside `.git`. Work from one machine at a time.
To keep `.git` out of Dropbox entirely, run
`xattr -w com.dropbox.ignored 1 .git`.
