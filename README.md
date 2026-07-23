# pdf2epub-skills

A Claude Code plugin marketplace with one plugin: **pdf2epub**, a Claude Code skill that converts a text-based PDF book into a clean, e-reader-ready EPUB using local text extraction plus LLM-powered cleanup.

It works with books in any language — title, author, and language are detected from the text itself, not from the PDF's (often unreliable) embedded metadata.

## Install

```
/plugin marketplace add idachev/pdf2epub-skills
/plugin install pdf2epub@pdf2epub-skills
```

Then ask Claude Code to convert a PDF, e.g. *"Convert /path/to/my-book.pdf to EPUB"*, and it will invoke the `pdf2epub` skill.

## How it works

```
PDF ──1──▶ Markdown ──2──▶ cleaned chunks ──3──▶ compiled_book.md ──4──▶ EPUB
```

1. **Parse** — [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) extracts the PDF to Markdown locally, then the text is split into ~2,000-word chunks, only at paragraph/heading boundaries (paragraphs split by page breaks are rejoined first).
2. **Clean** — each chunk goes through Gemini (`gemini-3.5-flash`) with strict directives: keep wording verbatim, never translate, strip headers/footers/page numbers, de-hyphenate, rejoin split paragraphs, normalize Markdown headings.
3. **Aggregate** — chunks are concatenated; title, author, and language are LLM-detected from the opening chunk (unless overridden) and injected as YAML frontmatter.
4. **Compile** — pandoc builds the EPUB (`--toc --split-level=2`, chapters split at `##` headings).

## Requirements

- Linux or macOS with [uv](https://docs.astral.sh/uv/) and [pandoc](https://pandoc.org/) on `PATH` (Python dependencies are declared inline in the script and resolved by `uv run` automatically)
- `GEMINI_API_KEY` environment variable

## Direct CLI usage

The skill wraps a standalone script you can also run directly:

```bash
uv run plugins/pdf2epub/skills/pdf2epub/scripts/convert_pymupdf.py "path/to/book.pdf" --keep-md
```

The EPUB lands next to the PDF as `<name>.epub`; `--keep-md` also keeps the compiled Markdown for review.

| Option | Meaning |
|---|---|
| `-o, --output PATH` | EPUB destination |
| `--title`, `--author` | Override the LLM-detected metadata |
| `-l, --language CODE` | BCP-47 EPUB language (default: LLM-detected, falling back to `en`) |
| `--model NAME` | Gemini model (default: `gemini-3.5-flash`) |
| `--keep-md` | Keep compiled Markdown next to the EPUB |
| `--max-chunks N` | Process only the first N chunks (cheap smoke test) |
| `--concurrency N` | Parallel cleanup calls (default: 4) |
| `--workdir PATH` | Checkpoint/cache directory (default: `tmp/pdf2epub`) |
| `--strip-watermark HOST` | Domain whose standalone-URL paragraphs get dropped as a distributor watermark; repeatable |

## Reliability behavior

- **Checkpoints & resume** — every cleaned chunk is written atomically to `tmp/pdf2epub/<book>-<hash>/pymupdf/chunks/` (the suffix is a content hash of the PDF, so same-named or modified PDFs never collide); rerunning skips finished chunks, so interrupted runs lose nothing. Delete the book's workdir to force a fresh run.
- **Fidelity guard** — each cleaned chunk must stay within a 0.70–1.20 output/input word ratio (one retry, then the run aborts loudly), so silent LLM truncation or paraphrasing can't slip through.
- **Safety-filter fallback** — Gemini's copyrighted-text filter rejects chunks that combine the book's title, author, and body text; such chunks are bisected by paragraph and any still-blocked paragraph is kept verbatim.
- **Rate limits** — API calls retry with exponential backoff; each parallel worker backs off independently.
- **ASCII heading ids** — headings get explicit `{#sec-NNNN}` ids injected before compiling, so EPUB validators don't flag non-Latin-script (Cyrillic, CJK, etc.) heading text turned into invalid ids.

## Testing

Run the suite with `pytest`, giving `uv` the one runtime dep the tests import
(`pymupdf`); pin Python ≥ 3.10 because the code uses PEP 604 unions:

```bash
uvx --python 3.12 --with pymupdf pytest tests/ -q
```

This is exactly what CI runs. No `GEMINI_API_KEY` or network is needed — every
Gemini call is monkeypatched, and the figure tests build/read local PDFs only.

- **Unit tests** (`tests/test_common.py`, `tests/test_figures.py`) cover the pure
  pipeline stages: chunking, watermark stripping, image-ref protection, the
  fidelity-ratio edge cases, and figure detection/redaction on synthetic PDFs
  built in-memory with PyMuPDF.
- **Fixture integration test** (`tests/test_sample_pdf.py`) runs the full
  detect → render → redact path against a committed, license-clean fixture,
  `tests/fixtures/sample_diagrams.pdf`. The fixture is authored entirely in code
  by `tests/fixtures/make_sample.py` (no third-party content) and covers the
  tricky layouts: a clean labelled diagram, a wide diagram with a body paragraph
  wrapped beside it, a text page on a background fill (a "back cover" that must
  stay text, not become an image), and a plain prose page. It asserts figures are
  captured only on the diagram pages, that narrative prose is preserved as
  reflowable text while short labels move into the image, and that the fill/text
  pages are left alone. This test always runs, CI included. Regenerate the fixture
  after changing the generator with `uv run tests/fixtures/make_sample.py`.
- **Book-gated test** (in `tests/test_figures.py`) is an *optional* extra check
  against a real book. It is skipped unless `PDF2EPUB_TEST_BOOK` points at a local
  PDF, so it never runs in CI:

  ```bash
  PDF2EPUB_TEST_BOOK="/path/to/book.pdf" uvx --python 3.12 --with pymupdf pytest tests/ -q
  ```

## Repository layout

```
.claude-plugin/marketplace.json                  marketplace manifest
plugins/pdf2epub/.claude-plugin/plugin.json       plugin manifest
plugins/pdf2epub/skills/pdf2epub/SKILL.md         Claude Code skill definition
plugins/pdf2epub/skills/pdf2epub/scripts/         converter (convert_pymupdf.py, common.py, figures.py, prompts/)
docs/specs/                                       design specs (e.g. image-preservation.md)
tests/                                            pytest suite + fixtures/ (sample_diagrams.pdf, make_sample.py)
tmp/                                              checkpoints and logs (not versioned)
```

## Delivery to Kindle

Send the EPUB via Amazon's **Send to Kindle** — it converts to the native Kindle format automatically. No AZW3/local conversion step is needed.
