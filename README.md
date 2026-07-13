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

The debug report includes visual lines, aligned logical lines, tables, text classification, `noise_lines`, and layout `regions` for the current reading-order pass.

The reader uses these regions for a basic reading-order pass: items in the same vertical band are ordered right-to-left, while clearly separated items keep their top-to-bottom order.

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
```

## Limitations

The input PDF must contain a real text layer. Scanned-only PDFs need OCR first.

Complex page layouts are still an active area. The structured extractor now separates likely captions and side text with layout heuristics and repairs some caption/body ordering issues, but full column-aware reading order is not implemented yet.

Multi-column callout boxes, such as "ask yourself" question panels, may still be flattened in the wrong order. A later layout pass should detect these boxed regions and preserve their internal columns before merging them into the reader flow.
