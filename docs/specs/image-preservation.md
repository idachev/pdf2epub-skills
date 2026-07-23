# Spec: Preserve images in the generated EPUB

Status: proposed · Author: pdf2epub maintainers

## 1. Problem

The pipeline drops every image. `extract_markdown()` in
`convert_pymupdf.py` calls `pymupdf4llm.to_markdown(str(pdf))` with defaults,
and `write_images`/`embed_images` both default to `False`, so illustrations,
figures, and diagrams never enter the Markdown and therefore never reach the
EPUB. For an illustrated book (e.g. *Naked Spirit*, "Supported with 271
Illustrations") the result is text-only.

## 2. Findings from the reference book (grounding, not assumptions)

Measured on `Naked Spirit … .pdf` (308 pages) with pymupdf4llm 1.28.0:

- **90 raster image XObjects across 15 pages**, plus **~37,600 vector draw
  ops in the first 40 pages alone**. The bulk of the "illustrations" are
  **vector graphics**, not raster images.
- Running `write_images=True` on 4 sample pages produced **45 image files, 44
  of which are thin horizontal slivers** (e.g. `284×43`, `377×28`, `921×10`,
  `765×7`). These are lines of text rendered as raster strips in the source
  layout — **noise, not figures**. A naive `write_images=True` would interleave
  hundreds of these slivers into the text and make the EPUB *worse* than
  text-only.
- pymupdf4llm emits refs as `![](<image_path>/<stem>.pdf-PPPP-NN.png)` — the
  path is literally the `image_path` string joined to the filename, i.e.
  **relative to the extraction CWD, not to the Markdown file**.
- Filenames embed the PDF stem verbatim, so they can contain spaces, em-dashes,
  and non-ASCII (`’`) — a portability risk for EPUB media names.
- pymupdf4llm **auto-invoked Tesseract OCR** on the image-heavy title page — a
  heavy, slow, external dependency that must not become an implicit hard
  requirement.
- pandoc **automatically bundles** a relative local image ref into the EPUB
  (`![](images/fig1.png)` → `EPUB/media/file0.png`) when the path resolves
  relative to the Markdown file. No extra pandoc flags are required beyond
  ensuring the working directory / ref paths line up.

**Conclusion:** the fix is not "flip `write_images=True`". It is "extract
images, then keep only figure-like ones, route them safely around the LLM
cleanup pass, and let pandoc bundle them." Sliver filtering is the core of the
work.

## 3. Goals

1. Real figures/illustrations that pymupdf4llm captures as raster images appear
   in the EPUB, in reading order.
2. Text-line slivers and decorative noise are filtered out.
3. Image references survive the Gemini cleanup pass **uncorrupted** and do not
   distort the fidelity word-count ratio.
4. Resume/caching still works; images live under the cached extraction dir.
5. No new *hard* dependency (Tesseract stays optional and off by default).
6. Fully backward-compatible default behavior is acceptable to change to
   "images on with filtering", but must degrade gracefully to text-only when a
   book yields no figure-like images.

## 4. Non-goals (this iteration)

- OCR of scanned pages. Note pymupdf4llm 1.28 *already* runs Tesseract OCR on
  image-heavy pages in the plain text path (pre-existing, not introduced here).
  The figure path passes `ignore_images=True` so pymupdf4llm skips the leftover
  raster slivers rather than OCR them (verified: `OCR on page.number=…` drops to
  zero), but we do not otherwise try to add or remove OCR.
- Precise **within-page** figure ordering. Figure refs are inserted at the end
  of their page's text (figures in the reference book sit at the page bottom, so
  this reads correctly); mid-page interleaving is a later refinement.
- Image deduplication across the whole book beyond exact-content hashing.
- Re-compression / resizing of rendered images.

## 5. Design

The pipeline does **all** image work itself with PyMuPDF and never uses
pymupdf4llm's `write_images` (which produced the slivers and triggered OCR).
pymupdf4llm is used for text only. New module `scripts/figures.py` owns
detection/rendering/redaction; `common.py` owns ref handling and LLM protection.

### 5.1 Figure detection — vector diagrams (`figures.py`)

Validated on the reference book (pages 32, 94, 171). For each page:

1. **Occupancy grid over vector draw-op rects.** Mark a coarse grid (cell
   `CELL = 6 pt`) covered by any `page.get_drawings()` rect with positive area.
