---
name: pdf2epub
description: Convert a text-based PDF book into a clean, e-reader-ready EPUB using local PyMuPDF4LLM extraction plus Gemini text cleanup. Works with any source language — title, author, and language are auto-detected. Use when the user asks to convert a PDF book to EPUB, clean up a scanned/exported PDF for Kindle or another e-reader, or resume/retry a previous conversion.
---

# PDF to EPUB Converter (PyMuPDF4LLM + Gemini)

Pipeline: local Markdown extraction (PyMuPDF4LLM) → chunking at paragraph boundaries → Gemini cleanup per chunk (verbatim, de-hyphenation, header/footer stripping) → metadata extraction (title, author, language) → pandoc EPUB compile. Implementation lives alongside this skill in `scripts/` (entry: `convert_pymupdf.py`, shared stages: `common.py`, prompts: `prompts/`) — resolve these paths relative to this skill's own directory, not the user's project root.

## Requirements

- `GEMINI_API_KEY` environment variable (`GOOGLE_API_KEY` works as an alternative; verify with `[ -n "$GEMINI_API_KEY" ]` — never print it).
- `uv` and `pandoc` on PATH. Dependencies are declared inline (PEP 723); `uv run` resolves them automatically.

## Usage

```bash
uv run <skill-dir>/scripts/convert_pymupdf.py "/path/to/book.pdf" [options]
```

Options:

| Option | Meaning |
|---|---|
| `-o, --output PATH` | EPUB destination (default: `<pdf-dir>/<stem>.epub`) |
| `--title`, `--author` | Override metadata (default: LLM-extracted from the first chunk; embedded PDF metadata is frequently unreliable, so extraction from the actual text is the default) |
| `-l, --language CODE` | BCP-47 EPUB language, e.g. `en`, `bg`, `fr`, `de` (default: LLM-detected from the text, falling back to `en`) |
| `--model NAME` | Gemini model (default: `gemini-3.5-flash`) |
| `--keep-md` | Keep compiled Markdown next to the EPUB |
| `--keep-toc` | Keep the source PDF's own printed Contents/TOC listing in the EPUB body (dropped by default) |
| `--max-chunks N` | Only first N chunks — use for cheap smoke tests before a full run |
| `--concurrency N` | Parallel Gemini cleanup calls (default: 4); each worker backs off on 429 |
| `--workdir PATH` | Checkpoint dir (default: `tmp/pdf2epub`) |
| `--strip-watermark HOST` | Domain (e.g. `scan-site.com`) whose standalone-URL paragraphs get dropped as a distributor watermark; repeatable, no domains stripped by default |
| `--images {auto,off}` | Capture figures/diagrams as images (`auto`, default) or skip them for a text-only EPUB (`off`) |
| `--image-min-px N`, `--image-max-aspect R` | Raster-image filter: min smaller-dimension px (default 128) and max width:height (default 6) — drops text-line slivers |
| `--figure-min-cells N`, `--figure-dpi N` | Vector-figure detection: min cluster size (default 120) and render resolution (default 150) |

For a full book run, capture the full output to a timestamped log file — a buried error can then be re-read without rerunning (and re-paying for) the conversion:

```bash
mkdir -p ./tmp/claude-logs && uv run <skill-dir>/scripts/convert_pymupdf.py "/path/to/book.pdf" \
  > ./tmp/claude-logs/pdf2epub-$(date +%Y%m%d-%H%M%S).log 2>&1
```

## Behavior to know

