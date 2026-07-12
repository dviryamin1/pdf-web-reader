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


if __name__ == "__main__":
    test_page_range_parser()
    test_conversion_report_html()
    print("smoke tests passed")
