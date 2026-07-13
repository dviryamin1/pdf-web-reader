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


def test_conversion_report_html() -> None:
    report = tool.build_conversion_report(
        [{"number": 1, "tables": [{"reconstructed": True}]}],
        total_pages=10,
        page_numbers={1},
        crop_settings=None,
        structured=True,
    )
    report_html = tool.conversion_report_to_html(report)
    assert "Conversion report" in report_html
    assert "1 of 10" in report_html
    assert "Tables reconstructed" in report_html


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


if __name__ == "__main__":
    test_page_range_parser()
    test_conversion_report_html()
    test_secondary_text_separation()
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
    print("smoke tests passed")
