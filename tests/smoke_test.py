from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "src" / "pdf_reader_tool.py"

spec = importlib.util.spec_from_file_location("pdf_reader_tool", TOOL_PATH)
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tool)


def test_page_range_parser() -> None:
    assert tool.parse_page_ranges(None) is None
    assert tool.parse_page_ranges("") is None
    assert tool.parse_page_ranges("1-3,5,8-9") == {1, 2, 3, 5, 8, 9}
    assert tool.page_range_label({1, 2, 3, 5, 8, 9}) == "1-3, 5, 8-9"
    assert tool.requested_page_indices(10, None) == list(range(10))
    assert tool.requested_page_indices(10, {8, 2, 99}) == [1, 7]


def test_safe_slug_preserves_hebrew_names() -> None:
    assert tool.safe_slug("פסיכולוגיה התפתחותית כרך א.pdf") == "פסיכולוגיה-התפתחותית-כרך-א"


def test_conversion_report_html() -> None:
    report = tool.build_conversion_report(
        [{"number": 1, "tables": [{"reconstructed": True}], "figure_groups": [{"id": "figure-group-1"}]}],
        total_pages=10,
        page_numbers={1},
        crop_settings=None,
        structured=True,
    )
    report_html = tool.conversion_report_to_html(report)
    assert "Conversion report" in report_html
    assert "1 of 10" in report_html
    assert "Tables reconstructed" in report_html
    assert report["figure_groups"] == 1
    assert "Figure groups" in report_html


def test_reader_html_includes_accessible_image_lightbox() -> None:
    rendered = tool.build_reader_html(
        "--- page ---",
        "Sample reader",
        "sample.pdf",
        reader_html='<figure class="embedded-image"><img src="data:image/jpeg;base64,test" alt="Sample image"></figure>',
    )

    assert 'id="imageLightbox"' in rendered
    assert 'role="dialog" aria-modal="true"' in rendered
    assert 'id="imageLightboxClose"' in rendered
    assert "function enhanceImageLightbox()" in rendered
    assert "image.setAttribute('role', 'button')" in rendered
    assert "if (event.key === 'Escape') closeImageLightbox();" in rendered
    assert ".image-lightbox[hidden]" in rendered


def test_image_embedding_options_validate_ranges() -> None:
    assert tool.validate_image_quality(35) == 35
    assert tool.validate_image_quality(95) == 95
    assert tool.validate_image_max_width(240) == 240
    assert tool.validate_image_max_width(2000) == 2000
    for invalid in (34, 96):
        try:
            tool.validate_image_quality(invalid)
            raise AssertionError("invalid image quality was accepted")
        except ValueError:
            pass
    for invalid in (239, 2001):
        try:
            tool.validate_image_max_width(invalid)
            raise AssertionError("invalid image width was accepted")
        except ValueError:
            pass


def test_byte_size_formatter_uses_readable_units() -> None:
    assert tool.format_byte_size(0) == "0 B"
    assert tool.format_byte_size(1536) == "1.5 KB"
    assert tool.format_byte_size(2 * 1024 * 1024) == "2.0 MB"


def test_secondary_text_separation() -> None:
    lines = [
        {"text": "Main body text spans the normal reading width", "size": 12.0, "font": "", "top": 10.0, "bottom": 22.0, "x0": 90.0, "x1": 520.0, "style_words": []},
        {"text": "More main text spans the normal reading width", "size": 12.0, "font": "", "top": 26.0, "bottom": 38.0, "x0": 92.0, "x1": 522.0, "style_words": []},
        {"text": "Small caption under an image", "size": 9.6, "font": "", "top": 44.0, "bottom": 54.0, "x0": 330.0, "x1": 500.0, "style_words": []},
    ]

    flow_lines, secondary_blocks = tool.separate_secondary_text(lines, body_size=12.0, page_width=600.0)

    assert [line["text"] for line in flow_lines] == [
        "Main body text spans the normal reading width",
        "More main text spans the normal reading width",
    ]
    assert len(secondary_blocks) == 1
    assert secondary_blocks[0]["class"] == "caption"
    assert "Small caption" in secondary_blocks[0]["html"]


def test_ltr_reference_fragment_in_body_band_is_not_caption() -> None:
    lines = [
        {"text": "Main body text spans the normal reading width", "size": 12.0, "font": "", "top": 10.0, "bottom": 22.0, "x0": 80.0, "x1": 520.0, "style_words": []},
        {"text": "Bacallao & Smokowski, 2007, p. 62)", "size": 9.6, "font": "", "top": 26.0, "bottom": 36.0, "x0": 220.0, "x1": 430.0, "style_words": []},
        {"text": "Small caption under an image", "size": 9.6, "font": "", "top": 60.0, "bottom": 70.0, "x0": 330.0, "x1": 500.0, "style_words": []},
    ]

    flow_lines, secondary_blocks = tool.separate_secondary_text(lines, body_size=12.0, page_width=600.0)

    assert any("Bacallao" in line["text"] for line in flow_lines)
    assert len(secondary_blocks) == 1
    assert "Small caption" in secondary_blocks[0]["html"]


def test_left_side_caption_lines_are_kept() -> None:
    words = [
        {"text": "caption", "top": 10.0, "bottom": 20.0, "x0": 40.0, "x1": 95.0, "size": 9.5, "fontname": ""},
        {"text": "text", "top": 10.2, "bottom": 20.2, "x0": 105.0, "x1": 135.0, "size": 9.5, "fontname": ""},
    ]

    lines = tool.cluster_visual_lines(words, page_width=600.0)

    assert len(lines) == 1
    assert lines[0]["x1"] < 600.0 * 0.34
    assert "caption" in lines[0]["text"]


def test_same_height_side_text_is_split_from_body_text() -> None:
    words = [
        {"text": "caption", "top": 10.0, "bottom": 20.0, "x0": 40.0, "x1": 95.0, "size": 9.5, "fontname": ""},
        {"text": "line", "top": 10.1, "bottom": 20.1, "x0": 105.0, "x1": 135.0, "size": 9.5, "fontname": ""},
        {"text": "body", "top": 10.0, "bottom": 22.0, "x0": 360.0, "x1": 405.0, "size": 12.0, "fontname": ""},
        {"text": "text", "top": 10.2, "bottom": 22.2, "x0": 415.0, "x1": 455.0, "size": 12.0, "fontname": ""},
    ]

    lines = tool.cluster_visual_lines(words, page_width=600.0)

    assert len(lines) == 2
    assert any("caption" in line["text"] for line in lines)
    assert any("body" in line["text"] for line in lines)


