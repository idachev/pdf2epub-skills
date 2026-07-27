"""Unit tests for the pure (network-free) EPUB stages in epubdoc.py.

Every fixture here is synthetic. The invariants under test are the ones that make
translation safe: markup survives, placeholders round-trip through word-order
changes, and a corrupted model response is rejected rather than written out.
"""

import zipfile

import pytest
from lxml import etree

import epubdoc

XHTML = epubdoc.XHTML_NS


def parse(fragment: str) -> etree._Element:
    """Parse an XHTML body fragment into its root element."""
    return etree.fromstring(
        f'<html xmlns="{XHTML}"><body>{fragment}</body></html>'.encode("utf-8")
    )


def inner(el: etree._Element) -> str:
    """Serialized inner content of an element, namespace prefixes stripped."""
    out = etree.tostring(el, encoding="unicode")
    out = out.replace(f' xmlns="{XHTML}"', "")
    return out[out.index(">") + 1 : out.rindex("<")]


# ----------------------------------------------------------------- find_units


def test_finds_block_elements_in_document_order():
    root = parse("<h1>Title</h1><p>First.</p><p>Second.</p>")
    units = epubdoc.find_units(root)
    assert [u.text for u in units] == ["Title", "First.", "Second."]


def test_recurses_into_wrappers_instead_of_treating_them_as_units():
    root = parse("<div><p>Inner one.</p><p>Inner two.</p></div>")
    units = epubdoc.find_units(root)
    assert [u.text for u in units] == ["Inner one.", "Inner two."]


def test_skips_script_and_style_subtrees():
    root = parse("<style>p { color: red }</style><script>alert(1)</script><p>Real.</p>")
    assert [u.text for u in epubdoc.find_units(root)] == ["Real."]


def test_ignores_whitespace_only_blocks():
    root = parse("<p>   </p><p>\n\t</p><p>Content.</p>")
    assert [u.text for u in epubdoc.find_units(root)] == ["Content."]


def test_list_items_are_separate_units():
    root = parse("<ul><li>One</li><li>Two</li></ul>")
    assert [u.text for u in epubdoc.find_units(root)] == ["One", "Two"]


def test_inline_markup_becomes_numbered_placeholders():
    root = parse("<p>He said <i>hello</i> to her.</p>")
    (unit,) = epubdoc.find_units(root)
    assert unit.text == "He said [[1]]hello[[/1]] to her."


def test_void_element_becomes_self_closing_placeholder():
    root = parse('<p>Line one<br/>line two</p>')
    (unit,) = epubdoc.find_units(root)
    assert unit.text == "Line one[[1/]]line two"


def test_nested_inline_markup_nests_placeholders():
    root = parse("<p>a <b>bold <i>and italic</i></b> z</p>")
    (unit,) = epubdoc.find_units(root)
    assert unit.text == "a [[1]]bold [[2]]and italic[[/2]][[/1]] z"


def test_word_count_excludes_placeholders():
    root = parse("<p>one <i>two</i> three</p>")
    (unit,) = epubdoc.find_units(root)
    assert unit.words == 3


# -------------------------------------------------------------- placeholder_ids


def test_placeholder_ids_is_order_insensitive():
    """Word order changes legitimately, so the comparison must not depend on it."""
    assert epubdoc.placeholder_ids("[[1]]a[[/1]] [[2/]]") == epubdoc.placeholder_ids(
        "[[2/]] [[1]]b[[/1]]"
    )


def test_placeholder_ids_records_kind_not_just_index():
    assert epubdoc.placeholder_ids("[[1]]a[[/1]]") == ((1, "close"), (1, "open"))
    assert epubdoc.placeholder_ids("[[1/]]") == ((1, "void"),)


def test_placeholder_ids_detects_a_dropped_half_of_a_pair():
    assert epubdoc.placeholder_ids("[[1]]a") != epubdoc.placeholder_ids("[[1]]a[[/1]]")


def test_placeholder_ids_detects_an_unbalanced_pair():
    """Regression: comparing bare indices made `[[1]]x[[1]]` (two opens) look
    identical to `[[1]]x[[/1]]`, and rebuilding it nested an element inside
    itself — silently corrupting the document."""
    assert epubdoc.placeholder_ids("[[1]]x[[1]]") != epubdoc.placeholder_ids("[[1]]x[[/1]]")


def test_placeholder_ids_detects_paired_vs_void_confusion():
    assert epubdoc.placeholder_ids("[[1/]]") != epubdoc.placeholder_ids("[[1]]x[[/1]]")


