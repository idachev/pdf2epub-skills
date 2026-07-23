#!/usr/bin/env -S uv run --with pymupdf python
"""Generate tests/fixtures/sample_diagrams.pdf — a small, license-clean PDF that
exercises the figure pipeline's tricky layouts (see test_sample_pdf.py):

  page 1  clean vector diagram (labels + caption) with body text above it
  page 2  wide vector diagram with a long body paragraph wrapped beside it
  page 3  a page of text on a background fill (a "back cover" — must NOT be captured)
  page 4  plain prose, no figure

Everything here is drawn programmatically, so the fixture carries no third-party
content. Re-run this script to regenerate the committed PDF.
"""

from pathlib import Path

import pymupdf

OUT = Path(__file__).parent / "sample_diagrams.pdf"

LOREM = (
    "This is a long narrative paragraph of ordinary body prose that belongs in the "
    "reading flow of the book and must remain reflowable, selectable text in the EPUB "
    "rather than being rasterized into a figure image alongside the diagram it sits near."
)


def _diagram(page, x0, y0, cols, rows, step=7.0):
    """A dense cluster of short strokes — stands in for a vector illustration."""
    for r in range(rows):
        for c in range(cols):
            x = x0 + c * step
            y = y0 + r * step
            page.draw_line((x, y), (x + step * 0.7, y + step * 0.7), width=0.4)


def main():
    doc = pymupdf.open()

    # page 1 — clean figure: body text above, diagram + labels + caption below
    p = doc.new_page(width=420, height=640)
    p.insert_textbox(pymupdf.Rect(50, 50, 370, 130), LOREM, fontsize=9)
    _diagram(p, 70, 300, cols=20, rows=14)
    p.insert_text((90, 330), "Alpha")
    p.insert_text((90, 380), "Beta")
    p.insert_text((90, 430), "Gamma")
    p.insert_textbox(pymupdf.Rect(70, 470, 360, 500), "Fig. 1. A sample labelled diagram.", fontsize=9)

    # page 2 — side-by-side: full-width diagram with a body paragraph over its left half
    p = doc.new_page(width=420, height=640)
    _diagram(p, 45, 300, cols=38, rows=20, step=9.0)
    p.insert_textbox(pymupdf.Rect(48, 305, 210, 560), LOREM, fontsize=8)
    p.insert_text((300, 360), "RightLabel")

    # page 3 — text on a background fill: not a diagram, must stay as text
    p = doc.new_page(width=420, height=640)
    p.draw_rect(pymupdf.Rect(20, 20, 400, 620), fill=(0, 0, 0))
    p.insert_textbox(
        pymupdf.Rect(40, 40, 380, 600),
        (LOREM + " ") * 4,
        fontsize=10,
        color=(1, 1, 1),
    )

    # page 4 — plain prose, no figure at all
    p = doc.new_page(width=420, height=640)
    p.insert_textbox(pymupdf.Rect(50, 60, 370, 560), (LOREM + "\n\n") * 3, fontsize=10)

    doc.save(str(OUT), deflate=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(doc)} pages)")


if __name__ == "__main__":
    main()
