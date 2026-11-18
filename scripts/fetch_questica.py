#!/usr/bin/env python3
"""Fetch City of New Orleans budget books from the public Questica OpenBook site.

Run:  python3 -I scripts/fetch_questica.py [--no-pdf] [--only <book-id-prefix>]

What it pulls (all anonymous, public endpoints used by the OpenBook viewer):
  GET /api/home/cards                                    -> list of published books
  GET /api/digitalbudgetbookviewer/versions/{bookId}     -> metadata + table of contents + publishedPdfHref
  GET /api/digitalbudgetbookviewer/pages/{pageId}        -> one page; publishedMarkup holds the rendered
                                                           pivot tables / text as HTML
  GET /api/digitalbudgetbookviewer/{bookId}/headersandfooters
  GET publishedPdfHref (Azure blob)                      -> the full PDF budget book

Raw responses are saved byte-for-byte under data/raw/questica/<year>/<operating|capital>/<stage>_<slug>/
and a manifest.json is written. Existing files are skipped (resumable). Rate limit: ~1.7 req/s.
"""
import argparse, hashlib, json, os, re, sys, time, urllib.request, urllib.error

BASE = "https://nola.openbook.questica.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "data", "raw", "questica")
DELAY = 0.6
_last = [0.0]


def get(url, accept="application/json"):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    for attempt in range(4):
        try:
            _last[0] = time.time()
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return e.code, e.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def classify(card):
    name = card["name"]
    typ = "capital" if re.search(r"capital", name, re.I) else "operating"
    stage = "adopted" if re.search(r"adopted", name, re.I) else "proposed"  # proposed incl. "Draft"
    return card["budgetYear"], typ, stage


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def toc_pages(items, acc):
    for i in sorted(items, key=lambda x: x.get("sort", 0)):
        pid = i.get("digitalBudgetBookPageId")
        if pid and pid not in acc:
            acc[pid] = i["title"]
        toc_pages(i.get("tableOfContentItems", []), acc)
    return acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    manifest, errors = [], []

    def record(file_path, url, year, stage, typ, desc):
        manifest.append(dict(year=year, stage=stage, type=typ, description=desc, url=url,
                             path=os.path.relpath(file_path, ROOT), bytes=os.path.getsize(file_path),
                             sha256=sha(file_path)))

    def fetch(file_path, url, year, stage, typ, desc, accept="application/json"):
        if not os.path.exists(file_path):
            st, body = get(url, accept)
            if st != 200:
                errors.append(dict(url=url, status=st, description=desc))
                print("  ! %s -> %s" % (url, st), file=sys.stderr)
                return None
            save(file_path, body)
        record(file_path, url, year, stage, typ, desc)
        return file_path

    st, body = get(BASE + "/api/home/cards")
    cards = json.loads(body)
    cards_path = os.path.join(OUT, "home_cards.json")
    save(cards_path, body)
    record(cards_path, BASE + "/api/home/cards", None, None, None, "Index of all published books on the OpenBook home page")

    for c in sorted(cards, key=lambda c: (c["budgetYear"], c["name"])):
        if a.only and not c["id"].startswith(a.only):
            continue
        year, typ, stage = classify(c)
        bdir = os.path.join(OUT, str(year), typ, "%s_%s" % (stage, slug(c["name"])))
        bid = c["id"]
        print("==", year, typ, stage, c["name"], bid)
        vpath = fetch(os.path.join(bdir, "version.json"), "%s/api/digitalbudgetbookviewer/versions/%s" % (BASE, bid),
                      year, stage, typ, "Book metadata + table of contents: " + c["name"])
        if not vpath:
            continue
        ver = json.load(open(vpath))
        fetch(os.path.join(bdir, "headers_and_footers.json"),
              "%s/api/digitalbudgetbookviewer/%s/headersandfooters" % (BASE, bid),
              year, stage, typ, "Headers/footers: " + c["name"])
        pages = toc_pages(ver["tableOfContents"]["tableOfContentsItems"], {})
        for n, (pid, title) in enumerate(pages.items(), 1):
            fetch(os.path.join(bdir, "pages", pid + ".json"),
                  "%s/api/digitalbudgetbookviewer/pages/%s" % (BASE, pid),
                  year, stage, typ, "Page: " + title.strip())
        print("   pages:", len(pages))
        pdf = ver.get("publishedPdfHref")
        if pdf and not a.no_pdf:
            fetch(os.path.join(bdir, os.path.basename(pdf)), pdf, year, stage, typ,
                  "Full PDF budget book: " + c["name"], accept="application/pdf")

    mpath = os.path.join(OUT, "manifest.json")
    if a.only and os.path.exists(mpath):
        print("--only used: not overwriting manifest.json", file=sys.stderr)
        mpath = os.path.join(OUT, "manifest.partial.json")
    with open(mpath, "w") as f:
        json.dump(dict(source=BASE, files=manifest, errors=errors), f, indent=1)
    print("manifest:", mpath, len(manifest), "files,", len(errors), "errors")


if __name__ == "__main__":
    main()