def test_medium_horizontal_gap_splits_mixed_layout_line() -> None:
    words = [
        {"text": "caption", "top": 10.0, "bottom": 20.0, "x0": 70.0, "x1": 126.0, "size": 10.0, "fontname": ""},
        {"text": "text", "top": 10.1, "bottom": 20.1, "x0": 134.0, "x1": 164.0, "size": 10.0, "fontname": ""},
        {"text": "body", "top": 10.0, "bottom": 22.0, "x0": 180.0, "x1": 226.0, "size": 12.0, "fontname": ""},
        {"text": "continues", "top": 10.2, "bottom": 22.2, "x0": 234.0, "x1": 302.0, "size": 12.0, "fontname": ""},
    ]

    lines = tool.cluster_visual_lines(words, page_width=573.0)

    assert len(lines) == 2
    assert any("caption" in line["text"] and "body" not in line["text"] for line in lines)
    assert any("body" in line["text"] and "caption" not in line["text"] for line in lines)


def test_crop_bounds_normalize_layout_coordinates() -> None:
    words = [
        {"text": "caption", "top": 10.0, "bottom": 20.0, "x0": 250.0, "x1": 305.0, "size": 9.5, "fontname": ""},
        {"text": "line", "top": 10.1, "bottom": 20.1, "x0": 315.0, "x1": 345.0, "size": 9.5, "fontname": ""},
    ]
    crop_bounds = (220.0, 0.0, 520.0, 700.0)

    normalized_words = tool.normalize_words_to_layout_bounds(words, crop_bounds)
    lines = tool.cluster_visual_lines(
        normalized_words,
        page_width=tool.layout_width_for_bounds(600.0, crop_bounds),
    )

    assert lines[0]["x0"] == 30.0
    assert lines[0]["x1"] == 125.0
    assert lines[0]["x1"] < tool.layout_width_for_bounds(600.0, crop_bounds)


def test_crop_alignment_keeps_only_visual_matches() -> None:
    visual_lines = [
        {"text": "cropped caption", "size": 9.5, "font": "", "top": 10.0, "bottom": 20.0, "x0": 30.0, "x1": 125.0, "style_words": []},
    ]
    full_page_text_lines = ["unrelated full page body line", "cropped caption"]

    corrected = tool.align_text_lines_to_style_lines(full_page_text_lines, visual_lines, require_visual_match=True)

    assert len(corrected) == 1
    assert corrected[0]["text"] == "cropped caption"
    assert "unrelated" not in corrected[0]["text"]


def test_layout_debug_helpers_report_line_matches() -> None:
    visual_lines = [
        {"text": "debug caption", "size": 9.5, "font": "", "top": 10.0, "bottom": 20.0, "x0": 30.0, "x1": 125.0, "style_words": []},
    ]

    match = tool.best_style_line_match("debug caption", visual_lines)
    record = tool.line_debug_record(visual_lines[0], index=2, role="visual")

    assert match["visual_index"] == 0
    assert match["score"] >= 0.9
    assert record["index"] == 2
    assert record["role"] == "visual"
    assert record["width"] == 95.0


def test_hebrew_similarity_matches_mixed_citation_line() -> None:
    visual_line = {
        "text": "ביותר לקדם התפתחות (& relgeiS;3102, tuoriJ &, niltaM, rhalK",
        "size": 12.0,
        "font": "",
        "top": 10.0,
        "bottom": 22.0,
        "x0": 20.0,
        "x1": 260.0,
        "style_words": [],
    }

    score = tool.style_line_similarity("Klahr, Matlin, & Jirout, 2013; Siegler &) ביותר לקדם התפתחות", visual_line)

    assert score >= 0.58


def test_ltr_citation_is_rendered_as_bidi_isolate() -> None:
    rendered = tool.text_with_bidi_isolates("׳©׳ ׳”׳—׳•׳§׳¨׳™׳ Klahr, Matlin, & Jirout, 2013; Siegler & ׳”׳׳©׳")

    assert '<bdi dir="ltr">Klahr, Matlin, &amp; Jirout, 2013; Siegler &amp;</bdi>' in rendered


def test_split_ltr_citation_is_repaired_before_rendering() -> None:
    repaired = tool.clean_extracted_text_line(
        "Klahr, Matlin, & Jirout, 2013; Siegler &) ביותר לקדם התפתחות 2006, Svetina)."
    )
    rendered = tool.text_with_bidi_isolates(repaired)

    assert repaired == "ביותר לקדם התפתחות (Klahr, Matlin, & Jirout, 2013; Siegler & Svetina, 2006)."
    assert '<bdi dir="ltr">(Klahr, Matlin, &amp; Jirout, 2013; Siegler &amp; Svetina, 2006)</bdi>' in rendered


def test_split_ltr_citation_is_repaired_across_paragraph_lines() -> None:
    lines = [
        {
            "text": "\u05d4\u05d3\u05e8\u05da \u05d4\u05d8\u05d5\u05d1\u05d4 Klahr, Matlin, & Jirout, 2013; Siegler &)",
            "top": 10.0,
            "bottom": 22.0,
            "size": 12.0,
            "font": "",
            "style_words": [],
        },
        {
            "text": "\u05d1\u05d9\u05d5\u05ea\u05e8 \u05dc\u05e7\u05d3\u05dd \u05d4\u05ea\u05e4\u05ea\u05d7\u05d5\u05ea 2006, Svetina). \u05d4\u05de\u05d1\u05e7\u05e8\u05d9\u05dd",
            "top": 24.0,
            "bottom": 36.0,
            "size": 12.0,
            "font": "",
            "style_words": [],
        },
    ]

    blocks = tool.lines_to_blocks(lines, body_size=12.0)
    text = tool.block_plain_text(blocks[0])

    assert "\u05d4\u05d3\u05e8\u05da \u05d4\u05d8\u05d5\u05d1\u05d4 \u05d1\u05d9\u05d5\u05ea\u05e8 \u05dc\u05e7\u05d3\u05dd \u05d4\u05ea\u05e4\u05ea\u05d7\u05d5\u05ea" in text
    assert "(Klahr, Matlin, & Jirout, 2013; Siegler & Svetina, 2006)." in text


