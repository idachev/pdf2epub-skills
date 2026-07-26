"""Pure (network-free) EPUB document surgery for the translation pipeline.

The whole point of this module is that translation never touches markup. Instead of
handing XHTML to a model and hoping it returns valid XHTML, we:

  1. locate the *translation units* — block elements whose content is text plus
     inline markup only (a `<p>`, an `<h2>`, an `<li>`; never a `<div>` that merely
     wraps other blocks);
  2. serialize each unit's inner content to a plain string in which inline tags
     become numbered placeholders (`[[1]]…[[/1]]`, `[[2/]]` for void elements);
  3. translate those strings;
  4. rebuild each unit's children from the placeholders in the *translated* string.

Step 4 is what makes word-order changes safe: Bulgarian moves an emphasized phrase
to a different position in the sentence, and because the placeholder carries the
tag identity by index, the rebuilt element gets its `<i>` around the moved phrase
rather than around whatever words happened to land in the original span.

Tag parity is therefore guaranteed by construction, not by asking politely — and
`placeholder_ids` lets callers assert it per unit before accepting a translation.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Elements that establish their own block context: if a candidate unit contains one
# of these, it is a wrapper and we recurse instead of treating it as a unit.
BLOCK_TAGS = frozenset(
    """address article aside blockquote body caption dd div dl dt figcaption figure
    footer form h1 h2 h3 h4 h5 h6 header hgroup li main nav ol p pre section table
    tbody td tfoot th thead tr ul""".split()
)

# Never send these to a translator: not prose, and mangling them breaks the book.
SKIP_TAGS = frozenset({"script", "style", "svg", "math", "head", "title"})

# Inline elements that carry no content of their own.
VOID_TAGS = frozenset({"br", "img", "hr", "wbr", "area", "col", "input", "source"})

PLACEHOLDER_RE = re.compile(r"\[\[(/?)(\d+)(/?)\]\]")
UNIT_MARKER_RE = re.compile(r"^<<<(\d+)>>>\s*$", re.MULTILINE)


def local_name(tag) -> str:
    """`{http://www.w3.org/1999/xhtml}p` -> `p`. Comments/PIs have non-str tags."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


# ------------------------------------------------------------------ translation units


@dataclass
class Unit:
    """One translatable block: the element, its placeholder-ized source string, and
    the inline elements the placeholders stand for (index -> element)."""

    element: etree._Element
    text: str
    inlines: dict[int, etree._Element] = field(default_factory=dict)

    @property
    def words(self) -> int:
        return len(PLACEHOLDER_RE.sub(" ", self.text).split())


def _has_block_descendant(el: etree._Element) -> bool:
    return any(local_name(child.tag) in BLOCK_TAGS for child in el.iter() if child is not el)


def _serialize(el: etree._Element, inlines: dict[int, etree._Element], counter: list[int]) -> str:
    """Inner content of `el` with inline children replaced by numbered placeholders."""
    parts: list[str] = [el.text or ""]
    for child in el:
        name = local_name(child.tag)
        if not name:  # comment / processing instruction — drop from the prose stream
            parts.append(child.tail or "")
            continue
        counter[0] += 1
        idx = counter[0]
        inlines[idx] = child
        if name in VOID_TAGS or (not len(child) and not (child.text or "").strip()):
            parts.append(f"[[{idx}/]]")
        else:
            parts.append(f"[[{idx}]]{_serialize(child, inlines, counter)}[[/{idx}]]")
        parts.append(child.tail or "")
    return "".join(parts)


def find_units(root: etree._Element) -> list[Unit]:
    """Every translatable block in document order.

    A unit is an element that (a) is not inside a skipped subtree, (b) contains no
    block-level descendant, and (c) has at least one non-whitespace character of
    text. Text that sits directly in a wrapper alongside block children (a stray
    sentence in a `<div>` next to a `<p>`) is picked up via the wrapper's own text
    only when the wrapper has no block descendants — otherwise it would be
    duplicated. Such stray text is vanishingly rare in real EPUBs and is left
    untranslated rather than risking duplication.
    """
    units: list[Unit] = []

    def walk(el: etree._Element) -> None:
        name = local_name(el.tag)
        if name in SKIP_TAGS:
            return
        if not name:
            return
        if _has_block_descendant(el):
            for child in el:
                walk(child)
            return
        inlines: dict[int, etree._Element] = {}
        text = _serialize(el, inlines, [0])
        if PLACEHOLDER_RE.sub("", text).strip():
            units.append(Unit(element=el, text=text, inlines=inlines))

    walk(root)
    return units


