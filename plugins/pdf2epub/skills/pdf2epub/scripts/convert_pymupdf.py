#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.21", "pymupdf4llm>=0.0.17", "pymupdf>=1.24"]
# ///
"""PDF-to-EPUB converter: local PyMuPDF4LLM extraction + Gemini cleanup."""

from pathlib import Path

import common

PARSER = "pymupdf"


def extract_markdown(pdf: Path, cache: Path, images_dir: Path, opts) -> str:
    """Extract Markdown, optionally capturing figures as images.

    With `opts` (images enabled): detect + render figure regions, redact them so
    their text isn't duplicated, extract per-page text, and append each page's
    image refs after its text. Without `opts`: plain text-only extraction.
    """
    if cache.exists():
        print("stage 1: using cached extraction")
        return cache.read_text(encoding="utf-8")
    import pymupdf4llm

    if opts is None:
        md = pymupdf4llm.to_markdown(str(pdf))
    else:
        import shutil

        import pymupdf

        import figures

        # fresh extraction (cache miss): drop any images from a prior/aborted run so the
        # folder only ever holds figures referenced by this extraction
        if images_dir.exists():
            shutil.rmtree(images_dir)
        doc = pymupdf.open(str(pdf))
        refs = figures.render_and_redact(doc, images_dir, opts, log=print)
        n_figs = sum(len(v) for v in refs.values())
        print(f"stage 1: captured {n_figs} figure(s) across {len(refs)} page(s)")
        # ignore_images: we captured every figure ourselves and redacted it, so the
        # only rasters left are the source's text-line slivers — let pymupdf4llm skip
        # them rather than emit/OCR them into the text.
        pages = pymupdf4llm.to_markdown(doc, page_chunks=True, ignore_images=True)
        parts = []
        for pno, chunk in enumerate(pages):
            text = chunk["text"].rstrip()
            if text:
                parts.append(text)
            for name in refs.get(pno, []):
                parts.append(f"![](images/{name})")
        md = "\n\n".join(parts)
    common.atomic_write(cache, md)
    return md


def main() -> None:
    common.setup_output()
    args = common.base_arg_parser(PARSER).parse_args()
    wd = common.work_dir(args, PARSER)
    opts = None
    if args.images == "auto":
        import figures

        opts = figures.FigureOptions(
            min_px=args.image_min_px,
            max_aspect=args.image_max_aspect,
            min_cells=args.figure_min_cells,
            dpi=args.figure_dpi,
        )
    md = extract_markdown(args.input_pdf, wd / "extracted.md", wd / "images", opts)
    chunks = common.chunk_markdown(md, strip_toc=not args.keep_toc)
    print(f"stage 1: {len(chunks)} chunks")
    client = common.get_client()
    cleaned = common.clean_chunks(
        client, args.model, chunks, wd / "chunks", args.max_chunks, args.concurrency
    )
    common.finalize(args, wd, cleaned, client)


if __name__ == "__main__":
    main()
