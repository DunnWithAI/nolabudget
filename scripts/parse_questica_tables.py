#!/usr/bin/env python3
"""Extract every HTML table (OpenBook 'PivotTable' content blocks and plain tables) from the raw
page JSON files into long-form CSVs, one per book under data/interim/questica/<book-dir>/.

Run: python3 -I scripts/parse_questica_tables.py
Output per book: tables.csv with columns
  page_id, page_title, table_idx, row_idx, row_path, row_level, row_dimension, col_header, value_raw, value_num, is_subtotal_cell
(row_path joins the hierarchical row labels with ' > '; row_dimension is the name of that level; subtotal rows have is_subtotal_cell=1.)
"""
import csv, glob, json, os, re
from html.parser import HTMLParser

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RAW = os.path.join(ROOT, "data", "raw", "questica")
OUT = os.path.join(ROOT, "data", "interim", "questica")


class T(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.cur, self.row, self.cell, self.depth = [], None, None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.cur = {"head": [], "body": []}
                self.sect = "body"
        elif self.cur is not None:
            if tag == "thead":
                self.sect = "head"
            elif tag == "tbody":
                self.sect = "body"
            elif tag == "tr":
                self.row = []
                self.row_cls = a.get("class", "")
            elif tag in ("td", "th"):
                self.cell = dict(tag=tag, text="", cls=a.get("class", ""), colspan=int(a.get("colspan", 1) or 1))

    def handle_endtag(self, tag):
        if tag == "table":
            if self.depth == 1 and self.cur is not None:
                self.tables.append(self.cur)
                self.cur = None
            self.depth -= 1
        elif self.cur is not None:
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(self.cell)
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.cur[self.sect].append(self.row)
                if "pivot-table-header-row" in self.row_cls and self.sect == "body":
                    self.cur["body"].pop()
                    self.cur["head"].append(self.row)
                self.row = None

    def handle_data(self, d):
        if self.cell is not None:
            self.cell["text"] += d


def num(s):
    s = s.strip().replace(",", "").replace("$", "")
    if not s:
        return ""
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    pct = s.endswith("%")
    s = s.rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return ""
    return -v if neg else v


def main():
    for bdir in sorted(glob.glob(os.path.join(RAW, "*", "*", "*_*"))):
        pages = glob.glob(os.path.join(bdir, "pages", "*.json"))
        if not pages:
            continue
        rel = os.path.relpath(bdir, RAW)
        out = os.path.join(OUT, rel)
        os.makedirs(out, exist_ok=True)
        n = 0
        with open(os.path.join(out, "tables.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["page_id", "page_title", "table_idx", "row_idx", "row_path", "row_level", "row_dimension", "col_header", "value_raw", "value_num", "is_subtotal_cell"])
            for pf in sorted(pages):
                d = json.load(open(pf))
                p = T()
                p.feed(d.get("publishedMarkup") or "")
                for ti, t in enumerate(p.tables):
                    heads = t["head"][-1] if t["head"] else []
                    hdr = [h["text"].strip() for h in heads if "rowHeaderCell" not in h["cls"]]
                    # dimension names of the row levels: first cell of each header row
                    lvl_names = [r[0]["text"].strip() for r in t["head"] if r]
                    stack = []
                    for ri, r in enumerate(t["body"]):
                        rh = [c for c in r if "rowHeaderCell" in c["cls"]] or [c for c in r if c["tag"] == "th"]
                        if rh:
                            first = rh[0]
                            mm = re.search(r"level(\d+)", first["cls"])
                            lvl = int(mm.group(1)) if mm else 0
                            stack = stack[:lvl] + [""] * (lvl - len(stack)) + [first["text"].strip()]
                            cells = [c for c in r if c is not first]
                        else:  # plain table: first cell is the label
                            lvl = 0
                            stack = [r[0]["text"].strip()] if r else [""]
                            cells = r[1:]
                        ch = hdr[len(hdr) - len(cells):] if len(hdr) >= len(cells) else [""] * len(cells)
                        for h, c in zip(ch, cells):
                            v = c["text"].strip()
                            w.writerow([d["id"], d["title"].strip(), ti, ri, " > ".join(stack), lvl,
                                        lvl_names[lvl] if lvl < len(lvl_names) else "", h, v, num(v),
                                        1 if "grandTotal" in c["cls"] or "total" in c["cls"].lower() else 0])
                            n += 1
        print(rel, n, "cells")


if __name__ == "__main__":
    main()