def _kind(match: re.Match) -> str:
    if match.group(1):
        return "close"
    return "void" if match.group(3) else "open"


def placeholder_ids(text: str) -> tuple[tuple[int, str], ...]:
    """Sorted multiset of (index, kind) placeholder pairs.

    Used to assert a translation kept every inline tag. Sorted because word order
    legitimately changes; a multiset because `[[1]]…[[/1]]` yields index 1 twice and
    dropping one half is a defect worth catching.

    The *kind* is part of the key, not discarded. Comparing bare indices would make
    the unbalanced `[[1]]x[[1]]` compare equal to the correct `[[1]]x[[/1]]` — and
    rebuilding the former nests an element inside itself, silently corrupting the
    document. Keeping the kind makes that a detectable mismatch instead.
    """
    return tuple(sorted((int(m.group(2)), _kind(m)) for m in PLACEHOLDER_RE.finditer(text)))


def placeholder_spans(text: str) -> dict[int, bool]:
    """For each paired placeholder index, whether its span holds non-whitespace text.

    Parity and nesting checks both pass for `[[1]][[/1]]` — the tags are all present
    and correctly balanced, they just have nothing between them. A model under filter
    pressure does exactly that: it keeps the markup and drops the emphasized words.
    Comparing spans against the source catches the lost content.
    """
    spans: dict[int, bool] = {}
    open_at: dict[int, int] = {}
    for m in PLACEHOLDER_RE.finditer(text):
        idx, kind = int(m.group(2)), _kind(m)
        if kind == "open":
            open_at[idx] = m.end()
        elif kind == "close" and idx in open_at:
            inner = text[open_at.pop(idx) : m.start()]
            # nested placeholders don't count as this span's own text
            spans[idx] = bool(PLACEHOLDER_RE.sub("", inner).strip())
    return spans


def keeps_placeholder_content(source: str, candidate: str) -> bool:
    """True if every paired placeholder that held text in `source` still holds text."""
    src, cand = placeholder_spans(source), placeholder_spans(candidate)
    return all(not had_text or cand.get(idx, False) for idx, had_text in src.items())


def _build(unit: Unit, translated: str) -> etree._Element:
    """Rebuild `translated` into a detached element, or raise ValueError.

    Shared by `apply_translation` and `can_apply` so validation and application can
    never disagree about what counts as a usable translation.
    """
    scratch = etree.Element("scratch")
    stack: list[etree._Element] = []

    def append_text(s: str) -> None:
        if not s:
            return
        target = stack[-1] if stack else scratch
        if len(target):
            target[-1].tail = (target[-1].tail or "") + s
        else:
            target.text = (target.text or "") + s

    pos = 0
    for m in PLACEHOLDER_RE.finditer(translated):
        append_text(translated[pos : m.start()])
        pos = m.end()
        idx, kind = int(m.group(2)), _kind(m)
        source = unit.inlines.get(idx)
        if source is None:
            raise ValueError(f"translation references unknown placeholder [[{idx}]]")
        if kind == "close":
            if not stack:
                raise ValueError(f"unmatched closing placeholder [[/{idx}]]")
            if stack[-1].get("__idx") != str(idx):
                raise ValueError(
                    f"placeholder [[/{idx}]] closes out of order "
                    f"(innermost open is {stack[-1].get('__idx')})"
                )
            stack.pop()
            continue
        new = etree.SubElement(
            stack[-1] if stack else scratch, source.tag, dict(source.attrib)
        )
        new.set("__idx", str(idx))
        if kind == "open":
            stack.append(new)
    append_text(translated[pos:])

    if stack:
        unclosed = ", ".join(f"[[{e.get('__idx')}]]" for e in stack)
        raise ValueError(f"unclosed placeholder(s): {unclosed}")
    return scratch


