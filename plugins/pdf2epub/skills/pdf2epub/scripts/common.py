"""Shared stages of the PDF-to-EPUB pipeline.

Used by convert_pymupdf.py (local PyMuPDF4LLM extraction + Gemini cleanup).
"""

import argparse
import hashlib
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


def setup_output() -> None:
    """Progress must reach redirected log files immediately, not sit in the block buffer."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)


def base_arg_parser(parser_name: str) -> argparse.ArgumentParser:
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
    p.add_argument(
        "--images",
        choices=["auto", "off"],
        default="auto",
        help="Capture figures/diagrams as images (auto, default) or skip them entirely (off)",
    )
    p.add_argument(
        "--image-min-px", type=int, default=128,
        help="Min smaller-dimension (px) of a raster image to keep as a figure (default: 128)",
    )
    p.add_argument(
        "--image-max-aspect", type=float, default=6.0,
        help="Max width:height (or inverse) of a raster image to keep (default: 6)",
    )
    p.add_argument(
        "--figure-min-cells", type=int, default=120,
        help="Min covered grid cells for a vector cluster to count as a figure (default: 120)",
    )
    p.add_argument(
        "--figure-dpi", type=int, default=150,
        help="Render resolution for captured figure images (default: 150)",
    )
    return p


def _image_options_signature(args: argparse.Namespace) -> str:
    """A short signature of the options that change what stage-1 extraction produces
    (which figures are detected, hence the Markdown's image refs). Folded into the
    workdir key so `--images`/figure-tuning reruns don't silently reuse a stale
    extraction — the documented tune-and-rerun workflow depends on this."""
    if getattr(args, "images", "auto") != "auto":
        return "img=off"
    return (
        f"img=auto:min_px={getattr(args, 'image_min_px', '')}"
        f":max_aspect={getattr(args, 'image_max_aspect', '')}"
        f":min_cells={getattr(args, 'figure_min_cells', '')}"
        f":dpi={getattr(args, 'figure_dpi', '')}"
    )


def work_dir(args: argparse.Namespace, parser_name: str) -> Path:
    """Checkpoint dir keyed by stem + content hash (+ image-options signature), so two
    PDFs that share a filename — or the same PDF run with different image settings —
    never reuse each other's cache."""
    if not args.input_pdf.is_file():
        sys.exit(f"error: input PDF not found: {args.input_pdf}")
    h = hashlib.sha256()
    with args.input_pdf.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    h.update(_image_options_signature(args).encode())
    d = args.workdir / f"{args.input_pdf.stem}-{h.hexdigest()[:8]}" / parser_name
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
        sys.exit("error: neither GEMINI_API_KEY nor GOOGLE_API_KEY is set")
    from google import genai

    return genai.Client()


class PromptBlockedError(RuntimeError):
    """The input was rejected by Gemini's non-configurable content filter."""


class EmptyResponseError(RuntimeError):
    """The model returned an empty completion (finish_reason=STOP, no text) on every
    retry. Some benign chunks trigger this deterministically; callers should bisect
    and keep the offending paragraph verbatim rather than abort the whole book."""


def generate(
    client,
    model: str,
    contents,
    system_instruction: str,
    max_retries: int = 5,
    temperature: float = 0.1,
) -> str:
    """generate_content with exponential backoff on rate limits / transient errors."""
    from google.genai import errors, types

    delay = 5.0
    last_error = "unknown error"
    last_was_empty = False
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction, temperature=temperature
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
            last_was_empty = True
        except errors.APIError as e:
            if e.code not in RETRYABLE_CODES:
                raise
            last_error = f"API error {e.code}"
            last_was_empty = False
        if attempt == max_retries:
            break
        print(f"  {last_error}; retrying in {delay:.0f}s", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 120)
    if last_was_empty:
        # a persistently-empty completion is effectively an output-side block — let the
        # caller bisect and keep the offending paragraph verbatim instead of aborting
        raise EmptyResponseError(last_error)
    sys.exit(f"error: Gemini call failed after {max_retries + 1} attempts ({last_error})")


def strip_md_fences(text: str) -> str:
    m = re.fullmatch(r"```(?:markdown|md|json)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text


# ------------------------------------------------- Stage 1: chunking helpers

_TERMINAL_PUNCT = tuple('.!?…:;"\'»)*')


def strip_page_artifacts(md: str) -> str:
    """Drop page-break rules and adjacent page-number lines left by local extractors.

    PyMuPDF4LLM marks each page break with a hyphen rule (`-----`); a bare number
    is treated as a page number only when it sits next to such a rule. Standalone
    numbers elsewhere (bare chapter numbers, years) and `***`/`___` rules (often
    scene breaks in the book itself) are kept — the LLM cleanup pass handles any
    stragglers, but must never be the reason real content disappears."""
    lines = md.splitlines()
    is_rule = [bool(re.fullmatch(r"-{3,}", line.strip())) for line in lines]

    def next_to_rule(idx: int) -> bool:
        for step in (-1, 1):
            j = idx + step
            while 0 <= j < len(lines) and not lines[j].strip():
                j += step
            if 0 <= j < len(lines) and is_rule[j]:
                return True
        return False

    kept = []
    for i, line in enumerate(lines):
        if is_rule[i]:
            continue
        if re.fullmatch(r"\d{1,4}", line.strip()) and next_to_rule(i):
            continue
        kept.append(line)
    return "\n".join(kept)


def merge_split_paragraphs(blocks: list[str]) -> list[str]:
    """Rejoin paragraphs split by page breaks before chunking (Stage 1 requirement)."""
    merged: list[str] = []
    for block in blocks:
        if merged and not block.startswith("#") and not block.startswith("!["):
            prev = merged[-1].rstrip()
            first = block.lstrip()[:1]
            if (
                not merged[-1].startswith("#")
                and not merged[-1].startswith("![")
                and not prev.endswith(_TERMINAL_PUNCT)
                and first.islower()
            ):
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

_IMAGE_BLOCK_RE = re.compile(r"\s*!\[[^\]]*\]\([^)]*\)\s*")


def _is_image_block(block: str) -> bool:
    """True if the block is only a Markdown image reference (optionally padded)."""
    return bool(_IMAGE_BLOCK_RE.fullmatch(block))


def _clean_body(
    client, model: str, chunk: str, prompt: str, temperature: float = 0.1, where: str = ""
) -> tuple[str, int, int]:
    """Clean a chunk's prose while passing image-ref blocks through verbatim.

    Image references must never reach the model (it would reword or drop them)
    and must not count toward the fidelity ratio (a figure-dense chunk would
    otherwise look like it lost content). Returns (cleaned, src_words, out_words)
    counting only the non-image text. A chunk with no image refs takes the exact
    same single-call path as before. `where` labels the chunk in warnings.
    """
    if not _IMAGE_BLOCK_RE.search(chunk):
        out = _clean_text(client, model, chunk, prompt, temperature, where)
        return out, len(chunk.split()), len(out.split())

    blocks = [b for b in re.split(r"\n\s*\n", chunk) if b.strip()]
    out_parts: list[str] = []
    src_words = out_words = 0
    buffer: list[str] = []

    def flush() -> None:
        nonlocal src_words, out_words, buffer
        if not buffer:
            return
        seg = "\n\n".join(buffer)
        cleaned = _clean_text(client, model, seg, prompt, temperature, where)
        out_parts.append(cleaned)
        src_words += len(seg.split())
        out_words += len(cleaned.split())
        buffer = []

    for block in blocks:
        if _is_image_block(block):
            flush()
            out_parts.append(block.strip())
        else:
            buffer.append(block)
    flush()
    return "\n\n".join(out_parts), src_words, out_words


def _clean_text(client, model: str, text: str, prompt: str, temperature: float = 0.1, where: str = "") -> str:
    """Clean one piece of text; on a block or a persistently-empty response, bisect by
    paragraph and recurse.

    Gemini's copyrighted-text filter can reject a chunk (typically one combining the
    book's title, author, and body text), and some benign chunks draw an empty
    completion on every retry. Both are handled the same way: halving isolates the
    offending paragraphs; a single paragraph that still fails is kept verbatim.
    `where` labels the source chunk so the verbatim warning is traceable.
    """
    try:
        return generate(client, model, text, prompt, temperature=temperature)
    except (PromptBlockedError, EmptyResponseError) as e:
        reason = "blocked by input filter" if isinstance(e, PromptBlockedError) else "empty model response"
        blocks = text.split("\n\n")
        if len(blocks) == 1:
            loc = f" in {where}" if where else ""
            print(f"  warning: paragraph {reason}{loc} ({e}); kept verbatim", file=sys.stderr)
            return text
        mid = len(blocks) // 2
        left = _clean_text(client, model, "\n\n".join(blocks[:mid]), prompt, temperature, where)
        right = _clean_text(client, model, "\n\n".join(blocks[mid:]), prompt, temperature, where)
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

    from google.genai import errors

    prompt = load_prompt("clean_chunk")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    todo = chunks[:max_chunks] if max_chunks is not None else chunks
    total = len(todo)

    def clean_one(numbered: tuple[int, str]) -> str:
        i, chunk = numbered
        # content-addressed checkpoint keyed by the chunk text AND the cleanup prompt: if
        # either changes (re-chunking, new extraction, an edited prompt), the hash changes
        # and the stale cache is a clean miss instead of being served wrongly. Identical
        # reruns still hit 100%.
        chunk_hash = hashlib.sha1(f"{prompt}\x00{chunk}".encode("utf-8")).hexdigest()[:10]
        ckpt = checkpoint_dir / f"chunk_{i:04d}_{chunk_hash}.md"
        if ckpt.exists():
            print(f"[{i}/{total}] cached")
            return ckpt.read_text(encoding="utf-8")
        out, ratio = "", 0.0
        for attempt in (1, 2):
            attempt_prompt, temperature = prompt, 0.1
            if attempt == 2:
                # a same-prompt low-temperature retry would mostly reproduce attempt 1;
                # tell the model what went wrong and let it explore a little more
                drift = (
                    "dropped or summarized parts of the source text"
                    if ratio < FIDELITY_BOUNDS[0]
                    else "added or duplicated content that is not in the source text"
                )
                attempt_prompt = (
                    f"{prompt}\n\nIMPORTANT: a previous attempt {drift}. Reproduce the "
                    "source text fully and exactly; remove only layout artifacts."
                )
                temperature = 0.4
            out, src_words, out_words = _clean_body(
                client, model, chunk, attempt_prompt, temperature, where=f"chunk {i}"
            )
            # an image-only chunk (no prose to clean) has no meaningful ratio — accept it
            # rather than fail the fidelity check on 0/0 and abort the whole run
            if src_words == 0:
                ratio = 1.0
                break
            ratio = out_words / src_words
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
        futures = [pool.submit(clean_one, item) for item in enumerate(todo, 1)]
        try:
            return [f.result() for f in futures]
        except RuntimeError as e:
            # cancel queued chunks: without this the with-block's shutdown(wait=True)
            # would run — and pay for — every remaining Gemini call before exiting
            pool.shutdown(cancel_futures=True)
            sys.exit(f"error: {e}")
        except errors.APIError as e:
            # non-retryable API failure (e.g. invalid key, quota exhausted) raised in a
            # worker thread — exit cleanly instead of dumping a multi-thread traceback
            pool.shutdown(cancel_futures=True)
            sys.exit(f"error: Gemini API call failed: {e}")
        except SystemExit:
            # generate() gave up after max_retries inside a worker
            pool.shutdown(cancel_futures=True)
            raise


# --------------------------------------- Stage 3 + 4: metadata, compile, output


def _single_line(value) -> str | None:
    """Coerce an LLM-supplied metadata field to a clean single-line string (or None)."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*")


def validate_language(value) -> str | None:
    """Accept only a plausible BCP-47 code — the model can answer e.g. "English"
    despite the prompt, which must not end up in the EPUB's dc:language."""
    if isinstance(value, str) and _LANGUAGE_RE.fullmatch(value.strip()):
        return value.strip()
    return None


def extract_metadata(
    client, model: str, sample_text: str
) -> tuple[str | None, str | None, str | None, str | None]:
    """Returns (title, author, language_code, language_name). The prompt asks for the
    full language name before the BCP-47 code: a bare code like "it" is ambiguous to
    an LLM (Italian? the pronoun?), so the model commits to the language in words
    first and derives the code from that. The code goes into the EPUB (dc:language
    must be BCP-47); the name is for humans and any future LLM-facing instruction."""
    from google.genai import errors

    try:
        out = generate(client, model, sample_text[:8000], load_prompt("extract_metadata"))
        data = json.loads(out)
        title = _single_line(data.get("title"))
        author = _single_line(data.get("author"))
        language = validate_language(data.get("language"))
        language_name = _single_line(data.get("language_name"))
        return title, author, language, language_name
    except (PromptBlockedError, EmptyResponseError, json.JSONDecodeError, AttributeError, errors.APIError) as e:
        print(f"  warning: metadata extraction failed ({e}); using fallbacks", file=sys.stderr)
        return None, None, None, None


def _normalize_host(text: str) -> str:
    bare = text.strip().strip("*_()[] \t")
    bare = re.sub(r"^(https?://)?(www\.)?", "", bare, flags=re.IGNORECASE).rstrip("/")
    return bare.lower()


def strip_watermarks(md: str, watermark_hosts: set[str]) -> str:
    """Drop paragraphs that are only a distributor watermark URL (e.g. a scan-site plug),
    not part of the book's actual content. Hosts are matched scheme/www./case-insensitively,
    so --strip-watermark accepts any of e.g. "site.com", "www.site.com", "http://site.com";
    a URL carrying a path (site.com/book/123) counts too. A paragraph with whitespace is
    never dropped — a real sentence mentioning the site is book content, not a watermark."""
    if not watermark_hosts:
        return md
    normalized_hosts = {_normalize_host(h) for h in watermark_hosts}

    def is_watermark(block: str) -> bool:
        bare = _normalize_host(block)
        if any(c.isspace() for c in bare):
            return False
        return any(bare == h or bare.startswith(h + "/") for h in normalized_hosts)

    blocks = re.split(r"\n\s*\n", md)
    return "\n\n".join(b for b in blocks if not is_watermark(b))


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
# opening fence: ``` plus an info string with no further backticks (so a single-line
# `` ```x``` inline `` paragraph never opens a fence that would swallow the rest of
# the book); closing fence: fence characters only
_FENCE_OPEN_RE = re.compile(r"(`{3,}[^`]*|~{3,}.*)")
_FENCE_CLOSE_RE = re.compile(r"(`{3,}|~{3,})\s*")


def _toggles_fence(line: str, in_fence: bool) -> bool:
    rule = _FENCE_CLOSE_RE if in_fence else _FENCE_OPEN_RE
    return bool(rule.fullmatch(line.strip()))


def assign_ascii_heading_ids(md: str) -> str:
    """Pandoc's EPUB writer slugifies each heading's own text into its section id
    regardless of Markdown's auto_identifiers setting; for Cyrillic headings that
    produces non-ASCII ids that fail strict EPUB id validation (e.g. Calibre's
    checker, some e-reader engines). Assign explicit ASCII ids via pandoc's
    `{#id}` header-attribute syntax so the writer uses those instead.
    Lines inside fenced code blocks are left untouched."""
    counter = 0
    in_fence = False
    out_lines = []
    for line in md.splitlines():
        if _toggles_fence(line, in_fence):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            counter += 1
            out_lines.append(f"{m.group(1)} {m.group(2)} {{#sec-{counter:04d}}}")
        else:
            out_lines.append(line)
    result = "\n".join(out_lines)
    return result + "\n" if md.endswith("\n") else result


def slugify(name: str) -> str:
    """Normalize a book name into a filesystem-safe path segment: lowercase, runs of
    whitespace/punctuation collapsed to a single hyphen. Used to namespace a kept
    Markdown's images/ folder so multiple books in the same output directory don't
    collide (figure filenames are page-based, e.g. fig-0039-00.png, and repeat per book)."""
    slug = re.sub(r"[^\w]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-_")
    return slug or "book"


def build_frontmatter(title: str, author: str, language: str) -> str:
    # collapse newlines first (a multi-line value would break the single-line
    # double-quoted YAML scalar), then escape for the double quotes
    esc = lambda s: " ".join(str(s).split()).replace("\\", "\\\\").replace('"', '\\"')
    # pandoc reads `lang` (not `language`) for the EPUB dc:language element
    return f'---\ntitle: "{esc(title)}"\nauthor: "{esc(author)}"\nlang: "{esc(language)}"\n---\n\n'


def compile_epub(md_path: Path, epub_path: Path, resource_path: Path | None = None) -> None:
    if not shutil.which("pandoc"):
        sys.exit("error: pandoc not found on PATH")
    cmd = ["pandoc", str(md_path), "-o", str(epub_path), "--toc", "--split-level=2", "--quiet"]
    if resource_path is not None:
        # so relative image refs (images/fig-….png) resolve regardless of CWD;
        # pandoc then bundles them into EPUB/media/ automatically
        cmd.append(f"--resource-path={resource_path}")
    subprocess.run(cmd, check=True)


def finalize(args: argparse.Namespace, wd: Path, cleaned_chunks: list[str], client) -> None:
    """Stage 3 (metadata + concatenation) and Stage 4 (pandoc compile).
    `wd` is the checkpoint dir already computed by the caller (work_dir() hashes the
    whole PDF, so recomputing it here would read the file a second time)."""
    if not cleaned_chunks:
        sys.exit("error: no cleaned chunks to compile")
    title, author, language = args.title, args.author, args.language
    language_name = None
    if not (title and author and language):
        found_title, found_author, found_language, language_name = extract_metadata(
            client, args.model, cleaned_chunks[0]
        )
        title = title or found_title or args.input_pdf.stem
        author = author or found_author or "Unknown"
        language = language or found_language or DEFAULT_LANGUAGE
    md_path = wd / "compiled_book.md"
    body = strip_watermarks("\n\n".join(cleaned_chunks), set(args.strip_watermark))
    body = assign_ascii_heading_ids(body)
    atomic_write(md_path, build_frontmatter(title, author, language) + body)
    epub_path = args.output or args.input_pdf.parent / f"{args.input_pdf.stem}.epub"
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    compile_epub(md_path, epub_path, resource_path=wd)
    if args.keep_md:
        kept = epub_path.with_suffix(".md")
        images_src = wd / "images"
        if images_src.is_dir():
            # copy figures next to the kept Markdown so its refs resolve for the user too;
            # namespaced per book so two books' images/ don't collide in the same folder
            slug = slugify(epub_path.stem)
            shutil.copytree(images_src, kept.parent / "images" / slug, dirs_exist_ok=True)
            kept_text = md_path.read_text(encoding="utf-8").replace("](images/", f"](images/{slug}/")
            atomic_write(kept, kept_text)
        else:
            shutil.copy(md_path, kept)
        print(f"markdown: {kept}")
    language_display = f"{language} ({language_name})" if language_name else language
    print(f"title: {title}\nauthor: {author}\nlanguage: {language_display}\nepub: {epub_path}")