# ------------------------------------------------------------ apply_translation


def test_translation_rebuilds_inline_markup():
    root = parse("<p>He said <i>hello</i> to her.</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "Той ѝ каза [[1]]здравей[[/1]].")
    assert inner(unit.element) == "Той ѝ каза <i>здравей</i>."


def test_emphasis_follows_the_phrase_when_word_order_changes():
    """The whole reason for the placeholder scheme: the tag lands on the moved
    words, not on whatever occupies the original span."""
    root = parse("<p>She saw the <i>red</i> door.</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "[[1]]Червената[[/1]] врата видя тя.")
    assert inner(unit.element) == "<i>Червената</i> врата видя тя."


def test_translation_preserves_inline_attributes():
    root = parse('<p>See <a href="x.html" class="ref">here</a>.</p>')
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "Виж [[1]]тук[[/1]].")
    link = unit.element.find(f"{{{XHTML}}}a")
    assert link is not None
    assert link.get("href") == "x.html"
    assert link.get("class") == "ref"
    assert link.text == "тук"


def test_translation_rebuilds_nested_inline_markup():
    root = parse("<p>a <b>bold <i>inner</i></b> z</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "я [[1]]дебел [[2]]вътрешен[[/2]][[/1]] а")
    assert inner(unit.element) == "я <b>дебел <i>вътрешен</i></b> а"


def test_translation_rebuilds_void_elements():
    root = parse("<p>one<br/>two</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "едно[[1/]]две")
    assert inner(unit.element) == "едно<br/>две"


def test_translation_with_no_markup_replaces_plain_text():
    root = parse("<p>Plain.</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "Обикновен.")
    assert unit.element.text == "Обикновен."
    assert len(unit.element) == 0


def test_unknown_placeholder_index_raises():
    """A corrupted response must fail loudly, not write a mangled chapter."""
    root = parse("<p>He said <i>hello</i>.</p>")
    (unit,) = epubdoc.find_units(root)
    with pytest.raises(ValueError, match="unknown placeholder"):
        epubdoc.apply_translation(unit, "Той каза [[7]]здравей[[/7]].")


def test_unmatched_closing_placeholder_raises():
    root = parse("<p>a <i>b</i> c</p>")
    (unit,) = epubdoc.find_units(root)
    with pytest.raises(ValueError, match="unmatched closing"):
        epubdoc.apply_translation(unit, "я [[1]]б[[/1]] в[[/1]]")


def test_unbalanced_open_placeholder_raises():
    """Regression: two opens where the source had an open/close pair used to build
    `<i><i/></i>` instead of failing."""
    root = parse("<p>a <i>b</i> c</p>")
    (unit,) = epubdoc.find_units(root)
    with pytest.raises(ValueError, match="unclosed placeholder"):
        epubdoc.apply_translation(unit, "я [[1]]б[[1]] в")


def test_out_of_order_close_raises():
    root = parse("<p>a <b>x <i>y</i></b></p>")
    (unit,) = epubdoc.find_units(root)
    with pytest.raises(ValueError, match="out of order"):
        epubdoc.apply_translation(unit, "я [[1]]х [[2]]у[[/1]][[/2]]")


def test_a_failed_translation_leaves_the_element_untouched():
    """The caller's fallback is 'keep the source text'. That is only safe if a
    rejected translation has not already half-rewritten the element."""
    root = parse("<p>a <i>b</i> c</p>")
    (unit,) = epubdoc.find_units(root)
    before = inner(unit.element)
    for bad in ("я [[7]]б[[/7]]", "я [[1]]б[[1]] в", "я [[/1]] в"):
        with pytest.raises(ValueError):
            epubdoc.apply_translation(unit, bad)
        assert inner(unit.element) == before, f"element mutated by rejected input {bad!r}"


def test_can_apply_accepts_a_valid_rearrangement():
    root = parse("<p>a <i>b</i> c</p>")
    (unit,) = epubdoc.find_units(root)
    assert epubdoc.can_apply(unit, "[[1]]б[[/1]] я в")


def test_can_apply_catches_nesting_order_that_parity_misses():
    """Parity is a sorted multiset, so it cannot see structure. These two strings have
    identical (index, kind) multisets but only one nests correctly — can_apply is what
    turns the broken one into a retry instead of a silent source-language fallback."""
    root = parse("<p>a <b>x <i>y</i></b></p>")
    (unit,) = epubdoc.find_units(root)
    good, bad = "[[1]]х [[2]]у[[/2]][[/1]]", "[[1]]х [[2]]у[[/1]][[/2]]"
    assert epubdoc.placeholder_ids(good) == epubdoc.placeholder_ids(bad)
    assert epubdoc.can_apply(unit, good)
    assert not epubdoc.can_apply(unit, bad)


def test_placeholder_spans_reports_whether_a_pair_holds_text():
    assert epubdoc.placeholder_spans("[[1]]words[[/1]]") == {1: True}
    assert epubdoc.placeholder_spans("[[1]][[/1]]") == {1: False}
    assert epubdoc.placeholder_spans("[[1]]   [[/1]]") == {1: False}
    assert epubdoc.placeholder_spans("[[1/]]") == {}


def test_placeholder_spans_ignores_nested_placeholders_as_own_text():
    assert epubdoc.placeholder_spans("[[1]][[2/]][[/1]]") == {1: False}
    assert epubdoc.placeholder_spans("[[1]]a[[2/]][[/1]]") == {1: True}


def test_emptied_markup_is_rejected():
    """Regression: `[[1]][[/1]]` has correct parity AND correct nesting, but has
    dropped the words the source emphasized. Seen in practice on passages the
    content filter resists."""
    src = "he said [[1]]something[[/1]] quietly"
    assert epubdoc.placeholder_ids(src) == epubdoc.placeholder_ids("той [[1]][[/1]] тихо")
    assert epubdoc.keeps_placeholder_content(src, "той каза [[1]]нещо[[/1]] тихо")
    assert not epubdoc.keeps_placeholder_content(src, "той каза [[1]][[/1]] тихо")


def test_an_empty_source_span_may_stay_empty():
    src = "text [[1]][[/1]] more"
    assert epubdoc.keeps_placeholder_content(src, "текст [[1]][[/1]] още")


def test_can_apply_mutates_nothing():
    root = parse("<p>a <i>b</i> c</p>")
    (unit,) = epubdoc.find_units(root)
    before = inner(unit.element)
    epubdoc.can_apply(unit, "[[1]]б[[/1]] я")
    epubdoc.can_apply(unit, "[[9]]nope[[/9]]")
    assert inner(unit.element) == before


def test_bookkeeping_attribute_never_leaks_into_output():
    root = parse("<p>a <b>x <i>y</i></b> z</p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "я [[1]]х [[2]]у[[/2]][[/1]] а")
    assert "__idx" not in etree.tostring(unit.element, encoding="unicode")


def test_translation_is_idempotent_on_repeated_application():
    root = parse("<p>a <i>b</i></p>")
    (unit,) = epubdoc.find_units(root)
    epubdoc.apply_translation(unit, "я [[1]]б[[/1]]")
    first = inner(unit.element)
    epubdoc.apply_translation(unit, "я [[1]]б[[/1]]")
    assert inner(unit.element) == first


# ------------------------------------------------------------------- chunking


def _units(*word_counts: int) -> list[epubdoc.Unit]:
    root = parse("".join(f"<p>{' '.join(['w'] * n)}</p>" for n in word_counts))
    return epubdoc.find_units(root)


def test_chunking_groups_up_to_the_target():
    chunks = epubdoc.chunk_units(_units(40, 40, 40), target_words=100)
    assert [len(c) for c in chunks] == [2, 1]


def test_a_unit_longer_than_the_target_is_never_split():
    chunks = epubdoc.chunk_units(_units(500), target_words=100)
    assert len(chunks) == 1 and len(chunks[0]) == 1


def test_small_trailing_chunk_is_merged_back():
    """A 10-word tail must not cost a whole prompt's input tokens."""
    chunks = epubdoc.chunk_units(_units(100, 10), target_words=100)
    assert len(chunks) == 1
    assert len(chunks[0]) == 2


def test_substantial_trailing_chunk_is_kept_separate():
    chunks = epubdoc.chunk_units(_units(100, 90), target_words=100)
    assert len(chunks) == 2


def test_chunking_preserves_every_unit():
    units = _units(30, 30, 30, 30, 30)
    flat = [u for c in epubdoc.chunk_units(units, target_words=60) for u in c]
    assert flat == units


# ------------------------------------------------------- wire format round-trip


def test_render_and_parse_round_trip():
    units = _units(3, 3)
    rendered = epubdoc.render_chunk(units)
    parsed = epubdoc.parse_chunk(rendered, len(units))
    assert parsed == {1: units[0].text, 2: units[1].text}


def test_parse_reports_missing_units_by_absence():
    text = "<<<1>>>\nfirst\n<<<3>>>\nthird"
    parsed = epubdoc.parse_chunk(text, 3)
    assert set(parsed) == {1, 3}


def test_parse_discards_out_of_range_markers():
    parsed = epubdoc.parse_chunk("<<<1>>>\na\n<<<9>>>\nb", 2)
    assert set(parsed) == {1}


def test_parse_keeps_multiline_unit_text():
    parsed = epubdoc.parse_chunk("<<<1>>>\nline one\nline two\n<<<2>>>\nnext", 2)
    assert parsed[1] == "line one\nline two"


# ------------------------------------------------------------- cyrillic_ratio


def test_cyrillic_ratio_scores_scripts():
    assert epubdoc.cyrillic_ratio("Това е български текст") == 1.0
    assert epubdoc.cyrillic_ratio("This is English") == 0.0
    assert epubdoc.cyrillic_ratio("") == 0.0


def test_cyrillic_ratio_ignores_digits_and_punctuation():
    assert epubdoc.cyrillic_ratio("1234 -- ,.!") == 0.0
    assert epubdoc.cyrillic_ratio("абв 123 !!!") == 1.0


# ------------------------------------------------------------ EPUB container


MINIMAL_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>A Title</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/>
    <itemref idref="c2"/>
  </spine>
</package>
"""

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


@pytest.fixture
def epub_tree(tmp_path):
    """A minimal but structurally valid unpacked EPUB."""
    root = tmp_path / "book"
    (root / "META-INF").mkdir(parents=True)
    (root / "OPS").mkdir()
    (root / "mimetype").write_text("application/epub+zip", encoding="utf-8")
    (root / "META-INF" / "container.xml").write_text(CONTAINER, encoding="utf-8")
    (root / "OPS" / "package.opf").write_text(MINIMAL_OPF, encoding="utf-8")
    for name in ("ch1.xhtml", "ch2.xhtml"):
        (root / "OPS" / name).write_text(
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<html xmlns="{XHTML}"><body><p>Text in {name}.</p></body></html>',
            encoding="utf-8",
        )
    (root / "OPS" / "toc.ncx").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        '<navPoint id="n1"><navLabel><text>Chapter One</text></navLabel>'
        '<content src="ch1.xhtml"/></navPoint></navMap></ncx>',
        encoding="utf-8",
    )
    return root


def test_opf_path_is_resolved_from_container_not_guessed(epub_tree):
    assert epubdoc.opf_path(epub_tree) == epub_tree / "OPS" / "package.opf"


def test_missing_container_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="container.xml"):
        epubdoc.opf_path(tmp_path)


def test_spine_documents_follow_reading_order(epub_tree):
    docs = epubdoc.spine_documents(epubdoc.opf_path(epub_tree))
    assert [d.name for d in docs] == ["ch1.xhtml", "ch2.xhtml"]


def test_ncx_is_discovered_from_the_manifest(epub_tree):
    paths = epubdoc.ncx_paths(epubdoc.opf_path(epub_tree))
    assert [p.name for p in paths] == ["toc.ncx"]


def test_set_opf_metadata_rewrites_language(epub_tree):
    opf = epubdoc.opf_path(epub_tree)
    epubdoc.set_opf_metadata(opf, "bg")
    tree = etree.parse(str(opf))
    langs = tree.findall(f".//{{{epubdoc.DC_NS}}}language")
    assert len(langs) == 1
    assert langs[0].text == "bg"


def test_set_opf_metadata_appends_title_suffix_once(epub_tree):
    opf = epubdoc.opf_path(epub_tree)
    epubdoc.set_opf_metadata(opf, "bg", "(превод)")
    epubdoc.set_opf_metadata(opf, "bg", "(превод)")
    title = etree.parse(str(opf)).find(f".//{{{epubdoc.DC_NS}}}title")
    assert title is not None
    assert title.text == "A Title (превод)"


# --------------------------------------------------------------- pack / unpack


def test_repack_puts_mimetype_first_and_stored(epub_tree, tmp_path):
    out = tmp_path / "out.epub"
    epubdoc.repack(epub_tree, out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"


def test_repack_unpack_round_trip_preserves_content(epub_tree, tmp_path):
    out = tmp_path / "out.epub"
    epubdoc.repack(epub_tree, out)
    restored = epubdoc.unpack(out, tmp_path / "restored")
    assert (restored / "OPS" / "package.opf").read_text(encoding="utf-8") == MINIMAL_OPF
    assert epubdoc.spine_documents(epubdoc.opf_path(restored))


def test_repack_overwrites_an_existing_file(epub_tree, tmp_path):
    out = tmp_path / "out.epub"
    out.write_bytes(b"stale")
    epubdoc.repack(epub_tree, out)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist()[0] == "mimetype"


def test_unpack_refuses_paths_escaping_the_destination(tmp_path):
    """Zip-slip: a crafted EPUB must not write outside the staging dir."""
    bad = tmp_path / "bad.epub"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../escaped.txt", "nope")
    with pytest.raises(ValueError, match="unsafe path"):
        epubdoc.unpack(bad, tmp_path / "stage")


def test_unpack_refuses_a_sibling_sharing_a_name_prefix(tmp_path):
    """`…/stage2` passes a naive str.startswith check against `…/stage` but is
    still an escape."""
    bad = tmp_path / "bad.epub"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../stage2/pwned.txt", "nope")
    with pytest.raises(ValueError, match="unsafe path"):
        epubdoc.unpack(bad, tmp_path / "stage")


def test_spine_referencing_the_same_file_twice_yields_it_once(tmp_path):
    """A duplicated spine entry would otherwise be translated twice — double cost,
    and the second pass would be fed already-translated text."""
    root = tmp_path / "book"
    (root / "META-INF").mkdir(parents=True)
    (root / "OPS").mkdir()
    (root / "META-INF" / "container.xml").write_text(CONTAINER, encoding="utf-8")
    (root / "OPS" / "package.opf").write_text(
        MINIMAL_OPF.replace(
            '<itemref idref="c2"/>', '<itemref idref="c2"/><itemref idref="c1"/>'
        ),
        encoding="utf-8",
    )
    for name in ("ch1.xhtml", "ch2.xhtml"):
        (root / "OPS" / name).write_text(
            f'<html xmlns="{XHTML}"><body><p>x</p></body></html>', encoding="utf-8"
        )
    docs = epubdoc.spine_documents(epubdoc.opf_path(root))
    assert [d.name for d in docs] == ["ch1.xhtml", "ch2.xhtml"]


def test_write_document_round_trips_a_parsed_tree(epub_tree):
    path = epub_tree / "OPS" / "ch1.xhtml"
    tree = etree.parse(str(path))
    (unit,) = epubdoc.find_units(tree.getroot())
    epubdoc.apply_translation(unit, "Преведен текст.")
    epubdoc.write_document(path, tree)
    assert "Преведен текст." in path.read_text(encoding="utf-8")
    # still parseable after the rewrite
    assert epubdoc.find_units(etree.parse(str(path)).getroot())


# ----------------------------------------------------------------- nav labels / fingerprints


def test_source_fingerprint_is_stable_and_content_sensitive():
    a = epubdoc.source_fingerprint("hello [[1]]world[[/1]]")
    b = epubdoc.source_fingerprint("hello [[1]]world[[/1]]")
    c = epubdoc.source_fingerprint("hello [[1]]there[[/1]]")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_find_nav_labels_reads_direct_ncx_text():
    root = etree.fromstring(
        b'<?xml version="1.0"?>'
        b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
        b'<navPoint><navLabel><text>Chapter One</text></navLabel></navPoint>'
        b"</navMap></ncx>"
    )
    labels = epubdoc.find_nav_labels(root)
    assert len(labels) == 1
    assert labels[0][1] == "Chapter One"


def test_find_nav_labels_reads_span_wrapped_epub3_nav():
    """EPUB3 nav often wraps titles in <a><span>…</span></a>; el.text is empty."""
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><nav>'
        f'<ol><li><a href="ch1.xhtml"><span>Chapter One</span></a></li></ol>'
        f"</nav></body></html>".encode("utf-8")
    )
    labels = epubdoc.find_nav_labels(root)
    assert len(labels) == 1
    assert labels[0][1] == "Chapter One"
    assert epubdoc.local_name(labels[0][0].tag) == "a"


def test_find_nav_labels_reads_mixed_content():
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body><nav>'
        f'<a href="ch1.xhtml">Chapter <em>One</em></a>'
        f"</nav></body></html>".encode("utf-8")
    )
    labels = epubdoc.find_nav_labels(root)
    assert len(labels) == 1
    assert labels[0][1] == "Chapter One"


def test_set_nav_label_replaces_children_with_single_text_node():
    root = etree.fromstring(
        f'<html xmlns="{XHTML}"><body>'
        f'<a href="ch1.xhtml"><span>Chapter One</span></a>'
        f"</body></html>".encode("utf-8")
    )
    a = root.find(f".//{{{XHTML}}}a")
    assert a is not None
    epubdoc.set_nav_label(a, "Глава първа")
    assert a.text == "Глава първа"
    assert list(a) == []
