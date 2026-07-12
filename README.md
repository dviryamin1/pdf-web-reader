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

Complex page layouts are still an active area: multi-column regions, side captions, and image-adjacent text boxes may need dedicated layout segmentation to avoid mixing secondary text into the main reading flow.
