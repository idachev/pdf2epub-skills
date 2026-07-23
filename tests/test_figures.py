"""Tests for figure detection/rasterization. Synthetic PDFs (built in-memory
with PyMuPDF) keep these network- and book-free so they run in CI. A book-gated
test at the bottom exercises the real reference PDF when it is available."""

import os

import pytest

pymupdf = pytest.importorskip("pymupdf")
import figures  # noqa: E402  (after importorskip)


def _page_with_cluster():
    """A page with body text at the top and a dense stroke cluster (a fake vector
    figure) plus an in-figure label in the lower half."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((60, 60), "Body paragraph text that sits above the figure region.")
    for i in range(240):  # 20 x 12 grid of short strokes, spaced one grid cell apart
        x = 80 + (i % 20) * 6
        y = 320 + (i // 20) * 6
        page.draw_line((x, y), (x + 3, y + 3), width=0.4)
    page.insert_text((110, 360), "LabelInsideFigure")
    return doc, page


def test_detects_vector_cluster_and_absorbs_label():
    doc, page = _page_with_cluster()
    regions = figures.detect_figure_regions(page, doc, figures.FigureOptions())
    assert len(regions) == 1
    r = regions[0]
    assert r.y0 > 100          # body text at y=60 is not swallowed
    assert r.y0 < 325 and r.y1 > 385   # covers the stroke cluster
    assert r.x1 > 190          # absorbs the "LabelInsideFigure" text block


def test_no_figure_on_plain_text_page():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((60, 60), "Just prose, no drawings at all on this page.")
    assert figures.detect_figure_regions(page, doc, figures.FigureOptions()) == []


def test_merge_bands_merges_side_by_side():
    a = pymupdf.Rect(0, 100, 50, 200)
    b = pymupdf.Rect(200, 100, 260, 200)  # same y-band, horizontal gap
    out = figures._merge_bands([a, b])
    assert len(out) == 1
    assert out[0].x0 == 0 and out[0].x1 == 260


def test_merge_bands_keeps_separate_y_bands():
    a = pymupdf.Rect(0, 100, 50, 150)
    b = pymupdf.Rect(0, 400, 50, 450)   # far below → different band
    assert len(figures._merge_bands([a, b])) == 2


def _pdf_with_image(w, h):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=600)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h))
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(40, 40, 40 + w * 0.6, 40 + h * 0.6), pixmap=pix)
    return doc, page


def test_raster_sliver_rejected_by_aspect_and_size():
    doc, page = _pdf_with_image(284, 43)
    assert figures._raster_regions(page, doc, 128, 6.0) == []


def test_raster_tall_thin_rejected():
    doc, page = _pdf_with_image(43, 284)
    assert figures._raster_regions(page, doc, 128, 6.0) == []


def test_raster_figure_accepted():
    doc, page = _pdf_with_image(400, 300)
    assert len(figures._raster_regions(page, doc, 128, 6.0)) >= 1


def test_render_and_redact_writes_png_and_strips_figure_text(tmp_path):
    doc, _ = _page_with_cluster()
    refs = figures.render_and_redact(doc, tmp_path / "images", figures.FigureOptions())
    assert refs.get(0), "expected one figure captured on page 0"
    assert (tmp_path / "images" / refs[0][0]).exists()
    remaining = doc[0].get_text()
    assert "LabelInsideFigure" not in remaining   # redacted into the image
    assert "Body paragraph" in remaining          # prose above the figure kept


# ---------------------------------------------------------- book-gated integration

_BOOK = os.environ.get("PDF2EPUB_TEST_BOOK")


@pytest.mark.skipif(not _BOOK, reason="set PDF2EPUB_TEST_BOOK to the reference PDF")
def test_reference_book_pages_have_figures():
    doc = pymupdf.open(_BOOK)
    opts = figures.FigureOptions()
    for pno in (31, 93, 170):  # 0-based: PDF pages 32, 94, 171
        regions = figures.detect_figure_regions(doc[pno], doc, opts)
        assert regions, f"expected a figure on page {pno + 1}"
        widest = max(r.width for r in regions)
        assert widest > 250   # labels absorbed → region spans most of the text column
