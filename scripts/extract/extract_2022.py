#!/usr/bin/env python
"""Extract the City of New Orleans 2022 Adopted Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2022.py

The 2022 book prints the same table family as the 2023 and 2024 books, so the engine in
extract_2023.py is reused; only the PDF path and book id differ.
Outputs: data/extracted/2022-operating-adopted/ (see data/extracted/SCHEMA.md).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_2023 import Config, ROOT, run  # noqa: E402

CFG = Config(pdf=ROOT / "data/raw/nola-gov/2022/City-of-New-Orleans-2022-Adopted-Budget-Book.pdf",
             book_id="2022-operating-adopted", seed=2022)


def main():
    run(CFG)


if __name__ == "__main__":
    main()
