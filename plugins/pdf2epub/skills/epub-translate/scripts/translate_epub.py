#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["google-genai>=1.21", "lxml>=5.0"]
# ///
"""Translate an EPUB into another language, preserving its structure and markup.

Complements the pdf2epub skill: that one turns a PDF into a clean EPUB, this one
translates an EPUB (from pdf2epub or bought from a store) into another language and
emits a valid EPUB with the same structure, images, and reading order.

Markup is never sent to the model as markup — see epubdoc.py for the placeholder
scheme that makes tag preservation a structural guarantee rather than a hope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lxml import etree

import epubdoc

# Reuse the pdf2epub skill's Gemini plumbing (client, retry/backoff, block detection)
# rather than forking it: the retry semantics around 429s and the RECITATION/SAFETY
# finish_reason handling are subtle, and one copy keeps them in step. Both skills ship
# in the same plugin, so this relative path is stable wherever the plugin is installed.
_SIBLING_SCRIPTS = Path(__file__).resolve().parents[2] / "pdf2epub" / "scripts"
if not (_SIBLING_SCRIPTS / "common.py").is_file():
    sys.exit(f"error: cannot find the pdf2epub skill's common.py at {_SIBLING_SCRIPTS}")
sys.path.insert(0, str(_SIBLING_SCRIPTS))

import common  # noqa: E402

PROMPTS_DIR = Path(__file__).parent / "prompts"
DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_TARGET_WORDS = 1500
# Below this share of Cyrillic letters a Cyrillic-target translation is assumed to have
# echoed the source instead of translating. Latin proper nouns and numbers survive in
# real Bulgarian prose, so the bar is deliberately low — this catches wholesale echo,
# not stylistic choices.
CYRILLIC_FLOOR = 0.45
CYRILLIC_SCRIPTS = {"bg", "ru", "uk", "sr", "mk", "be"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Translate an EPUB, preserving structure")
    p.add_argument("input_epub", type=Path, help="Source EPUB file")
    p.add_argument("-o", "--output", type=Path, help="Destination EPUB path")
    p.add_argument(
        "-t", "--target-language", default="bg",
        help="BCP-47 code of the target language (default: bg)",
    )
    p.add_argument(
        "--target-language-name", default=None,
        help="Full English name of the target language (default: derived from the code)",
    )
    p.add_argument(
        "--glossary", type=Path, default=None,
        help="Path to a JSON glossary of source->target terms to apply consistently "
        "across the whole book (see SKILL.md for the file shape)",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    p.add_argument(
        "--thinking-level", default="low",
        choices=["minimal", "low", "medium", "high", "none"],
        help="Gemini 3 thinking level (default: low; 'none' omits it for older models)",
    )
    p.add_argument(
        "--target-words", type=int, default=DEFAULT_TARGET_WORDS,
        help=f"Approx. source words per API call (default: {DEFAULT_TARGET_WORDS})",
    )
    p.add_argument(
        "--concurrency", type=int, default=4,
        help="Parallel Gemini translation calls (default: 4); 429s back off per worker",
    )
    p.add_argument("--max-chunks", type=int, help="Translate only the first N chunks (smoke test)")
    p.add_argument(
        "--doc-filter", default=None,
        help="Only translate content documents whose filename contains this substring",
    )
    p.add_argument(
        "--title-suffix", default=None,
        help="Appended to the EPUB's dc:title (e.g. '(български превод)')",
    )
    p.add_argument(
        "--workdir", type=Path, default=Path("tmp/epub-translate"),
        help="Checkpoint/cache directory (default: tmp/epub-translate)",
    )
    return p.parse_args()


LANGUAGE_NAMES = {
    "bg": "Bulgarian", "ru": "Russian", "uk": "Ukrainian", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pl": "Polish",
    "pt": "Portuguese", "nl": "Dutch", "cs": "Czech", "sr": "Serbian",
    "el": "Greek", "tr": "Turkish", "ro": "Romanian", "hu": "Hungarian",
    "en": "English",
}


def resolve_glossary(path: Path | None) -> tuple[dict[str, str], str]:
    """Load a user-supplied JSON glossary.

    Returns (terms, prompt_block). The prompt block is stable text so the checkpoint
    hash changes if and only if the glossary actually changes. Glossaries are always
    external to this skill: they encode terminology for one specific book or series,
    which belongs with that book's own working directory, not in a shared plugin.
    """
    if path is None:
        return {}, ""
    if not path.is_file():
        sys.exit(f"error: glossary not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"error: glossary {path} is not valid JSON ({e})")
    terms = data.get("terms", data) if isinstance(data, dict) else {}
    terms = {k: v for k, v in terms.items() if isinstance(k, str) and isinstance(v, str)}
    if not terms:
        sys.exit(f"error: glossary {path} contains no usable string terms")
    lines = "\n".join(f"- {src} -> {dst}" for src, dst in sorted(terms.items()))
    block = (
        "\n\n## Glossary (authoritative)\n\nTranslate these terms exactly as given, "
        "every time they occur, inflecting for grammar as the target language requires. "
        "Where a term below conflicts with your own preference, the glossary wins.\n\n"
        f"{lines}\n"
    )
    return terms, block


def build_system_prompt(args: argparse.Namespace, glossary_block: str) -> str:
    template = (PROMPTS_DIR / "translate_chunk.md").read_text(encoding="utf-8")
    name = str(
        args.target_language_name
        or LANGUAGE_NAMES.get(args.target_language.split("-")[0].lower(), args.target_language)
    )
    return (
        template.replace("{TARGET_LANGUAGE_NAME}", name).replace(
            "{TARGET_LANGUAGE}", args.target_language
        )
        + glossary_block
    )


# --------------------------------------------------------------- per-chunk translation


class ChunkStats:
    """Counters aggregated across worker threads.

    `+=` on an attribute is a read-modify-write, not an atomic op, so concurrent
    workers can lose an increment. These numbers are the run's report — a spurious
    "0 kept verbatim" would tell the user the book is clean when it isn't — so the
    increments are serialized behind a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chunks = 0
        self.cached = 0
        self.units_ok = 0
        self.units_retried = 0
        self.units_verbatim = 0
        self.blocked = 0

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)


