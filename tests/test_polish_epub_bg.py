"""Offline unit tests for the BgGPT polish stage.

Network-free: openai_client.chat_complete is replaced with a scripted fake.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

# Scripts live under the skill; tests import them the same way as test_translate_epub.
SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "pdf2epub"
    / "skills"
    / "epub-translate"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import epubdoc  # noqa: E402
import openai_client as oai  # noqa: E402
import polish_epub_bg as polish  # noqa: E402

XHTML = epubdoc.XHTML_NS


def make_unit(text: str = "едно две три четири пет шест седем осем девет десет"):
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><p>{text}</p></body></html>'.encode("utf-8")
    )
    (unit,) = epubdoc.find_units(root)
    return unit


def make_marked_unit():
    root = etree.fromstring(
        (
            f'<html xmlns="{XHTML}"><body><p>той ѝ каза <em>нещо тихо</em> още веднъж '
            f"преди дългата студена нощ най-накрая да свърши напълно</p></body></html>"
        ).encode("utf-8")
    )
    (unit,) = epubdoc.find_units(root)
    return unit


def args(**over):
    base = dict(
        model="bggpt-test",
        temperature=0.2,
        unit_retries=2,
        with_source_en=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeChat:
    """Scripted stand-in for openai_client.chat_complete.

    `plan` is a list of outcomes consumed in order. An outcome is a full response
    string (with markers) or the EMPTY sentinel. The queue is lock-guarded so
    parallel solo-retries in tests do not race on `list.pop`.
    """

    EMPTY = object()

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, client, model, system_instruction, user_content, **kw):
        with self._lock:
            self.calls += 1
            if not self.plan:
                raise oai.EmptyResponseError("empty")
            outcome = self.plan.pop(0)
        if outcome is self.EMPTY:
            raise oai.EmptyResponseError("empty")
        return outcome


@pytest.fixture
def patched(monkeypatch):
    def install(plan):
        fake = FakeChat(plan)
        monkeypatch.setattr(polish.oai, "chat_complete", fake)
        return fake

    return install


# --------------------------------------------------------------- looks_untranslated


def test_bulgarian_prose_is_not_flagged_as_english():
    text = "Той погледна пристанището и си спомни за старите дни в Ривъртон."
    assert not polish.looks_untranslated(text)


def test_long_english_leftover_is_flagged():
    text = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights."
    )
    assert polish.looks_untranslated(text)


def test_short_latin_name_is_not_flagged():
    assert not polish.looks_untranslated("Marco Velez")


def test_short_latin_dialogue_is_flagged():
    """Dialogue under the old 8-word floor must still enter EN→BG polish."""
    assert polish.looks_untranslated("No, I will not go back there.")
    assert polish.looks_untranslated("She said nothing of the sort.")


def test_digit_countdown_is_not_flagged():
    assert not polish.looks_untranslated("0  9  8  7  6  5  4  3  2  1")


def test_digit_countdown_passes_unit_ok_unchanged():
    """Non-prose digit lines must not fail the Cyrillic floor (avoids useless retries)."""
    pre = "0  9  8  7  6  5  4  3  2  1"
    assert polish.unit_ok(pre, pre, None)


# ---------------------------------------------------------------- unit_ok gates


def test_accepts_faithful_polish():
    unit = make_marked_unit()
    pre = unit.text
    # Mild edit — same placeholders, still Bulgarian, similar length
    cand = pre.replace("тихо", "спокойно")
    assert polish.unit_ok(pre, cand, unit)


def test_rejects_placeholder_drop():
    unit = make_marked_unit()
    pre = unit.text
    # Drop the emphasis pair entirely
    bad = epubdoc.PLACEHOLDER_RE.sub("", pre)
    assert not polish.unit_ok(pre, bad, unit)


def test_rejects_emptied_markup():
    unit = make_marked_unit()
    pre = unit.text
    bad = pre.replace("[[1]]нещо тихо[[/1]]", "[[1]][[/1]]")
    # pad so length ratio does not fire first
    bad = bad + " още думи" * 3
    assert not polish.unit_ok(pre, bad, unit)


def test_rejects_summarized_polish():
    unit = make_marked_unit()
    pre = unit.text
    assert not polish.unit_ok(pre, "той [[1]]каза[[/1]]", unit)


def test_rejects_english_echo_as_polish_output():
    """A long Latin response fails the Cyrillic floor — catches failed EN→BG."""
    pre = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    unit = make_unit(pre)
    assert not polish.unit_ok(pre, pre, unit)


def test_accepts_en_to_bg_retranslation():
    pre = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    unit = make_unit(pre)
    bg = (
        "Той се вгледа в пристанището и си спомни старите дни в Ривъртон, "
        "когато камбаните още биеха под светлините на вечерния пазар."
    )
    assert polish.unit_ok(pre, bg, unit)


# --------------------------------------------------------------- polish workers


def test_polish_chunk_accepts_good_response(patched):
    unit = make_unit()
    item = polish.PolishUnit(unit=unit, pre=unit.text)
    improved = "едно две три четири пет шест седем осем девет десет и още"
    # Keep similar length — unit_ok uses char ratio for long texts; this is short
    fake = patched([f"<<<1>>>\n{unit.text}"])
    stats = polish.PolishStats()
    out = polish._polish_units(None, args(), "sys", [item], "chunk 1", stats)
    assert out[1] == unit.text
    assert fake.calls == 1
    assert stats.units_unchanged == 1


def test_polish_retries_then_keeps_pre_on_failure(patched):
    unit = make_unit()
    item = polish.PolishUnit(unit=unit, pre=unit.text)
    # Every call empty → keep pre
    fake = patched([FakeChat.EMPTY, FakeChat.EMPTY, FakeChat.EMPTY])
    stats = polish.PolishStats()
    out = polish._polish_units(None, args(unit_retries=2), "sys", [item], "chunk 1", stats)
    assert out[1] == unit.text
    assert stats.units_kept == 1
    assert fake.calls >= 1


def test_polish_retranslates_english_leftover(patched):
    en = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    bg = (
        "Той се вгледа в пристанището и си спомни старите дни в Ривъртон, "
        "когато камбаните още биеха под светлините на вечерния пазар."
    )
    unit = make_unit(en)
    item = polish.PolishUnit(unit=unit, pre=en)
    fake = patched([f"<<<1>>>\n{bg}"])
    stats = polish.PolishStats()
    out = polish._polish_units(None, args(), "sys", [item], "chunk 1", stats)
    assert out[1] == bg
    assert stats.units_retranslated == 1
    assert fake.calls == 1


def test_strip_bg_en_labels():
    raw = "[BG]\nздравей свят\n[EN]\nhello world"
    assert polish._strip_bg_en_labels(raw) == "здравей свят"


def test_render_with_source_en():
    unit = make_unit("бг текст тук")
    item = polish.PolishUnit(unit=unit, pre="бг текст тук", source_en="en text here")
    payload = polish.render_polish_chunk([item], with_source_en=True)
    assert "[BG]" in payload and "[EN]" in payload
    assert "<<<1>>>" in payload


def test_chunk_polish_items_respects_target():
    items = [
        polish.PolishUnit(unit=make_unit("дума " * 20), pre="дума " * 20) for _ in range(5)
    ]
    chunks = polish.chunk_polish_items(items, target_words=50)
    assert len(chunks) >= 2
    assert sum(len(c) for c in chunks) == 5


def test_english_only_filter_keeps_latin_prose():
    en = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    bg = "Той се вгледа в пристанището и си спомни старите дни в Ривъртон."
    items = [
        polish.PolishUnit(unit=make_unit(en), pre=en),
        polish.PolishUnit(unit=make_unit(bg), pre=bg),
    ]
    kept = [it for it in items if polish.looks_untranslated(it.pre)]
    assert len(kept) == 1
    assert kept[0].pre == en


def test_english_only_must_not_drop_non_english_from_apply_set():
    """Regression: workdir mode applies Gemini BG for all units; english-only only
    filters the API job list. Filtering the apply set would leave most of the book
    in English source text.
    """
    en = (
        "He looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    bg = "Той се вгледа в пристанището и си спомни старите дни в Ривъртон."
    # Simulate workdir units: unit.text is still English source; pre is Gemini BG
    # for successful units and English for leftovers.
    src_ok = make_unit(
        "She looked out across the harbor and remembered the old days in Riverton "
        "when the bells still rang under the lights of the evening market."
    )
    src_en = make_unit(en)
    all_items = [
        polish.PolishUnit(unit=src_ok, pre=bg, source_en=src_ok.text),
        polish.PolishUnit(unit=src_en, pre=en, source_en=en),
    ]
    job_items = [it for it in all_items if polish.looks_untranslated(it.pre)]
    assert len(job_items) == 1
    # Apply set must still include the already-Bulgarian Gemini unit
    final = {}
    for it in all_items:
        if it.pre and it.pre != it.unit.text:
            final[id(it.unit)] = it.pre
    assert id(src_ok) in final
    assert final[id(src_ok)] == bg


def test_stats_bump_is_thread_safe():
    from concurrent.futures import ThreadPoolExecutor

    stats = polish.PolishStats()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: stats.bump("units_polished"), range(2000)))
    assert stats.units_polished == 2000


def test_retry_units_parallel_retries_every_failed_index(monkeypatch):
    """Multiple validation failures must each get a solo retry (not only the first)."""
    seen: list[int] = []
    lock = threading.Lock()

    def fake_retry(client, args, system_prompt, item):
        idx = int(item.pre.split()[-1])
        with lock:
            seen.append(idx)
        return f"едно две три четири пет шест седем осем {idx}"

    monkeypatch.setattr(polish, "_retry_unit", fake_retry)
    items = [
        (1, polish.PolishUnit(unit=make_unit("x 1"), pre="latin leftover words here 1")),
        (2, polish.PolishUnit(unit=make_unit("x 2"), pre="latin leftover words here 2")),
        (3, polish.PolishUnit(unit=make_unit("x 3"), pre="latin leftover words here 3")),
    ]
    out = polish._retry_units_parallel(None, args(concurrency=3), "sys", items)
    assert sorted(seen) == [1, 2, 3]
    assert set(out) == {1, 2, 3}
    assert all(out[i] is not None for i in (1, 2, 3))


def test_retry_units_parallel_single_unit_skips_pool(monkeypatch):
    calls = {"n": 0}

    def fake_retry(client, args, system_prompt, item):
        calls["n"] += 1
        return "едно две три"

    monkeypatch.setattr(polish, "_retry_unit", fake_retry)
    item = polish.PolishUnit(unit=make_unit(), pre=make_unit().text)
    out = polish._retry_units_parallel(None, args(), "sys", [(1, item)])
    assert out == {1: "едно две три"}
    assert calls["n"] == 1


def test_accept_or_retry_cached_repairs_bad_entries(monkeypatch):
    unit = make_unit()
    item = polish.PolishUnit(unit=unit, pre=unit.text)
    # Cache has empty string → fails unit_ok → retry supplies good text
    monkeypatch.setattr(
        polish, "_retry_unit", lambda *a, **k: unit.text + " и още"
    )
    stats = polish.PolishStats()
    accepted, dirty = polish._accept_or_retry_cached(
        None, args(), "sys", [item], {1: ""}, stats
    )
    assert dirty
    assert accepted[1] == unit.text + " и още"


def test_polish_units_retries_multiple_failures_via_parallel_helper(monkeypatch):
    """First-pass miss → `_retry_units_parallel` covers every failed index."""
    u1 = make_unit("едно две три четири пет шест седем осем едно")
    u2 = make_unit("едно две три четири пет шест седем осем две")
    items = [
        polish.PolishUnit(unit=u1, pre=u1.text),
        polish.PolishUnit(unit=u2, pre=u2.text),
    ]

    def fake_complete(client, model, system_instruction, user_content, **kw):
        # Chunk path always empty so both units need solo retry
        raise oai.EmptyResponseError("empty")

    def fake_parallel(client, args, system_prompt, need_retry):
        assert [i for i, _ in need_retry] == [1, 2]
        return {1: u1.text, 2: u2.text}

    monkeypatch.setattr(polish.oai, "chat_complete", fake_complete)
    monkeypatch.setattr(polish, "_retry_units_parallel", fake_parallel)
    stats = polish.PolishStats()
    out = polish._polish_units(
        None, args(concurrency=2, unit_retries=1), "sys", items, "chunk 1", stats
    )
    assert out[1] == u1.text
    assert out[2] == u2.text
    assert stats.units_retried == 2


# ---------------------------------------------------------- fingerprint / pre safety


def test_load_gemini_map_format2_with_fingerprints(tmp_path):
    unit_src = "one two three four five six seven eight nine"
    fp = epubdoc.source_fingerprint(unit_src)
    ckpt_dir = tmp_path / "chunks"
    ckpt_dir.mkdir()
    (ckpt_dir / "chunk_0001_abc.json").write_text(
        __import__("json").dumps(
            {"format": 2, "units": {"1": {"t": "едно две три", "s": fp}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    gmap = polish._load_gemini_map(ckpt_dir, 1)
    assert gmap is not None
    assert gmap[1] == ("едно две три", fp)


def test_load_gemini_map_legacy_flat(tmp_path):
    ckpt_dir = tmp_path / "chunks"
    ckpt_dir.mkdir()
    (ckpt_dir / "chunk_0001_old.json").write_text(
        '{"1": "едно две три"}', encoding="utf-8"
    )
    gmap = polish._load_gemini_map(ckpt_dir, 1)
    assert gmap is not None
    assert gmap[1] == ("едно две три", None)


def test_fingerprint_mismatch_refuses_wrong_unit_text(tmp_path):
    """Wrong --translate-target-words would map chunk index N to different units."""
    real_src = "alpha beta gamma delta epsilon zeta eta theta"
    wrong_src = "one two three four five six seven eight nine ten"
    fp_wrong = epubdoc.source_fingerprint(wrong_src)
    ckpt_dir = tmp_path / "chunks"
    ckpt_dir.mkdir()
    (ckpt_dir / "chunk_0001_x.json").write_text(
        __import__("json").dumps(
            {
                "format": 2,
                "units": {"1": {"t": "грешен превод за друг абзац тук", "s": fp_wrong}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    gmap = polish._load_gemini_map(ckpt_dir, 1)
    assert gmap is not None
    text, fp = gmap[1]
    # Simulate plan_from_workdir decision
    if fp is not None and fp != epubdoc.source_fingerprint(real_src):
        pre = real_src
    else:
        pre = text
    assert pre == real_src


def test_pre_is_structurally_safe_rejects_emptied_markup():
    unit = make_marked_unit()
    pre = unit.text
    bad = pre.replace("[[1]]нещо тихо[[/1]]", "[[1]][[/1]]") + " още думи" * 2
    assert polish.pre_is_structurally_safe(unit, pre)
    assert not polish.pre_is_structurally_safe(unit, bad)


def test_pre_is_structurally_safe_allows_source():
    unit = make_unit("english source words here")
    assert polish.pre_is_structurally_safe(unit, unit.text)


def test_plan_from_workdir_alignment(tmp_path):
    """End-to-end: matching fingerprints apply Gemini text; mismatches keep source."""
    import zipfile

    # Build a tiny source EPUB with two paragraphs
    book = tmp_path / "src"
    (book / "META-INF").mkdir(parents=True)
    (book / "OPS").mkdir()
    (book / "mimetype").write_text("application/epub+zip", encoding="utf-8")
    (book / "META-INF" / "container.xml").write_text(
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles><rootfile full-path=\"OPS/package.opf\" "
        'media-type="application/oebps-package+xml"/></rootfiles></container>',
        encoding="utf-8",
    )
    (book / "OPS" / "package.opf").write_text(
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">x</dc:identifier><dc:title>t</dc:title>'
        "<dc:language>en</dc:language></metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest><spine><itemref idref=\"c1\"/></spine></package>",
        encoding="utf-8",
    )
    p1 = "alpha beta gamma delta epsilon zeta eta theta iota"
    p2 = "one two three four five six seven eight nine ten eleven"
    (book / "OPS" / "ch1.xhtml").write_text(
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<html xmlns="{XHTML}"><body><p>{p1}</p><p>{p2}</p></body></html>',
        encoding="utf-8",
    )
    epub_path = tmp_path / "book.epub"
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for path in sorted(book.rglob("*")):
            if path.is_file() and path.name != "mimetype":
                zf.write(path, path.relative_to(book).as_posix())

    workdir = tmp_path / "wd"
    chunks = workdir / "chunks"
    chunks.mkdir(parents=True)
    # Matching format-2 map for both units in one chunk
    (chunks / "chunk_0001_ok.json").write_text(
        __import__("json").dumps(
            {
                "format": 2,
                "units": {
                    "1": {
                        "t": "алфа бета гама делта епсилон зета ета тета йота",
                        "s": epubdoc.source_fingerprint(p1),
                    },
                    "2": {
                        "t": "едно две три четири пет шест седем осем девет десет единадесет",
                        "s": epubdoc.source_fingerprint(p2),
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ns = args(
        translate_target_words=1500,
        doc_filter=None,
        with_source_en=False,
    )
    _stage, _docs, items = polish.plan_from_workdir(epub_path, workdir, ns)
    assert len(items) == 2
    assert "алфа" in items[0].pre
    assert "едно" in items[1].pre

    # Mismatched fingerprints → source English kept
    (chunks / "chunk_0001_ok.json").write_text(
        __import__("json").dumps(
            {
                "format": 2,
                "units": {
                    "1": {
                        "t": "грешен текст който не трябва да се приложи тук",
                        "s": epubdoc.source_fingerprint("totally different source text here"),
                    },
                    "2": {
                        "t": "също грешен втори абзац който не е правилен",
                        "s": epubdoc.source_fingerprint("another wrong source string entirely"),
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _stage, _docs, items2 = polish.plan_from_workdir(epub_path, workdir, ns)
    assert items2[0].pre == p1
    assert items2[1].pre == p2
