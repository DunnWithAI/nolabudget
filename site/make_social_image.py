# ABOUTME: Draws site/static/social.png, the 1200x630 image shown when a page is shared on social media.
# ABOUTME: No figures on it, so it never goes out of date; rerun only to change the design (needs ImageMagick).
"""Make the social sharing image.

    python3 -I site/make_social_image.py

Uses ImageMagick (`magick`) and macOS system fonts (Iowan Old Style for the title, the
site's serif; Avenir for the small text). The colours are the site's light-theme tokens
from static/style.css. The bar is the site's "out of every $100" picture, drawn as a
shape only, with no numbers: the image must stay true whatever the budget says.
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "static", "social.png")

W, H = 1200, 630
BG, INK, MUTED, ACCENT = "#fbfaf6", "#1c1b18", "#67635a", "#1f5c8a"
BAR = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#cfcbc0"]   # --c1..--c5, --c-other
SHARES = [0.30, 0.22, 0.16, 0.12, 0.08, 0.12]  # a shape, not data
SERIF, SERIF_BOLD, SANS, SANS_BOLD = "Iowan-Old-Style-Roman", "Iowan-Old-Style-Bold", "Avenir-Book", "Avenir-Heavy"


def main():
    left, right = 80, W - 80
    cmd = ["magick", "-size", "%dx%d" % (W, H), "xc:" + BG,
           "-fill", ACCENT, "-draw", "rectangle 0,0 %d,14" % W]

    def text(font, size, color, x, y, s):
        cmd.extend(["-font", font, "-pointsize", str(size), "-fill", color,
                    "-annotate", "+%d+%d" % (x, y), s])

    text(SANS_BOLD, 30, ACCENT, left, 108, "NOLA BUDGET")
    text(SERIF_BOLD, 74, INK, left, 222, "The City of New Orleans")
    text(SERIF_BOLD, 74, INK, left, 310, "budget, explained")
    text(SANS, 34, MUTED, left, 390, "Where the money comes from, where it goes,")
    text(SANS, 34, MUTED, left, 438, "and how New Orleans compares.")
    # The split bar: six parts with 6px gaps and rounded ends, like the site's.
    y0, y1, gap = 488, 540, 6
    span = right - left - gap * (len(SHARES) - 1)
    x = left
    for share, color in zip(SHARES, BAR):
        w = span * share
        cmd.extend(["-fill", color, "-draw", "roundrectangle %d,%d %d,%d 8,8" % (x, y0, x + w, y1)])
        x += w + gap
    text(SANS, 26, MUTED, left, 592, "nolabudget.dunn.us")
    cmd.extend(["-gravity", "southeast", "-font", SANS, "-pointsize", "26", "-fill", MUTED,
                "-annotate", "+80+38", "Plain words, with a source for every figure"])
    cmd.extend(["-depth", "8", OUT])
    subprocess.run(cmd, check=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
