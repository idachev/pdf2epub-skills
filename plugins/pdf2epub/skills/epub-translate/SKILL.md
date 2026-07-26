---
name: epub-translate
description: Translate an existing EPUB into another language with Gemini, preserving the book's structure, markup, images, and reading order. Inline tags survive word-order changes, chapter titles and the table of contents are translated too, and a user-supplied glossary keeps invented terminology consistent across the whole book. Use when the user asks to translate an EPUB or e-book into another language, localize a book they own, or resume/retry a previous translation.
---

# EPUB Translator (structure-preserving, Gemini)

Companion to the `pdf2epub` skill: that one produces a clean EPUB from a PDF, this one translates an EPUB — from `pdf2epub` or bought from a store — into another language and emits a valid EPUB with the same structure, images, and reading order.

Pipeline: unpack → resolve the spine via `META-INF/container.xml` → find *translation units* (block elements holding text plus inline markup) → serialize each unit with inline tags replaced by numbered placeholders → chunk per content document → Gemini translate per chunk (concurrent, checkpointed) → rebuild each unit's children from the placeholders in the translated string → translate TOC labels → rewrite `dc:language` → repack.

Implementation lives alongside this skill in `scripts/` (entry: `translate_epub.py`, pure EPUB/XHTML logic: `epubdoc.py`, prompt: `prompts/`) — resolve these paths relative to this skill's own directory, not the user's project root.

## Why placeholders instead of translating XHTML directly

Handing markup to a model and asking for markup back invites dropped attributes, unbalanced tags, and invented elements. Instead each unit becomes plain text like `He said [[1]]hello[[/1]] to her.`; the model returns the same string translated, and the tags are rebuilt from the placeholders. Two consequences worth knowing:

- **Tag preservation is structural, not aspirational.** Placeholder multisets are compared before a translation is accepted, so a unit can never land with different markup than it started with.
- **Word-order changes are safe.** A target language that moves an emphasized phrase to the front of the sentence moves the placeholder pair with it, so the emphasis lands on the right words instead of on whatever happened to occupy the original span.

## Requirements

- `GEMINI_API_KEY` environment variable (`GOOGLE_API_KEY` works as an alternative; verify with `[ -n "$GEMINI_API_KEY" ]` — never print it).
- `uv` on PATH. Dependencies are declared inline (PEP 723); `uv run` resolves them automatically. No `pandoc` needed — the EPUB is repacked directly, so the source book's own CSS, fonts, and images carry through untouched.
- The `pdf2epub` skill must be present in the same plugin: this skill imports its `common.py` for the Gemini client, retry/backoff, and content-filter handling rather than forking that logic.

## Usage

```bash
uv run <skill-dir>/scripts/translate_epub.py "/path/to/book.epub" [options]
```

Options:

| Option | Meaning |
|---|---|
| `-o, --output PATH` | EPUB destination (default: `<stem>.<lang>.epub` beside the input) |
| `-t, --target-language CODE` | BCP-47 target language (default: `bg`) |
| `--target-language-name NAME` | Full English name of the target language (default: derived from the code) |
| `--glossary PATH` | JSON glossary of source→target terms applied consistently across the book |
| `--model NAME` | Gemini model (default: `gemini-3.6-flash`) |
| `--thinking-level LEVEL` | `minimal`/`low`/`medium`/`high`, or `none` to omit for pre-Gemini-3 models (default: `low`) |
| `--target-words N` | Approx. source words per API call (default: 1500) |
| `--concurrency N` | Parallel Gemini calls (default: 4); each worker backs off on 429 |
| `--max-chunks N` | Only first N chunks — use for cheap smoke tests before a full run |
| `--doc-filter SUBSTR` | Only content documents whose filename contains this substring |
| `--title-suffix TEXT` | Appended to the EPUB's `dc:title` |
| `--workdir PATH` | Checkpoint dir (default: `tmp/epub-translate`) |

For a full book run, capture the full output to a timestamped log file — a buried warning can then be re-read without rerunning (and re-paying for) the translation:

```bash
mkdir -p ./tmp/claude-logs && uv run <skill-dir>/scripts/translate_epub.py "/path/to/book.epub" \
  -t bg --glossary /path/to/glossary.json \
  > ./tmp/claude-logs/epub-translate-$(date +%Y%m%d-%H%M%S).log 2>&1
```

## Glossary format

A glossary is the difference between a book that calls an invented device three different things and one that reads like a single translator's work. It is injected verbatim into every chunk prompt, so terminology stays identical across hundreds of independent API calls.

Glossaries are always **user-supplied and external to this skill** — they encode terminology for one specific book or series, which belongs with that book's own working directory:

```json
{
  "terms": {
    "Source Term": "целеви термин",
    "another term": "друг термин"
  }
}
```

Only string→string pairs under `terms` are read; any other keys (comments, notes, uncertainty lists) are ignored, so the file doubles as working notes. A bare `{"a": "b"}` object without a `terms` wrapper also works. Keys are matched by the model as whole terms, not by string substitution — inflecting them for grammar is the model's job.

## Notes

- **Nothing is translated twice.** Checkpoints are content-addressed on the prompt, glossary, model, thinking level, and the chunk's exact text, so an interrupted run resumes for free while an edited prompt or glossary is a clean cache miss rather than a stale hit. The staging tree is re-unpacked fresh each run — only the expensive API results are reused.
- **Failures degrade, they don't abort.** A chunk whose response comes back misaligned is retried unit by unit; a unit that still fails validation (missing placeholders, wrong script, empty) is left in the source language with a warning on stderr. Losing one paragraph's translation is recoverable; corrupting a chapter's markup is not. The final summary reports the verbatim count — check it, and grep stderr for `kept in the source language`.
- **Content-filter blocks are expected on some books.** Gemini's non-configurable filters (`RECITATION`, `SAFETY`) can reject a chunk. Those are caught and retried per unit exactly like a misalignment, so a block costs one paragraph, not the run.
- **Script check.** For Cyrillic targets a unit of 8+ words that comes back mostly Latin is treated as an untranslated echo and retried. Short units (a name, a number) are exempt, since those legitimately stay in Latin script.
- **Translations are only applied after all API work succeeds**, so a mid-run failure never leaves a half-translated staging tree.
- **Cost.** Roughly 300k source words ≈ $7–9 with `gemini-3.6-flash` at `--thinking-level low`. Thinking tokens bill at the output rate, so the API default of `medium` is a large avoidable expense on a book — hence the `low` default here. Smoke-test with `--max-chunks 2` before a full run.
- **What is not translated:** `<script>`, `<style>`, `svg`/`math` subtrees, and attribute text such as `alt`/`title`. Image files, CSS, and embedded fonts pass through byte-for-byte.
- **Rewritten content documents get normalized by lxml.** Only files that actually changed are rewritten, but for those the serializer may reorder attributes and — for XHTML public doctypes — add a `<meta http-equiv="Content-Type" charset=utf-8>` to `<head>`. Both are standards-valid and cosmetic (the output *is* UTF-8); they are not content changes. Untouched files are copied byte-for-byte, so a diff against the source shows only the documents that were translated.
- **TOC labels are translated independently of chapter headings**, in one call per navigation document. A chapter title can therefore differ slightly between the table of contents and the chapter's own heading. Keeping them in one call is what makes the whole TOC internally consistent; reconciling the two is not attempted.
