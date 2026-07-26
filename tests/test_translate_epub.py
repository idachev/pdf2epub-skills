"""Unit tests for the retry/fallback ladder in translate_epub.py.

Network-free: `common.generate` is replaced with a scripted fake, so these assert the
*policy* (how many attempts, on which models, in what order) rather than any model
behaviour. Fixtures are synthetic XHTML.
"""

from types import SimpleNamespace

import pytest
from lxml import etree

import epubdoc
import translate_epub as te

XHTML = epubdoc.XHTML_NS


def make_unit(text: str = "one two three four five six seven eight nine"):
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><p>{text}</p></body></html>'.encode("utf-8")
    )
    (unit,) = epubdoc.find_units(root)
    return unit


def args(**over):
    base = dict(
        model="primary",
        fallback_model=None,
        unit_retries=2,
        thinking_level="low",
        target_language="bg",
        target_language_name=None,
        repair_verbatim=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeGenerate:
    """Scripted stand-in for common.generate.

    `plan` maps a model name to a list of outcomes consumed in order. An outcome is
    either a translated string or the BLOCK sentinel, which raises the same exception
    the real content filter path raises.
    """

    BLOCK = object()

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls = []

    def __call__(self, client, model, contents, system_instruction, **kw):
        self.calls.append(model)
        queue = self.plan.get(model, [])
        outcome = queue.pop(0) if queue else self.BLOCK
        if outcome is self.BLOCK:
            raise te.common.PromptBlockedError("finish_reason=RECITATION")
        return f"<<<1>>>\n{outcome}"


GOOD = "едно две три четири пет шест седем осем девет"


@pytest.fixture
def patched(monkeypatch):
    def install(plan):
        fake = FakeGenerate(plan)
        monkeypatch.setattr(te.common, "generate", fake)
        return fake

    return install


# ------------------------------------------------------------------ retry ladder


def test_first_attempt_success_makes_no_further_calls(patched):
    fake = patched({"primary": [GOOD]})
    got, via_fallback = te._retry_unit(None, args(), "sys", make_unit(), "low")
    assert got is not None and not via_fallback
    assert fake.calls == ["primary"]


def test_a_blocked_unit_is_retried_on_the_same_model(patched):
    """Blocks are partly non-deterministic, so a plain repeat is worth one call."""
    fake = patched({"primary": [FakeGenerate.BLOCK, GOOD]})
    got, via_fallback = te._retry_unit(None, args(), "sys", make_unit(), "low")
    assert got is not None and not via_fallback
    assert fake.calls == ["primary", "primary"]


def test_primary_is_exhausted_before_the_fallback_is_paid_for(patched):
    fake = patched(
        {"primary": [FakeGenerate.BLOCK, FakeGenerate.BLOCK], "backup": [GOOD]}
    )
    got, via_fallback = te._retry_unit(
        None, args(fallback_model="backup"), "sys", make_unit(), "low"
    )
    assert got is not None
    assert via_fallback is True
    assert fake.calls == ["primary", "primary", "backup"]


def test_no_fallback_configured_means_no_second_model(patched):
    fake = patched({"primary": [FakeGenerate.BLOCK, FakeGenerate.BLOCK]})
    got, via_fallback = te._retry_unit(None, args(), "sys", make_unit(), "low")
    assert got is None and not via_fallback
    assert fake.calls == ["primary", "primary"]


def test_both_models_exhausted_gives_up(patched):
    fake = patched({})  # every call blocks
    got, _ = te._retry_unit(
        None, args(fallback_model="backup"), "sys", make_unit(), "low"
    )
    assert got is None
    assert fake.calls == ["primary", "primary", "backup", "backup"]


def test_unit_retries_controls_attempts_per_model(patched):
    fake = patched({})
    te._retry_unit(
        None, args(fallback_model="backup", unit_retries=3), "sys", make_unit(), "low"
    )
    assert fake.calls == ["primary"] * 3 + ["backup"] * 3


def test_unit_retries_below_one_still_attempts_once(patched):
    fake = patched({"primary": [GOOD]})
    te._retry_unit(None, args(unit_retries=0), "sys", make_unit(), "low")
    assert fake.calls == ["primary"]


def test_a_response_failing_validation_counts_as_a_failed_attempt(patched):
    """An echo of the English source must not be accepted just because it parsed."""
    echo = "one two three four five six seven eight nine"
    fake = patched({"primary": [echo, GOOD]})
    got, _ = te._retry_unit(None, args(), "sys", make_unit(), "low")
    assert got is not None
    assert epubdoc.cyrillic_ratio(got) > 0.9
    assert fake.calls == ["primary", "primary"]


# ------------------------------------------------------------ unit acceptance


def marked_unit():
    """16 source words — above the 12-word floor where the ratio guard engages."""
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><p>he said <em>something quiet</em> to her '
        f"once more before the long cold night had finally ended</p></body></html>".encode(
            "utf-8"
        )
    )
    (unit,) = epubdoc.find_units(root)
    assert unit.words >= 12, "fixture must be long enough to trigger the ratio guard"
    return unit