def _translate_units(
    client, args: argparse.Namespace, system_prompt: str, units: list[epubdoc.Unit], where: str,
    stats: ChunkStats,
) -> dict[int, str]:
    """Translate one chunk, returning {unit_index_1based: translated_text}.

    Validation is per unit, and every failure mode degrades rather than aborts:
    a unit whose markers or placeholders came back wrong is retried on its own, and
    a unit that still fails is left in the source language with a warning. Losing one
    paragraph's translation is recoverable; corrupting a chapter's markup is not.
    """
    thinking = None if args.thinking_level == "none" else args.thinking_level
    payload = epubdoc.render_chunk(units)
    expected = len(units)
    result: dict[int, str] = {}

    try:
        raw = common.generate(
            client, args.model, payload, system_prompt,
            temperature=0.3, thinking_level=thinking,
        )
        parsed = epubdoc.parse_chunk(raw, expected)
    except (common.PromptBlockedError, common.EmptyResponseError) as e:
        print(f"  warning: {where} blocked/empty ({e}); falling back per unit", file=sys.stderr)
        stats.bump("blocked")
        parsed = {}

    for i, unit in enumerate(units, 1):
        candidate = parsed.get(i)
        if candidate is not None and _unit_ok(unit, candidate, args):
            result[i] = candidate
            stats.bump("units_ok")
            continue
        # Retry this unit alone: a single paragraph with its own marker is a much
        # easier alignment problem than a 20-unit chunk, and isolating it also
        # isolates whatever content tripped a filter.
        stats.bump("units_retried")
        solo = _translate_single(client, args, system_prompt, unit, thinking)
        if solo is not None and _unit_ok(unit, solo, args):
            result[i] = solo
            stats.bump("units_ok")
        else:
            print(
                f"  warning: {where} unit {i} could not be translated safely; "
                "kept in the source language",
                file=sys.stderr,
            )
            stats.bump("units_verbatim")
            result[i] = unit.text
    return result


def _translate_single(
    client, args: argparse.Namespace, system_prompt: str, unit: epubdoc.Unit, thinking: str | None
) -> str | None:
    try:
        raw = common.generate(
            client, args.model, epubdoc.render_chunk([unit]), system_prompt,
            temperature=0.3, thinking_level=thinking,
        )
    except (common.PromptBlockedError, common.EmptyResponseError):
        return None
    return epubdoc.parse_chunk(raw, 1).get(1)


