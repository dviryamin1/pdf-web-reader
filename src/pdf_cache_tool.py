from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import pdfplumber
from pypdf import PdfReader


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DEFAULT_CACHE_DIR = PROJECT_DIR / "extraction-cache"
BUNDLED_PDFTOPPM = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "native"
    / "poppler"
    / "Library"
    / "bin"
    / "pdftoppm.exe"
)


def resolve_pdftoppm() -> Path:
    configured = os.environ.get("PDFTOPPM_PATH")
    if configured:
        return Path(configured)
    # The bundled runtime also puts a .CMD shim on PATH. subprocess cannot execute
    # that shim directly without a shell, so prefer the real executable on Windows.
    if BUNDLED_PDFTOPPM.is_file():
        return BUNDLED_PDFTOPPM
    return Path(shutil.which("pdftoppm") or BUNDLED_PDFTOPPM)


PDFTOPPM = resolve_pdftoppm()

CACHE_SCHEMA = "pdf-web-reader-structured-cache"
CACHE_SCHEMA_VERSION = 1
PAYLOAD_COMPRESSION = "zlib-json-v1"
DEFAULT_RENDER_DPI = 120
RENDER_MODES = {"none", "images", "all"}
WORD_EXTRA_ATTRIBUTES = ["fontname", "size", "non_stroking_color"]

ProgressCallback = Callable[[str], None]


class CacheError(RuntimeError):
    """Raised when a cache is invalid or belongs to a different PDF."""


@dataclass(frozen=True)
class BuildResult:
    cache_path: Path
    total_pages: int
    requested_pages: int
    extracted_pages: int
    skipped_pages: int
    rendered_pages: int
    complete: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_slug(value: str) -> str:
    stem = Path(value).stem.strip() or "pdf"
    slug = "-".join(part for part in stem.replace("_", " ").split() if part)
    return "".join(character for character in slug if character.isalnum() or character in "-.") or "pdf"


def default_cache_path(pdf_path: Path) -> Path:
    return DEFAULT_CACHE_DIR / f"{safe_slug(pdf_path.name)}.pdfcache"


def parse_page_ranges(value: str | None) -> set[int] | None:
    if not value or not value.strip():
        return None
    pages: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = [piece.strip() for piece in part.split("-", 1)]
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError("Page range must use numbers, for example: 1-5,8,12")
            start = int(start_text)
            end = int(end_text)
            if start < 1 or end < start:
                raise ValueError("Page ranges must contain positive pages in ascending order")
            pages.update(range(start, end + 1))
        else:
            if not part.isdigit() or int(part) < 1:
                raise ValueError("Page range must use positive numbers, for example: 1-5,8,12")
            pages.add(int(part))
    return pages or None


def requested_pages(total_pages: int, page_numbers: set[int] | None) -> list[int]:
    if page_numbers is None:
        return list(range(1, total_pages + 1))
    invalid = sorted(page for page in page_numbers if page > total_pages)
    if invalid:
        raise ValueError(f"Requested page {invalid[0]} exceeds the PDF page count ({total_pages})")
    return sorted(page_numbers)