def test_parenthetical_reference_list_is_repaired_across_lines() -> None:
    lines = [
        {
            "text": "\u05d4\u05e9\u05d9\u05e0\u05d5\u05d9\u05d9\u05dd \u05de\u05ea\u05e8\u05d7\u05e9\u05d9\u05dd \u05de\u05db\u05e4\u05d9 \u05e9\u05e1\u05d1\u05e8 \u05e4\u05d9\u05d0\u05d6'\u05d4 (Case",
            "top": 10.0,
            "bottom": 22.0,
            "size": 12.0,
            "font": "",
            "style_words": [],
        },
        {
            "text": "1998). \u05d1\u05e7\u05e8\u05d1 \u05e9\u05d5\u05dc\u05dc\u05d9; Fischer & Bidell, 2006; Halford & Andrews, 2011; Morra et al., 2008 \u05e8\u05e6\u05e3 \u05d4\u05e9\u05dc\u05d1\u05d9\u05dd",
            "top": 24.0,
            "bottom": 36.0,
            "size": 12.0,
            "font": "",
            "style_words": [],
        },
    ]

    blocks = tool.lines_to_blocks(lines, body_size=12.0)
    text = tool.block_plain_text(blocks[0])

    assert "(Case, 1998; Fischer & Bidell, 2006; Halford & Andrews, 2011; Morra et al., 2008)." in text
    assert "\u05d1\u05e7\u05e8\u05d1 \u05e9\u05d5\u05dc\u05dc\u05d9 \u05e8\u05e6\u05e3 \u05d4\u05e9\u05dc\u05d1\u05d9\u05dd" in text


def test_question_callout_uses_visual_column_order() -> None:
    def visual(value: str) -> str:
        return value[::-1] if any("\u0590" <= char <= "\u05ff" for char in value) else value

    visual_lines = [
        {"text": visual("\u05e9\u05d0\u05dc\u05d5 \u05d0\u05ea \u05e2\u05e6\u05de\u05db\u05dd"), "top": 100.0, "bottom": 112.0, "x0": 330.0, "x1": 410.0, "size": 12.0, "font": "", "style_words": []},
        {"text": visual("\u2022 \u05e9\u05d0\u05dc\u05d4 \u05d9\u05de\u05e0\u05d9\u05ea"), "top": 125.0, "bottom": 137.0, "x0": 290.0, "x1": 410.0, "size": 12.0, "font": "", "style_words": []},
        {"text": visual("\u05d4\u05de\u05e9\u05da \u05d9\u05de\u05e0\u05d9"), "top": 140.0, "bottom": 152.0, "x0": 292.0, "x1": 405.0, "size": 12.0, "font": "", "style_words": []},
        {"text": visual("\u2022 \u05e9\u05d0\u05dc\u05d4 \u05e9\u05de\u05d0\u05dc\u05d9\u05ea"), "top": 125.0, "bottom": 137.0, "x0": 95.0, "x1": 225.0, "size": 12.0, "font": "", "style_words": []},
        {"text": visual("\u05d4\u05de\u05e9\u05da \u05e9\u05de\u05d0\u05dc\u05d9"), "top": 140.0, "bottom": 152.0, "x0": 100.0, "x1": 220.0, "size": 12.0, "font": "", "style_words": []},
        {"text": visual("\u05db\u05d5\u05ea\u05e8\u05ea \u05d0\u05d7\u05e8\u05d9"), "top": 205.0, "bottom": 217.0, "x0": 300.0, "x1": 410.0, "size": 12.0, "font": "", "style_words": []},
    ]

    flow_lines = tool.visual_lines_to_text_lines(visual_lines)
    remaining, callouts = tool.extract_question_callouts(flow_lines, visual_lines, body_size=12.0)
    text = tool.block_plain_text(callouts[0])

    assert len(callouts) == 1
    assert "\u05db\u05d5\u05ea\u05e8\u05ea \u05d0\u05d7\u05e8\u05d9" in tool.block_plain_text({"lines": remaining})
    assert text.index("\u05e9\u05d0\u05dc\u05d4 \u05d9\u05de\u05e0\u05d9\u05ea") < text.index("\u05e9\u05d0\u05dc\u05d4 \u05e9\u05de\u05d0\u05dc\u05d9\u05ea")
    assert 'class="question-columns"' in callouts[0]["html"]


def test_layout_regions_include_kind_zone_and_order() -> None:
    body = {
        "top": 10.0,
        "bottom": 40.0,
        "lines": [{"text": "body", "top": 10.0, "bottom": 40.0, "x0": 80.0, "x1": 430.0}],
        "tag": "p",
        "html": "body",
    }
    question = {
        "top": 50.0,
        "bottom": 90.0,
        "reading_top": 50.0,
        "class": "question-box",
        "lines": [{"text": "question", "top": 50.0, "bottom": 90.0, "x0": 110.0, "x1": 420.0}],
        "tag": "aside",
        "html": "question",
    }
    caption = {
        "top": 100.0,
        "bottom": 120.0,
        "reading_top": 100.0,
        "class": "caption",
        "lines": [{"text": "caption", "top": 100.0, "bottom": 120.0, "x0": 70.0, "x1": 220.0}],
        "tag": "aside",
        "html": "caption",
    }
    table = {"index": 1, "top": 130.0, "bottom": 190.0, "x0": 75.0, "x1": 435.0, "rows": 4, "cols": 2, "data": [["a", "b"]], "reconstructed": True}

    regions = tool.build_layout_regions([body, question], [caption], [table], page_width=500.0)

    assert [region["kind"] for region in regions] == ["body", "question", "caption", "table"]
    assert regions[1]["internal_flow"] == "rtl_columns"
    assert regions[2]["zone"] == "left"
    assert regions[3]["reconstructed"] is True


def test_layout_item_sort_uses_rtl_order_for_same_band() -> None:
    left = {
        "top": 100.0,
        "bottom": 120.0,
        "lines": [{"text": "left", "top": 100.0, "bottom": 120.0, "x0": 70.0, "x1": 220.0, "size": 12.0}],
    }
    right = {
        "top": 101.0,
        "bottom": 121.0,
        "lines": [{"text": "right", "top": 101.0, "bottom": 121.0, "x0": 310.0, "x1": 460.0, "size": 12.0}],
    }
    later = {
        "top": 155.0,
        "bottom": 175.0,
        "lines": [{"text": "later", "top": 155.0, "bottom": 175.0, "x0": 310.0, "x1": 460.0, "size": 12.0}],
    }

    ordered = tool.sort_layout_items([("body", left), ("body", later), ("body", right)], page_width=520.0, body_size=12.0)

    assert [tool.block_plain_text(item) for _, item in ordered] == ["right", "left", "later"]


