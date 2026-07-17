# Hebrew PDF Reader

A local tool for converting text-layer Hebrew PDFs into standalone, mobile-friendly HTML readers.

The tool is designed for academic books and course materials where plain PDF reading is uncomfortable on mobile. It preserves paragraph flow, basic styling, RTL text, mixed Hebrew/English punctuation, reconstructed tables where possible, page crop bounds, and optional page-range conversion for fast testing.

## Features

- Converts a text-layer PDF into a self-contained HTML reader.
- Keeps Hebrew RTL reading comfortable on desktop and mobile.
- Joins wrapped PDF lines into readable paragraphs.
- Preserves basic font size, bold, and italic styling.
- Reconstructs some tables and shows mobile-friendly table cards/carousels.
- Supports separate crop bounds for right/left pages.
- Shows page previews for visual crop adjustment in the local web UI.
- Supports partial conversion with page ranges such as `1-5,8,12`.
- Embeds a conversion report in each generated reader.
- Separates likely image captions and side text from the main paragraph flow, with basic reading-order repair so captions do not interrupt nearby body text.
- Embeds detected content images as cropped inline figures when structured extraction succeeds, using tentative caption matching.
- Renders clustered galleries and multi-part figures as responsive grouped figures with one shared caption and retained local labels.
- Opens embedded images in an accessible full-screen lightbox by tap, click, Enter, or Space; Escape, the close button, or the backdrop closes it.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For page preview rendering in the web UI, install Poppler and make `pdftoppm` available on `PATH`, or set:

```powershell
$env:PDFTOPPM_PATH = "C:\path\to\pdftoppm.exe"
```

## CLI Usage

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf"
```

Convert only selected pages:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --pages "1-5,8,12"
```

For long books, `--pages` limits both layout extraction and logical text extraction to the selected pages, so sampled checks stay fast.

Create or reuse a structured extraction cache while converting:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --cache
```

The first run extracts the requested pages into `extraction-cache\<book>.pdfcache`. Later runs load those pages from the cache and add only pages that are missing. Use `--cache "C:\path\to\book.pdfcache"` to choose a specific cache file. `--cache` also works with `--pages` and `--debug-layout`.

Reduce standalone HTML size by lowering embedded-image quality and dimensions:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --image-quality 65 --image-max-width 700
```

Skip image rendering and embedding entirely:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --no-images
```

Valid JPEG quality values are 35-95, and valid maximum widths are 240-2000 pixels. The CLI prints image-rendering progress by page. The conversion report records these settings, embedded image data size, and estimated final HTML size. The local web UI exposes the same options.

Use crop bounds:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --right-crop "5,8,5,2" --left-crop "5,2,5,8"
```

Crop order is:

```text
top,right,bottom,left
```

Write a layout debug report for selected pages:

```powershell
python src\pdf_reader_tool.py "C:\path\to\book.pdf" --pages "3-4" --debug-layout
```

The debug report includes visual lines, aligned logical lines, tables, text classification, `noise_lines`, conservative body-block merge metadata, `image_regions`, `figure_groups`, and layout `regions` for the current reading-order pass.

Word ordering combines Unicode token direction with positioned PDF characters. The logical word extractor is kept when character geometry confirms it; character-level reconstruction is used only when the glyph positions show that a word is reversed. Logical text lines are matched to visual lines with an order-preserving pass, followed by high-confidence geometry recovery for out-of-flow captions and margin notes. Debug line records expose `base_direction`, `word_order_source`, `word_order_confidence`, `alignment_reason`, `alignment_score`, `logical_index`, and per-token ordering metadata.

Short bold or turquoise terminology notes in an outer margin are linked to the matching emphasized occurrence in the body and rendered beside that paragraph. Margin position alone is not treated as evidence: the note must have term-like styling and a strong semantic match in the main body band. If the PDF logical layer appended the margin label to a body line, that duplicate is removed after the match is confirmed. Desktop readers preserve the approximate vertical alignment; narrow screens place the term above its linked paragraph.

The reader uses these regions for a basic reading-order pass: items in the same vertical band are ordered right-to-left, while clearly separated items keep their top-to-bottom order. Adjacent body blocks may be merged only when they are in the same horizontal region and do not look like headings, captions, tables, or question boxes.

Image regions include tentative caption matching fields such as `nearest_caption_id`, `match_confidence`, `match_reason`, `group_candidate`, and `ambiguous_caption_candidates`. The generated reader embeds cropped image figures from these regions, while low-confidence captions remain separate text.