def file_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return {
        "filename": path.name,
        "path_at_creation": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def clean_pdf_object(item: dict) -> dict:
    cleaned: dict = {}
    for key, value in item.items():
        if key in {"stream", "page"}:
            continue
        cleaned[str(key)] = json_safe(value)
    stream = item.get("stream")
    stream_attrs = getattr(stream, "attrs", None)
    if isinstance(stream_attrs, dict):
        cleaned["stream_attrs"] = json_safe(stream_attrs)
    return cleaned


def page_box(box: object) -> list[float] | None:
    try:
        return [float(box.left), float(box.bottom), float(box.right), float(box.top)]
    except (AttributeError, TypeError, ValueError):
        return None


def extract_table_candidates(page: pdfplumber.page.Page) -> list[dict]:
    try:
        tables = page.find_tables()
    except Exception as exc:
        return [{"error": str(exc)}]

    candidates: list[dict] = []
    for index, table in enumerate(tables, start=1):
        try:
            rows = table.extract() or []
        except Exception as exc:
            rows = []
            extraction_error = str(exc)
        else:
            extraction_error = None
        record = {
            "index": index,
            "bbox": json_safe(table.bbox),
            "cells": json_safe(getattr(table, "cells", [])),
            "rows": json_safe(rows),
        }
        if extraction_error:
            record["extraction_error"] = extraction_error
        candidates.append(record)
    return candidates


def extract_page_payload(
    plumber_page: pdfplumber.page.Page,
    pypdf_page: object,
    page_number: int,
) -> dict:
    logical_text = pypdf_page.extract_text() or ""
    words = plumber_page.extract_words(
        keep_blank_chars=False,
        use_text_flow=True,
        extra_attrs=WORD_EXTRA_ATTRIBUTES,
    )
    chars = [clean_pdf_object(item) for item in plumber_page.chars]
    images = [clean_pdf_object(item) for item in plumber_page.images]
    lines = [clean_pdf_object(item) for item in plumber_page.lines]
    rects = [clean_pdf_object(item) for item in plumber_page.rects]
    curves = [clean_pdf_object(item) for item in plumber_page.curves]
    edges = [clean_pdf_object(item) for item in plumber_page.edges]
    annots = [clean_pdf_object(item) for item in (plumber_page.annots or [])]
    hyperlinks = [clean_pdf_object(item) for item in (plumber_page.hyperlinks or [])]
    table_candidates = extract_table_candidates(plumber_page)

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "page_number": page_number,
        "page": {
            "width": float(plumber_page.width),
            "height": float(plumber_page.height),
            "rotation": int(getattr(pypdf_page, "rotation", 0) or 0),
            "mediabox": page_box(getattr(pypdf_page, "mediabox", None)),
            "cropbox": page_box(getattr(pypdf_page, "cropbox", None)),
            "initial_doctop": float(getattr(plumber_page, "initial_doctop", 0.0) or 0.0),
        },
        "logical_text": {
            "text": logical_text,
            "lines": [line.strip() for line in logical_text.splitlines() if line.strip()],
        },
        "layout": {
            "word_settings": {
                "keep_blank_chars": False,
                "use_text_flow": True,
                "extra_attrs": list(WORD_EXTRA_ATTRIBUTES),
            },
            "words": [clean_pdf_object(item) for item in words],
            "chars": chars,
            "images": images,
            "lines": lines,
            "rects": rects,
            "curves": curves,
            "edges": edges,
            "annots": annots,
            "hyperlinks": hyperlinks,
            "table_candidates": table_candidates,
        },
        "counts": {
            "logical_characters": len(logical_text),
            "logical_lines": len([line for line in logical_text.splitlines() if line.strip()]),
            "words": len(words),
            "chars": len(chars),
            "images": len(images),
            "lines": len(lines),
            "rects": len(rects),
            "curves": len(curves),
            "edges": len(edges),
            "annots": len(annots),
            "hyperlinks": len(hyperlinks),
            "table_candidates": len([item for item in table_candidates if "error" not in item]),
        },
        "has_text_layer": bool(logical_text.strip() or words),
        "extracted_at": utc_now(),
    }
    return payload


