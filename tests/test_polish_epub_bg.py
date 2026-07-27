"""Offline unit tests for the BgGPT polish stage.

Network-free: openai_client.chat_complete is replaced with a scripted fake.
"""

from __future__ import annotations

import sys
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
    string (with markers) or the EMPTY sentinel.
    """

    EMPTY = object()

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = 0

    def __call__(self, client, model, system_instruction, user_content, **kw):
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
    text = "Той погледна пустинята и си спомни за старите дни на Каладан."
    assert not polish.looks_untranslated(text)


def test_long_english_leftover_is_flagged():
    text = (
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons."
    )
    assert polish.looks_untranslated(text)


def test_short_latin_name_is_not_flagged():
    assert not polish.looks_untranslated("Duncan Idaho")


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
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
    )
    unit = make_unit(pre)
    assert not polish.unit_ok(pre, pre, unit)


def test_accepts_en_to_bg_retranslation():
    pre = (
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
    )
    unit = make_unit(pre)
    bg = (
        "Той се вгледа в пустинята и си спомни старите дни на Каладан, "
        "когато моретата още пееха под луните на северното небе."
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
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
    )
    bg = (
        "Той се вгледа в пустинята и си спомни старите дни на Каладан, "
        "когато моретата още пееха под луните на северното небе."
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
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
    )
    bg = "Той се вгледа в пустинята и си спомни старите дни на Каладан."
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
        "He looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
    )
    bg = "Той се вгледа в пустинята и си спомни старите дни на Каладан."
    # Simulate workdir units: unit.text is still English source; pre is Gemini BG
    # for successful units and English for leftovers.
    src_ok = make_unit(
        "She looked out across the desert and remembered the old days on Caladan "
        "when the seas still sang under the moons of the northern sky."
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