def test_noise_lines_are_split_from_content() -> None:
    lines = [
        {"text": "File #0005218 belongs to Dvir Yamin- do not distribute", "top": 20.0, "bottom": 30.0, "x0": 100.0, "x1": 400.0, "size": 8.0},
        {"text": "28 פסיכולוגיה התפתחותית", "top": 58.0, "bottom": 70.0, "x0": 320.0, "x1": 500.0, "size": 10.0},
        {"text": "Main paragraph text should remain in the reader", "top": 130.0, "bottom": 145.0, "x0": 80.0, "x1": 430.0, "size": 12.0},
        {"text": "10493-Book 1-10.indb 28 08/09/2022 14:28:42", "top": 785.0, "bottom": 798.0, "x0": 30.0, "x1": 260.0, "size": 8.0},
    ]

    content, noise = tool.split_noise_lines(lines, page_width=600.0, page_height=840.0, body_size=12.0)

    assert [line["text"] for line in content] == ["Main paragraph text should remain in the reader"]
    assert [line["noise_reason"] for line in noise] == ["watermark", "running_header", "footer_metadata"]


def test_secondary_text_blocks_split_by_horizontal_region() -> None:
    lines = [
        {"text": "left caption first line", "top": 100.0, "bottom": 112.0, "x0": 50.0, "x1": 230.0, "size": 9.5, "font": "", "style_words": []},
        {"text": "right caption first line", "top": 100.5, "bottom": 112.5, "x0": 330.0, "x1": 510.0, "size": 9.5, "font": "", "style_words": []},
        {"text": "left caption second line", "top": 116.0, "bottom": 128.0, "x0": 52.0, "x1": 228.0, "size": 9.5, "font": "", "style_words": []},
        {"text": "right caption second line", "top": 116.5, "bottom": 128.5, "x0": 332.0, "x1": 508.0, "size": 9.5, "font": "", "style_words": []},
    ]

    blocks = tool.secondary_lines_to_blocks(lines, body_size=12.0)

    assert len(blocks) == 2
    texts = [tool.block_plain_text(block) for block in blocks]
    assert any("left caption first line" in text and "left caption second line" in text for text in texts)
    assert any("right caption first line" in text and "right caption second line" in text for text in texts)


def test_secondary_text_blocks_split_adjacent_image_captions() -> None:
    lines = [
        {"text": "left image caption", "top": 100.0, "bottom": 112.0, "x0": 78.0, "x1": 278.0, "size": 9.5, "font": "", "style_words": []},
        {"text": "right image caption", "top": 100.2, "bottom": 112.2, "x0": 294.0, "x1": 559.0, "size": 9.5, "font": "", "style_words": []},
    ]

    blocks = tool.secondary_lines_to_blocks(lines, body_size=12.0)

    assert len(blocks) == 2


def test_secondary_text_reading_order_does_not_interrupt_nearby_body_blocks() -> None:
    body_blocks = [
        {"top": 10.0, "bottom": 42.0, "lines": [], "tag": "p", "html": "first body"},
        {"top": 82.0, "bottom": 114.0, "lines": [], "tag": "p", "html": "continued body"},
    ]
    secondary_blocks = [
        {"top": 52.0, "bottom": 68.0, "lines": [], "tag": "aside", "class": "caption", "html": "caption"},
    ]

    tool.assign_secondary_reading_order(body_blocks, secondary_blocks, body_size=12.0)

    assert secondary_blocks[0]["reading_top"] > body_blocks[1]["bottom"]


def test_secondary_text_waits_for_continuing_body_sentence() -> None:
    body_blocks = [
        {
            "top": 10.0,
            "bottom": 42.0,
            "lines": [{"text": "body sentence continues without final punctuation"}],
            "tag": "p",
            "html": "body sentence continues without final punctuation",
        },
        {
            "top": 190.0,
            "bottom": 222.0,
            "lines": [{"text": "and finishes only here."}],
            "tag": "p",
            "html": "and finishes only here.",
        },
    ]
    secondary_blocks = [
        {"top": 72.0, "bottom": 94.0, "lines": [{"text": "caption"}], "tag": "aside", "class": "caption", "html": "caption"},
    ]

    tool.assign_secondary_reading_order(body_blocks, secondary_blocks, body_size=12.0)

    assert secondary_blocks[0]["reading_top"] > body_blocks[1]["bottom"]


def test_adjacent_body_blocks_merge_in_same_region() -> None:
    blocks = [
        {
            "top": 100.0,
            "bottom": 124.0,
            "lines": [{"text": "first part without final punctuation", "top": 100.0, "bottom": 124.0, "x0": 35.0, "x1": 485.0, "size": 12.0, "font": "", "style_words": []}],
            "tag": "p",
            "html": "first part without final punctuation",
        },
        {
            "top": 142.0,
            "bottom": 166.0,
            "lines": [{"text": "second part finishes it.", "top": 142.0, "bottom": 166.0, "x0": 37.0, "x1": 483.0, "size": 12.0, "font": "", "style_words": []}],
            "tag": "p",
            "html": "second part finishes it.",
        },
    ]

    merged = tool.merge_adjacent_body_blocks(blocks, page_width=520.0, body_size=12.0)

    assert len(merged) == 1
    assert merged[0]["merged_block_count"] == 2
    assert merged[0]["merge_reasons"] == ["continuing_sentence"]
    assert "second part finishes it." in tool.block_plain_text(merged[0])


def test_rtl_first_line_indent_creates_protected_paragraph_boundaries() -> None:
    def line(text: str, top: float, x1: float) -> dict:
        return {
            "text": text,
            "top": top,
            "bottom": top + 10.0,
            "x0": 205.0,
            "x1": x1,
            "size": 10.0,
            "font": "",
            "style_words": [],
        }

    lines = [
        line("\u05e1\u05d9\u05d5\u05dd \u05d4\u05e4\u05e1\u05e7\u05d4 \u05d4\u05e8\u05d0\u05e9\u05d5\u05e0\u05d4.", 10.0, 559.0),
        line("\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05e4\u05e1\u05e7\u05d4 \u05d4\u05e9\u05e0\u05d9\u05d9\u05d4", 25.0, 545.0),
        line("\u05d4\u05de\u05e9\u05da \u05e9\u05dc \u05d4\u05e4\u05e1\u05e7\u05d4", 40.0, 559.0),
        line("\u05e1\u05d9\u05d5\u05dd \u05d4\u05e4\u05e1\u05e7\u05d4 \u05d4\u05e9\u05e0\u05d9\u05d9\u05d4.", 55.0, 559.0),
        line("\u05ea\u05d7\u05d9\u05dc\u05ea \u05d4\u05e4\u05e1\u05e7\u05d4 \u05d4\u05e9\u05dc\u05d9\u05e9\u05d9\u05ea", 70.0, 545.0),
        line("\u05d4\u05de\u05e9\u05da \u05d4\u05e4\u05e1\u05e7\u05d4 \u05d4\u05e9\u05dc\u05d9\u05e9\u05d9\u05ea", 85.0, 559.0),
    ]

    blocks = tool.lines_to_blocks(lines, body_size=10.0, page_width=637.0)
    merged = tool.merge_adjacent_body_blocks(blocks, page_width=637.0, body_size=10.0)

    assert len(blocks) == 3
    assert [block["paragraph_start_reason"] for block in blocks] == [
        "page_start",
        "rtl_first_line_indent",
        "rtl_first_line_indent",
    ]
    assert len(merged) == 3