def can_apply(unit: Unit, translated: str) -> bool:
    """True if `translated` would rebuild cleanly. Mutates nothing.

    Placeholder *parity* (see `placeholder_ids`) is necessary but not sufficient: a
    sorted multiset cannot see nesting order, so `[[2]]x[[/1]][[1]]y[[/2]]` has the
    right tags in the wrong structure. Dry-running the real builder lets the caller
    retry such a unit instead of silently falling back to the source language.
    """
    try:
        _build(unit, translated)
        return True
    except ValueError:
        return False


def apply_translation(unit: Unit, translated: str) -> None:
    """Replace `unit.element`'s content with `translated`, rebuilding inline children.

    Raises ValueError if `translated` is not a faithful re-arrangement of the unit's
    placeholders: an unknown index, an unbalanced pair, or a close that does not match
    the innermost open. A corrupted response must not produce a mangled chapter.

    The rebuild happens in a **detached** element and is only moved into the document
    once the whole string has parsed cleanly. Mutating `unit.element` up front would
    mean a mid-scan failure leaves a half-rebuilt element behind — and the caller's
    "keep the source text" fallback would then be keeping corruption instead.
    """
    scratch = _build(unit, translated)
    el = unit.element
    for child in list(el):
        el.remove(child)
    el.text = scratch.text
    for child in list(scratch):
        for node in child.iter():
            node.attrib.pop("__idx", None)
        el.append(child)


# ----------------------------------------------------------------- chunking


def chunk_units(units: list[Unit], target_words: int, tail_merge: float = 0.3) -> list[list[Unit]]:
    """Group consecutive units into chunks of roughly `target_words`.

    A single unit longer than the target is never split — splitting mid-paragraph
    would cost the model the sentence context that makes literary translation
    coherent, and one long paragraph still fits the output cap comfortably.

    Because chunking runs per content document, a chapter slightly longer than the
    target would otherwise leave a stub final chunk (e.g. 70 words) that costs a
    full prompt's input tokens for almost no output. A trailing chunk under
    `tail_merge` of the target is folded back into its predecessor.
    """
    chunks: list[list[Unit]] = []
    current: list[Unit] = []
    words = 0
    for unit in units:
        n = unit.words
        if current and words + n > target_words:
            chunks.append(current)
            current, words = [], 0
        current.append(unit)
        words += n
    if current:
        chunks.append(current)
    if len(chunks) > 1:
        tail_words = sum(u.words for u in chunks[-1])
        if tail_words < target_words * tail_merge:
            chunks[-2].extend(chunks.pop())
    return chunks


def render_chunk(units: list[Unit]) -> str:
    """The wire format sent to the model: each unit preceded by a `<<<n>>>` marker.

    Explicit markers (rather than blank-line separation) give a hard alignment
    signal, so a model that merges or splits paragraphs is detected instead of
    silently shifting every subsequent unit's text by one.
    """
    return "\n".join(f"<<<{i}>>>\n{u.text}" for i, u in enumerate(units, 1))


def parse_chunk(text: str, expected: int) -> dict[int, str]:
    """Inverse of `render_chunk`. Returns {unit_number: translated_text}.

    Missing or extra markers are reported by absence rather than raising: the
    caller decides whether to retry the chunk, fall back per unit, or keep the
    source. Indices outside 1..expected are discarded.
    """
    out: dict[int, str] = {}
    matches = list(UNIT_MARKER_RE.finditer(text))
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        if 1 <= idx <= expected:
            out[idx] = text[m.end() : end].strip("\n")
    return out


# ----------------------------------------------------------------- EPUB container


def opf_path(epub_root: Path) -> Path:
    """Resolve the package document via META-INF/container.xml (never guessed)."""
    container = epub_root / "META-INF" / "container.xml"
    if not container.is_file():
        raise ValueError("not an EPUB: META-INF/container.xml missing")
    tree = etree.parse(str(container))
    rootfile = tree.find(f".//{{{CONTAINER_NS}}}rootfile")
    full_path = rootfile.get("full-path") if rootfile is not None else None
    if not full_path:
        raise ValueError("container.xml has no rootfile/@full-path")
    return epub_root / full_path


