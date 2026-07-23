#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.21", "pymupdf4llm>=0.0.17"]
# ///
"""PDF-to-EPUB converter: local PyMuPDF4LLM extraction + Gemini cleanup."""

from pathlib import Path

import common

PARSER = "pymupdf"


def extract_markdown(pdf: Path, cache: Path) -> str:
    if cache.exists():
        print("stage 1: using cached extraction")
        return cache.read_text(encoding="utf-8")
    import pymupdf4llm

    md = pymupdf4llm.to_markdown(str(pdf))
    common.atomic_write(cache, md)
    return md


def main() -> None:
    common.setup_output()
    args = common.base_arg_parser(PARSER).parse_args()
    wd = common.work_dir(args, PARSER)
    md = extract_markdown(args.input_pdf, wd / "extracted.md")
    chunks = common.chunk_markdown(md)
    print(f"stage 1: {len(chunks)} chunks")
    client = common.get_client()
    cleaned = common.clean_chunks(
        client, args.model, chunks, wd / "chunks", args.max_chunks, args.concurrency
    )
    common.finalize(args, wd, cleaned, client)


if __name__ == "__main__":
    main()
