#!/usr/bin/env python3
"""OCR every page of the 2008 adopted budget book with the Vision tool and cache the result.

Run:  .venv/bin/python scripts/extract/ocr_2008.py [first last]

Reads   data/raw/pre-2011/2008/CNO_BudgetBook2008Adopted3.pdf  (read-only)
Writes  data/interim/ocr/2008/p0001.json ... p0608.json  (one file per page, see ocr_vision.swift for the shape)
        data/interim/ocr/2008/manifest.json  (rotation used per page)

The book is a Crystal Reports PDF whose landscape tables are printed sideways on portrait pages (or with /Rotate 270),
so each page is rendered with the rotation that makes its text upright (the render rotates the page clockwise by `rotate`):
  * the text layer (PyMuPDF) says which: pages whose characters run vertically, or that carry /Rotate 270 -> rotate 90;
  * pages with no text layer at all (scanned inserts) are tried at 0, 90 and 270 and the rotation with the most text in long
    horizontal lines plus recognisable table words wins (Vision also reads sideways text, but then returns tall, unusable boxes).
Existing cache files are never overwritten (delete them to redo); the extraction scripts only ever READ this cache,
so extract_2008.py is deterministic given the cache.
"""
import json
import subprocess
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "data/raw/pre-2011/2008/CNO_BudgetBook2008Adopted3.pdf"
TOOL = ROOT / "scripts/extract/bin/ocr_vision"
OUT = ROOT / "data/interim/ocr/2008"
DPI = 300


def orient(page):
    """-> 90 if the page's text runs sideways in the rendered page, else 0; None when there is no text layer."""
    d = page.get_text("dict")
    h = v = 0
    for b in d["blocks"]:
        for l in b.get("lines", []):
            n = sum(len(s["text"].strip()) for s in l["spans"])
            if abs(l["dir"][1]) > 0.9:
                v += n
            else:
                h += n
    if h + v < 30:
        return None
    return 90 if (v > h or page.rotation == 270) else 0


UPRIGHT_WORDS = {"PROGRAM", "DETAIL", "SUMMARY", "EXPENDITURE", "EXPENDITURES", "PERSONNEL", "DEPARTMENT", "DEPARTMENTAL", "BUDGET", "TOTAL", "Actual",
                 "Adopted", "Proposed", "Personal", "Services", "Operating", "Service", "Grade", "FUND", "GENERAL", "SOURCE", "FUNDING"}


def score(j):
    """Upright reading = many long HORIZONTAL text lines (Vision also reads sideways text, but then returns tall boxes for it)
    AND recognisable budget-table words, AND the title near the top (an upside-down page is horizontal and readable too)."""
    horiz = sum(len(l["text"]) for l in j["lines"] if l["w"] > 2 * l["h"])
    vocab = sum(1 for l in j["lines"] for w in l["words"] if w["text"].strip(".,:") in UPRIGHT_WORDS)
    # Vision reads upside-down text correctly but reports it from the bottom up, so also require the page title (a line holding
    # SUMMARY / DETAIL) to sit in the top quarter of the rendered image
    top_title = any(("SUMMARY" in l["text"] or "DETAIL" in l["text"]) and l["y"] < 0.25 * j["img_h"] for l in j["lines"])
    return horiz + 50 * vocab + (1000 if top_title else 0)


def run(n, rot):
    out = OUT / ("p%04d.json" % n)
    r = subprocess.run([str(TOOL), str(PDF), str(n), "-", "--dpi", str(DPI), "--rotate", str(rot)], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout)


def main():
    doc = fitz.open(str(PDF))
    OUT.mkdir(parents=True, exist_ok=True)
    first = int(sys.argv[1]) if len(sys.argv) > 2 else 1
    last = int(sys.argv[2]) if len(sys.argv) > 2 else doc.page_count
    mf_path = OUT / "manifest.json"
    manifest = json.loads(mf_path.read_text()) if mf_path.exists() else {}
    for n in range(first, last + 1):
        f = OUT / ("p%04d.json" % n)
        if f.exists():
            continue
        o = orient(doc[n - 1])
        if o is None:
            # no text layer: blank page or a scanned insert -> try both orientations
            best, best_score = None, -1
            for rot in (0, 90, 270):
                j = run(n, rot)
                if j:
                    sc = score(j)
                    if sc > best_score:
                        best, best_score = j, sc
            j, rot = best, (best or {}).get("rotate", 0)
            how = "no text layer: best of 0/90/270 by horizontal-text + vocabulary score"
        else:
            j, rot, how = run(n, o), o, "text layer says %d" % o
        if j is None:
            print("page %d: OCR failed" % n, file=sys.stderr)
            continue
        f.write_text(json.dumps(j, separators=(",", ":")), encoding="utf-8")
        manifest[str(n)] = dict(rotate=rot, how=how, lines=len(j["lines"]))
        if n % 25 == 0:
            mf_path.write_text(json.dumps(manifest, indent=0), encoding="utf-8")
            print("p%d" % n, flush=True)
    mf_path.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
