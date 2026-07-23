"""End-to-end figure test against a committed, license-clean fixture PDF
(tests/fixtures/sample_diagrams.pdf, built by fixtures/make_sample.py). Unlike the
book-gated test, this always runs — in CI too — because the fixture ships with the
repo. It exercises the full detect -> render -> redact path on realistic layouts."""

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
import figures  # noqa: E402  (after importorskip)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_diagrams.pdf"


def test_fixture_present():
    assert FIXTURE.is_file(), "run tests/fixtures/make_sample.py to (re)generate the fixture"


def test_clean_figure_page_detected():
    doc = pymupdf.open(str(FIXTURE))
    regions = figures.detect_figure_regions(doc[0], doc, figures.FigureOptions())
    assert len(regions) == 1


def test_sample_pdf_end_to_end(tmp_path):
    doc = pymupdf.open(str(FIXTURE))
    refs = figures.render_and_redact(doc, tmp_path / "images", figures.FigureOptions())

    # figures captured on the two diagram pages (0,1) — not on the fill or plain-text pages
    assert set(refs) == {0, 1}
    for names in refs.values():
        assert names
        for name in names:
            assert (tmp_path / "images" / name).is_file()

    # side-by-side page: the narrative paragraph stays as reflowable text, the short
    # label is moved into the image (this is the regression the deterministic fix added)
    page2 = doc[1].get_text()
    assert "reflowable" in page2
    assert "RightLabel" not in page2

    # background-fill page (a "back cover"): left as text, never captured as a figure
    assert "narrative paragraph" in doc[2].get_text()
    assert 2 not in refs

    # plain prose page: no figure
    assert 3 not in refs
