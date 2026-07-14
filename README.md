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

## Development Checks

```powershell
python -m py_compile src\pdf_reader_tool.py
python tests\smoke_test.py
python tests\book_layout_regression_test.py
```

The book layout regression test uses `input\פסיכולוגיה התפתחותית כרך א.pdf` when it is available. Because `input\` is intentionally ignored by Git, another local text-layer PDF can be selected with `PDF_READER_TEST_PDF`.

## Limitations

The input PDF must contain a real text layer. Scanned-only PDFs need OCR first.

Complex page layouts are still an active area. The structured extractor now separates likely captions and side text with layout heuristics, repairs some caption/body ordering issues, preserves basic multi-column question panels, and performs conservative same-region body-block merges. Full column-aware reading order is not implemented yet.

Image extraction depends on Poppler / `pdftoppm`, because images are cropped from rendered PDF pages. Image-caption matching is still heuristic; gallery pages and complex screenshot/callout pages should be reviewed with `--debug-layout`.
