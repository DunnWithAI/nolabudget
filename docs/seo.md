# Search engines and AI assistants

What the build does so search engines and AI assistants can find and read the site, and
how to take the site public. The code is in `site/templates/base.html` (the `<head>`),
`site/nolabudget/seo.py` (robots.txt, sitemap.xml, llms.txt, _headers) and the checks in
`seo_checks()` in `site/checks.py`.

## What every page has

- `<html lang="en">`, one `<h1>`, headings that never skip a level.
- A `<title>`: "Page title | NOLA Budget" (the home page is "NOLA Budget: tagline").
  Titles must be unique; revenue pages start with "Revenue:" because some revenue
  categories share a name with a department ("Intergovernmental", "Miscellaneous").
- A meta description from the template's `{% block description %}`. Some include
  figures; those come from the build context like every other figure.
- A canonical URL and `og:url`: `base_url` from `content/site.yml` plus the page URL.
- Open Graph and Twitter card tags (title, description, URL, site name, type, image).
  The image is `static/social.png` (1200x630), drawn by `site/make_social_image.py`
  with ImageMagick. It carries no figures, so it never goes out of date; rerun the
  script only to change the design. `twitter:card` is `summary_large_image`.
- Structured data (JSON-LD), stating only what the page shows:
  - every page with a breadcrumb trail: `BreadcrumbList`, built from the same
    `crumbs` list as the visible trail;
  - home: `WebSite` with the author, and `FAQPage` for "Common questions". Each answer
    is written once in `home.html` and used for both the page and the JSON-LD.
    (Google shows FAQ rich results only for government and health sites, so this
    mostly helps other search engines and AI assistants.)
  - about: `Dataset` for the CSV downloads (no figures, just the files and years).
- Content works without JavaScript; `site.js` is deferred; fonts are system fonts.

The 404 page has `noindex` and no canonical URL. `wrangler.jsonc` serves it with a 404
status (`not_found_handling: 404-page`).

`/llms.txt` is a short plain-text guide for AI assistants (llmstxt.org): what the site
is, the main pages, revenue categories, departments and data files. It has no budget
figures, only years from `site.yml`. Template: `site/templates/llms.txt`.

## The noindex switch

`noindex` in `content/site.yml` is the one switch. The build follows it everywhere:

| | `noindex: true` | `noindex: false` (now, since November 17, 2026) |
|---|---|---|
| `<meta name="robots">` | `noindex, nofollow` on every page | none (404 page keeps `noindex`) |
| `_headers` (Cloudflare) | `X-Robots-Tag: noindex, nofollow` on every response, including CSVs, llms.txt and the sitemap | not written |
| robots.txt | `Allow: /`, no Sitemap line | `Allow: /` and `Sitemap: <base_url>/sitemap.xml` |
| sitemap.xml | written, not advertised | written and advertised |

Why robots.txt allows crawling while the site is private: a search engine can only see a
`noindex` tag on a page it is allowed to fetch. A `Disallow: /` hides the tag, and a
blocked URL can still turn up in results (address only) if another site links to it.
Allowing crawling plus `noindex` is what Google documents for keeping pages out.

One trade-off: `noindex` keeps pages out of search results, but it does not stop
crawlers that collect text for AI training. If that matters before launch, add
`User-agent: GPTBot`/`Disallow: /` style blocks for those crawlers to `robots()` in
`seo.py` (the checks only object to blocking every crawler).

The checks fail the build if the robots tag, `_headers`, robots.txt or the sitemap
disagree with `noindex`, if the sitemap misses a page, if JSON-LD does not parse or does
not match the visible breadcrumbs or answers, or if a page lacks a canonical URL, a
description or a unique title.

## Going public

1. In `site/content/site.yml`, set `noindex: false`.
2. Build: `.venv/bin/python site/build.py` must end "All checks passed". Spot-check
   `dist/robots.txt` (has the Sitemap line), `dist/_headers` (gone), and a page's
   `<head>` (no robots meta tag).
3. Deploy: `wrangler deploy`. Then check the live site:
   `curl -sI https://nolabudget.dunn.us/ | grep -i x-robots` prints nothing, and
   `curl -s https://nolabudget.dunn.us/robots.txt` shows the Sitemap line.
4. Google Search Console: add the property for `nolabudget.dunn.us` (a DNS TXT record
   in Cloudflare, or the "domain" property type), then Sitemaps > submit
   `https://nolabudget.dunn.us/sitemap.xml`. Use URL Inspection on the home page to
   request indexing.
5. Bing Webmaster Tools: import the site from Search Console (or verify it the same
   way) and submit the same sitemap. Bing's index also feeds several AI assistants.
6. Optional: run a few pages through Google's Rich Results Test and
   validator.schema.org to confirm the structured data reads as expected.

Pages that were crawled while `noindex` was on are re-read on the next crawl; there is
nothing else to undo.
