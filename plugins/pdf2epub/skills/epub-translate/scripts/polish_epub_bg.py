#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["openai>=1.40", "lxml>=5.0"]
# ///
"""Post-process a Bulgarian EPUB translation with BgGPT.

Runs after Gemini translation (epub-translate). Polishes already-Bulgarian prose for
fluency and idiom, and retranslates any units still left in English (Gemini content-
filter give-ups). Structure and placeholders are preserved via the same epubdoc
contract as the translator.

Two inputs:
  --workdir + --source-epub  polish from Gemini checkpoints without re-calling Gemini
  --input-epub               polish an already-emitted Bulgarian EPUB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

import epubdoc
import openai_client as oai

# Shared only for setup_output / atomic_write — not for Gemini.
_SIBLING_SCRIPTS = Path(__file__).resolve().parents[2] / "pdf2epub" / "scripts"
if not (_SIBLING_SCRIPTS / "common.py").is_file():
    sys.exit(f"error: cannot find the pdf2epub skill's common.py at {_SIBLING_SCRIPTS}")
sys.path.insert(0, str(_SIBLING_SCRIPTS))

import common  # noqa: E402

PROMPTS_DIR = Path(__file__).parent / "prompts"
DEFAULT_TARGET_WORDS = 1000
# Gemini translate default; used only when reloading its checkpoints so chunk
# boundaries match the files under workdir/chunks/.
DEFAULT_TRANSLATE_TARGET_WORDS = 1500
CYRILLIC_FLOOR = 0.50
# Character-length ratio polished/pre-polish. Catches summarization or runaway expansion.
CHAR_RATIO_BOUNDS = (0.4, 2.5)
WORD_RATIO_BOUNDS = (0.5, 2.2)
PROMPT_VERSION = "polish_bg_v1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polish a Bulgarian EPUB translation with BgGPT (optional EN→BG repair)"
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--workdir",
        type=Path,
        help="Gemini translation workdir (…/<book-id>/ containing chunks/). "
        "Requires --source-epub.",
    )
    src.add_argument(
        "--input-epub",
        type=Path,
        help="Already-translated Bulgarian EPUB to polish in place (no Gemini checkpoints)",
    )
    p.add_argument(
        "--source-epub",
        type=Path,
        default=None,
        help="Original source EPUB (required with --workdir; used to rebuild units)",
    )
    p.add_argument("-o", "--output", type=Path, required=True, help="Destination polished EPUB")
    p.add_argument(
        "--glossary",
        type=Path,
        default=None,
        help="JSON glossary of source→target terms (same shape as translate_epub.py)",
    )
    p.add_argument(
        "--model",
        default=None,
        help=f"BgGPT model id (default: $BGGPT_MODEL or {oai.DEFAULT_MODEL})",
    )
    p.add_argument(
        "--bggpt-base-url",
        default=None,
        help=f"OpenAI-compatible base URL (default: $BGGPT_BASE_URL or {oai.DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature (default: 0.2)",
    )
    p.add_argument(
        "--target-words",
        type=int,
        default=DEFAULT_TARGET_WORDS,
        help=f"Approx. words per polish API call (default: {DEFAULT_TARGET_WORDS})",
    )
    p.add_argument(
        "--translate-target-words",
        type=int,
        default=DEFAULT_TRANSLATE_TARGET_WORDS,
        help="Word target used when the Gemini run was chunked (default: 1500). "
        "Must match the original translation so checkpoints line up. "
        "Format-2 Gemini checkpoints also fingerprint each unit's source text and "
        "refuse mismatched entries even if the chunk index still exists.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Parallel BgGPT calls for polish chunks and for solo unit retries "
        "inside a chunk (default: 2); lower on 429 storms",
    )
    p.add_argument(
        "--unit-retries",
        type=int,
        default=2,
        help="Solo retries per unit that fails validation in its chunk (default: 2)",
    )
    p.add_argument("--max-chunks", type=int, help="Polish only the first N polish-chunks (smoke)")
    p.add_argument(
        "--doc-filter",
        default=None,
        help="Only polish content documents whose filename contains this substring",
    )
    p.add_argument(
        "--title-suffix",
        default=None,
        help="Appended to the EPUB's dc:title (e.g. '(BgGPT polish)')",
    )
    p.add_argument(
        "--polish-workdir",
        type=Path,
        default=None,
        help="Where to store polish checkpoints (default: <workdir>/polish or "
        "tmp/epub-polish/<stem>-<hash>/polish)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore polish checkpoints and re-call BgGPT",
    )
    p.add_argument(
        "--english-only",
        action="store_true",
        help="Only process units that still look like leftover English (targeted "
        "EN→BG repair). Skips already-Bulgarian prose.",
    )
    p.add_argument(
        "--with-source-en",
        action="store_true",
        help="Attach original English next to BG text when available (workdir mode only)",
    )
    p.add_argument(
        "--dry-run-sample",
        type=int,
        default=0,
        metavar="N",
        help="Print N before/after unit pairs to stdout and exit without writing an EPUB",
    )
    p.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path("tmp/epub-polish"),
        help="Root for polish checkpoints when using --input-epub (default: tmp/epub-polish)",
    )
    return p.parse_args()


# ------------------------------------------------------------------ glossary / prompt


def resolve_glossary(path: Path | None) -> tuple[dict[str, str], str]:
    """Same glossary shape as translate_epub.py — kept local to avoid importing Gemini."""
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
        "\n\n## Glossary (authoritative)\n\nUse these terms exactly as given whenever they "
        "appear, inflecting for Bulgarian grammar as required. Where a term below conflicts "
        "with your own preference, the glossary wins.\n\n"
        f"{lines}\n"
    )
    return terms, block


def build_system_prompt(glossary_block: str) -> str:
    template = (PROMPTS_DIR / "polish_bg_chunk.md").read_text(encoding="utf-8")
    return template + glossary_block


# ---------------------------------------------------------------- unit classification


def looks_untranslated(text: str, min_words: int = 8) -> bool:
    """True when a unit still looks like leftover English (mostly Latin prose).

    Pure digit/punctuation lines (countdowns, page ornaments) have no letters, so they
    are not treated as English leftovers. Long units use the usual word/letter floors;
    shorter high-Latin fragments (dialogue, short sentences) are also flagged so
    `--english-only` and TOC polish catch them. Names and tiny tokens stay exempt.
    """
    stripped = epubdoc.PLACEHOLDER_RE.sub(" ", text)
    words = stripped.split()
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    cyr = epubdoc.cyrillic_ratio(stripped)
    if cyr >= CYRILLIC_FLOOR:
        return False
    latin_share = 1.0 - cyr
    if len(words) >= min_words and len(letters) >= 24:
        return True
    # Short high-Latin phrases (e.g. dialogue) — not single/double-token names.
    if latin_share >= 0.9 and len(words) >= 4 and len(letters) >= 12:
        return True
    if latin_share >= 0.9 and len(words) >= 3 and len(letters) >= 16:
        return True
    return False


def _plain_len(text: str) -> int:
    return len(epubdoc.PLACEHOLDER_RE.sub(" ", text))


def _plain_words(text: str) -> int:
    return len(epubdoc.PLACEHOLDER_RE.sub(" ", text).split())


# ------------------------------------------------------------------- acceptance


def unit_ok(pre: str, candidate: str, unit: epubdoc.Unit | None = None) -> bool:
    """Accept a polished unit only if markup survived and the text still looks Bulgarian.

    `pre` is the pre-polish text (Gemini BG or leftover English). When the input was
    untranslated English, the length check is looser because translation can expand
    more than polish edits.
    """
    if not candidate.strip():
        return False
    if epubdoc.placeholder_ids(candidate) != epubdoc.placeholder_ids(pre):
        return False
    # Dry-run rebuild when we have the live Unit (needs original inline elements).
    if unit is not None and not epubdoc.can_apply(unit, candidate):
        return False
    # Emptied emphasis: compare against pre-polish text (the markup baseline we sent).
    if not epubdoc.keeps_placeholder_content(pre, candidate):
        return False

    pre_chars = _plain_len(pre)
    cand_chars = _plain_len(candidate)
    if pre_chars >= 40:
        ratio = cand_chars / pre_chars
        lo, hi = CHAR_RATIO_BOUNDS
        if looks_untranslated(pre):
            # EN→BG translation can expand; allow a wider upper bound
            hi = max(hi, 3.0)
            lo = min(lo, 0.35)
        if not lo <= ratio <= hi:
            return False

    pre_words = _plain_words(pre)
    if pre_words >= 12 and not looks_untranslated(pre):
        # Polish should not summarily shrink/expand word count either
        wr = _plain_words(candidate) / pre_words
        if not WORD_RATIO_BOUNDS[0] <= wr <= WORD_RATIO_BOUNDS[1]:
            return False

    # Long outputs must be predominantly Cyrillic when the input was real prose.
    # Digit countdowns / punctuation ornaments have almost no letters — do not reject
    # them for low Cyrillic ratio (that would burn solo retries and still keep `pre`).
    if _plain_words(candidate) >= 8:
        cand_ratio = epubdoc.cyrillic_ratio(epubdoc.PLACEHOLDER_RE.sub(" ", candidate))
        if cand_ratio < CYRILLIC_FLOOR:
            pre_letters = [c for c in epubdoc.PLACEHOLDER_RE.sub(" ", pre) if c.isalpha()]
            if len(pre_letters) >= 24:
                return False
    return True


# ------------------------------------------------------------------- stats / wire


class PolishStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chunks = 0
        self.cached = 0
        self.units_polished = 0
        self.units_kept = 0  # validation failed → pre-polish text kept
        self.units_retried = 0
        self.units_retranslated = 0  # detected EN leftovers that became BG
        self.units_unchanged = 0  # model returned essentially the same text
        self.blocked = 0

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)


@dataclass
class PolishUnit:
    """One unit ready for polish: DOM unit + pre-polish text (+ optional EN source)."""

    unit: epubdoc.Unit
    pre: str
    source_en: str | None = None

    @property
    def words(self) -> int:
        return _plain_words(self.pre)


def render_polish_chunk(items: list[PolishUnit], with_source_en: bool) -> str:
    parts: list[str] = []
    for i, item in enumerate(items, 1):
        if with_source_en and item.source_en and item.source_en != item.pre:
            body = f"[BG]\n{item.pre}\n[EN]\n{item.source_en}"
        else:
            body = item.pre
        parts.append(f"<<<{i}>>>\n{body}")
    return "\n".join(parts)


def render_chunk_from_pres(pres: list[str]) -> str:
    return "\n".join(f"<<<{i}>>>\n{t}" for i, t in enumerate(pres, 1))


# --------------------------------------------------------------- polish workers


def _polish_units(
    client,
    args: argparse.Namespace,
    system_prompt: str,
    items: list[PolishUnit],
    where: str,
    stats: PolishStats,
) -> dict[int, str]:
    """Polish one chunk → {1-based index: accepted_text}."""
    payload = render_polish_chunk(items, args.with_source_en)
    expected = len(items)
    result: dict[int, str] = {}

    try:
        raw = oai.chat_complete(
            client,
            args.model,
            system_prompt,
            payload,
            temperature=args.temperature,
            max_tokens=oai.estimate_max_tokens(payload),
        )
        parsed = epubdoc.parse_chunk(raw, expected)
        # If with-source-en, model might still emit [BG] labels — strip lightly
        parsed = {k: _strip_bg_en_labels(v) for k, v in parsed.items()}
    except oai.EmptyResponseError as e:
        print(f"  warning: {where} empty ({e}); falling back per unit", file=sys.stderr)
        stats.bump("blocked")
        parsed = {}
    except Exception as e:
        # RuntimeError from exhausted 429/5xx retries, network blips, etc. — degrade
        # per unit rather than aborting the whole book.
        print(f"  warning: {where} failed ({e}); falling back per unit", file=sys.stderr)
        stats.bump("blocked")
        parsed = {}

    need_retry: list[tuple[int, PolishUnit]] = []
    for i, item in enumerate(items, 1):
        candidate = parsed.get(i)
        if candidate is not None and unit_ok(item.pre, candidate, item.unit):
            result[i] = candidate
            _bump_accept(stats, item, candidate)
        else:
            need_retry.append((i, item))

    if need_retry:
        retries = _retry_units_parallel(client, args, system_prompt, need_retry)
        for i, item in need_retry:
            stats.bump("units_retried")
            solo = retries.get(i)
            if solo is not None:
                result[i] = solo
                _bump_accept(stats, item, solo)
            else:
                print(
                    f"  warning: {where} unit {i} polish rejected; kept pre-polish text",
                    file=sys.stderr,
                )
                stats.bump("units_kept")
                result[i] = item.pre
    return result


def _bump_accept(stats: PolishStats, item: PolishUnit, candidate: str) -> None:
    if looks_untranslated(item.pre) and not looks_untranslated(candidate):
        stats.bump("units_retranslated")
    if candidate.strip() == item.pre.strip():
        stats.bump("units_unchanged")
    else:
        stats.bump("units_polished")


def _strip_bg_en_labels(text: str) -> str:
    """If the model echoed [BG]/[EN] scaffolding, keep the BG portion."""
    t = text.strip()
    if t.startswith("[BG]"):
        t = t[4:].lstrip("\n")
        if "\n[EN]" in t:
            t = t.split("\n[EN]", 1)[0]
    return t.strip("\n")


def _retry_unit(
    client, args: argparse.Namespace, system_prompt: str, item: PolishUnit
) -> str | None:
    for _ in range(max(1, args.unit_retries)):
        payload = render_polish_chunk([item], args.with_source_en)
        if looks_untranslated(item.pre):
            sys_p = (
                system_prompt
                + "\n\n## Retry reminder\nThis unit is still English. Translate it "
                "fully into literary Bulgarian. Keep every placeholder. Output only "
                "`<<<1>>>` and the Bulgarian text.\n"
            )
        else:
            sys_p = (
                system_prompt
                + "\n\n## Retry reminder\nReturn exactly one unit with marker `<<<1>>>`. "
                "Preserve every placeholder. Output only markers and text.\n"
            )
        try:
            raw = oai.chat_complete(
                client,
                args.model,
                sys_p,
                payload,
                temperature=args.temperature,
                max_tokens=oai.estimate_max_tokens(payload),
            )
        except Exception:
            continue
        got = _strip_bg_en_labels(epubdoc.parse_chunk(raw, 1).get(1, ""))
        if got and unit_ok(item.pre, got, item.unit):
            return got
    return None


def _retry_units_parallel(
    client,
    args: argparse.Namespace,
    system_prompt: str,
    need_retry: list[tuple[int, PolishUnit]],
) -> dict[int, str | None]:
    """Solo-retry several units; parallel when more than one needs a network call.

    Chunk-level concurrency already covers independent polish jobs. The remaining
    latency is often *within* a chunk: the first-pass model response fails validation
    for several units, and each solo retry is an independent API call. Running those
    serially multiplies wait time; bounding by `--concurrency` keeps 429 pressure
    in the same ballpark as parallel chunks.
    """
    if not need_retry:
        return {}
    if len(need_retry) == 1:
        i, item = need_retry[0]
        return {i: _retry_unit(client, args, system_prompt, item)}

    workers = min(len(need_retry), max(1, args.concurrency))
    out: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_retry_unit, client, args, system_prompt, item): i
            for i, item in need_retry
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception:
                # Defensive: _retry_unit already swallows API errors; keep shape stable.
                out[i] = None
    return out


def _accept_or_retry_cached(
    client,
    args: argparse.Namespace,
    system_prompt: str,
    items: list[PolishUnit],
    cached: dict[int, str],
    stats: PolishStats,
) -> tuple[dict[int, str], bool]:
    """Re-validate a cached polish map; parallel-retry units that fail current gates.

    Returns (accepted_map, dirty) where dirty means at least one unit was repaired
    or fell back to pre-polish text (checkpoint should be rewritten).
    """
    accepted: dict[int, str] = {}
    need_retry: list[tuple[int, PolishUnit]] = []
    for i, item in enumerate(items, 1):
        cand = cached.get(i)
        if cand is not None and unit_ok(item.pre, cand, item.unit):
            accepted[i] = cand
            _bump_accept(stats, item, cand)
        else:
            need_retry.append((i, item))

    dirty = bool(need_retry)
    if need_retry:
        retries = _retry_units_parallel(client, args, system_prompt, need_retry)
        for i, item in need_retry:
            solo = retries.get(i)
            if solo is not None:
                accepted[i] = solo
                _bump_accept(stats, item, solo)
            else:
                accepted[i] = item.pre
                stats.bump("units_kept")
    return accepted, dirty


# --------------------------------------------------------------- load / plan


def _load_gemini_map(
    ckpt_dir: Path, chunk_no: int
) -> dict[int, tuple[str, str | None]] | None:
    """Load Gemini unit map for chunk_no → {index: (text, source_fp_or_None)}.

    Understands format-2 checkpoints (with per-unit source fingerprints) and the
    legacy flat `{ "1": "…" }` shape. Prefer the newest mtime when multiple prompt
    hashes exist for the same chunk number.
    """
    matches = sorted(ckpt_dir.glob(f"chunk_{chunk_no:04d}_*.json"))
    if not matches:
        return None
    path = max(matches, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format") == 2 and isinstance(data.get("units"), dict):
        out: dict[int, tuple[str, str | None]] = {}
        for k, v in data["units"].items():
            if isinstance(v, dict) and isinstance(v.get("t"), str):
                fp = v.get("s")
                out[int(k)] = (v["t"], fp if isinstance(fp, str) else None)
        return out or None
    flat = {
        int(k): (v, None) for k, v in data.items() if isinstance(v, str)
    }
    return flat or None


def pre_is_structurally_safe(unit: epubdoc.Unit, pre: str) -> bool:
    """True if `pre` can rebuild onto `unit` without emptying emphasis or breaking tags.

    Used when seeding the apply set from Gemini checkpoints so a laxer older map
    cannot reintroduce emptied markup into the polished EPUB.
    """
    if not pre or pre == unit.text:
        return True
    if not epubdoc.can_apply(unit, pre):
        return False
    if not epubdoc.keeps_placeholder_content(unit.text, pre):
        return False
    return True


def plan_from_workdir(
    source_epub: Path,
    workdir: Path,
    args: argparse.Namespace,
) -> tuple[Path, list[tuple[Path, etree._ElementTree, list[epubdoc.Unit]]], list[PolishUnit]]:
    """Unpack source EPUB, overlay Gemini translations, return polish-ready units."""
    gemini_chunks = workdir / "chunks"
    if not gemini_chunks.is_dir():
        sys.exit(f"error: no Gemini chunks/ under workdir: {workdir}")

    stage = workdir / "polish_unpacked"
    if stage.exists():
        shutil.rmtree(stage)
    epubdoc.unpack(source_epub, stage)
    opf = epubdoc.opf_path(stage)
    docs = epubdoc.spine_documents(opf)
    if args.doc_filter:
        docs = [d for d in docs if args.doc_filter in d.name]
    if not docs:
        sys.exit("error: no content documents found in the EPUB spine")

    parser = etree.XMLParser(resolve_entities=False, recover=True)
    parsed_docs: list[tuple[Path, etree._ElementTree, list[epubdoc.Unit]]] = []
    polish_items: list[PolishUnit] = []
    gemini_job = 0
    missing_ckpts = 0
    fp_mismatches = 0
    legacy_maps = 0
    en_leftovers = 0

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
        for chunk in epubdoc.chunk_units(units, args.translate_target_words):
            gemini_job += 1
            gmap = _load_gemini_map(gemini_chunks, gemini_job)
            if gmap is None:
                missing_ckpts += 1
            elif any(fp is None for _, fp in gmap.values()):
                legacy_maps += 1
            for i, unit in enumerate(chunk, 1):
                pre = unit.text
                if gmap and i in gmap:
                    text, fp = gmap[i]
                    if fp is not None and fp != epubdoc.source_fingerprint(unit.text):
                        # Chunk index exists but holds a different source unit —
                        # wrong --translate-target-words / doc set. Do not apply.
                        fp_mismatches += 1
                    else:
                        pre = text
                source_en = unit.text if args.with_source_en else None
                if looks_untranslated(pre):
                    en_leftovers += 1
                polish_items.append(PolishUnit(unit=unit, pre=pre, source_en=source_en))

    if missing_ckpts:
        print(
            f"  warning: {missing_ckpts} Gemini chunk checkpoint(s) missing — "
            "those units use source English and will be translated by BgGPT",
            file=sys.stderr,
        )
    if fp_mismatches:
        print(
            f"  warning: {fp_mismatches} Gemini unit(s) refused: source fingerprint "
            "mismatch (check --translate-target-words / --doc-filter match the "
            "original translate run). Those units stay in source English.",
            file=sys.stderr,
        )
    if legacy_maps:
        print(
            "  note: some Gemini checkpoints lack source fingerprints (pre-format-2); "
            "alignment is trusted by chunk index only — re-run translate for safer polish",
            file=sys.stderr,
        )
    if en_leftovers:
        print(
            f"plan: {en_leftovers} unit(s) still look English (leftover/untranslated) "
            "and will be retranslated to Bulgarian"
        )
    return stage, parsed_docs, polish_items


def plan_from_input_epub(
    input_epub: Path,
    ckpt_root: Path,
    args: argparse.Namespace,
) -> tuple[Path, list[tuple[Path, etree._ElementTree, list[epubdoc.Unit]]], list[PolishUnit]]:
    book_hash = hashlib.sha256(input_epub.read_bytes()).hexdigest()[:8]
    wd = ckpt_root / f"{input_epub.stem}-{book_hash}"
    stage = wd / "unpacked"
    if stage.exists():
        shutil.rmtree(stage)
    epubdoc.unpack(input_epub, stage)
    opf = epubdoc.opf_path(stage)
    docs = epubdoc.spine_documents(opf)
    if args.doc_filter:
        docs = [d for d in docs if args.doc_filter in d.name]
    if not docs:
        sys.exit("error: no content documents found in the EPUB spine")

    parser = etree.XMLParser(resolve_entities=False, recover=True)
    parsed_docs: list[tuple[Path, etree._ElementTree, list[epubdoc.Unit]]] = []
    polish_items: list[PolishUnit] = []
    en_leftovers = 0

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
        for unit in units:
            pre = unit.text
            if looks_untranslated(pre):
                en_leftovers += 1
            polish_items.append(PolishUnit(unit=unit, pre=pre, source_en=None))

    if en_leftovers:
        print(
            f"plan: {en_leftovers} unit(s) still look English (leftover/untranslated) "
            "and will be retranslated to Bulgarian"
        )
    return stage, parsed_docs, polish_items


def chunk_polish_items(items: list[PolishUnit], target_words: int) -> list[list[PolishUnit]]:
    """Same greedy + tail-merge chunking as epubdoc.chunk_units, on PolishUnit.words."""
    chunks: list[list[PolishUnit]] = []
    current: list[PolishUnit] = []
    words = 0
    for item in items:
        n = item.words
        if current and words + n > target_words:
            chunks.append(current)
            current, words = [], 0
        current.append(item)
        words += n
    if current:
        chunks.append(current)
    if len(chunks) > 1:
        tail_words = sum(u.words for u in chunks[-1])
        if tail_words < target_words * 0.3:
            chunks[-2].extend(chunks.pop())
    return chunks


# ------------------------------------------------------------------------ main


def main() -> None:
    common.setup_output()
    args = parse_args()
    args.model = args.model or os.environ.get("BGGPT_MODEL") or oai.DEFAULT_MODEL
    args.with_source_en = bool(args.with_source_en)

    if args.workdir is not None:
        if args.source_epub is None or not args.source_epub.is_file():
            sys.exit("error: --workdir requires an existing --source-epub")
        workdir = args.workdir
        if not workdir.is_dir():
            sys.exit(f"error: workdir not found: {workdir}")
        stage, parsed_docs, polish_items = plan_from_workdir(args.source_epub, workdir, args)
        polish_dir = args.polish_workdir or (workdir / "polish")
    else:
        if not args.input_epub.is_file():
            sys.exit(f"error: input EPUB not found: {args.input_epub}")
        stage, parsed_docs, polish_items = plan_from_input_epub(
            args.input_epub, args.checkpoint_dir, args
        )
        polish_dir = args.polish_workdir or (
            args.checkpoint_dir
            / f"{args.input_epub.stem}-{hashlib.sha256(args.input_epub.read_bytes()).hexdigest()[:8]}"
            / "polish"
        )

    _, glossary_block = resolve_glossary(args.glossary)
    system_prompt = build_system_prompt(glossary_block)
    polish_dir.mkdir(parents=True, exist_ok=True)

    # Keep the full unit list for apply/rebuild (especially workdir mode, where
    # `pre` is Gemini BG on English source units). English-only only filters what
    # is sent to the API — never drop Gemini text from the apply set.
    all_items = polish_items
    if args.english_only:
        before = len(all_items)
        polish_items = [it for it in all_items if looks_untranslated(it.pre)]
        print(
            f"english-only: {len(polish_items)} leftover English unit(s) "
            f"of {before} total"
        )

    jobs = chunk_polish_items(polish_items, args.target_words) if polish_items else []
    total_words = sum(it.words for it in all_items)
    if args.max_chunks is not None:
        jobs = jobs[: args.max_chunks]
    job_words = sum(it.words for chunk in jobs for it in chunk)
    print(f"spine docs with text: {len(parsed_docs)}")
    print(
        f"plan: {len(jobs)} polish chunk(s), ~{job_words:,} words this run "
        f"(book total ~{total_words:,}), model={args.model}"
    )

    stats = PolishStats()
    started = time.time()
    results: list[tuple[int, list[PolishUnit], dict[int, str]]] = []

    if not jobs:
        if args.english_only and not polish_items:
            print("nothing left in English — applying pre-polish text only (no API calls)")
            if args.dry_run_sample > 0:
                print("dry-run: nothing to sample")
                return
        else:
            sys.exit("error: nothing to polish")
        client = None  # no API work this run
    else:
        client = oai.get_client(base_url=args.bggpt_base_url)
        sample_pairs: list[tuple[str, str]] = []

        def run_job(
            job: tuple[int, list[PolishUnit]],
        ) -> tuple[int, list[PolishUnit], dict[int, str]]:
            n, items = job
            # Content-addressed on polish prompt, model, temperature, and pre-polish texts
            key_material = "\x00".join(
                [
                    PROMPT_VERSION,
                    system_prompt,
                    args.model,
                    str(args.temperature),
                    "en1" if args.with_source_en else "en0",
                    render_chunk_from_pres([it.pre for it in items]),
                ]
            )
            key = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:10]
            ckpt = polish_dir / f"chunk_{n:04d}_{key}.json"
            if ckpt.exists() and not args.force:
                try:
                    cached = {
                        int(k): v
                        for k, v in json.loads(ckpt.read_text(encoding="utf-8")).items()
                    }
                    # Re-validate against current rules; solo-retry failures in parallel
                    accepted, dirty = _accept_or_retry_cached(
                        client, args, system_prompt, items, cached, stats
                    )
                    if dirty:
                        common.atomic_write(
                            ckpt,
                            json.dumps(
                                {str(k): v for k, v in accepted.items()},
                                ensure_ascii=False,
                            ),
                        )
                        print(f"[{n}/{len(jobs)}] cached, repaired some unit(s)")
                    else:
                        stats.bump("cached")
                        print(f"[{n}/{len(jobs)}] cached")
                    return n, items, accepted
                except (json.JSONDecodeError, ValueError):
                    pass

            out = _polish_units(client, args, system_prompt, items, f"chunk {n}", stats)
            common.atomic_write(
                ckpt, json.dumps({str(k): v for k, v in out.items()}, ensure_ascii=False)
            )
            stats.bump("chunks")
            print(f"[{n}/{len(jobs)}] polished ({sum(it.words for it in items)} words)")
            return n, items, out

        numbered = list(enumerate(jobs, 1))
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = [pool.submit(run_job, (n, items)) for n, items in numbered]
            results = [f.result() for f in futures]

        # Collect dry-run samples before apply
        if args.dry_run_sample > 0:
            for _, items, out in sorted(results, key=lambda r: r[0]):
                for i, item in enumerate(items, 1):
                    after = out.get(i, item.pre)
                    if after != item.pre or looks_untranslated(item.pre):
                        sample_pairs.append((item.pre, after))
                    if len(sample_pairs) >= args.dry_run_sample:
                        break
                if len(sample_pairs) >= args.dry_run_sample:
                    break
            for j, (before, after) in enumerate(sample_pairs, 1):
                print(f"\n===== sample {j} BEFORE =====\n{before}\n===== AFTER =====\n{after}")
            print(f"\ndry-run: showed {len(sample_pairs)} pair(s); no EPUB written")
            return

    # Apply after all API work succeeds so a mid-run failure never half-writes the tree.
    # workdir mode: unit.text is still the English source; pre is Gemini BG (or leftover
    # EN). Seed every unit from the full pre-polish set (all_items), then overlay accepted
    # polish results. English-only / max-chunks still yield a full Bulgarian book.
    # Re-validate Gemini `pre` (and final overlays) with structural checks so a laxer
    # older checkpoint cannot empty emphasis into the polished EPUB.
    final_by_unit: dict[int, str] = {}
    for item in all_items:
        if item.pre and item.pre != item.unit.text:
            if pre_is_structurally_safe(item.unit, item.pre):
                final_by_unit[id(item.unit)] = item.pre
            else:
                print(
                    "  warning: Gemini pre failed structural checks; "
                    "kept source text for that unit",
                    file=sys.stderr,
                )
                stats.bump("units_kept")
    for _, items, out in results:
        for i, item in enumerate(items, 1):
            text = out.get(i, item.pre)
            if not text or text == item.unit.text:
                continue
            if not pre_is_structurally_safe(item.unit, text):
                # Leave whatever was already seeded (safe pre, or nothing).
                print(
                    "  warning: polished unit failed structural checks; not applied",
                    file=sys.stderr,
                )
                stats.bump("units_kept")
                continue
            final_by_unit[id(item.unit)] = text

    for item in all_items:
        text = final_by_unit.get(id(item.unit))
        if not text:
            continue
        try:
            epubdoc.apply_translation(item.unit, text)
        except ValueError as e:
            print(f"  warning: kept a unit unapplied ({e})", file=sys.stderr)
            stats.bump("units_kept")

    changed_unit_ids = set(final_by_unit)
    for doc, tree, units in parsed_docs:
        if any(id(u) in changed_unit_ids for u in units):
            epubdoc.write_document(doc, tree)

    opf = epubdoc.opf_path(stage)
    # TOC: only retranslate labels that still look English (needs a live client)
    if client is not None:
        _polish_navigation(client, args, system_prompt, opf, stats)
    epubdoc.set_opf_metadata(opf, "bg", args.title_suffix)

    out_path = args.output
    epubdoc.repack(stage, out_path)

    elapsed = time.time() - started
    print(
        f"\nchunks: {stats.chunks} polished, {stats.cached} cached\n"
        f"units: {stats.units_polished} changed, {stats.units_unchanged} unchanged, "
        f"{stats.units_retranslated} EN→BG retranslated, "
        f"{stats.units_retried} retried, {stats.units_kept} kept pre-polish\n"
        f"empty/failed chunks: {stats.blocked}\n"
        f"time: {elapsed / 60:.1f} min\n"
        f"model: {args.model}\n"
        f"epub: {out_path}"
    )
    if stats.units_kept:
        print(
            f"note: {stats.units_kept} unit(s) kept pre-polish text — "
            "grep stderr for 'kept pre-polish'",
            file=sys.stderr,
        )


def _polish_navigation(
    client, args: argparse.Namespace, system_prompt: str, opf: Path, stats: PolishStats
) -> None:
    """Retranslate TOC labels that still look English.

    Already-Bulgarian labels are left alone: a full-TOC rewrite risks drifting
    good titles and can blow the output token budget on large nav documents.
    Uses full text content so span-wrapped EPUB3 nav entries are included.
    """
    for nav in epubdoc.ncx_paths(opf):
        try:
            tree = etree.parse(str(nav), etree.XMLParser(resolve_entities=False, recover=True))
        except etree.XMLSyntaxError:
            continue
        root = tree.getroot()
        if root is None:
            continue
        items: list[PolishUnit] = []
        for el, text in epubdoc.find_nav_labels(root):
            if not looks_untranslated(text):
                continue
            items.append(PolishUnit(unit=epubdoc.Unit(element=el, text=text), pre=text))
        if not items:
            continue
        try:
            payload = render_polish_chunk(items, False)
            # Solo-ish reminder: these labels are leftover English
            sys_p = (
                system_prompt
                + "\n\n## Navigation labels\nThese units are still English table-of-contents "
                "labels. Translate each into concise literary Bulgarian. Keep markers; "
                "output only markers and text.\n"
            )
            raw = oai.chat_complete(
                client,
                args.model,
                sys_p,
                payload,
                temperature=args.temperature,
                max_tokens=oai.estimate_max_tokens(payload),
            )
            parsed = epubdoc.parse_chunk(raw, len(items))
        except Exception as e:
            # Do not abort a finished prose polish because TOC failed (incl. 503 storms).
            print(
                f"  warning: could not polish navigation in {nav.name} ({e})",
                file=sys.stderr,
            )
            continue
        changed = 0
        for i, item in enumerate(items, 1):
            text = (parsed.get(i) or "").strip()
            if not text:
                continue
            # Reject if still looks English / is an echo of the source label
            if looks_untranslated(text):
                continue
            if text.casefold() == item.pre.casefold():
                continue
            if _plain_words(text) >= 3 and epubdoc.cyrillic_ratio(text) < CYRILLIC_FLOOR:
                continue
            epubdoc.set_nav_label(item.unit.element, text)
            changed += 1
            stats.bump("units_retranslated")
        if changed:
            epubdoc.write_document(nav, tree)
        print(f"navigation: {changed}/{len(items)} English label(s) retranslated in {nav.name}")


if __name__ == "__main__":
    main()
