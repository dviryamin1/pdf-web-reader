import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "src" / "pdf_reader_tool.py"
DEFAULT_BOOK_PATH = ROOT / "input" / "פסיכולוגיה התפתחותית כרך א.pdf"
REGRESSION_PAGES = {31, 38, 41, 44, 50, 53}

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

    page38 = only_image(pages_by_number[38])
    match38 = assert_confirmed_caption(page38, "side_overlap")
    assert match38.get("caption_side") == "right"
    assert "with_continuation" in str(page38.get("match_reason") or "")
    assert len(page38.get("caption_source_blocks", [])) >= 2
    assert "side-caption-right" in rendered_figure_class(page38)

    page41 = only_image(pages_by_number[41])
    match41 = assert_confirmed_caption(page41, "side_overlap")
    assert match41.get("caption_side") == "right"
    assert "side-caption-right" in rendered_figure_class(page41)

    page44 = only_image(pages_by_number[44])
    match44 = assert_confirmed_caption(page44, "above_overlap")
    assert match44.get("caption_id") == "caption-2"
    assert match44.get("source_caption_type") == "sidebar"
    assert match44.get("edge_aligned") is True
    assert "caption-above" in rendered_figure_class(page44)

    page50 = only_image(pages_by_number[50])
    match50 = assert_confirmed_caption(page50, "side_overlap")
    assert match50.get("caption_side") == "left"
    assert "side-caption-left" in rendered_figure_class(page50)

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