def test_adjacent_body_blocks_do_not_merge_across_columns() -> None:
    blocks = [
        {
            "top": 100.0,
            "bottom": 124.0,
            "lines": [{"text": "right column continues", "top": 100.0, "bottom": 124.0, "x0": 330.0, "x1": 500.0, "size": 12.0, "font": "", "style_words": []}],
            "tag": "p",
            "html": "right column continues",
        },
        {
            "top": 142.0,
            "bottom": 166.0,
            "lines": [{"text": "left column text", "top": 142.0, "bottom": 166.0, "x0": 60.0, "x1": 230.0, "size": 12.0, "font": "", "style_words": []}],
            "tag": "p",
            "html": "left column text",
        },
    ]

    merged = tool.merge_adjacent_body_blocks(blocks, page_width=560.0, body_size=12.0)

    assert len(merged) == 2


def test_body_merge_skips_question_blocks() -> None:
    body = {
        "top": 100.0,
        "bottom": 124.0,
        "lines": [{"text": "body continues", "top": 100.0, "bottom": 124.0, "x0": 80.0, "x1": 440.0, "size": 12.0, "font": "", "style_words": []}],
        "tag": "p",
        "html": "body continues",
    }
    question = {
        "top": 140.0,
        "bottom": 180.0,
        "class": "question-box",
        "lines": [{"text": "question text", "top": 140.0, "bottom": 180.0, "x0": 90.0, "x1": 430.0, "size": 12.0, "font": "", "style_words": []}],
        "tag": "aside",
        "html": "question text",
    }

    merged = tool.merge_adjacent_body_blocks([body, question], page_width=520.0, body_size=12.0)

    assert len(merged) == 2
    assert merged[1]["class"] == "question-box"


def test_body_merge_skips_heading_like_blocks() -> None:
    heading = {
        "top": 100.0,
        "bottom": 124.0,
        "lines": [{"text": "Section heading without punctuation", "top": 100.0, "bottom": 124.0, "x0": 170.0, "x1": 350.0, "size": 13.0, "font": "", "style_words": []}],
        "tag": "p",
        "html": "Section heading without punctuation",
    }
    body = {
        "top": 144.0,
        "bottom": 168.0,
        "lines": [{"text": "body paragraph starts here.", "top": 144.0, "bottom": 168.0, "x0": 80.0, "x1": 440.0, "size": 12.0, "font": "", "style_words": []}],
        "tag": "p",
        "html": "body paragraph starts here.",
    }

    merged = tool.merge_adjacent_body_blocks([heading, body], page_width=520.0, body_size=12.0)

    assert len(merged) == 2


def test_content_image_records_ignore_full_page_backgrounds() -> None:
    images = [
        {"x0": 0.0, "x1": 600.0, "top": 0.0, "bottom": 800.0},
        {"x0": 80.0, "x1": 300.0, "top": 120.0, "bottom": 360.0},
    ]

    records = tool.content_image_records(images, page_width=600.0, page_height=800.0)

    assert len(records) == 1
    assert records[0]["id"] == "image-1"
    assert records[0]["source_index"] == 2


def test_image_caption_match_detects_side_caption() -> None:
    image = {"id": "image-1", "x0": 205.0, "x1": 560.0, "top": 234.0, "bottom": 467.0}
    caption = {
        "id": "caption-1",
        "box": (78.0, 395.0, 185.0, 469.0),
        "text": "caption beside image",
        "line_count": 3,
        "zone": "left",
    }

    match = tool.score_image_caption_match(image, caption, page_width=637.0)

    assert match["match_reason"] == "side_overlap"
    assert match["match_confidence"] >= 0.55
    assert match["caption_side"] == "left"
    assert match["geometry_confirmed"] is True


def test_geometry_promotes_narrow_irregular_captions_but_not_distant_notes() -> None:
    image = {"id": "image-1", "x0": 80.0, "x1": 430.0, "top": 200.0, "bottom": 520.0}
    side_caption = {
        "id": "caption-side",
        "box": (450.0, 350.0, 560.0, 500.0),
        "text": "A narrow multi-line caption beside an image",
        "line_count": 6,
        "zone": "right",
        "caption_type": "sidebar",
    }
    below_caption = {
        "id": "caption-below",
        "box": (90.0, 530.0, 420.0, 610.0),
        "text": "A narrow multi-line caption below an image",
        "line_count": 5,
        "zone": "left",
        "caption_type": "sidebar",
    }
    distant_note = {
        "id": "caption-distant",
        "box": (90.0, 40.0, 420.0, 100.0),
        "text": "A distant note above the image",
        "line_count": 4,
        "zone": "left",
        "caption_type": "sidebar",
    }

    side_match = tool.score_image_caption_match(image, side_caption, page_width=637.0)
    below_match = tool.score_image_caption_match(image, below_caption, page_width=637.0)
    distant_match = tool.score_image_caption_match(image, distant_note, page_width=637.0)

    assert side_match["geometry_confirmed"] is True
    assert side_match["caption_type"] == "full_caption"
    assert side_match["source_caption_type"] == "sidebar"
    assert below_match["geometry_confirmed"] is True
    assert below_match["caption_type"] == "full_caption"
    assert distant_match["geometry_confirmed"] is False
    assert distant_match["caption_type"] == "sidebar"
    assert distant_match["match_confidence"] < tool.IMAGE_CAPTION_CONFIDENCE_THRESHOLD


def test_image_caption_match_uses_close_edge_alignment_for_narrow_above_caption() -> None:
    image = {"id": "image-1", "x0": 78.0, "x1": 559.0, "top": 413.0, "bottom": 709.0}
    caption = {
        "id": "caption-1",
        "box": (452.0, 308.0, 560.0, 393.0),
        "text": "narrow multi-line caption immediately above the image edge",
        "line_count": 4,
        "zone": "right",
        "caption_type": "sidebar",
    }
    shifted_note = {**caption, "id": "caption-2", "box": (300.0, 308.0, 408.0, 393.0)}

    match = tool.score_image_caption_match(image, caption, page_width=637.0)
    shifted_match = tool.score_image_caption_match(image, shifted_note, page_width=637.0)

    assert match["edge_aligned"] is True
    assert match["geometry_confirmed"] is True
    assert match["caption_type"] == "full_caption"
    assert match["match_confidence"] >= tool.IMAGE_CAPTION_CONFIDENCE_THRESHOLD
    assert shifted_match["edge_aligned"] is False
    assert shifted_match["geometry_confirmed"] is False
    assert shifted_match["caption_type"] == "sidebar"


