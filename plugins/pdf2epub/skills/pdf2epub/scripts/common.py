"""Shared stages of the PDF-to-EPUB pipeline (see docs/plans/PDF_to_EPUB_Pipeline_Spec.md).

Used by convert_pymupdf.py (local PyMuPDF4LLM extraction + Gemini cleanup).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
DEFAULT_MODEL = "gemini-3.5-flash"
TARGET_CHUNK_WORDS = 2000
FIDELITY_BOUNDS = (0.70, 1.20)  # cleaned/source word ratio; generous to allow header stripping
RETRYABLE_CODES = {429, 500, 502, 503, 504}
BLOCKING_FINISH_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}
DEFAULT_LANGUAGE = "en"

# ---------------------------------------------------------------- CLI / paths


def base_arg_parser(parser_name: str) -> argparse.ArgumentParser:
    # progress must reach redirected log files immediately, not sit in the block buffer
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    p = argparse.ArgumentParser(description=f"PDF-to-EPUB converter ({parser_name} parser)")
    p.add_argument("input_pdf", type=Path, help="Source PDF file")
    p.add_argument("-o", "--output", type=Path, help="Destination EPUB path")
    p.add_argument("--title", help="Override document title")
    p.add_argument("--author", help="Override document author")
    p.add_argument(
        "-l", "--language", default=None,
        help="BCP-47 language code (default: auto-detected from the book's text, falling back to en)",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    p.add_argument("--keep-md", action="store_true", help="Keep compiled Markdown next to the EPUB")
    p.add_argument("--max-chunks", type=int, help="Process only the first N chunks (smoke test)")
    p.add_argument(
        "--concurrency", type=int, default=4,
        help="Parallel Gemini cleanup calls (default: 4); 429s back off per worker",
    )
    p.add_argument(
        "--workdir",
        type=Path,
        default=Path("tmp/pdf2epub"),
        help="Checkpoint/cache directory (default: tmp/pdf2epub)",
    )
    p.add_argument(
        "--strip-watermark",
        action="append",
        default=[],
        metavar="HOST",
        help="Domain (e.g. scan-site.com) whose standalone-URL paragraphs should be dropped "
        "as distributor watermarks; repeatable",
    )
    return p


def work_dir(args: argparse.Namespace, parser_name: str) -> Path:
    d = args.workdir / args.input_pdf.stem / parser_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def atomic_write(path: Path, text: str) -> None:
    """Write via tmp+rename so an interrupted run never leaves a truncated checkpoint."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------- Gemini


def get_client():
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("error: GEMINI_API_KEY is not set")
    from google import genai

    return genai.Client()


class PromptBlockedError(RuntimeError):
    """The input was rejected by Gemini's non-configurable content filter."""


def generate(client, model: str, contents, system_instruction: str, max_retries: int = 5) -> str:
    """generate_content with exponential backoff on rate limits / transient errors."""
    from google.genai import errors, types

    delay = 5.0
    last_error = "unknown error"
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction, temperature=0.1
                ),
            )
            if resp.prompt_feedback and resp.prompt_feedback.block_reason:
                # deterministic input block (e.g. copyrighted-text filter) — no point retrying
                raise PromptBlockedError(str(resp.prompt_feedback.block_reason))
            text = strip_md_fences((resp.text or "").strip())
            if text:
                return text
            finish = resp.candidates[0].finish_reason if resp.candidates else None
            finish_name = getattr(finish, "name", None)
            if finish_name in BLOCKING_FINISH_REASONS:
                # output-side block is as deterministic as an input block — let callers bisect
                raise PromptBlockedError(f"finish_reason={finish_name}")
            last_error = f"empty model response (finish_reason={finish_name})"
        except errors.APIError as e:
            if e.code not in RETRYABLE_CODES:
                raise
            last_error = f"API error {e.code}"
        if attempt == max_retries:
            break
        print(f"  {last_error}; retrying in {delay:.0f}s", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 120)
    sys.exit(f"error: Gemini call failed after {max_retries + 1} attempts ({last_error})")


