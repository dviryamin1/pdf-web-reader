import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "src" / "pdf_reader_tool.py"
DEFAULT_BOOK_PATH = ROOT / "input" / "פסיכולוגיה התפתחותית כרך א.pdf"
REGRESSION_PAGES = {31, 35, 38, 39, 41, 44, 45, 46, 49, 50, 53, 60, 65, 66, 68, 100}

spec = importlib.util.spec_from_file_location("pdf_reader_tool", TOOL_PATH)
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tool)


def resolve_book_path() -> Path | None:
    configured = os.environ.get("PDF_READER_TEST_PDF")
    candidate = Path(configured).expanduser() if configured else DEFAULT_BOOK_PATH
    return candidate.resolve() if candidate.is_file() else None


def only_image(page: dict) -> dict:
    images = page.get("image_regions", [])
    assert len(images) == 1, f"page {page['number']}: expected one content image, found {len(images)}"
    return images[0]


def assert_confirmed_caption(image: dict, expected_reason: str) -> dict:
    match = image.get("caption_match") or {}
    assert match.get("caption_type") == "full_caption"
    assert match.get("geometry_confirmed") is True
    assert float(image.get("match_confidence") or 0.0) >= tool.IMAGE_CAPTION_CONFIDENCE_THRESHOLD
    assert expected_reason in str(image.get("match_reason") or "")
    assert match.get("caption_text")
    return match


def rendered_figure_class(image: dict) -> str:
    renderable = dict(image)
    renderable["src"] = "data:image/jpeg;base64,test"
    caption_text = str((image.get("caption_match") or {}).get("caption_text") or "")
    return tool.image_figure_html(renderable, caption_text=caption_text)


