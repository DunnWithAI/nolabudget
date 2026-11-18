#!/usr/bin/env python
"""Extract the City of New Orleans 2024 Adopted Operating Budget book into long-format CSVs.

Usage:  .venv/bin/python scripts/extract/extract_2024.py

The 2024 book prints exactly the same table family as the 2023 book (statements of revenues,
department expenditure statements, per-fund by-type tables, department Expenditure-by-Type /
Funding-Source pages, PROGRAM DETAIL / EXPENDITURE SUMMARY / PERSONNEL SUMMARY pages), so the
engine in extract_2023.py is reused unchanged; only the PDF path and book id differ.
Outputs: data/extracted/2024-operating-adopted/ (see data/extracted/SCHEMA.md).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_2023 import Config, ROOT, run  # noqa: E402

CFG = Config(pdf=ROOT / "data/raw/nola-gov/2024/2024-Adopted-Budget-Book.pdf",
             book_id="2024-operating-adopted", seed=2024)


def main():
    run(CFG)


if __name__ == "__main__":
    main()
