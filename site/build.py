"""Build the NOLA Budget site into dist/.

    .venv/bin/python site/build.py            # build, then run the checks
    .venv/bin/python site/build.py --no-checks

Reads data/combined/*.csv, data/crosswalk/*.csv, data/raw/*/manifest.json (for source
PDF links) and site/content/*.yml. Writes a fully static site with clean URLs
(dist/departments/police/index.html). See site/README.md.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from nolabudget.site import Site  # noqa: E402
from nolabudget.pages import PAGES  # noqa: E402
from nolabudget.data import COMBINED  # noqa: E402
from nolabudget import seo  # noqa: E402

DIST = os.path.join(ROOT, "dist")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def prune(site):
    """Remove files in dist/ that this build did not write (stale pages, sync conflicts)."""
    for dirpath, dirnames, filenames in os.walk(DIST, topdown=False):
        for fn in filenames:
            p = os.path.abspath(os.path.join(dirpath, fn))
            if p not in site.written:
                os.remove(p)
        if dirpath != DIST and not os.listdir(dirpath):
            os.rmdir(dirpath)


def main(argv):
    t0 = time.time()
    os.makedirs(DIST, exist_ok=True)
    site = Site(DIST)
    site.downloads = []
    for module in PAGES:
        module.build(site)
    # static assets (favicon at the root)
    static = os.path.join(HERE, "static")
    for name in sorted(os.listdir(static)):
        target = os.path.join(DIST, name) if name == "favicon.svg" else os.path.join(DIST, "static", name)
        site.write(target, read_bytes(os.path.join(static, name)))
    # CSV downloads (the about page decides which files)
    for f in site.downloads:
        site.write(os.path.join(DIST, "data", f["name"]), read_bytes(os.path.join(COMBINED, f["name"])))
    site.write(os.path.join(DIST, "404.html"),
               site.env.get_template("404.html").render(page_url="/404/", title="Page not found").encode("utf-8"))
    seo.write(site, DIST)   # robots.txt, sitemap.xml, llms.txt, _headers
    prune(site)
    t1 = time.time()
    print("Built %d pages into %s in %.1fs" % (len(site.pages), os.path.relpath(DIST, ROOT), t1 - t0))
    if "--no-checks" not in argv:
        import checks
        errors = checks.run(site, DIST)
        if errors:
            print("\n%d check(s) failed:" % len(errors))
            for e in errors[:200]:
                print("  - " + e)
            return 1
        print("All checks passed in %.1fs" % (time.time() - t1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
