"""Unit tests for the pure (network-free) pipeline stages in common.py."""

import common


# ------------------------------------------------------------ strip_page_artifacts


def test_drops_page_number_next_to_page_break_rule():
    md = "end of a paragraph.\n\n-----\n\n12\n\nstart of the next page"
    out = common.strip_page_artifacts(md)
    assert "12" not in out
    assert "-----" not in out
    assert "end of a paragraph." in out
    assert "start of the next page" in out


def test_keeps_standalone_chapter_number():
    md = "## Part One\n\n7\n\nIt was a dark and stormy night."
    out = common.strip_page_artifacts(md)
    assert "7" in out.splitlines()


def test_keeps_asterisk_scene_break():
    md = "The door closed.\n\n***\n\nYears later, everything had changed."
    out = common.strip_page_artifacts(md)
    assert "***" in out


def test_drops_rule_and_number_separated_by_blank_lines():
    md = "text\n\n42\n\n-----\n\nmore text"
    out = common.strip_page_artifacts(md)
    assert "42" not in out


# --------------------------------------------------------- merge_split_paragraphs


def test_merges_paragraph_split_by_page_break():
    blocks = ["The sentence was split", "across two pages."]
    assert common.merge_split_paragraphs(blocks) == ["The sentence was split across two pages."]


def test_merges_hyphenated_word_across_blocks():
    blocks = ["He walked to-", "ward the door."]
    assert common.merge_split_paragraphs(blocks) == ["He walked toward the door."]


def test_does_not_merge_after_terminal_punctuation():
    blocks = ["A complete sentence.", "another paragraph starting lowercase"]
    assert common.merge_split_paragraphs(blocks) == blocks


def test_does_not_merge_into_or_out_of_headings():
    blocks = ["# a title", "chapter text continues here"]
    assert common.merge_split_paragraphs(blocks) == blocks


# ------------------------------------------------------------------ chunk_markdown


def test_chunks_split_only_at_block_boundaries():
    # proper sentences, so merge_split_paragraphs doesn't rejoin them
    blocks = [f"Block {i} starts here. " + "Word. " * 50 for i in range(10)]
    md = "\n\n".join(blocks)
    chunks = common.chunk_markdown(md, target_words=120)
    assert len(chunks) > 1
    reassembled = "\n\n".join(chunks)
    for block in blocks:
        assert block.strip() in reassembled


def test_single_oversized_block_becomes_its_own_chunk():
    md = "word " * 500
    chunks = common.chunk_markdown(md, target_words=100)
    assert len(chunks) == 1


# ----------------------------------------------------------------- strip_md_fences


def test_strips_full_fence_wrapper():
    assert common.strip_md_fences("```markdown\nhello\n```") == "hello"


def test_leaves_partial_fences_alone():
    text = "prose\n```\ncode\n```\nmore prose"
    assert common.strip_md_fences(text) == text


# ---------------------------------------------------------------- strip_watermarks


def test_drops_standalone_watermark_url_paragraph():
    md = "real paragraph\n\nhttps://www.scan-site.com/\n\nanother paragraph"
    out = common.strip_watermarks(md, {"scan-site.com"})
    assert "scan-site.com" not in out
    assert "real paragraph" in out and "another paragraph" in out


def test_watermark_matching_ignores_scheme_www_case_and_emphasis():
    md = "text\n\n**WWW.Scan-Site.COM**\n\ntext two"
    out = common.strip_watermarks(md, {"http://scan-site.com/"})
    assert "Scan-Site" not in out


def test_no_hosts_is_a_no_op():
    md = "a\n\nb"
    assert common.strip_watermarks(md, set()) == md


# ------------------------------------------------------- assign_ascii_heading_ids


def test_assigns_sequential_ascii_ids():
    md = "# Заглавие\n\ntext\n\n## Глава първа\n\nmore"
    out = common.assign_ascii_heading_ids(md)
    assert "# Заглавие {#sec-0001}" in out
    assert "## Глава първа {#sec-0002}" in out


def test_skips_hash_lines_inside_code_fences():
    md = "# Real heading\n\n```\n# not a heading\n```\n"
    out = common.assign_ascii_heading_ids(md)
    assert "# Real heading {#sec-0001}" in out
    assert "# not a heading {#sec" not in out
    assert out.endswith("\n")


# ----------------------------------------------------- metadata helpers / frontmatter


def test_validate_language_accepts_bcp47():
    assert common.validate_language("en") == "en"
    assert common.validate_language(" bg ") == "bg"
    assert common.validate_language("pt-BR") == "pt-BR"


def test_validate_language_rejects_prose_and_non_strings():
    assert common.validate_language("English") is None
    assert common.validate_language(None) is None
    assert common.validate_language(12) is None


def test_single_line_collapses_whitespace_and_rejects_non_strings():
    assert common._single_line("A Title\nSplit Over Lines") == "A Title Split Over Lines"
    assert common._single_line("   ") is None
    assert common._single_line(42) is None


def test_frontmatter_escapes_quotes_backslashes_and_newlines():
    fm = common.build_frontmatter('He said "hi"\nagain', "A\\B", "en")
    assert 'title: "He said \\"hi\\" again"' in fm
    assert 'author: "A\\\\B"' in fm
    assert fm.count("\n---\n") == 1