def spine_documents(opf: Path) -> list[Path]:
    """Content documents in reading order, resolved relative to the OPF."""
    tree = etree.parse(str(opf))
    manifest = {}
    for item in tree.iter(f"{{{OPF_NS}}}item"):
        if item.get("id"):
            manifest[item.get("id")] = item.get("href", "")
    docs: list[Path] = []
    seen: set[Path] = set()
    for ref in tree.iter(f"{{{OPF_NS}}}itemref"):
        href = manifest.get(ref.get("idref", ""))
        if not href:
            continue
        path = (opf.parent / href).resolve()
        # De-duplicate: a spine that references the same file twice would otherwise be
        # translated twice — double the cost, and the second pass would be fed the
        # already-translated text.
        if path.is_file() and path not in seen:
            seen.add(path)
            docs.append(path)
    return docs


def ncx_paths(opf: Path) -> list[Path]:
    """toc.ncx / nav documents referenced by the manifest, if present.

    Translating these is what puts Bulgarian chapter titles in the e-reader's
    table of contents rather than leaving a Cyrillic book with an English TOC.
    """
    tree = etree.parse(str(opf))
    found: list[Path] = []
    for item in tree.iter(f"{{{OPF_NS}}}item"):
        href = item.get("href", "")
        media = (item.get("media-type") or "").lower()
        props = (item.get("properties") or "").lower()
        if media == "application/x-dtbncx+xml" or "nav" in props.split():
            path = (opf.parent / href).resolve()
            if path.is_file():
                found.append(path)
    return found


def set_opf_metadata(opf: Path, language: str, title_suffix: str | None = None) -> None:
    """Point dc:language at the translation and optionally tag the title.

    Readers group and sort by dc:language, and hyphenation//text-shaping engines
    consult it — leaving `en` on a Bulgarian book is a real, visible defect.
    """
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(opf), parser)
    root = tree.getroot()
    metadata = root.find(f"{{{OPF_NS}}}metadata")
    if metadata is None:
        return
    langs = metadata.findall(f"{{{DC_NS}}}language")
    if langs:
        for extra in langs[1:]:
            metadata.remove(extra)
        langs[0].text = language
    else:
        etree.SubElement(metadata, f"{{{DC_NS}}}language").text = language
    if title_suffix:
        title_el = metadata.find(f"{{{DC_NS}}}title")
        if title_el is not None and title_suffix not in (title_el.text or ""):
            title_el.text = f"{(title_el.text or '').strip()} {title_suffix}".strip()
    tree.write(str(opf), xml_declaration=True, encoding="utf-8")


def write_document(path: Path, tree: etree._ElementTree) -> None:
    """Serialize a parsed content document back to disk preserving its doctype."""
    path.write_bytes(
        etree.tostring(
            tree, xml_declaration=True, encoding="utf-8", doctype=tree.docinfo.doctype or None
        )
    )


def repack(src_dir: Path, out_path: Path) -> None:
    """Zip an unpacked EPUB per OCF rules: `mimetype` first, stored, no extra fields."""
    mimetype = src_dir / "mimetype"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            mimetype.read_bytes() if mimetype.is_file() else b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        for path in sorted(src_dir.rglob("*")):
            if not path.is_file() or path == mimetype:
                continue
            zf.write(path, path.relative_to(src_dir).as_posix(), zipfile.ZIP_DEFLATED)


def unpack(epub: Path, dest: Path) -> Path:
    """Extract an EPUB, refusing entries that would escape `dest` (zip-slip)."""
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    with zipfile.ZipFile(epub) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            # is_relative_to, not str.startswith: a sibling directory sharing a name
            # prefix (…/stage2 vs …/stage) passes a prefix test but is still an escape.
            if target != resolved_dest and not target.is_relative_to(resolved_dest):
                raise ValueError(f"unsafe path in EPUB: {info.filename}")
        zf.extractall(dest)
    return dest


def cyrillic_ratio(text: str) -> float:
    """Share of alphabetic characters that are Cyrillic — a cheap, language-agnostic
    signal that a chunk actually came back translated rather than echoed."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters)