Caption candidates also include `caption_type`: `full_caption`, `figure_title`, `local_label`, `sidebar`, or `unknown`. Sidebars are excluded from image captions, local labels stay inside grouped figures, and grouped HTML promotes only `full_caption` or `figure_title` matches to `<figcaption>`; uncertain text remains separate.

When a grouped figure's full caption was classified as ordinary body text, the tool can recover a nearby block beginning with `תרשים`, `איור`, or `טבלה` and merge tightly adjacent continuation fragments. Debug output records `caption_source_blocks`, `caption_complete`, and `caption_merge_reason`; confirmed source fragments are consumed from ordinary HTML flow so the caption appears only once.

Nearby images with shared-caption, dense-gallery, or ambiguous-label evidence are clustered into `figure_group` regions. Group records include their member image IDs, union bounds, grouping reason, local labels, and a group-level caption match. The reader renders these groups as responsive grids or vertical stacks, while confidently and independently captioned images remain separate.

## Local Web UI

```powershell
python src\pdf_reader_tool.py --serve
```

Then open:

```text
http://127.0.0.1:8765/
```

After a PDF is selected, the preview response also reads its built-in outline/bookmarks. If an outline exists, the upload form shows a nested table-of-contents selector. Selecting one or more chapters or sections converts their PDF destinations into a compact physical page range; that range controls which pages are rendered into the standalone reader. PDFs without an outline keep the manual page-range workflow. Generated readers include a print button and print only the pages included in that reader, with controls and reports removed from the print layout.

## Reusable Structured Extraction Cache

`pdf_cache_tool.py` can scan a book before conversion and save its reusable raw layout data. The main reader consumes the same cache format through `--cache`, and the local upload UI enables cache reuse by default. The cache keeps the logical text, positioned words and characters, font/color attributes, image metadata, vector geometry, annotations, and initial table candidates for every cached page.

Build a cache for an entire book:

```powershell
python src\pdf_cache_tool.py build "input\book.pdf"
```

The default output is `extraction-cache\book.pdfcache`. Cache files are ignored by Git because they are generated from local PDFs and may be large.

Cache files contain extracted book text and may contain rendered pages. Keep `extraction-cache\` private and delete caches you no longer need.

Interrupted builds are resumable. Running the same command again verifies the PDF fingerprint, skips pages already present, and continues with missing pages. A cache cannot accidentally be reused for a different PDF.

For a small test range or a custom output path:

```powershell
python src\pdf_cache_tool.py build "input\book.pdf" --pages "1-5,8,12" -o "work\book.pdfcache"
```

Optionally store rendered PNG pages inside the same cache. `images` renders pages containing PDF image objects; `all` renders every selected page. This can substantially increase cache size.

```powershell
python src\pdf_cache_tool.py build "input\book.pdf" --render-pages images --render-dpi 120
```

Inspect and validate a cache:

```powershell
python src\pdf_cache_tool.py inspect "extraction-cache\book.pdfcache" --page 31
python src\pdf_cache_tool.py validate "extraction-cache\book.pdfcache" --pdf "input\book.pdf"
```

Export only the cached logical text when needed:

```powershell
python src\pdf_cache_tool.py export-text "extraction-cache\book.pdfcache" -o "work\book.txt"
```

The integration API is intentionally small: `PdfExtractionCache.get_page()`, `iter_pages()`, `get_rendered_page()`, and `load_cached_pages()`. Other reader components can consume those records without changing the cache file format.

The reader integration currently rebuilds paragraphs, tables, captions, glossary links, and reading order from cached raw records on every conversion. This allows improvements to those algorithms without rescanning the PDF. When a cache contains a rendered page at 120 DPI or higher, image crops also reuse that render; otherwise the reader renders the image page from the source PDF as before.

## Development Checks

```powershell
python -m py_compile src\pdf_reader_tool.py
python -m py_compile src\pdf_cache_tool.py
python tests\cache_tool_test.py
python tests\smoke_test.py
python tests\book_layout_regression_test.py
```

The book layout regression test uses `input\פסיכולוגיה התפתחותית כרך א.pdf` when it is available. Because `input\` is intentionally ignored by Git, another local text-layer PDF can be selected with `PDF_READER_TEST_PDF`.

## Limitations

The input PDF must contain a real text layer. Scanned-only PDFs need OCR first.

Complex page layouts are still an active area. The structured extractor now separates likely captions and side text with layout heuristics, repairs some caption/body ordering issues, preserves basic multi-column question panels, and performs conservative same-region body-block merges. Full column-aware reading order is not implemented yet.

Image extraction depends on Poppler / `pdftoppm`, because images are cropped from rendered PDF pages. Image-caption matching is still heuristic; gallery pages and complex screenshot/callout pages should be reviewed with `--debug-layout`.
