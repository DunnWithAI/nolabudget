# ABOUTME: Files for search engines and AI assistants: robots.txt, sitemap.xml, llms.txt
# ABOUTME: and _headers, all following the noindex switch in content/site.yml (docs/seo.md).
import os
from xml.sax.saxutils import escape


def robots(config):
    """While noindex is on, crawlers may fetch pages (so they can read each page's
    noindex tag) but no sitemap is offered. Blocking them with Disallow would hide the
    noindex tag, and a blocked URL can still be listed from links elsewhere."""
    lines = ["User-agent: *", "Allow: /"]
    if not config.get("noindex"):
        lines += ["", "Sitemap: %s/sitemap.xml" % config["base_url"]]
    return "\n".join(lines) + "\n"


def headers(config):
    """Cloudflare's _headers file (read by the host, never served). While noindex is on,
    every response, including CSVs, llms.txt and the sitemap, says noindex too."""
    if not config.get("noindex"):
        return None
    return "/*\n  X-Robots-Tag: noindex, nofollow\n"


def sitemap(config, urls):
    rows = "".join("  <url><loc>%s</loc></url>\n" % escape(config["base_url"] + u) for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s</urlset>\n' % rows)


def write(site, dist):
    config = site.config
    urls = sorted(u for u, _ in site.pages)
    site.write(os.path.join(dist, "robots.txt"), robots(config).encode("utf-8"))
    site.write(os.path.join(dist, "sitemap.xml"), sitemap(config, urls).encode("utf-8"))
    h = headers(config)
    if h:
        site.write(os.path.join(dist, "_headers"), h.encode("utf-8"))
    titles = dict(site.pages)
    section = lambda prefix: [(u, titles[u]) for u in urls if u.startswith(prefix) and u != prefix]
    llms = site.env.get_template("llms.txt").render(
        departments=section("/departments/"),
        revenue=[("/revenue/%s/" % c["slug"], c["category"], c["short"]) for c in site.content["revenue_categories"]],
        downloads=getattr(site, "downloads", []))
    site.write(os.path.join(dist, "llms.txt"), llms.encode("utf-8"))
