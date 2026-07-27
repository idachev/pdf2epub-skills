---
name: epub-translate
description: Translate an existing EPUB into another language with Gemini, preserving the book's structure, markup, images, and reading order. Inline tags survive word-order changes, chapter titles and the table of contents are translated too, and a user-supplied glossary keeps invented terminology consistent across the whole book. For Bulgarian targets, an optional BgGPT post-pass polishes prose and retranslates any English leftovers Gemini refused. Use when the user asks to translate an EPUB or e-book into another language, localize a book they own, polish a Bulgarian translation, or resume/retry a previous translation.
---

# EPUB Translator (structure-preserving, Gemini)

Companion to the `pdf2epub` skill: that one produces a clean EPUB from a PDF, this one translates an EPUB — from `pdf2epub` or bought from a store — into another language and emits a valid EPUB with the same structure, images, and reading order.

Pipeline: unpack → resolve the spine via `META-INF/container.xml` → find *translation units* (block elements holding text plus inline markup) → serialize each unit with inline tags replaced by numbered placeholders → chunk per content document → Gemini translate per chunk (concurrent, checkpointed) → rebuild each unit's children from the placeholders in the translated string → translate TOC labels → rewrite `dc:language` → repack. Optional Bulgarian polish: BgGPT post-edits the Gemini units (and retranslates leftover English) before a final rebuild.

Implementation lives alongside this skill in `scripts/` (entry: `translate_epub.py`, polish: `polish_epub_bg.py`, pure EPUB/XHTML logic: `epubdoc.py`, prompts: `prompts/`) — resolve these paths relative to this skill's own directory, not the user's project root.

## Why placeholders instead of translating XHTML directly

Handing markup to a model and asking for markup back invites dropped attributes, unbalanced tags, and invented elements. Instead each unit becomes plain text like `He said [[1]]hello[[/1]] to her.`; the model returns the same string translated, and the tags are rebuilt from the placeholders. Two consequences worth knowing:

- **Tag preservation is structural, not aspirational.** Placeholder multisets are compared before a translation is accepted, so a unit can never land with different markup than it started with.
- **Word-order changes are safe.** A target language that moves an emphasized phrase to the front of the sentence moves the placeholder pair with it, so the emphasis lands on the right words instead of on whatever happened to occupy the original span.

## Requirements

- `GEMINI_API_KEY` environment variable (`GOOGLE_API_KEY` works as an alternative; verify with `[ -n "$GEMINI_API_KEY" ]` — never print it).
- For the optional Bulgarian polish stage: `BGGPT_API_KEY` (hosted API at `https://api.bggpt.ai/v1`). Optional overrides: `BGGPT_BASE_URL` (local vLLM/Ollama), `BGGPT_MODEL` (default `bggpt-gemma-3-27b-fp8`).
- `uv` on PATH. Dependencies are declared inline (PEP 723); `uv run` resolves them automatically. No `pandoc` needed — the EPUB is repacked directly, so the source book's own CSS, fonts, and images carry through untouched.
- The `pdf2epub` skill must be present in the same plugin: this skill imports its `common.py` for the Gemini client, retry/backoff, and content-filter handling rather than forking that logic. The polish script uses a separate OpenAI-compatible client (`openai_client.py`) so the Gemini path never depends on the `openai` package.

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
| `--fallback-model NAME` | Second model to retry a unit the primary keeps refusing (off by default) |
| `--unit-retries N` | Solo attempts per model for a unit that failed in its chunk (default: 2) |
| `--repair-verbatim` | On cached chunks, re-translate units whose cached text fails current validation. Implied by `--fallback-model` |
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

## Bulgarian polish (BgGPT)

After a Gemini translation to Bulgarian, prose is often readable but not fully native — calques, name drift, stiff dialogue. Optionally run a **separate** BgGPT post-pass that:

1. **Polishes** already-Bulgarian units (grammar, idiom, register, consistent transliteration).
2. **Retranslates** units still left in English (Gemini `RECITATION`/`SAFETY` give-ups that stayed in the source language).
3. Keeps the same placeholder contract; on validation failure keeps the pre-polish text.
4. Writes its own checkpoints under `polish/` so Gemini `chunks/` stay the recoverable baseline.

Polish is **opt-in** and does not call Gemini. Two ways to feed it:

```bash
# From an existing Gemini workdir (no re-translation cost)
uv run <skill-dir>/scripts/polish_epub_bg.py \
  --workdir tmp/epub-translate/<book-id> \
  --source-epub /path/to/original.en.epub \
  -o /path/to/book.bg.polished.epub \
  --glossary /path/to/glossary.json

# From an already-emitted Bulgarian EPUB
uv run <skill-dir>/scripts/polish_epub_bg.py \
  --input-epub /path/to/book.bg.epub \
  -o /path/to/book.bg.polished.epub \
  --glossary /path/to/glossary.json
```