def test_image_caption_matching_marks_ambiguous_candidate_sets() -> None:
    images = [{"id": "image-1", "x0": 80.0, "x1": 300.0, "top": 120.0, "bottom": 320.0}]
    captions = [
        {"id": f"caption-{index}", "box": (80.0, 330.0 + index, 300.0, 350.0 + index), "text": f"short label {index}", "line_count": 1, "zone": "left"}
        for index in range(1, 7)
    ]

    tool.attach_image_caption_matches(images, captions, page_width=600.0)

    assert images[0]["ambiguous_caption_candidates"] is True
    assert images[0]["match_reason"].startswith("ambiguous_")


def test_caption_candidate_types_distinguish_figure_labels_and_sidebars() -> None:
    assert tool.classify_caption_candidate("\u05ea\u05e8\u05e9\u05d9\u05dd 5.5 \u05ea\u05d9\u05d0\u05d5\u05e8 \u05d4\u05d3\u05de\u05d9\u05d4", 3, 80.0, 540.0, 620.0) == "full_caption"
    assert tool.classify_caption_candidate("\u05d2\u05d9\u05dc \u05d0\u05e8\u05d1\u05e2", 1, 80.0, 150.0, 620.0) == "local_label"
    assert tool.classify_caption_candidate("A narrow explanatory note beside the figure", 4, 500.0, 610.0, 620.0) == "sidebar"
    assert tool.classify_caption_candidate("A descriptive caption containing enough detail to explain the complete multi-part figure to the reader.", 2, 80.0, 540.0, 620.0) == "full_caption"
    assert tool.classify_caption_candidate("\u05d4\u05d9\u05dc\u05d3\u05d5\u05ea \u05e0\u05e8\u05d0\u05d9\u05ea \u05d1\u05e6\u05d9\u05d5\u05e8 \u05d6\u05d4 \u05d1\u05e4\u05e8\u05d8\u05d9\u05dd \u05e8\u05d1\u05d9\u05dd", 5, 500.0, 610.0, 620.0) == "sidebar"


def test_side_image_caption_recovers_tiny_trailing_body_fragment() -> None:
    image = {"id": "image-1", "x0": 77.5, "x1": 432.7, "top": 376.5, "bottom": 720.9}
    caption = {
        "top": 637.8,
        "bottom": 711.8,
        "lines": [
            {"text": "\u05d1\u05e6\u05d9\u05d5\u05e8 \u05d6\u05d4", "top": 637.8, "bottom": 647.8, "x0": 453.1, "x1": 559.5},
            {"text": "\u05d9\u05dc\u05d3\u05d9\u05dd", "top": 653.8, "bottom": 663.8, "x0": 453.1, "x1": 559.5},
            {"text": "\u05de\u05e9\u05d7\u05e7\u05d9\u05dd", "top": 669.8, "bottom": 679.8, "x0": 453.1, "x1": 559.5},
            {"text": "\u05de\u05e9\u05d7\u05e7\u05d9 \u05e8\u05d7\u05d5\u05d1", "top": 685.8, "bottom": 695.8, "x0": 453.1, "x1": 559.5},
            {"text": "\u05de\u05dc\u05d0\u05d9", "top": 701.8, "bottom": 711.8, "x0": 453.1, "x1": 559.5},
        ],
    }
    trailing = {
        "top": 714.9,
        "bottom": 722.9,
        "lines": [{"text": "\u05d7\u05d9\u05d9\u05dd.", "top": 714.9, "bottom": 722.9, "x0": 541.9, "x1": 559.1}],
    }
    candidates = tool.caption_candidate_records([caption], page_width=637.2)
    tool.attach_image_caption_matches([image], candidates, page_width=637.2)

    tool.recover_image_caption_continuations(
        [image], candidates, [trailing], [caption], page_width=637.2, page_height=807.35
    )

    assert tool.usable_image_caption(image)
    assert image["match_reason"] == "side_overlap_with_continuation"
    assert image["caption_match"]["caption_text"].endswith("\u05d7\u05d9\u05d9\u05dd.")
    assert image["caption_source_blocks"] == ["caption-1", "body-block-1"]
    assert trailing["consumed_by_image"] == "image-1"
    regions = tool.build_layout_regions([trailing], [caption], [], 637.2, image_regions=[image])
    assert all(region.get("text") != "\u05d7\u05d9\u05d9\u05dd." for region in regions)


def test_caption_candidate_records_add_type_to_debug_source_block() -> None:
    block = {
        "top": 100.0,
        "bottom": 112.0,
        "lines": [{"text": "\u05d2\u05d9\u05dc \u05d7\u05de\u05e9", "top": 100.0, "bottom": 112.0, "x0": 80.0, "x1": 145.0}],
    }

    candidates = tool.caption_candidate_records([block], page_width=620.0)

    assert candidates[0]["caption_type"] == "local_label"
    assert block["caption_type"] == "local_label"
    assert block["id"] == "caption-1"


def test_figure_group_does_not_promote_local_label_to_shared_caption() -> None:
    members = [
        {"id": "image-1", "source_index": 1, "source_box": [80.0, 100.0, 240.0, 240.0], "x0": 80.0, "x1": 240.0, "top": 100.0, "bottom": 240.0},
        {"id": "image-2", "source_index": 2, "source_box": [250.0, 100.0, 410.0, 240.0], "x0": 250.0, "x1": 410.0, "top": 100.0, "bottom": 240.0},
    ]
    local_label = {"id": "caption-1", "box": (90.0, 245.0, 180.0, 255.0), "text": "\u05d2\u05d9\u05dc \u05d0\u05e8\u05d1\u05e2", "line_count": 1, "zone": "left", "caption_type": "local_label"}

    group = tool.figure_group_record(1, members, [local_label], page_width=620.0, grouping_reason="ambiguous_nearby_images")

    assert group["caption_match"] is None
    assert group["match_reason"] == "no_group_caption_candidates"
    assert group["local_labels"][0]["caption_type"] == "local_label"