2. **Connected components with gap bridging.** BFS over covered cells,
   connecting cells within Chebyshev distance `DILATE = 3` cells (≈18 pt), so
   the thousands of thin strokes making up one diagram fuse into one component
   while a stray underline stays separate. Keep a component only if it covers
   `≥ MIN_CELLS = 120` original cells and its bbox is `≥ 60×60 pt`.
3. **Vertical-band merge.** Union components whose y-ranges overlap or are
   within `BAND_GAP = 18 pt`, so a figure and its side label-column (separated
   by a horizontal gap) become one region. *(Without this, page 171's bodies and
   their chakra-label boxes split into two crops.)*
4. **Absorb intersecting text blocks (iterated to a fixed point).** Grow the
   region to include any `get_text("dict")` block that **strictly intersects**
   it — this pulls in the in-figure labels (Wisdom, Crown Chakra…) and the
   caption (Fig. 8:6…) that extend past the vector ink, while body text *above*
   the figure never intersects and is left alone. Pad by `PAD = 4 pt`.

Each surviving region is a figure bbox. Constants live at module top and are
overridable via CLI (§5.5).

### 5.2 Figure detection — raster images (`figures.py`)

For each `page.get_images()` placement rect, keep it as a figure region only if
the source pixmap's smaller dimension `≥ MIN_FIGURE_PX = 128 px` **and** aspect
ratio within `[1/ASPECT_MAX, ASPECT_MAX]` (`ASPECT_MAX = 6`). This drops the
book's text-line slivers (`284×43`, `921×10`, …) while keeping real photos or
scanned figures. Raster regions join the vector regions and go through the same
band-merge, so a raster inside a vector figure doesn't double up.

### 5.3 Render + redact (`figures.py`)

Per page, in order:

1. **Render** each figure bbox from the original page:
   `page.get_pixmap(clip=bbox, dpi=IMG_DPI=150)` → `images/fig-PPPP-KK.png`
   (page + index; ASCII, so EPUB media names are always safe regardless of the
   source PDF stem).
