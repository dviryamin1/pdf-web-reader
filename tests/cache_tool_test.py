from pathlib import Path
import io
import sqlite3
import sys
import tempfile

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pdf_cache_tool as cache_tool  # noqa: E402
import pdf_reader_tool as reader_tool  # noqa: E402


def create_text_pdf(path: Path, page_count: int = 2, label: str = "Cache") -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for page_number in range(1, page_count + 1):
        page = writer.add_blank_page(width=300, height=400)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_reference}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(
            (
                f"BT /F1 16 Tf 40 350 Td ({label} page {page_number}) Tj ET\n"
                "40 250 180 45 re S\n"
            ).encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as output:
        writer.write(output)


def test_page_range_parser() -> None:
    assert cache_tool.parse_page_ranges(None) is None
    assert cache_tool.parse_page_ranges("1-3,5") == {1, 2, 3, 5}
    assert cache_tool.requested_pages(5, {5, 2}) == [2, 5]
    try:
        cache_tool.requested_pages(3, {4})
        raise AssertionError("out-of-range page was accepted")
    except ValueError:
        pass


def test_structured_cache_build_resume_and_read() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        pdf_path = temp / "sample.pdf"
        cache_path = temp / "sample.pdfcache"
        text_path = temp / "sample.txt"
        create_text_pdf(pdf_path)

        first = cache_tool.build_cache(pdf_path, cache_path, page_numbers={1})
        assert first.total_pages == 2
        assert first.extracted_pages == 1
        assert first.skipped_pages == 0
        assert first.complete is False

        with cache_tool.PdfExtractionCache(cache_path) as cache:
            manifest = cache.manifest
            assert manifest["schema"] == cache_tool.CACHE_SCHEMA
            assert manifest["status"]["cached_pages"] == [1]
            payload = cache.get_page(1)
            assert payload["page_number"] == 1
            assert payload["page"]["width"] == 300.0
            assert "Cache page 1" in payload["logical_text"]["text"]
            assert [word["text"] for word in payload["layout"]["words"]] == [
                "Cache",
                "page",
                "1",
            ]
            assert payload["counts"]["chars"] > 0
            assert payload["counts"]["rects"] == 1
            assert cache.validate() == []

        resumed = cache_tool.build_cache(pdf_path, cache_path)
        assert resumed.extracted_pages == 1
        assert resumed.skipped_pages == 1
        assert resumed.complete is True

        pages = cache_tool.load_cached_pages(cache_path)
        assert [page["page_number"] for page in pages] == [1, 2]
        summary = cache_tool.inspect_cache(cache_path, page_number=2)
        assert summary["status"]["complete"] is True
        assert summary["page"]["counts"]["words"] == 3

        exported = cache_tool.export_logical_text(cache_path, text_path)
        exported_text = exported.read_text(encoding="utf-8")
        assert "--- page 1 ---" in exported_text
        assert "Cache page 2" in exported_text


def test_cache_rejects_another_pdf_and_force_replaces_it() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        first_pdf = temp / "first.pdf"
        second_pdf = temp / "second.pdf"
        cache_path = temp / "book.pdfcache"
        create_text_pdf(first_pdf, page_count=1, label="First")
        create_text_pdf(second_pdf, page_count=1, label="Second")
        cache_tool.build_cache(first_pdf, cache_path)

        try:
            cache_tool.build_cache(second_pdf, cache_path)
            raise AssertionError("cache accepted a different source PDF")
        except cache_tool.CacheError:
            pass

        replaced = cache_tool.build_cache(second_pdf, cache_path, force=True)
        assert replaced.extracted_pages == 1
        assert "Second" in cache_tool.load_cached_pages(cache_path)[0]["logical_text"]["text"]


def test_cache_validation_detects_payload_checksum_damage() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        pdf_path = temp / "sample.pdf"
        cache_path = temp / "sample.pdfcache"
        create_text_pdf(pdf_path, page_count=1)
        cache_tool.build_cache(pdf_path, cache_path)

        connection = sqlite3.connect(str(cache_path))
        connection.execute("UPDATE pages SET payload_sha256 = ? WHERE page_number = 1", ("0" * 64,))
        connection.commit()
        connection.close()

        with cache_tool.PdfExtractionCache(cache_path) as cache:
            errors = cache.validate()
        assert any("checksum" in error.lower() for error in errors)


def test_render_selection_only_uses_raw_image_count() -> None:
    image_page = {"counts": {"images": 2}}
    text_page = {"counts": {"images": 0}}
    assert cache_tool.should_render_page("none", image_page) is False
    assert cache_tool.should_render_page("images", image_page) is True
    assert cache_tool.should_render_page("images", text_page) is False
    assert cache_tool.should_render_page("all", text_page) is True


def test_poppler_resolver_prefers_real_bundled_executable() -> None:
    # The runtime can expose a .CMD wrapper through PATH. The tool must resolve an
    # executable that subprocess can launch directly when the bundled binary exists.
    if cache_tool.BUNDLED_PDFTOPPM.is_file() and not cache_tool.os.environ.get("PDFTOPPM_PATH"):
        assert cache_tool.resolve_pdftoppm() == cache_tool.BUNDLED_PDFTOPPM


def test_reader_cached_and_live_extraction_are_equivalent() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        pdf_path = temp / "sample.pdf"
        cache_path = temp / "sample.pdfcache"
        create_text_pdf(pdf_path)

        live = reader_tool.extract_pdf_structured(
            pdf_path,
            page_numbers={1, 2},
            embed_images=False,
        )
        cached = reader_tool.extract_pdf_structured(
            pdf_path,
            page_numbers={1, 2},
            cache_path=cache_path,
            embed_images=False,
        )
        assert cached == live

        live_debug = reader_tool.build_layout_debug_report(pdf_path, page_numbers={1, 2})
        cached_debug = reader_tool.build_layout_debug_report(
            pdf_path,
            page_numbers={1, 2},
            cache_path=cache_path,
        )
        assert cached_debug.pop("cache_used") is True
        assert cached_debug.pop("extraction_cache") == str(cache_path.resolve())
        assert live_debug.pop("cache_used") is False
        assert live_debug.pop("extraction_cache") is None
        assert cached_debug == live_debug


def test_reader_report_identifies_cache_use() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        pdf_path = temp / "sample.pdf"
        cache_path = temp / "sample.pdfcache"
        output_dir = temp / "readers"
        create_text_pdf(pdf_path, page_count=1)

        output = reader_tool.create_reader(
            pdf_path,
            output_dir,
            cache_path=cache_path,
            embed_images=False,
        )
        html = output.read_text(encoding="utf-8-sig")
        assert "<dt>Layout cache</dt><dd>used</dd>" in html
        assert f"<dt>Cache file</dt><dd>{cache_path.name}</dd>" in html


def test_cached_page_render_is_used_without_poppler() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        temp = Path(tmpdir)
        cache_path = temp / "sample.pdfcache"
        image_buffer = io.BytesIO()
        Image.new("RGB", (200, 120), (20, 120, 160)).save(image_buffer, format="PNG")

        with cache_tool.PdfExtractionCache(cache_path, create=True) as cache:
            cache.put_rendered_page(1, image_buffer.getvalue(), reader_tool.IMAGE_RENDER_DPI)
            cache.commit()
            pages = [
                {
                    "number": 1,
                    "width": 200.0,
                    "height": 120.0,
                    "source_width": 200.0,
                    "source_height": 120.0,
                    "image_regions": [
                        {"source_box": [20.0, 10.0, 180.0, 110.0]}
                    ],
                }
            ]
            reader_tool.embed_page_images(
                temp / "source-does-not-need-to-exist.pdf",
                pages,
                page_cache=cache,
            )

        assert pages[0]["image_render_source"] == "cache"
        assert pages[0]["image_regions"][0]["src"].startswith("data:image/jpeg;base64,")


def test_web_upload_cache_path_is_content_addressed() -> None:
    first = reader_tool.upload_cache_path("book.pdf", b"first")
    repeated = reader_tool.upload_cache_path("book.pdf", b"first")
    changed = reader_tool.upload_cache_path("book.pdf", b"changed")
    assert first == repeated
    assert first != changed
    assert first.parent == cache_tool.DEFAULT_CACHE_DIR


if __name__ == "__main__":
    test_page_range_parser()
    test_structured_cache_build_resume_and_read()
    test_cache_rejects_another_pdf_and_force_replaces_it()
    test_cache_validation_detects_payload_checksum_damage()
    test_render_selection_only_uses_raw_image_count()
    test_poppler_resolver_prefers_real_bundled_executable()
    test_reader_cached_and_live_extraction_are_equivalent()
    test_reader_report_identifies_cache_use()
    test_cached_page_render_is_used_without_poppler()
    test_web_upload_cache_path_is_content_addressed()
    print("cache tool tests passed")