GOOD_MARKED = (
    "той ѝ каза [[1]]нещо тихо[[/1]] още веднъж преди дългата студена нощ "
    "най-накрая да свърши"
)


def test_accepts_a_faithful_translation():
    assert te._unit_ok(marked_unit(), GOOD_MARKED, args())


def test_rejects_emptied_markup():
    """Parity and nesting both pass; the emphasized words are gone. The candidate is
    otherwise the same length, so only the content check can catch it."""
    unit = marked_unit()
    bad = GOOD_MARKED.replace("[[1]]нещо тихо[[/1]]", "[[1]][[/1]] нещо тихо още")
    assert epubdoc.placeholder_ids(bad) == epubdoc.placeholder_ids(unit.text)
    assert epubdoc.can_apply(unit, bad)
    assert not te._unit_ok(unit, bad, args())


def test_rejects_a_truncated_translation():
    unit = marked_unit()
    assert not te._unit_ok(unit, "той [[1]]нещо тихо[[/1]]", args())


def test_rejects_a_padded_translation():
    unit = marked_unit()
    padded = GOOD_MARKED + " отново" * 30
    assert not te._unit_ok(unit, padded, args())


def test_rejects_an_untranslated_echo():
    unit = marked_unit()
    assert not te._unit_ok(unit, unit.text, args())


def test_short_units_are_exempt_from_the_script_check():
    """A name or numeral legitimately stays in Latin script."""
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><p>Duncan</p></body></html>'.encode("utf-8")
    )
    (unit,) = epubdoc.find_units(root)
    assert te._unit_ok(unit, "Duncan", args())


# ------------------------------------------------------------- verbatim repair


def _cached_chunk(text):
    return {1: text}


def test_repair_is_off_without_the_flag_or_a_fallback(patched):
    fake = patched({"primary": [GOOD]})
    unit = make_unit()
    cached = _cached_chunk(unit.text)
    n = te._repair_verbatim(None, args(), "sys", [unit], cached, 1, te.ChunkStats())
    assert n == 0
    assert fake.calls == []
    assert cached[1] == unit.text


def test_repair_retries_a_unit_left_in_the_source_language(patched):
    fake = patched({"primary": [GOOD]})
    unit = make_unit()
    cached = _cached_chunk(unit.text)
    stats = te.ChunkStats()
    n = te._repair_verbatim(
        None, args(repair_verbatim=True), "sys", [unit], cached, 1, stats
    )
    assert n == 1
    assert cached[1] != unit.text
    assert stats.units_repaired == 1
    assert fake.calls == ["primary"]


def test_a_fallback_model_implies_repair(patched):
    patched({"primary": [FakeGenerate.BLOCK, FakeGenerate.BLOCK], "backup": [GOOD]})
    unit = make_unit()
    cached = _cached_chunk(unit.text)
    stats = te.ChunkStats()
    n = te._repair_verbatim(
        None, args(fallback_model="backup"), "sys", [unit], cached, 1, stats
    )
    assert n == 1
    assert stats.units_via_fallback == 1


def test_repair_leaves_already_translated_units_alone(patched):
    """The expensive successes in a checkpoint must never be re-paid for."""
    fake = patched({"primary": [GOOD]})
    unit = make_unit()
    cached = _cached_chunk(GOOD)
    n = te._repair_verbatim(
        None, args(repair_verbatim=True), "sys", [unit], cached, 1, te.ChunkStats()
    )
    assert n == 0
    assert fake.calls == []
    assert cached[1] == GOOD


def test_repair_skips_short_units_that_translate_to_themselves(patched):
    """A name or numeral always equals its source, so retrying it would burn a call
    on every resume and never change anything."""
    fake = patched({"primary": [GOOD]})
    unit = make_unit("Duncan")
    cached = _cached_chunk(unit.text)
    n = te._repair_verbatim(
        None, args(repair_verbatim=True), "sys", [unit], cached, 1, te.ChunkStats()
    )
    assert n == 0
    assert fake.calls == []


def test_repair_keeps_the_source_when_every_attempt_still_blocks(patched):
    patched({})
    unit = make_unit()
    cached = _cached_chunk(unit.text)
    stats = te.ChunkStats()
    n = te._repair_verbatim(
        None, args(fallback_model="backup"), "sys", [unit], cached, 1, stats
    )
    assert n == 0
    assert cached[1] == unit.text
    assert stats.units_repaired == 0


# ------------------------------------------------------------------ stats safety


def test_stats_bump_is_serialized_across_threads():
    from concurrent.futures import ThreadPoolExecutor

    stats = te.ChunkStats()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: stats.bump("units_ok"), range(2000)))
    assert stats.units_ok == 2000