def strip_md_fences(text: str) -> str:
    m = re.fullmatch(r"```(?:markdown|md|json)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text


# ------------------------------------------------- Stage 1: chunking helpers

_TERMINAL_PUNCT = tuple('.!?…:;"\'»)*')


def strip_page_artifacts(md: str) -> str:
    """Drop page-separator rules and bare page-number lines left by local extractors."""
    kept = []
    for line in md.splitlines():
        s = line.strip()
        if re.fullmatch(r"[-*_]{3,}", s):
            continue
        if re.fullmatch(r"\d{1,4}", s):
            continue
        kept.append(line)
    return "\n".join(kept)


def merge_split_paragraphs(blocks: list[str]) -> list[str]:
    """Rejoin paragraphs split by page breaks before chunking (Stage 1 requirement)."""
    merged: list[str] = []
    for block in blocks:
        if merged and not block.startswith("#"):
            prev = merged[-1].rstrip()
            first = block.lstrip()[:1]
            if not merged[-1].startswith("#") and not prev.endswith(_TERMINAL_PUNCT) and first.islower():
                if prev.endswith("-"):
                    merged[-1] = prev[:-1] + block.lstrip()
                else:
                    merged[-1] = prev + " " + block.lstrip()
                continue
        merged.append(block)
    return merged


def chunk_markdown(md: str, target_words: int = TARGET_CHUNK_WORDS) -> list[str]:
    """Split Markdown into ~target_words chunks, only at paragraph/heading boundaries."""
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", strip_page_artifacts(md)) if b.strip()]
    blocks = merge_split_paragraphs(raw_blocks)
    chunks: list[str] = []
    current: list[str] = []
    words = 0
    for block in blocks:
        n = len(block.split())
        if current and words + n > target_words:
            chunks.append("\n\n".join(current))
            current, words = [], 0
        current.append(block)
        words += n
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ----------------------------------------- Stage 2: LLM cleanup with checkpoints


def _clean_text(client, model: str, text: str, prompt: str) -> str:
    """Clean one piece of text; on an input-filter block, bisect by paragraph and recurse.

    Gemini's copyrighted-text filter can reject a chunk (typically one combining the
    book's title, author, and body text). Halving isolates the offending paragraphs;
    a single paragraph that is still blocked is kept verbatim.
    """
    try:
        return generate(client, model, text, prompt)
    except PromptBlockedError as e:
        blocks = text.split("\n\n")
        if len(blocks) == 1:
            print(f"  warning: paragraph blocked by input filter ({e}); kept verbatim", file=sys.stderr)
            return text
        mid = len(blocks) // 2
        left = _clean_text(client, model, "\n\n".join(blocks[:mid]), prompt)
        right = _clean_text(client, model, "\n\n".join(blocks[mid:]), prompt)
        return left + "\n\n" + right


def clean_chunks(
    client,
    model: str,
    chunks: list[str],
    checkpoint_dir: Path,
    max_chunks: int | None = None,
    concurrency: int = 4,
) -> list[str]:
    """Clean chunks via Gemini in a bounded worker pool; checkpoint to disk;
    verify fidelity by word-count ratio. Order of results matches input order."""
    from concurrent.futures import ThreadPoolExecutor

    prompt = load_prompt("clean_chunk")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    todo = chunks[:max_chunks] if max_chunks else chunks
    total = len(todo)

    def clean_one(numbered: tuple[int, str]) -> str:
        i, chunk = numbered
        ckpt = checkpoint_dir / f"chunk_{i:04d}.md"
        if ckpt.exists():
            print(f"[{i}/{total}] cached")
            return ckpt.read_text(encoding="utf-8")
        out, ratio = "", 0.0
        for attempt in (1, 2):
            out = _clean_text(client, model, chunk, prompt)
            ratio = len(out.split()) / max(1, len(chunk.split()))
            if FIDELITY_BOUNDS[0] <= ratio <= FIDELITY_BOUNDS[1]:
                break
            print(
                f"[{i}/{total}] fidelity ratio {ratio:.2f} outside {FIDELITY_BOUNDS}"
                f" (attempt {attempt})",
                file=sys.stderr,
            )
            if attempt == 2:
                raise RuntimeError(f"chunk {i} failed fidelity check twice — aborting")
        atomic_write(ckpt, out)
        print(f"[{i}/{total}] cleaned (ratio {ratio:.2f})")
        return out

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        try:
            return list(pool.map(clean_one, enumerate(todo, 1)))
        except RuntimeError as e:
            sys.exit(f"error: {e}")


# --------------------------------------- Stage 3 + 4: metadata, compile, output


def extract_metadata(client, model: str, sample_text: str) -> tuple[str | None, str | None, str | None]:
    try:
        out = generate(client, model, sample_text[:8000], load_prompt("extract_metadata"))
        data = json.loads(out)
        return data.get("title"), data.get("author"), data.get("language")
    except (PromptBlockedError, json.JSONDecodeError, AttributeError):
        return None, None, None


def _is_watermark_block(block: str, watermark_hosts: set[str]) -> bool:
    bare = block.strip().strip("*_()[] \t")
    bare = re.sub(r"^(https?://)?(www\.)?", "", bare, flags=re.IGNORECASE).rstrip("/")
    return bare.lower() in watermark_hosts


def strip_watermarks(md: str, watermark_hosts: set[str]) -> str:
    """Drop paragraphs that are only a distributor watermark URL (e.g. a scan-site plug),
    not part of the book's actual content."""
    if not watermark_hosts:
        return md
    blocks = re.split(r"\n\s*\n", md)
    return "\n\n".join(b for b in blocks if not _is_watermark_block(b, watermark_hosts))


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def assign_ascii_heading_ids(md: str) -> str:
    """Pandoc's EPUB writer slugifies each heading's own text into its section id
    regardless of Markdown's auto_identifiers setting; for Cyrillic headings that
    produces non-ASCII ids that fail strict EPUB id validation (e.g. Calibre's
    checker, some e-reader engines). Assign explicit ASCII ids via pandoc's
    `{#id}` header-attribute syntax so the writer uses those instead."""
    counter = 0

    def repl(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        return f"{m.group(1)} {m.group(2)} {{#sec-{counter:04d}}}"

    return _HEADING_RE.sub(repl, md)


def build_frontmatter(title: str, author: str, language: str) -> str:
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    # pandoc reads `lang` (not `language`) for the EPUB dc:language element
    return f'---\ntitle: "{esc(title)}"\nauthor: "{esc(author)}"\nlang: "{esc(language)}"\n---\n\n'


def compile_epub(md_path: Path, epub_path: Path) -> None:
    if not shutil.which("pandoc"):
        sys.exit("error: pandoc not found on PATH")
    subprocess.run(
        ["pandoc", str(md_path), "-o", str(epub_path), "--toc", "--split-level=2", "--quiet"],
        check=True,
    )


def finalize(args: argparse.Namespace, parser_name: str, cleaned_chunks: list[str], client) -> None:
    """Stage 3 (metadata + concatenation) and Stage 4 (pandoc compile)."""
    if not cleaned_chunks:
        sys.exit("error: no cleaned chunks to compile")
    title, author, language = args.title, args.author, args.language
    if not (title and author and language):
        found_title, found_author, found_language = extract_metadata(client, args.model, cleaned_chunks[0])
        title = title or found_title or args.input_pdf.stem
        author = author or found_author or "Unknown"
        language = language or found_language or DEFAULT_LANGUAGE
    md_path = work_dir(args, parser_name) / "compiled_book.md"
    body = strip_watermarks("\n\n".join(cleaned_chunks), set(args.strip_watermark))
    body = assign_ascii_heading_ids(body)
    md_path.write_text(
        build_frontmatter(title, author, language) + body,
        encoding="utf-8",
    )
    epub_path = args.output or args.input_pdf.parent / f"{args.input_pdf.stem}.epub"
    compile_epub(md_path, epub_path)
    if args.keep_md:
        kept = epub_path.with_suffix(".md")
        shutil.copy(md_path, kept)
        print(f"markdown: {kept}")
    print(f"title: {title}\nauthor: {author}\nlanguage: {language}\nepub: {epub_path}")