def test_figure_group_recovers_marker_body_caption_and_continuation() -> None:
    group = {
        "id": "figure-group-1",
        "kind": "figure_group",
        "x0": 80.0,
        "x1": 540.0,
        "top": 100.0,
        "bottom": 300.0,
        "caption_match": None,
        "match_confidence": 0.0,
    }
    anchor = {
        "top": 315.0,
        "bottom": 340.0,
        "lines": [{"text": "\u05ea\u05e8\u05e9\u05d9\u05dd 8.1 full caption begins", "top": 315.0, "bottom": 340.0, "x0": 80.0, "x1": 540.0}],
    }
    continuation = {
        "id": "caption-1",
        "caption_type": "local_label",
        "top": 344.0,
        "bottom": 354.0,
        "lines": [{"text": "and continues on the next extracted block.", "top": 344.0, "bottom": 354.0, "x0": 300.0, "x1": 540.0}],
    }

    tool.recover_figure_group_captions([group], [anchor], [continuation], page_width=620.0, page_height=800.0)

    assert group["caption_complete"] is True
    assert group["caption_match"]["caption_type"] == "full_caption"
    assert "full caption begins" in group["caption_match"]["caption_text"]
    assert "continues on the next" in group["caption_match"]["caption_text"]
    assert group["caption_source_blocks"] == ["body-block-1", "caption-1"]
    assert anchor["consumed_by_figure_group"] == "figure-group-1"
    assert continuation["consumed_by_figure_group"] == "figure-group-1"
    assert tool.build_layout_regions([anchor], [continuation], [], 620.0, figure_groups=[group]) == [{**group, "order": 1}]


def test_image_clustering_groups_shared_caption_triptych() -> None:
    images = [
        {"id": "image-1", "source_index": 1, "source_box": [70.0, 170.0, 230.0, 320.0], "x0": 70.0, "x1": 230.0, "top": 170.0, "bottom": 320.0, "nearest_caption_id": "caption-1", "match_confidence": 0.50},
        {"id": "image-2", "source_index": 2, "source_box": [240.0, 170.0, 390.0, 320.0], "x0": 240.0, "x1": 390.0, "top": 170.0, "bottom": 320.0, "nearest_caption_id": "caption-1", "match_confidence": 0.60},
        {"id": "image-3", "source_index": 3, "source_box": [400.0, 170.0, 560.0, 320.0], "x0": 400.0, "x1": 560.0, "top": 170.0, "bottom": 320.0, "nearest_caption_id": "caption-1", "match_confidence": 0.95},
    ]
    captions = [{"id": "caption-1", "box": (80.0, 340.0, 550.0, 410.0), "text": "׳×׳¨׳©׳™׳ 5.5 shared figure caption", "line_count": 4, "zone": "full"}]

    groups = tool.cluster_image_regions(images, captions, page_width=637.0, page_height=807.0)

    assert len(groups) == 1
    assert groups[0]["kind"] == "figure_group"
    assert groups[0]["member_image_ids"] == ["image-1", "image-2", "image-3"]
    assert groups[0]["grouping_reason"] == "shared_caption"
    assert groups[0]["nearest_caption_id"] == "caption-1"
    assert all(image["figure_group_id"] == "figure-group-1" for image in images)


def test_image_clustering_keeps_independently_captioned_images_separate() -> None:
    images = [
        {"id": "image-1", "x0": 75.0, "x1": 280.0, "top": 350.0, "bottom": 660.0, "nearest_caption_id": "caption-1", "match_confidence": 0.98},
        {"id": "image-2", "x0": 295.0, "x1": 560.0, "top": 400.0, "bottom": 660.0, "nearest_caption_id": "caption-2", "match_confidence": 0.98},
    ]

    groups = tool.cluster_image_regions(images, [], page_width=637.0, page_height=807.0)

    assert groups == []
    assert all("figure_group_id" not in image for image in images)


def test_layout_regions_replace_group_members_with_figure_group() -> None:
    images = [
        {"id": "image-1", "kind": "image", "x0": 80.0, "x1": 250.0, "top": 100.0, "bottom": 250.0},
        {"id": "image-2", "kind": "image", "x0": 260.0, "x1": 440.0, "top": 100.0, "bottom": 250.0},
    ]
    group = {
        "id": "figure-group-1",
        "kind": "figure_group",
        "member_image_ids": ["image-1", "image-2"],
        "x0": 80.0,
        "x1": 440.0,
        "top": 100.0,
        "bottom": 250.0,
        "zone": "full",
    }

    regions = tool.build_layout_regions([], [], [], 520.0, image_regions=images, figure_groups=[group])

    assert [region["kind"] for region in regions] == ["figure_group"]
    assert regions[0]["member_image_ids"] == ["image-1", "image-2"]


def test_figure_group_html_renders_members_and_shared_caption_once() -> None:
    members = [
        {"id": "image-1", "src": "data:image/jpeg;base64,one", "x0": 70.0, "x1": 220.0, "top": 100.0, "bottom": 240.0},
        {"id": "image-2", "src": "data:image/jpeg;base64,two", "x0": 230.0, "x1": 380.0, "top": 100.0, "bottom": 240.0},
        {"id": "image-3", "src": "data:image/jpeg;base64,three", "x0": 390.0, "x1": 540.0, "top": 100.0, "bottom": 240.0},
    ]
    group = {
        "id": "figure-group-1",
        "grouping_reason": "shared_caption",
        "caption_match": {"caption_id": "caption-1"},
        "match_confidence": 0.92,
        "match_reason": "group_below_overlap",
    }

    rendered = tool.figure_group_html(group, members, caption_text="Shared figure caption")

    assert 'class="embedded-figure-group group-layout-row"' in rendered
    assert rendered.count('class="figure-group-item"') == 3
    assert rendered.count("Shared figure caption") == 4  # three image alt values plus one figcaption
    assert rendered.count("<figcaption>") == 1


def test_side_caption_figure_uses_source_side_and_responsive_css() -> None:
    image = {
        "id": "image-1",
        "src": "data:image/jpeg;base64,one",
        "match_confidence": 0.99,
        "match_reason": "side_overlap_with_continuation",
        "caption_match": {"caption_side": "right"},
    }

    figure = tool.image_figure_html(image, caption_text="Side caption")
    reader = tool.build_reader_html("plain", "Reader", "source.pdf", reader_html=figure)

    assert 'class="embedded-image side-caption side-caption-right"' in figure
    assert 'grid-template-areas: "image caption"' in reader
    assert 'grid-template-areas: "image" "caption"' in reader

    above_image = {
        "id": "image-2",
        "src": "data:image/jpeg;base64,two",
        "match_confidence": 0.9,
        "match_reason": "above_overlap",
        "caption_match": {},
    }
    above_figure = tool.image_figure_html(above_image, caption_text="Caption above")
    assert 'class="embedded-image caption-above"' in above_figure
    assert ".content .embedded-image.caption-above img { order: 2; }" in reader