def run_book_layout_regressions(book_path: Path) -> None:
    pages, _, converted_count, _ = tool.extract_pdf_structured(
        book_path,
        page_numbers=REGRESSION_PAGES,
        embed_images=False,
    )
    assert converted_count == len(REGRESSION_PAGES)
    pages_by_number = {int(page["number"]): page for page in pages}
    assert set(pages_by_number) == REGRESSION_PAGES

    page31 = only_image(pages_by_number[31])
    match31 = assert_confirmed_caption(page31, "side_overlap")
    assert match31.get("caption_side") == "left"
    assert "side-caption-left" in rendered_figure_class(page31)

    page35 = pages_by_number[35]
    glossary_terms35 = page35.get("glossary_terms", [])
    assert len(glossary_terms35) == 1
    glossary35 = glossary_terms35[0]
    assert glossary35.get("text") == "\u05d2\u05de\u05d9\u05e9\u05d5\u05ea (plasticity)"
    assert glossary35.get("source") == "visual_margin"
    assert glossary35.get("match_reason") == "bold_margin_semantic_duplicate"
    assert glossary35.get("duplicate_removed") == "\u05d2\u05de\u05d9\u05e9\u05d5\u05ea (plasticity)"
    assert float(glossary35.get("block_offset_em") or 0.0) >= 5.0
    assert all("plasticity" not in tool.block_plain_text(block) for block in page35.get("blocks", []))
    page35_html = tool.pages_to_html([page35])
    assert page35_html.count('class="glossary-term"') == 1
    assert page35_html.count("plasticity") == 1
    assert 'class="body-with-glossary glossary-left"' in page35_html
    assert 'style="--glossary-offset:' in page35_html

    page38 = only_image(pages_by_number[38])
    match38 = assert_confirmed_caption(page38, "side_overlap")
    assert match38.get("caption_side") == "right"
    assert "with_continuation" in str(page38.get("match_reason") or "")
    assert len(page38.get("caption_source_blocks", [])) >= 2
    assert "side-caption-right" in rendered_figure_class(page38)

    page39 = pages_by_number[39]
    locke_blocks = [
        tool.block_plain_text(block)
        for block in page39.get("blocks", [])
        if "John Locke" in tool.block_plain_text(block)
    ]
    assert len(locke_blocks) == 1
    assert locke_blocks[0].startswith("ג'ון לוק כתביו של הפילוסוף הבריטי ג'ון לוק (John Locke, 1632-1704) בישרו השקפה")
    assert not locke_blocks[0].startswith("בישרו השקפה")

    page41 = only_image(pages_by_number[41])
    match41 = assert_confirmed_caption(page41, "side_overlap")
    assert match41.get("caption_side") == "right"
    assert "side-caption-right" in rendered_figure_class(page41)

    page44 = only_image(pages_by_number[44])
    match44 = assert_confirmed_caption(page44, "above_overlap")
    assert "ילדה קזחית" in str(match44.get("caption_text") or "")
    assert match44.get("source_caption_type") == "sidebar"
    assert match44.get("edge_aligned") is True
    assert "caption-above" in rendered_figure_class(page44)

    page50 = only_image(pages_by_number[50])
    match50 = assert_confirmed_caption(page50, "side_overlap")
    assert match50.get("caption_side") == "left"
    assert "side-caption-left" in rendered_figure_class(page50)

    page49 = pages_by_number[49]
    glossary_terms49 = page49.get("glossary_terms", [])
    assert len(glossary_terms49) == 1
    glossary49 = glossary_terms49[0]
    assert glossary49.get("match_reason") == "turquoise_semantic_duplicate"
    assert float(glossary49.get("match_confidence") or 0.0) >= 0.9
    assert glossary49.get("side") == "left"
    assert glossary49.get("linked_body_block_id")
    assert len(glossary49.get("lines", [])) == 4
    assert "cognitive-developmental" in str(glossary49.get("text") or "")
    page49_html = tool.pages_to_html([page49])
    assert page49_html.count('class="body-with-glossary glossary-left"') == 1
    assert page49_html.count('class="glossary-term"') == 1
    assert '<aside class="caption">cognitive-developmental)' not in page49_html

    page46 = pages_by_number[46]
    assert len(page46.get("glossary_terms", [])) == 1
    assert "behaviorism" in str(page46["glossary_terms"][0].get("text") or "")

    page60 = pages_by_number[60]
    assert len(page60.get("glossary_terms", [])) == 2
    assert all(term.get("linked_body_block_id") for term in page60["glossary_terms"])
    assert tool.pages_to_html([page60]).count('class="glossary-term"') == 2

    expected_tables = {
        45: (9, 3),
        50: (5, 3),
        68: (11, 4),
        100: (7, 5),
    }
    for page_number, (expected_rows, expected_cols) in expected_tables.items():
        tables = pages_by_number[page_number].get("tables", [])
        assert len(tables) == 1, f"page {page_number}: expected one reconstructed table"
        table = tables[0]
        assert table.get("reconstructed") is True
        assert table.get("rows") == expected_rows
        assert table.get("cols") == expected_cols
        rendered_table_page = tool.pages_to_html([pages_by_number[page_number]])
        assert rendered_table_page.count('class="detected-table estimated"') == 1

    continuation_tables = {65: (6, 4), 66: (2, 4)}
    for page_number, (expected_rows, expected_cols) in continuation_tables.items():
        tables = pages_by_number[page_number].get("tables", [])
        assert len(tables) == 1
        table = tables[0]
        assert table.get("continuation") is True
        assert (table.get("rows"), table.get("cols")) == (expected_rows, expected_cols)
        assert table.get("data", [])[0][0] == "התיאוריה"

    page53 = pages_by_number[53]
    body_blocks53 = page53.get("blocks", [])
    assert len(body_blocks53) == 5
    assert [block.get("paragraph_start_reason") for block in body_blocks53] == [
        "page_start",
        "rtl_first_line_indent",
        "rtl_first_line_indent",
        "rtl_first_line_indent",
        "rtl_first_line_indent",
    ]
    assert tool.pages_to_html([page53]).count("<p>") == 5


if __name__ == "__main__":
    book_path = resolve_book_path()
    if book_path is None:
        print("book layout regression test skipped: set PDF_READER_TEST_PDF to a local text-layer PDF")
    else:
        run_book_layout_regressions(book_path)
        print(f"book layout regression tests passed: {book_path.name} ({len(REGRESSION_PAGES)} pages)")