| Option | Meaning |
|---|---|
| `--workdir PATH` | Gemini translation workdir containing `chunks/` (needs `--source-epub`) |
| `--source-epub PATH` | Original English (or source) EPUB used for the Gemini run |
| `--input-epub PATH` | Already-translated BG EPUB (alternative to workdir mode) |
| `-o, --output PATH` | Destination polished EPUB (**required**) |
| `--glossary PATH` | Same glossary JSON as translate |
| `--model NAME` | BgGPT model (default: `$BGGPT_MODEL` or `bggpt-gemma-3-27b-fp8`) |
| `--bggpt-base-url URL` | OpenAI-compatible base URL (default: `$BGGPT_BASE_URL` or hosted API) |
| `--target-words N` | Words per polish call (default: 1000; lower than translate for careful edits) |
| `--translate-target-words N` | Must match the Gemini run's chunking when using `--workdir` (default: 1500) |
| `--concurrency N` | Parallel BgGPT calls (default: 2) |
| `--max-chunks N` / `--doc-filter SUBSTR` | Smoke-test controls |
| `--force` | Ignore polish checkpoints |
| `--english-only` | Only send leftover-English units to BgGPT (targeted EN→BG repair). In workdir mode still applies the full Gemini baseline so the rest of the book is not dropped |
| `--with-source-en` | Attach original English beside BG when available (workdir only) |
| `--dry-run-sample N` | Print N before/after pairs and exit without writing an EPUB |

Smoke-test before a full book:

```bash
mkdir -p ./tmp/claude-logs && uv run <skill-dir>/scripts/polish_epub_bg.py \
  --workdir tmp/epub-translate/<book-id> --source-epub book.epub \
  -o /tmp/polish-smoke.epub --max-chunks 2 --dry-run-sample 5 \
  > ./tmp/claude-logs/polish-smoke-$(date +%Y%m%d-%H%M%S).log 2>&1
```

The polish prompt treats long mostly-Latin units as **untranslated English** and instructs BgGPT to translate them into literary Bulgarian (not to "polish" English into different English). Short Latin names/numerals stay exempt, same as the translator's script check.

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
- **Content-filter blocks are expected on some books.** Gemini's non-configurable filters (`RECITATION`, `SAFETY`) can reject a chunk. Those are caught and retried per unit exactly like a misalignment, so a block costs one paragraph, not the run. On a well-known in-copyright novel expect a real block rate — measured at ~15% of chunks on one such book, which cost 0.74% of its paragraphs after per-unit retries.
- **Two measured properties of those blocks shape the retry ladder.** Blocks are partly **non-deterministic**, so a plain repeat of an identical request recovers a useful share (hence `--unit-retries`, default 2). And they are partly **model-specific with complementary failure sets** — a smaller model has memorized less of a famous text and trips recitation filters less often, while occasionally refusing something the primary handled. On one measured sample of 58 stuck paragraphs: primary recovered 29%, a smaller fallback 45%, and **either one 52%** — so a fallback recovers strictly more than raising retries on a single model. Order is cheapest-signal-first: the primary is exhausted before a different model is paid for.
- **Resuming repairs, it doesn't just skip.** A checkpoint holds a whole chunk, including units a previous run gave up on. With `--repair-verbatim` (implied by `--fallback-model`), loading a cached chunk re-translates exactly the units whose cached text fails the *current* validation — give-ups, and units an older/laxer validator wrongly accepted. Putting the fallback model in the cache key instead would invalidate every successful unit and re-translate the whole book to fix a handful of paragraphs.
- **Emptied markup is rejected.** A model under filter pressure sometimes returns `[[1]][[/1]]` — placeholders intact, balanced, correctly nested, but with the emphasized words dropped. Parity and nesting checks both pass, so acceptance also compares whether each pair still holds text, plus a loose word-count ratio for wholesale truncation or padding.
- **Script check.** For Cyrillic targets a unit of 8+ words that comes back mostly Latin is treated as an untranslated echo and retried. Short units (a name, a number) are exempt, since those legitimately stay in Latin script.
- **Translations are only applied after all API work succeeds**, so a mid-run failure never leaves a half-translated staging tree.
- **Cost.** Roughly 300k source words ≈ $7–9 with `gemini-3.6-flash` at `--thinking-level low`. Thinking tokens bill at the output rate, so the API default of `medium` is a large avoidable expense on a book — hence the `low` default here. Smoke-test with `--max-chunks 2` before a full run.
- **What is not translated:** `<script>`, `<style>`, `svg`/`math` subtrees, and attribute text such as `alt`/`title`. Image files, CSS, and embedded fonts pass through byte-for-byte.
- **Rewritten content documents get normalized by lxml.** Only files that actually changed are rewritten, but for those the serializer may reorder attributes and — for XHTML public doctypes — add a `<meta http-equiv="Content-Type" charset=utf-8>` to `<head>`. Both are standards-valid and cosmetic (the output *is* UTF-8); they are not content changes. Untouched files are copied byte-for-byte, so a diff against the source shows only the documents that were translated.
- **TOC labels are translated independently of chapter headings**, in one call per navigation document. A chapter title can therefore differ slightly between the table of contents and the chapter's own heading. Keeping them in one call is what makes the whole TOC internally consistent; reconciling the two is not attempted.