- **Resume:** every cleaned chunk is checkpointed under `<workdir>/<pdf-stem>-<hash>/pymupdf/chunks/` (the 8-char suffix is a content hash of the PDF **plus the image options**, so different PDFs — or the same PDF at different `--images`/figure settings — never reuse each other's cache); rerunning skips cached chunks. Each chunk checkpoint is itself named by a hash of its source text **and** the cleanup prompt, so a re-chunked extraction or an edited prompt is a clean cache miss rather than being served stale (identical reruns still hit 100%). Stage-1 extraction is cached as `extracted.md`; that upstream file is **not** auto-invalidated on a code change, so after editing extraction/figure code delete the book's workdir to force a fresh run.
- **Fidelity check:** each cleaned chunk must keep a 0.70–1.20 output/input word ratio; one retry, then the run aborts loudly. Inspect the offending chunk in the checkpoint dir.
- **Safety-filter blocks:** Gemini's input filter (and output-side `finish_reason` blocks like `SAFETY`/`RECITATION`) rejects chunks combining the book's title+author with body text. The pipeline auto-bisects blocked chunks and keeps still-blocked single paragraphs verbatim — warnings appear on stderr; this is expected for chunk 1.
- **Contents/TOC stripping (default on, `--keep-toc` to disable):** the source PDF's own printed Contents page (chapter titles + page numbers, with no real links) is a non-clickable duplicate of the nav pandoc already builds from the compiled headings (`--toc`). `strip_table_of_contents()` (Stage 1) finds the first heading whose text is exactly "Contents"/"Table of Contents" and drops the TOC-shaped blocks that follow it — pymupdf4llm renders these as a single-column pipe table, or as dot-leader lines (`Chapter Title .......... 12`) in plainer PDFs — including short running headers/titles repeated between TOC pages, stopping at the first block that looks like real prose. If nothing after the heading matches that shape, the Markdown is left untouched rather than risk dropping a real section.
- **Watermark stripping:** pass one or more `--strip-watermark HOST` flags for domains found in the source scan (e.g. a distributor site plugging itself in a standalone paragraph); `strip_watermarks()` (Stage 3) then deterministically drops paragraphs that are only that URL. The cleanup prompt also asks the LLM to strip obvious scan-site watermarks heuristically, but the deterministic pass is the actual guarantee — use it whenever a book's source PDF carries a known watermark domain.
- **Language/metadata auto-detection:** if `--title`/`--author`/`--language` aren't all given, the pipeline sends the first cleaned chunk to Gemini to fill in whatever's missing (title/author kept in the book's original language; language as a BCP-47 code). Override any of the three explicitly to skip detection for that field. The model is asked for the full language name ("Italian") before the code ("it") — a bare code is ambiguous to an LLM — and the final summary prints both (`language: it (Italian)`); check that line to confirm detection got the right language.
- **ASCII heading ids:** pandoc's EPUB writer slugifies each heading's own text into its section `id`, which is invalid (non-ASCII) for non-Latin-script headings (Cyrillic, CJK, etc.) per strict EPUB id validation. `assign_ascii_heading_ids()` (Stage 3) injects explicit `{#sec-NNNN}` header attributes into the compiled Markdown before compiling, so ids are always ASCII regardless of the book's script; pandoc's own nav/TOC generation picks these up automatically.
- **Images (`--images auto`, default):** the pipeline captures figures itself with PyMuPDF — it does **not** use pymupdf4llm's `write_images` (which emitted text-line slivers). **Vector diagrams** (line-art illustrations, the common case in this genre) are found by clustering the page's draw ops, expanded to swallow their labels/captions, rendered to a PNG, and **redacted** from the page so the label text isn't duplicated as loose prose. **Raster images** are kept only if they pass a size/aspect filter (drops slivers). **Body text is protected:** within a figure region, narrative paragraphs (≥22 words) are kept as reflowable EPUB text and blanked from the rendered image, while short labels stay in the image — so a diagram sharing a page with body text (text wrapped beside a wide figure) doesn't bake prose into a picture. A page of text on a background fill (few vector ops, e.g. a back cover) is left as text, not captured. Refs are `images/fig-PPPP-KK.png`, placed at the end of their page's text (figures in this genre sit at the page bottom, so this reads correctly); mid-page placement is a known limitation. Image refs bypass Gemini entirely (never reworded, excluded from the fidelity ratio) and pandoc bundles them into `EPUB/media/`. Tune with `--figure-min-cells` / `--image-min-px` / `--image-max-aspect` if a book over- or under-detects; each captured figure is logged with its page and size. Use `--images off` for a fast text-only EPUB.
- **OCR:** pymupdf4llm 1.28 runs Tesseract on image-heavy pages on its own (pre-existing). The figure path passes `ignore_images=True` so leftover slivers aren't OCR'd; you may still see a one-time "Using Tesseract for OCR processing" line, which is harmless.
- **Cost:** a ~100-page book ≈ 28 chunks ≈ 0.60 USD with gemini-3.5-flash. Smoke-test with `--max-chunks 2` first when iterating on prompts. (Do not write a dollar sign followed by a digit in this file — skill argument substitution mangles it.)
- Prompt changes: edit `scripts/prompts/clean_chunk.md` (cleanup rules) or `extract_metadata.md`. The `clean_chunk` prompt is part of each chunk checkpoint's hash, so editing it auto-invalidates the cleanup cache (no manual delete needed); `extract_metadata` runs once and isn't cached.

## Verify the output

- **Structural validity (authoritative):** run `epubcheck <out>.epub` if it is installed. If not, but calibre is, its "Check Book" engine is exposed via `calibre-debug`:
  ```bash
  calibre-debug -c "
  from calibre.ebooks.oeb.polish.check.main import run_checks
  from calibre.ebooks.oeb.polish.container import get_container
  from calibre.ebooks.oeb.polish.check.base import WARN
  c = get_container('<out>.epub', tweak_mode=True)
  errors = run_checks(c)
  warn_count = 0
  for e in errors:
      if e.level == WARN:
          warn_count += 1
          print('WARN:', e.name, e.msg)
  print('warn_count:', warn_count, '/ total:', len(errors))
  "
  ```
  Expect `warn_count: 0` (the ASCII-id fix above clears the "Invalid id" warnings this used to report). A baseline ~30-70 `ERROR`-level findings are expected and harmless — they're pandoc's own default EPUB template boilerplate (empty CSS placeholders like `h1.title { }`, selector-ordering nitpicks), not something this pipeline introduces. Don't use a list/dict comprehension referencing an outer variable in `calibre-debug -c` scripts — its exec scoping breaks closures; use a plain `for` loop instead.
- Check `dc:language`/`dc:title`/`dc:creator` in `EPUB/content.opf`.
- Skim the `--keep-md` Markdown for leftover page numbers or broken paragraphs. Check the title page especially for displaced drop-cap letters (e.g. `HIRD EDITION (T )`) — decorative typesetting is where extraction most often mangles text; the cleanup prompt repairs the obvious cases, but a decorated title page can still slip through.
- Deliver via Amazon Send to Kindle (EPUB is auto-converted; no AZW3 needed).
- If a rendering complaint comes from testing on-device (any e-reader app, e.g. Moon+ Reader on Android): check the actual generated CSS/XHTML first (`unzip -p <epub> EPUB/styles/*.css`) before assuming the pipeline is at fault — the reader's own settings (text alignment, "use publisher's style" toggle) are a common cause once the CSS itself checks out.