2. **Redact** the same bboxes on the (in-memory) working document
   (`add_redact_annot` + `apply_redactions`), then hand that redacted document
   to pymupdf4llm for text. This removes the in-figure label/caption text so it
   is **not duplicated** as floating prose next to the image. *(Verified: after
   redaction page 171's remaining text is exactly the body paragraphs.)*

Return `{page_index: [ref_name, …]}` ordered top-to-bottom.

### 5.4 Extraction assembly (`convert_pymupdf.py`)

- Extract into `<workdir>/images/` (sibling of `chunks/`, `extracted.md`);
  covered by the content-hash cache, so figures render once and survive resume.
- `pymupdf4llm.to_markdown(redacted_doc, page_chunks=True, ignore_images=True)`
  → per-page text (`ignore_images` so the leftover slivers are neither emitted
  nor OCR'd). For each page, append its figure refs
  (`![](images/fig-PPPP-KK.png)`) after the page's text, then join pages.
- Cache the assembled Markdown (refs included) as `extracted.md` via
  `atomic_write`.
- `--images off` skips `figures.py` entirely (current text-only behavior, fast,
  no Pillow/extra work) for a clean regression baseline.

### 5.5 Reference protection through cleanup (`common.py`)

The chunker splits on blank lines, so each `![...](...)` ref is its own block.

- **Never send image-only blocks to Gemini.** `clean_chunks` splits every chunk
  into text vs image-ref segments (regex `^!\[.*\]\(.*\)$`, whitespace-wrapped
  allowed), cleans only the text segments, and reassembles in order. Image refs
  are therefore passed through **verbatim** and **excluded from the fidelity
  word-count ratio** (a figure-dense chunk can't trip the 0.70–1.20 guard, and
  the model can't drop/reword a ref). This is the deterministic option — no
  reliance on prompt compliance.
- `merge_split_paragraphs` must not merge an `![`-starting block into an
  adjacent paragraph (add the guard alongside the existing `#` guard).
- `strip_page_artifacts` leaves refs untouched (it only removes rules and bare
  numbers adjacent to rules — an image ref is neither).

### 5.6 Compile (`common.py: finalize` / `compile_epub`)

- `compiled_book.md` lives in `<workdir>/`; images in `<workdir>/images/`; refs
  are `images/fig-….png`. Run pandoc with `--resource-path=<workdir>` so refs
  resolve regardless of CWD; pandoc bundles them into `EPUB/media/`
  automatically (confirmed).
- With `--keep-md`, copy `images/` next to the kept `.md` so its refs resolve
  for the user too.

### 5.7 CLI (`common.py: base_arg_parser`)

- `--images {auto,off}` (default `auto`).
- Tuning knobs (default to the validated constants): `--image-min-px`,
  `--image-max-aspect`, `--figure-min-cells`, `--figure-dpi`.
- Update `SKILL.md` (options table + an "Images" note describing vector-diagram
  capture, the sliver filter, and end-of-page figure placement).

### 5.8 Dependencies

- `pymupdf` already ships transitively via `pymupdf4llm`; import it directly in
  `figures.py` and add it explicitly to the PEP-723 deps. No Pillow needed —
  raster dimensions come from `doc.extract_image(...)`.
- Tesseract is pulled in by pymupdf4llm's own OCR fallback (pre-existing); this
  feature neither adds nor removes that.

## 6. Testing

CI-portable unit tests (build synthetic PDFs in-memory with PyMuPDF — no book,
no network). Add `pymupdf`/`pillow` to the CI test run
(`uvx --python 3.12 --with pymupdf --with pillow pytest`):

- `figures.detect_figure_regions`: a synthetic page with a dense cluster of
  short strokes + a separate body-text block yields exactly one region that
  covers the strokes and any strictly-intersecting label, and excludes the body
  text above.
- band-merge: two clusters separated by a horizontal gap but sharing a y-range
  merge into one region; two clusters in different y-bands stay separate.
- raster filter: a `284×43` and a `921×10` image are rejected; a `400×300` image
  is accepted; aspect and min-px branches both covered.
- render+redact: after redaction the figure-region text is gone from
  `page.get_text()` while body text remains; a PNG is written per region.
- `common` ref protection: an image-only block is detected, excluded from the
  word-count ratio, and passed through `clean_chunks` untouched (extend the
  existing `monkeypatch` fake-generate test); `merge_split_paragraphs` does not
  merge an `![](…)` block.

Book-gated integration test (skipped in CI): if `PDF2EPUB_TEST_BOOK` points at
the *Naked Spirit* PDF, assert ≥1 figure region on pages 32, 94, 171 and that
each region's bbox spans most of the text column width (labels absorbed).

Manual end-to-end (costs Gemini tokens): convert a page range containing
pages 32/94/171; assert `EPUB/media/` figure count matches, calibre
`warn_count: 0`, and spot-check the kept `.md` and rendered figures.

## 7. Rollout / validation gates

1. All unit tests green in CI (with pymupdf/pillow added to the run).
2. Book-gated detection test passes locally on the reference PDF.
3. Manual end-to-end on pages 32/94/171: figures present and complete,
   `warn_count: 0`, no duplicated label text in prose.
4. `--images off` reproduces the 0.3.0 text-only output (regression guard).
5. Bump to 0.4.0; update `SKILL.md` + `plugin.json`.

## 8. Risks

- **Body text baked into a figure image.** A wide diagram (e.g. the book's
  Kundalini page, whose dashed aura spans the full width) or a full-page fill can
  make a figure region cover body paragraphs. Mitigated in `_plan_page` /
  `render_and_redact`: text blocks inside a region are split by word count
  (`_split_body_vs_labels`) — narrative prose (≥ `BODY_MIN_WORDS`) is kept as
  reflowable EPUB text and *blanked from the rendered image* (rendered from a
  scratch page with those glyphs redacted), while short labels stay in the image
  and are removed from the text. Residual risk: a genuinely long in-figure
  annotation (≥ `BODY_MIN_WORDS`) is treated as prose (pulled out of the image);
  a very short caption is treated as a label (kept in the image, not searchable).
- **Text page misread as a figure.** A page of text on a background fill (a back
  cover) has almost no vector ink. `_plan_page` drops any region with fewer than
  `MIN_FIG_DRAW_OPS` vector ops and no raster, so it stays as text. Measured
  separation on the reference book is wide (3 ops for the back cover vs 300–6000
  for real figures).
- **Over/under-detection** of vector figures on other books. Mitigation:
  `--figure-min-cells` / `--image-min-px` / `--image-max-aspect` knobs; log each
  detected region's page and size so a user can re-tune and rerun. The workdir
  key includes an image-options signature (`_image_options_signature`), so
  changing any of these flags forces a fresh extraction rather than silently
  reusing the cached `extracted.md`.
- **Within-page ordering**: refs land at page end (correct for this book;
  documented limitation for mid-page figures).
- **Performance**: `get_drawings()` on figure-dense pages is the main cost;
  detection is a one-time cached step and most pages have few drawings. Skip
  pages with no drawings early.
- **EPUB size** grows on figure-dense books. Acceptable; note in SKILL.md.
