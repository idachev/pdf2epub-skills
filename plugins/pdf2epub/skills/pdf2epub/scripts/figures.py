"""Figure detection and rasterization for the PDF-to-EPUB pipeline.

The books this pipeline targets often carry their real illustrations as *vector*
graphics — thousands of individual draw ops — rather than embedded raster
images. pymupdf4llm's text extraction drops those entirely, so diagram-heavy
books came out text-only. This module finds each diagram region (plus any
genuine raster figures), renders it to a PNG, and redacts the region from a
working copy of the document so the figure's own label and caption text is not
duplicated as floating prose in the extracted Markdown.

Everything here is deterministic and network-free. PyMuPDF is imported lazily so
the module only costs anything when `--images auto` is in effect.

Detection constants were validated against the reference book (pages 32, 94,
171); see docs/specs/image-preservation.md.
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

CELL = 6.0            # occupancy-grid cell size, pt
DILATE = 3            # bridge gaps up to DILATE cells (~18pt) when connecting cells
MIN_CELLS = 120       # min covered cells for a vector cluster to count as a figure
MIN_FIGURE_PT = 60.0  # min width and height of a figure bbox, pt
BAND_GAP = 18.0       # merge regions whose vertical ranges are within this gap, pt
PAD = 4.0             # padding added around an expanded figure region, pt
ABSORB_ITERS = 6      # fixed-point cap for text-block absorption
MIN_FIGURE_PX = 128   # min smaller-dimension of a raster image to keep, px
ASPECT_MAX = 6.0      # max width:height (or its inverse) of a raster to keep
IMG_DPI = 150         # render resolution for figure PNGs


@dataclass
class FigureOptions:
    min_px: int = MIN_FIGURE_PX
    max_aspect: float = ASPECT_MAX
    min_cells: int = MIN_CELLS
    dpi: int = IMG_DPI


def _rect(x0, y0, x1, y1):
    import pymupdf

    return pymupdf.Rect(x0, y0, x1, y1)


def _vector_clusters(page, min_cells):
    """Cluster the page's vector draw ops into candidate figure bboxes via a
    coarse occupancy grid and gap-bridging connected components."""
    drawings = page.get_drawings()
    if not drawings:
        return []
    R = page.rect
    cols = int(R.width // CELL) + 1
    rows = int(R.height // CELL) + 1
    covered = [[False] * cols for _ in range(rows)]
    for d in drawings:
        r = d["rect"]
        if r.width <= 0 or r.height <= 0:
            continue
        c0 = max(0, int(r.x0 // CELL))
        c1 = min(cols - 1, int(r.x1 // CELL))
        r0 = max(0, int(r.y0 // CELL))
        r1 = min(rows - 1, int(r.y1 // CELL))
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                covered[rr][cc] = True

    seen = [[False] * cols for _ in range(rows)]
    out = []
    for sr in range(rows):
        for sc in range(cols):
            if not covered[sr][sc] or seen[sr][sc]:
                continue
            # BFS connecting covered cells within Chebyshev distance DILATE, so
            # the many thin strokes of one diagram fuse while a stray underline
            # stays its own (sub-threshold) component.
            q = deque([(sr, sc)])
            seen[sr][sc] = True
            cells = []
            while q:
                a, b = q.popleft()
                cells.append((a, b))
                for da in range(-DILATE, DILATE + 1):
                    for db in range(-DILATE, DILATE + 1):
                        na, nb = a + da, b + db
                        if 0 <= na < rows and 0 <= nb < cols and covered[na][nb] and not seen[na][nb]:
                            seen[na][nb] = True
                            q.append((na, nb))
            if len(cells) < min_cells:
                continue
            ys = [a for a, _ in cells]
            xs = [b for _, b in cells]
            rect = _rect(min(xs) * CELL, min(ys) * CELL, (max(xs) + 1) * CELL, (max(ys) + 1) * CELL) & R
            if rect.width >= MIN_FIGURE_PT and rect.height >= MIN_FIGURE_PT:
                out.append(rect)
    return out


def _raster_regions(page, doc, min_px, max_aspect):
    """Placement rects of raster images worth keeping — drops the text-line
    slivers (fail min-px or aspect) while keeping real photos/figures."""
    out = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            info = doc.extract_image(xref)
        except Exception:
            continue
        w, h = info.get("width", 0), info.get("height", 0)
        if min(w, h) < min_px or max(w, h) > max_aspect * max(1, min(w, h)):
            continue
        for r in page.get_image_rects(xref):
            rect = (_rect(r.x0, r.y0, r.x1, r.y1)) & page.rect
            if rect.width >= MIN_FIGURE_PT and rect.height >= MIN_FIGURE_PT:
                out.append(rect)
    return out


def _merge_bands(rects):
    """Union regions whose vertical ranges overlap or are within BAND_GAP, so a
    figure and its side label-column (or two side-by-side figures) become one."""
    rects = sorted(rects, key=lambda r: r.y0)
    out = []
    for r in rects:
        if out and r.y0 <= out[-1].y1 + BAND_GAP:
            out[-1] = out[-1] | r
        else:
            out.append(_rect(r.x0, r.y0, r.x1, r.y1))
    return out


def _absorb_text(page, fig):
    """Grow the region to include text blocks that strictly intersect it — the
    in-figure labels and caption that extend past the vector ink. Body text
    above the figure does not intersect, so it is left alone."""
    tblocks = [b["bbox"] for b in page.get_text("dict")["blocks"] if "lines" in b]
    for _ in range(ABSORB_ITERS):
        grew = False
        for bb in tblocks:
            tb = _rect(bb[0], bb[1], bb[2], bb[3])
            if (tb & fig).get_area() > 0 and not fig.contains(tb):
                fig |= tb
                grew = True
        if not grew:
            break
    return fig & page.rect


def detect_figure_regions(page, doc, opts):
    """Return the figure bboxes on a page, ordered top-to-bottom."""
    regions = _vector_clusters(page, opts.min_cells)
    regions += _raster_regions(page, doc, opts.min_px, opts.max_aspect)
    if not regions:
        return []
    expanded = [_absorb_text(page, r + (-PAD, -PAD, PAD, PAD)) for r in _merge_bands(regions)]
    return _merge_bands(expanded)  # re-merge in case absorption caused overlaps


def render_and_redact(doc, images_dir, opts, log=lambda _m: None):
    """Render every figure region to a PNG and redact it from `doc` in place.

    Rendering reads the original page; redaction then blanks the same regions so
    pymupdf4llm's later text pass does not re-emit the figure's label/caption
    text. Returns {page_index: [png_filename, ...]} in reading order.
    """
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    refs = {}
    for pno in range(len(doc)):
        page = doc[pno]
        figs = detect_figure_regions(page, doc, opts)
        if not figs:
            continue
        names = []
        for k, rect in enumerate(figs):
            page.get_pixmap(clip=rect, dpi=opts.dpi).save(str(images_dir / f"fig-{pno + 1:04d}-{k:02d}.png"))
            names.append(f"fig-{pno + 1:04d}-{k:02d}.png")
            page.add_redact_annot(rect)
            log(f"  figure fig-{pno + 1:04d}-{k:02d}.png: {round(rect.width)}x{round(rect.height)}pt on page {pno + 1}")
        # graphics=2 (REMOVE_IF_TOUCHED): strip every vector stroke of the figure,
        # not just those fully covered — otherwise leftover line art makes
        # pymupdf4llm rasterize+OCR the region (slow, and needs Tesseract).
        import pymupdf

        page.apply_redactions(graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
        refs[pno] = names
    return refs
