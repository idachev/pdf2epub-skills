# pdf2epub-skills

A Claude Code plugin marketplace with one plugin, **pdf2epub**, hosting two complementary skills:

| Skill | What it does |
|---|---|
| **`pdf2epub`** | Converts a text-based PDF book into a clean, e-reader-ready EPUB using local text extraction plus LLM-powered cleanup. |
| **`epub-translate`** | Translates an existing EPUB into another language, preserving structure, markup, images, and reading order. Optional BgGPT post-pass polishes Bulgarian prose and retranslates leftover English. |

Both work with books in any language — for conversion, title, author, and language are detected from the text itself, not from the PDF's (often unreliable) embedded metadata.

## Install

```
/plugin marketplace add idachev/pdf2epub-skills
/plugin install pdf2epub@pdf2epub-skills
```

Then ask Claude Code to convert a PDF (*"Convert /path/to/my-book.pdf to EPUB"*), translate one (*"Translate /path/to/book.epub into Bulgarian"*), or polish a Bulgarian translation (*"Polish this BG EPUB with BgGPT"*), and it will invoke the matching skill.

## How it works

```
PDF ──1──▶ Markdown ──2──▶ cleaned chunks ──3──▶ compiled_book.md ──4──▶ EPUB
```

1. **Parse** — [PyMuPDF4LLM](https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/) extracts the PDF to Markdown locally, then the text is split into ~2,000-word chunks, only at paragraph/heading boundaries (paragraphs split by page breaks are rejoined first).
2. **Clean** — each chunk goes through Gemini (`gemini-3.5-flash`) with strict directives: keep wording verbatim, never translate, strip headers/footers/page numbers, de-hyphenate, rejoin split paragraphs, normalize Markdown headings.
3. **Aggregate** — chunks are concatenated; title, author, and language are LLM-detected from the opening chunk (unless overridden) and injected as YAML frontmatter.
4. **Compile** — pandoc builds the EPUB (`--toc --split-level=2`, chapters split at `##` headings).

## Translating an EPUB

The `epub-translate` skill takes an EPUB — from `pdf2epub` or bought from a store — and emits a translated EPUB with the same structure.

```
EPUB ──1──▶ translation units ──2──▶ translated chunks ──3──▶ rebuilt XHTML ──4──▶ EPUB
```

1. **Unpack & locate** — the spine is resolved via `META-INF/container.xml`, and each content document is scanned for *translation units*: block elements holding text plus inline markup only.
2. **Serialize** — each unit becomes a plain string with inline tags replaced by numbered placeholders (`He said [[1]]hello[[/1]].`), then units are grouped into ~1,500-word chunks per document.
3. **Translate** — each chunk goes through Gemini (`gemini-3.6-flash`, `thinking_level: low`) with a literary-fidelity prompt and an optional glossary that keeps invented terminology consistent across the whole book.
4. **Rebuild & repack** — each unit's children are rebuilt from the placeholders in the *translated* string, TOC labels are translated, `dc:language` is rewritten, and the EPUB is repacked directly (no pandoc — the book's own CSS, fonts, and images carry through untouched).

**Markup never reaches the model as markup.** That is the point of the placeholder scheme: tag preservation becomes a structural guarantee rather than a hope, and because a placeholder carries its tag identity by index, a target language that moves an emphasized phrase to the front of the sentence carries the emphasis with it instead of stranding it on the wrong words.

```bash
uv run plugins/pdf2epub/skills/epub-translate/scripts/translate_epub.py "path/to/book.epub" \
  -t bg --glossary path/to/glossary.json
```