def _unit_ok(unit: epubdoc.Unit, candidate: str, args: argparse.Namespace) -> bool:
    """Accept a translated unit only if markup survived and it looks translated."""
    if not candidate.strip():
        return False
    if epubdoc.placeholder_ids(candidate) != epubdoc.placeholder_ids(unit.text):
        return False
    # Parity is necessary but not sufficient — it cannot see nesting order. Dry-run
    # the real rebuild so a structurally broken unit is retried rather than silently
    # falling back to the source language.
    if not epubdoc.can_apply(unit, candidate):
        return False
    if args.target_language.split("-")[0].lower() in CYRILLIC_SCRIPTS:
        stripped = epubdoc.PLACEHOLDER_RE.sub(" ", candidate)
        # Short units are legitimately all-Latin (a name, a number, "OK") — only
        # judge the script of units long enough for the ratio to mean something.
        if len(stripped.split()) >= 8 and epubdoc.cyrillic_ratio(stripped) < CYRILLIC_FLOOR:
            return False
    return True


# --------------------------------------------------------------------- main pipeline


def main() -> None:
    common.setup_output()
    args = parse_args()
    if not args.input_epub.is_file():
        sys.exit(f"error: input EPUB not found: {args.input_epub}")

    _, glossary_block = resolve_glossary(args.glossary)
    system_prompt = build_system_prompt(args, glossary_block)

    book_hash = hashlib.sha256(args.input_epub.read_bytes()).hexdigest()[:8]
    wd = args.workdir / f"{args.input_epub.stem}-{book_hash}"
    stage = wd / "unpacked"
    ckpt_dir = wd / "chunks"
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    # Always unpack fresh: the staging tree is mutated in place, so reusing a partially
    # translated tree from an aborted run would double-translate it. Checkpoints (which
    # hold the expensive API results) live outside `stage` and are reused.
    if stage.exists():
        shutil.rmtree(stage)
    epubdoc.unpack(args.input_epub, stage)
    opf = epubdoc.opf_path(stage)
    docs = epubdoc.spine_documents(opf)
    if args.doc_filter:
        docs = [d for d in docs if args.doc_filter in d.name]
    if not docs:
        sys.exit("error: no content documents found in the EPUB spine")
    print(f"spine: {len(docs)} content document(s)")

    # Parse every document up front so chunk numbering is stable across resumes.
    parsed_docs: list[tuple[Path, etree._ElementTree, list[epubdoc.Unit]]] = []
    jobs: list[tuple[int, Path, list[epubdoc.Unit]]] = []
    parser = etree.XMLParser(resolve_entities=False, recover=True)
    for doc in docs:
        try:
            tree = etree.parse(str(doc), parser)
        except etree.XMLSyntaxError as e:
            print(f"  warning: skipping unparseable {doc.name} ({e})", file=sys.stderr)
            continue
        root = tree.getroot()
        if root is None:
            continue
        units = epubdoc.find_units(root)
        if not units:
            continue
        parsed_docs.append((doc, tree, units))
        for chunk in epubdoc.chunk_units(units, args.target_words):
            jobs.append((len(jobs) + 1, doc, chunk))

    total_words = sum(u.words for _, _, units in parsed_docs for u in units)
    if args.max_chunks is not None:
        jobs = jobs[: args.max_chunks]
    print(f"plan: {len(jobs)} chunk(s), ~{total_words:,} translatable words")
    if not jobs:
        sys.exit("error: nothing translatable found")

    client = common.get_client()
    stats = ChunkStats()
    started = time.time()

    def run_job(job: tuple[int, Path, list[epubdoc.Unit]]) -> tuple[int, Path, list[epubdoc.Unit], dict[int, str]]:
        n, doc, units = job
        # Content-addressed on everything that can change the result, so an edited
        # prompt or glossary is a clean cache miss instead of a stale hit.
        key = hashlib.sha1(
            "\x00".join(
                [system_prompt, args.model, str(args.thinking_level), epubdoc.render_chunk(units)]
            ).encode("utf-8")
        ).hexdigest()[:10]
        ckpt = ckpt_dir / f"chunk_{n:04d}_{key}.json"
        if ckpt.exists():
            try:
                cached = json.loads(ckpt.read_text(encoding="utf-8"))
                stats.bump("cached")
                print(f"[{n}/{len(jobs)}] cached")
                return n, doc, units, {int(k): v for k, v in cached.items()}
            except (json.JSONDecodeError, ValueError):
                pass  # corrupt checkpoint — retranslate
        out = _translate_units(client, args, system_prompt, units, f"chunk {n}", stats)
        common.atomic_write(ckpt, json.dumps({str(k): v for k, v in out.items()}, ensure_ascii=False))
        stats.bump("chunks")
        print(f"[{n}/{len(jobs)}] translated ({sum(u.words for u in units)} words)")
        return n, doc, units, out

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(run_job, job) for job in jobs]
        try:
            results = [f.result() for f in futures]
        except SystemExit:
            pool.shutdown(cancel_futures=True)
            raise

    # Apply translations after all API work is done, so a mid-run failure never leaves
    # a half-translated tree that a later resume would translate again.
    for _, _, units, translated in results:
        for i, unit in enumerate(units, 1):
            text = translated.get(i)
            if text is not None and text != unit.text:
                try:
                    epubdoc.apply_translation(unit, text)
                except ValueError as e:
                    print(f"  warning: kept a unit verbatim ({e})", file=sys.stderr)
                    stats.bump("units_verbatim")

    touched = {doc for _, doc, _, _ in results}
    for doc, tree, _ in parsed_docs:
        if doc in touched:
            epubdoc.write_document(doc, tree)

    translate_navigation(client, args, system_prompt, opf)
    epubdoc.set_opf_metadata(opf, args.target_language, args.title_suffix)

    out_path = args.output or args.input_epub.with_name(
        f"{args.input_epub.stem}.{args.target_language}.epub"
    )
    epubdoc.repack(stage, out_path)

    elapsed = time.time() - started
    print(
        f"\nchunks: {stats.chunks} translated, {stats.cached} cached\n"
        f"units: {stats.units_ok} ok, {stats.units_retried} retried, "
        f"{stats.units_verbatim} kept verbatim\n"
        f"blocked chunks: {stats.blocked}\n"
        f"time: {elapsed / 60:.1f} min\n"
        f"language: {args.target_language}\n"
        f"epub: {out_path}"
    )
    if stats.units_verbatim:
        print(
            f"note: {stats.units_verbatim} unit(s) stayed in the source language — "
            "grep stderr for 'kept in the source language'",
            file=sys.stderr,
        )