def encode_payload(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return zlib.compress(raw, level=6), hashlib.sha256(raw).hexdigest()


def decode_payload(compressed: bytes, expected_sha256: str | None = None) -> dict:
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as exc:
        raise CacheError(f"Cached page data is not valid zlib data: {exc}") from exc
    if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CacheError("Cached page checksum does not match its stored checksum")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CacheError(f"Cached page data is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CacheError("Cached page payload must be a JSON object")
    return payload


class PdfExtractionCache:
    """Reusable reader/writer for the structured per-page cache."""

    def __init__(self, path: Path | str, *, create: bool = False):
        self.path = Path(path).expanduser().resolve()
        if not create and not self.path.is_file():
            raise FileNotFoundError(f"Cache not found: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        if create:
            self._create_schema()
        self._check_schema()

    def __enter__(self) -> "PdfExtractionCache":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                page_number INTEGER PRIMARY KEY,
                payload BLOB NOT NULL,
                payload_sha256 TEXT NOT NULL,
                compression TEXT NOT NULL,
                extracted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rendered_pages (
                page_number INTEGER PRIMARY KEY,
                mime_type TEXT NOT NULL,
                dpi INTEGER NOT NULL,
                data BLOB NOT NULL,
                data_sha256 TEXT NOT NULL,
                rendered_at TEXT NOT NULL
            );
            """
        )
        self.connection.execute(f"PRAGMA user_version = {CACHE_SCHEMA_VERSION}")
        self.connection.commit()

    def _check_schema(self) -> None:
        user_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != CACHE_SCHEMA_VERSION:
            raise CacheError(
                f"Unsupported cache schema version {user_version}; expected {CACHE_SCHEMA_VERSION}"
            )
        required = {"metadata", "pages", "rendered_pages"}
        tables = {
            str(row[0])
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not required.issubset(tables):
            raise CacheError("File is not a pdf-web-reader structured cache")

    def set_metadata(self, key: str, value: object) -> None:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, serialized),
        )

    def get_metadata(self, key: str, default: object = None) -> object:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return json.loads(str(row[0])) if row else default

    @property
    def manifest(self) -> dict:
        manifest = self.get_metadata("manifest", {})
        if not isinstance(manifest, dict) or manifest.get("schema") != CACHE_SCHEMA:
            raise CacheError("Cache manifest is missing or invalid")
        return manifest

    def page_numbers(self) -> list[int]:
        return [int(row[0]) for row in self.connection.execute("SELECT page_number FROM pages ORDER BY page_number")]

    def has_page(self, page_number: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM pages WHERE page_number = ?", (page_number,)
        ).fetchone()
        return row is not None

    def put_page(self, page_number: int, payload: dict) -> None:
        compressed, payload_sha256 = encode_payload(payload)
        self.connection.execute(
            "INSERT INTO pages(page_number, payload, payload_sha256, compression, extracted_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(page_number) DO UPDATE SET "
            "payload = excluded.payload, payload_sha256 = excluded.payload_sha256, "
            "compression = excluded.compression, extracted_at = excluded.extracted_at",
            (page_number, compressed, payload_sha256, PAYLOAD_COMPRESSION, utc_now()),
        )

    def get_page(self, page_number: int, *, verify: bool = True) -> dict:
        row = self.connection.execute(
            "SELECT payload, payload_sha256, compression FROM pages WHERE page_number = ?",
            (page_number,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Page {page_number} is not cached")
        if row["compression"] != PAYLOAD_COMPRESSION:
            raise CacheError(f"Unsupported payload compression: {row['compression']}")
        return decode_payload(bytes(row["payload"]), str(row["payload_sha256"]) if verify else None)

    def iter_pages(self, page_numbers: Iterable[int] | None = None) -> Iterator[dict]:
        selected = self.page_numbers() if page_numbers is None else sorted(set(page_numbers))
        for page_number in selected:
            yield self.get_page(page_number)

    def rendered_page_info(self, page_number: int) -> dict | None:
        row = self.connection.execute(
            "SELECT mime_type, dpi, length(data), data_sha256, rendered_at "
            "FROM rendered_pages WHERE page_number = ?",
            (page_number,),
        ).fetchone()
        if row is None:
            return None
        return {
            "mime_type": str(row[0]),
            "dpi": int(row[1]),
            "size_bytes": int(row[2]),
            "sha256": str(row[3]),
            "rendered_at": str(row[4]),
        }

    def put_rendered_page(self, page_number: int, data: bytes, dpi: int) -> None:
        self.connection.execute(
            "INSERT INTO rendered_pages(page_number, mime_type, dpi, data, data_sha256, rendered_at) "
            "VALUES (?, 'image/png', ?, ?, ?, ?) "
            "ON CONFLICT(page_number) DO UPDATE SET mime_type = excluded.mime_type, "
            "dpi = excluded.dpi, data = excluded.data, data_sha256 = excluded.data_sha256, "
            "rendered_at = excluded.rendered_at",
            (page_number, dpi, data, hashlib.sha256(data).hexdigest(), utc_now()),
        )

    def get_rendered_page(self, page_number: int, *, verify: bool = True) -> bytes:
        row = self.connection.execute(
            "SELECT data, data_sha256 FROM rendered_pages WHERE page_number = ?", (page_number,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Page {page_number} has no cached render")
        data = bytes(row[0])
        if verify and hashlib.sha256(data).hexdigest() != str(row[1]):
            raise CacheError(f"Rendered page {page_number} checksum mismatch")
        return data

    def rendered_page_numbers(self) -> list[int]:
        return [
            int(row[0])
            for row in self.connection.execute("SELECT page_number FROM rendered_pages ORDER BY page_number")
        ]

    def commit(self) -> None:
        self.connection.commit()

    def validate(self) -> list[str]:
        errors: list[str] = []
        try:
            manifest = self.manifest
        except CacheError as exc:
            return [str(exc)]
        total_pages = int(manifest.get("source", {}).get("page_count") or 0)
        for page_number in self.page_numbers():
            if not 1 <= page_number <= total_pages:
                errors.append(f"Cached page {page_number} is outside the source page count")
            try:
                payload = self.get_page(page_number)
            except (CacheError, KeyError) as exc:
                errors.append(f"Page {page_number}: {exc}")
                continue
            if int(payload.get("page_number") or 0) != page_number:
                errors.append(f"Page {page_number}: payload page number does not match")
        for page_number in self.rendered_page_numbers():
            try:
                self.get_rendered_page(page_number)
            except (CacheError, KeyError) as exc:
                errors.append(f"Rendered page {page_number}: {exc}")
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            errors.append(f"SQLite integrity check failed: {integrity[0] if integrity else 'unknown error'}")
        return errors


def build_manifest(pdf_path: Path, fingerprint: dict, total_pages: int) -> dict:
    return {
        "schema": CACHE_SCHEMA,
        "schema_version": CACHE_SCHEMA_VERSION,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source": {
            **fingerprint,
            "page_count": total_pages,
        },
        "extractor": {
            "tool": "pdf_cache_tool",
            "python": platform.python_version(),
            "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            "pypdf": getattr(sys.modules.get("pypdf"), "__version__", "unknown"),
            "payload_compression": PAYLOAD_COMPRESSION,
        },
        "contents": {
            "logical_text": True,
            "positioned_words": True,
            "characters": True,
            "image_metadata": True,
            "vector_geometry": True,
            "annotations": True,
            "table_candidates": True,
            "rendered_pages_optional": True,
        },
        "status": {},
    }


def update_manifest_status(cache: PdfExtractionCache, manifest: dict) -> dict:
    page_numbers = cache.page_numbers()
    rendered_pages = cache.rendered_page_numbers()
    total_pages = int(manifest.get("source", {}).get("page_count") or 0)
    updated = dict(manifest)
    updated["updated_at"] = utc_now()
    updated["status"] = {
        "cached_page_count": len(page_numbers),
        "cached_pages": page_numbers,
        "rendered_page_count": len(rendered_pages),
        "rendered_pages": rendered_pages,
        "complete": page_numbers == list(range(1, total_pages + 1)),
    }
    cache.set_metadata("manifest", updated)
    return updated


def render_page_png(pdf_path: Path, page_number: int, dpi: int) -> bytes:
    if not PDFTOPPM.is_file():
        raise FileNotFoundError(
            "pdftoppm was not found. Install Poppler or set PDFTOPPM_PATH before using rendered-page caching."
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = Path(tmpdir) / f"page-{page_number}"
        result = subprocess.run(
            [
                str(PDFTOPPM),
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-singlefile",
                "-png",
                "-r",
                str(dpi),
                str(pdf_path),
                str(prefix),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pdftoppm failed")
        output = prefix.with_suffix(".png")
        if not output.is_file():
            raise RuntimeError("pdftoppm completed without producing a PNG")
        return output.read_bytes()


def should_render_page(render_mode: str, payload: dict) -> bool:
    if render_mode == "all":
        return True
    if render_mode == "images":
        return int(payload.get("counts", {}).get("images") or 0) > 0
    return False


def build_cache(
    pdf_path: Path | str,
    cache_path: Path | str | None = None,
    *,
    page_numbers: set[int] | None = None,
    render_mode: str = "none",
    render_dpi: int = DEFAULT_RENDER_DPI,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> BuildResult:
    source = Path(pdf_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF not found: {source}")
    if source.suffix.lower() != ".pdf":
        raise ValueError("Input must be a PDF file")
    if render_mode not in RENDER_MODES:
        raise ValueError(f"Render mode must be one of: {', '.join(sorted(RENDER_MODES))}")
    if not 36 <= int(render_dpi) <= 300:
        raise ValueError("Render DPI must be between 36 and 300")

    destination = Path(cache_path).expanduser().resolve() if cache_path else default_cache_path(source).resolve()
    if force and destination.exists():
        destination.unlink()

    if progress_callback:
        progress_callback("Fingerprinting PDF")
    fingerprint = file_fingerprint(source)

    cache_exists = destination.is_file()
    with PdfExtractionCache(destination, create=not cache_exists) as cache:
        if cache_exists:
            manifest = cache.manifest
            cached_hash = str(manifest.get("source", {}).get("sha256") or "")
            if cached_hash != fingerprint["sha256"]:
                raise CacheError(
                    "This cache belongs to a different PDF. Use a different output path or --force to replace it."
                )
            total_pages = int(manifest.get("source", {}).get("page_count") or 0)
            if total_pages < 1:
                raise CacheError("Cached source page count is missing")
            reader: PdfReader | None = None
        else:
            reader = PdfReader(str(source))
            total_pages = len(reader.pages)
            manifest = build_manifest(source, fingerprint, total_pages)
            cache.set_metadata("manifest", manifest)
            cache.commit()
        selected_pages = requested_pages(total_pages, page_numbers)

        extracted_count = 0
        skipped_count = 0
        rendered_count = 0
        missing_pages = [page_number for page_number in selected_pages if not cache.has_page(page_number)]
        if missing_pages and reader is None:
            reader = PdfReader(str(source))
            if len(reader.pages) != total_pages:
                raise CacheError("Cached source page count does not match the PDF")
        plumber_pdf = pdfplumber.open(str(source)) if missing_pages else None
        try:
            for position, page_number in enumerate(selected_pages, start=1):
                if cache.has_page(page_number):
                    payload = cache.get_page(page_number) if render_mode == "images" else None
                    skipped_count += 1
                    if progress_callback:
                        progress_callback(
                            f"Page {page_number} already cached ({position}/{len(selected_pages)})"
                        )
                else:
                    if plumber_pdf is None or reader is None:
                        raise CacheError("PDF extraction resources were not initialized")
                    if progress_callback:
                        progress_callback(f"Extracting page {page_number} ({position}/{len(selected_pages)})")
                    payload = extract_page_payload(
                        plumber_pdf.pages[page_number - 1], reader.pages[page_number - 1], page_number
                    )
                    cache.put_page(page_number, payload)
                    extracted_count += 1
                    cache.commit()

                should_render = render_mode == "all" or (
                    render_mode == "images" and payload is not None and should_render_page(render_mode, payload)
                )
                if should_render:
                    render_info = cache.rendered_page_info(page_number)
                    if render_info is None or int(render_info["dpi"]) != int(render_dpi):
                        if progress_callback:
                            progress_callback(f"Rendering page {page_number} at {render_dpi} DPI")
                        cache.put_rendered_page(
                            page_number,
                            render_page_png(source, page_number, int(render_dpi)),
                            int(render_dpi),
                        )
                        rendered_count += 1
                        cache.commit()
        finally:
            if plumber_pdf is not None:
                plumber_pdf.close()

        manifest = update_manifest_status(cache, manifest)
        manifest["last_build"] = {
            "requested_pages": selected_pages,
            "render_mode": render_mode,
            "render_dpi": int(render_dpi),
            "extracted_pages": extracted_count,
            "skipped_pages": skipped_count,
            "rendered_pages": rendered_count,
        }
        cache.set_metadata("manifest", manifest)
        cache.commit()
        complete = bool(manifest.get("status", {}).get("complete"))

    return BuildResult(
        cache_path=destination,
        total_pages=total_pages,
        requested_pages=len(selected_pages),
        extracted_pages=extracted_count,
        skipped_pages=skipped_count,
        rendered_pages=rendered_count,
        complete=complete,
    )


def load_cached_pages(
    cache_path: Path | str,
    page_numbers: Iterable[int] | None = None,
) -> list[dict]:
    """Integration entry point for consumers such as pdf_reader_tool.py."""
    with PdfExtractionCache(cache_path) as cache:
        return list(cache.iter_pages(page_numbers))


def export_logical_text(
    cache_path: Path | str,
    output_path: Path | str,
    *,
    page_numbers: set[int] | None = None,
) -> Path:
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with PdfExtractionCache(cache_path) as cache:
        available = cache.page_numbers()
        selected = available if page_numbers is None else sorted(page_numbers)
        missing = [page for page in selected if page not in available]
        if missing:
            raise KeyError(f"Page {missing[0]} is not cached")
        parts = []
        for payload in cache.iter_pages(selected):
            page_number = int(payload["page_number"])
            text = str(payload.get("logical_text", {}).get("text") or "").strip()
            parts.append(f"--- page {page_number} ---\n{text}\n")
    destination.write_text("\n".join(parts), encoding="utf-8")
    return destination


def inspect_cache(cache_path: Path | str, page_number: int | None = None) -> dict:
    path = Path(cache_path).expanduser().resolve()
    with PdfExtractionCache(path) as cache:
        manifest = cache.manifest
        summary = {
            "cache_path": str(path),
            "cache_size_bytes": path.stat().st_size,
            "schema": manifest.get("schema"),
            "schema_version": manifest.get("schema_version"),
            "source": manifest.get("source"),
            "status": manifest.get("status"),
            "extractor": manifest.get("extractor"),
        }
        if page_number is not None:
            payload = cache.get_page(page_number)
            summary["page"] = {
                "page_number": page_number,
                "page": payload.get("page"),
                "counts": payload.get("counts"),
                "has_text_layer": payload.get("has_text_layer"),
                "logical_text_preview": str(payload.get("logical_text", {}).get("text") or "")[:500],
                "rendered_page": cache.rendered_page_info(page_number),
            }
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and inspect reusable structured extraction caches for PDF Web Reader."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Extract PDF pages into a resumable structured cache")
    build.add_argument("pdf", type=Path, help="Source text-layer PDF")
    build.add_argument("-o", "--output", type=Path, help="Output .pdfcache file")
    build.add_argument("--pages", help="Optional page range, for example 1-5,8,12")
    build.add_argument(
        "--render-pages",
        choices=sorted(RENDER_MODES),
        default="none",
        help="Cache PNG renders for no pages, image-bearing pages, or all pages",
    )
    build.add_argument("--render-dpi", type=int, default=DEFAULT_RENDER_DPI)
    build.add_argument("--force", action="store_true", help="Replace an existing cache")

    inspect_command = subparsers.add_parser("inspect", help="Print cache metadata and page counts")
    inspect_command.add_argument("cache", type=Path)
    inspect_command.add_argument("--page", type=int, help="Include a summary for one cached page")

    validate = subparsers.add_parser("validate", help="Verify checksums and SQLite integrity")
    validate.add_argument("cache", type=Path)
    validate.add_argument("--pdf", type=Path, help="Also confirm that the cache belongs to this PDF")

    export = subparsers.add_parser("export-text", help="Export cached logical text to UTF-8 text")
    export.add_argument("cache", type=Path)
    export.add_argument("-o", "--output", type=Path, required=True)
    export.add_argument("--pages", help="Optional cached page range")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_cache(
                args.pdf,
                args.output,
                page_numbers=parse_page_ranges(args.pages),
                render_mode=args.render_pages,
                render_dpi=args.render_dpi,
                force=args.force,
                progress_callback=print,
            )
            print(f"Cache: {result.cache_path}")
            print(
                f"Pages: {result.requested_pages} requested, {result.extracted_pages} extracted, "
                f"{result.skipped_pages} reused"
            )
            if result.rendered_pages:
                print(f"Rendered pages added: {result.rendered_pages}")
            print(f"Whole book cached: {'yes' if result.complete else 'no'}")
            return 0

        if args.command == "inspect":
            print(json.dumps(inspect_cache(args.cache, args.page), ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate":
            with PdfExtractionCache(args.cache) as cache:
                errors = cache.validate()
                if args.pdf:
                    source = Path(args.pdf).expanduser().resolve()
                    expected = str(cache.manifest.get("source", {}).get("sha256") or "")
                    actual = str(file_fingerprint(source)["sha256"])
                    if actual != expected:
                        errors.append("The supplied PDF does not match the cache fingerprint")
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print("Cache is valid")
            return 0

        if args.command == "export-text":
            output = export_logical_text(
                args.cache,
                args.output,
                page_numbers=parse_page_ranges(args.pages),
            )
            print(f"Text exported to: {output}")
            return 0
    except (CacheError, FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