def test_pages_to_html_replaces_grouped_images_with_one_group_figure() -> None:
    images = [
        {"id": "image-1", "src": "data:image/jpeg;base64,one", "x0": 70.0, "x1": 220.0, "top": 100.0, "bottom": 240.0},
        {"id": "image-2", "src": "data:image/jpeg;base64,two", "x0": 230.0, "x1": 380.0, "top": 100.0, "bottom": 240.0},
        {"id": "image-3", "src": "data:image/jpeg;base64,three", "x0": 390.0, "x1": 540.0, "top": 100.0, "bottom": 240.0},
    ]
    group = {
        "id": "figure-group-1",
        "kind": "figure_group",
        "grouping_reason": "shared_caption",
        "member_image_ids": ["image-1", "image-2", "image-3"],
        "x0": 70.0,
        "x1": 540.0,
        "top": 100.0,
        "bottom": 240.0,
        "caption_match": {"caption_id": "caption-1", "caption_text": "Shared caption", "caption_type": "full_caption"},
        "match_confidence": 0.92,
        "match_reason": "group_below_overlap",
        "local_labels": [{"caption_id": "caption-2", "text": "Local panel label", "box": [80.0, 220.0, 180.0, 235.0]}],
    }
    caption = {"id": "caption-1", "class": "caption", "tag": "aside", "html": "Shared caption", "top": 250.0, "bottom": 275.0, "lines": []}
    local_caption = {"id": "caption-2", "class": "caption", "tag": "aside", "html": "Local panel label", "top": 220.0, "bottom": 235.0, "lines": []}
    page = {
        "number": 1,
        "width": 620.0,
        "lines": [],
        "blocks": [],
        "secondary_blocks": [caption, local_caption],
        "image_regions": images,
        "figure_groups": [group],
        "tables": [],
    }

    rendered = tool.pages_to_html([page])

    assert rendered.count('class="embedded-figure-group group-layout-row"') == 1
    assert 'class="embedded-image' not in rendered
    assert rendered.count("<figcaption>") == 1
    assert '<aside class="caption">Shared caption</aside>' not in rendered
    assert '<div class="figure-group-labels">' in rendered
    assert "Local panel label" in rendered
    assert '<aside class="caption">Local panel label</aside>' not in rendered


def test_pages_to_html_without_embedded_images_keeps_caption_text() -> None:
    image = {
        "id": "image-1",
        "x0": 80.0,
        "x1": 420.0,
        "top": 100.0,
        "bottom": 260.0,
        "caption_match": {"caption_id": "caption-1", "caption_text": "Visible caption", "caption_type": "full_caption"},
        "match_confidence": 0.92,
        "match_reason": "below_overlap",
    }
    caption = {"id": "caption-1", "class": "caption", "tag": "aside", "html": "Visible caption", "top": 270.0, "bottom": 290.0, "lines": []}
    page = {
        "number": 1,
        "width": 620.0,
        "lines": [],
        "blocks": [],
        "secondary_blocks": [caption],
        "image_regions": [image],
        "figure_groups": [],
        "tables": [],
    }

    rendered = tool.pages_to_html([page])

    assert 'class="embedded-image' not in rendered
    assert '<aside class="caption">Visible caption</aside>' in rendered


if __name__ == "__main__":
    test_page_range_parser()
    test_safe_slug_preserves_hebrew_names()
    test_conversion_report_html()
    test_reader_html_includes_accessible_image_lightbox()
    test_image_embedding_options_validate_ranges()
    test_byte_size_formatter_uses_readable_units()
    test_secondary_text_separation()
    test_ltr_reference_fragment_in_body_band_is_not_caption()
    test_left_side_caption_lines_are_kept()
    test_same_height_side_text_is_split_from_body_text()
    test_medium_horizontal_gap_splits_mixed_layout_line()
    test_crop_bounds_normalize_layout_coordinates()
    test_crop_alignment_keeps_only_visual_matches()
    test_layout_debug_helpers_report_line_matches()
    test_hebrew_similarity_matches_mixed_citation_line()
    test_ltr_citation_is_rendered_as_bidi_isolate()
    test_split_ltr_citation_is_repaired_before_rendering()
    test_split_ltr_citation_is_repaired_across_paragraph_lines()
    test_parenthetical_reference_list_is_repaired_across_lines()
    test_question_callout_uses_visual_column_order()
    test_layout_regions_include_kind_zone_and_order()
    test_layout_item_sort_uses_rtl_order_for_same_band()
    test_noise_lines_are_split_from_content()
    test_secondary_text_blocks_split_by_horizontal_region()
    test_secondary_text_blocks_split_adjacent_image_captions()
    test_secondary_text_reading_order_does_not_interrupt_nearby_body_blocks()
    test_secondary_text_waits_for_continuing_body_sentence()
    test_adjacent_body_blocks_merge_in_same_region()
    test_rtl_first_line_indent_creates_protected_paragraph_boundaries()
    test_adjacent_body_blocks_do_not_merge_across_columns()
    test_body_merge_skips_question_blocks()
    test_body_merge_skips_heading_like_blocks()
    test_content_image_records_ignore_full_page_backgrounds()
    test_image_caption_match_detects_side_caption()
    test_geometry_promotes_narrow_irregular_captions_but_not_distant_notes()
    test_image_caption_match_uses_close_edge_alignment_for_narrow_above_caption()
    test_image_caption_matching_marks_ambiguous_candidate_sets()
    test_caption_candidate_types_distinguish_figure_labels_and_sidebars()
    test_side_image_caption_recovers_tiny_trailing_body_fragment()
    test_caption_candidate_records_add_type_to_debug_source_block()
    test_figure_group_does_not_promote_local_label_to_shared_caption()
    test_figure_group_recovers_marker_body_caption_and_continuation()
    test_image_clustering_groups_shared_caption_triptych()
    test_image_clustering_keeps_independently_captioned_images_separate()
    test_layout_regions_replace_group_members_with_figure_group()
    test_figure_group_html_renders_members_and_shared_caption_once()
    test_side_caption_figure_uses_source_side_and_responsive_css()
    test_pages_to_html_replaces_grouped_images_with_one_group_figure()
    test_pages_to_html_without_embedded_images_keeps_caption_text()
    print("smoke tests passed")