def translate_navigation(client, args: argparse.Namespace, system_prompt: str, opf: Path) -> None:
    """Translate TOC labels so the e-reader's navigation is in the target language.

    Without this a fully translated book still shows an English table of contents,
    which is the first thing the reader sees.
    """
    thinking = None if args.thinking_level == "none" else args.thinking_level
    for nav in epubdoc.ncx_paths(opf):
        try:
            tree = etree.parse(str(nav), etree.XMLParser(resolve_entities=False, recover=True))
        except etree.XMLSyntaxError:
            continue
        root = tree.getroot()
        if root is None:
            continue
        labels: list[etree._Element] = []
        for el in root.iter():
            name = epubdoc.local_name(el.tag)
            if name in ("text", "a") and (el.text or "").strip():
                labels.append(el)
        if not labels:
            continue
        units = [epubdoc.Unit(element=el, text=(el.text or "").strip()) for el in labels]
        payload = epubdoc.render_chunk(units)
        try:
            raw = common.generate(
                client, args.model, payload, system_prompt,
                temperature=0.3, thinking_level=thinking,
            )
            parsed = epubdoc.parse_chunk(raw, len(units))
        except (common.PromptBlockedError, common.EmptyResponseError):
            print(f"  warning: could not translate navigation in {nav.name}", file=sys.stderr)
            continue
        changed = 0
        for i, unit in enumerate(units, 1):
            text = parsed.get(i)
            if text and text.strip():
                unit.element.text = text.strip()
                changed += 1
        epubdoc.write_document(nav, tree)
        print(f"navigation: {changed}/{len(units)} label(s) translated in {nav.name}")
        if changed < len(units):
            # Silently leaving part of the TOC in English is a visible defect — the
            # table of contents is the first thing the reader opens.
            print(
                f"  warning: {len(units) - changed} navigation label(s) in {nav.name} "
                "kept in the source language",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