| Option | Meaning |
|---|---|
| `-o, --output PATH` | EPUB destination (default: `<stem>.<lang>.epub` beside the input) |
| `-t, --target-language CODE` | BCP-47 target language (default: `bg`) |
| `--target-language-name NAME` | Full English language name (default: derived from the code) |
| `--glossary PATH` | JSON glossary of source→target terms applied consistently |
| `--model NAME` | Gemini model (default: `gemini-3.6-flash`) |
| `--fallback-model NAME` | Second model to retry a unit the primary keeps refusing (off by default) |
| `--unit-retries N` | Solo attempts per model for a unit that failed in its chunk (default: 2) |
| `--repair-verbatim` | On cached chunks, re-translate units failing current validation. Implied by `--fallback-model` |
| `--thinking-level LEVEL` | `minimal`/`low`/`medium`/`high`, or `none` for pre-Gemini-3 models (default: `low`) |
| `--target-words N` | Approx. source words per API call (default: 1500) |
| `--concurrency N` | Parallel calls (default: 4) |
| `--max-chunks N` | Only first N chunks (cheap smoke test) |
| `--doc-filter SUBSTR` | Only content documents whose filename contains this substring |
| `--title-suffix TEXT` | Appended to the EPUB's `dc:title` |
| `--workdir PATH` | Checkpoint dir (default: `tmp/epub-translate`) |

Glossaries are user-supplied and live outside this repo — they encode terminology for one specific book or series. The format is a JSON object with string→string pairs under `terms`; any other keys (comments, notes) are ignored, so the file doubles as working notes.

**Failures degrade rather than abort.** A chunk that comes back misaligned is retried unit by unit; a unit that still fails validation is left in the source language with a warning, and the run summary reports the count. Validation rejects a unit whose inline markup changed, whose placeholder nesting is broken, whose emphasized text was emptied out, whose length is implausible, or which came back in the wrong script — so a bad response costs one paragraph, never a corrupted chapter.

**Content filters and the retry ladder.** Translating in-copyright prose runs into Gemini's non-configurable `RECITATION` filter, which refuses rather than answers. Two measured properties shape the response: blocks are partly *non-deterministic* (a plain repeat recovers a useful share — `--unit-retries`), and partly *model-specific with complementary failure sets* (a smaller model has memorized less of a famous text, so it refuses different passages — `--fallback-model`). On one measured sample of 58 stuck paragraphs the primary recovered 29%, a smaller fallback 45%, and either one 52%.

Because checkpoints store a whole chunk including its give-ups, adding a fallback later would otherwise be served from cache. `--repair-verbatim` (implied by `--fallback-model`) re-translates exactly the units whose cached text fails current validation, so fixing a handful of paragraphs costs a handful of calls rather than a whole book:

```bash
uv run .../translate_epub.py "book.epub" -t bg --glossary g.json \
  --fallback-model gemini-3.5-flash-lite
```

### Bulgarian polish (BgGPT, optional)

After Gemini translation to Bulgarian, an optional second pass uses [BgGPT](https://models.bggpt.ai/docs) to polish literary Bulgarian and **retranslate any paragraphs still left in English** (content-filter give-ups). It is **opt-in** (not wired into `translate_epub.py -t bg`), does not re-call Gemini, and keeps its own checkpoints under `polish/` so Gemini `chunks/` stay the recoverable baseline. Pass the same `--glossary` you used for translation when you have one.

```bash
# From an existing Gemini workdir
uv run plugins/pdf2epub/skills/epub-translate/scripts/polish_epub_bg.py \
  --workdir tmp/epub-translate/<book-id> \
  --source-epub path/to/original.en.epub \
  -o path/to/book.bg.polished.epub \
  --glossary path/to/glossary.json

# Or from an already-emitted BG EPUB
uv run plugins/pdf2epub/skills/epub-translate/scripts/polish_epub_bg.py \
  --input-epub path/to/book.bg.epub \
  -o path/to/book.bg.polished.epub \
  --glossary path/to/glossary.json

# Targeted repair: only leftover English units (keeps the rest of the book as-is)
uv run plugins/pdf2epub/skills/epub-translate/scripts/polish_epub_bg.py \
  --input-epub path/to/book.bg.polished.epub \
  -o path/to/book.bg.polished.epub \
  --english-only --force --glossary path/to/glossary.json
```

Requires `BGGPT_API_KEY`. Optional: `BGGPT_BASE_URL` (local OpenAI-compatible server), `BGGPT_MODEL` (default `bggpt-gemma-3-27b-fp8`). Smoke with `--max-chunks 2` or `--dry-run-sample 5` before a full book. Full flag list: `epub-translate` skill docs (`SKILL.md`).

## Requirements

- Linux or macOS with [uv](https://docs.astral.sh/uv/) on `PATH`, plus [pandoc](https://pandoc.org/) for `pdf2epub` (Python dependencies are declared inline in each script and resolved by `uv run` automatically). `epub-translate` needs no pandoc.
- `GEMINI_API_KEY` environment variable
- For Bulgarian polish only: `BGGPT_API_KEY`

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

Run the suite with `pytest`, giving `uv` the two runtime deps the tests import
(`pymupdf` for the figure tests, `lxml` for the translation tests); pin Python ≥ 3.10
because the code uses PEP 604 unions:

```bash
uvx --python 3.12 --with pymupdf --with lxml pytest tests/ -q
```

This is exactly what CI runs. No `GEMINI_API_KEY` or network is needed — every
Gemini call is monkeypatched, and the figure tests build/read local PDFs only.

- **Unit tests** (`tests/test_common.py`, `tests/test_figures.py`) cover the pure
  pipeline stages: chunking, watermark stripping, image-ref protection, the
  fidelity-ratio edge cases, and figure detection/redaction on synthetic PDFs
  built in-memory with PyMuPDF.
- **Translation tests** (`tests/test_epubdoc.py`) cover the pure EPUB stages on
  synthetic fixtures: translation-unit discovery, placeholder serialization,
  rebuilding inline markup through word-order changes, and the rejection cases
  that keep a bad model response from corrupting a chapter — unbalanced or
  out-of-order placeholders, unknown indices, and paired-vs-void confusion. It
  also asserts a *rejected* translation leaves the element untouched, since the
  caller's fallback is "keep the source text" and that is only safe if nothing
  was half-rewritten. Container handling (spine order, dedupe, OPF metadata,
  OCF-conformant repack, zip-slip refusal) is covered too. These need `lxml`:

  ```bash
  uvx --python 3.12 --with pymupdf --with lxml pytest tests/ -q
  ```
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
  PDF2EPUB_TEST_BOOK="/path/to/book.pdf" uvx --python 3.12 --with pymupdf --with lxml pytest tests/ -q
  ```

## Repository layout

```
.claude-plugin/marketplace.json                    marketplace manifest
plugins/pdf2epub/.claude-plugin/plugin.json         plugin manifest (the only place `version` lives)
plugins/pdf2epub/skills/pdf2epub/SKILL.md           PDF→EPUB skill definition
plugins/pdf2epub/skills/pdf2epub/scripts/           converter (convert_pymupdf.py, common.py, figures.py, prompts/)
plugins/pdf2epub/skills/epub-translate/SKILL.md     EPUB translation skill definition
plugins/pdf2epub/skills/epub-translate/scripts/     translator + polish (translate_epub.py, polish_epub_bg.py, epubdoc.py, openai_client.py, prompts/)
docs/specs/                                         design specs (e.g. image-preservation.md)
tests/                                              pytest suite + fixtures/ (sample_diagrams.pdf, make_sample.py)
tmp/                                                checkpoints and logs (not versioned)
```

`epub-translate` imports `common.py` from the `pdf2epub` skill rather than forking it, so the Gemini client, retry/backoff, and content-filter handling have a single source of truth. Both skills ship in the same plugin, which is what makes that relative import stable wherever the plugin is installed.

## Delivery to Kindle

Send the EPUB via Amazon's **Send to Kindle** — it converts to the native Kindle format automatically. No AZW3/local conversion step is needed.
