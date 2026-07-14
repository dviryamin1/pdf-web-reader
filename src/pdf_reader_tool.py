from __future__ import annotations

import argparse
import base64
import difflib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import pdfplumber
from PIL import Image
from pypdf import PdfReader


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "generated-readers"
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
BUNDLED_PDFTOPPM = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
PDFTOPPM = Path(os.environ["PDFTOPPM_PATH"]) if os.environ.get("PDFTOPPM_PATH") else Path(shutil.which("pdftoppm") or BUNDLED_PDFTOPPM)
IMAGE_RENDER_DPI = 120
IMAGE_MAX_EMBED_WIDTH = 900
IMAGE_JPEG_QUALITY = 84
IMAGE_CAPTION_CONFIDENCE_THRESHOLD = 0.55
DEFAULT_CROP_SETTINGS = {
    "right": {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0},
    "left": {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0},
}


def safe_slug(value: str) -> str:
    stem = Path(value).stem.strip() or "pdf-reader"
    slug = re.sub(r"[^\w.-]+", "-", stem, flags=re.UNICODE).strip("-._")
    return slug or "pdf-reader"


def clamp_percent(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, 0.0), 45.0)


def crop_settings_enabled(crop_settings: dict | None) -> bool:
    if not crop_settings:
        return False
    return any(
        clamp_percent(crop_settings.get(side, {}).get(edge, 0.0)) > 0
        for side in ("right", "left")
        for edge in ("top", "right", "bottom", "left")
    )


def normalize_crop_settings(crop_settings: dict | None = None) -> dict:
    normalized = {
        side: dict(DEFAULT_CROP_SETTINGS[side])
        for side in ("right", "left")
    }
    if crop_settings:
        for side in ("right", "left"):
            for edge in ("top", "right", "bottom", "left"):
                normalized[side][edge] = clamp_percent(crop_settings.get(side, {}).get(edge, normalized[side][edge]))
    return normalized


def parse_crop_argument(value: str | None) -> dict[str, float]:
    result = {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0}
    if not value:
        return result
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("Crop must be four comma-separated percentages: top,right,bottom,left")
    for edge, part in zip(("top", "right", "bottom", "left"), parts):
        result[edge] = clamp_percent(part)
    return result


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
            if start < 1 or end < 1 or end < start:
                raise ValueError("Page range must contain positive pages in ascending order")
            pages.update(range(start, end + 1))
        else:
            if not part.isdigit():
                raise ValueError("Page range must use numbers, for example: 1-5,8,12")
            page = int(part)
            if page < 1:
                raise ValueError("Page range must contain positive page numbers")
            pages.add(page)
    return pages or None


def page_range_label(page_numbers: set[int] | None) -> str:
    if not page_numbers:
        return "all pages"
    sorted_pages = sorted(page_numbers)
    ranges: list[str] = []
    start = previous = sorted_pages[0]
    for page in sorted_pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def requested_page_indices(total_pages: int, page_numbers: set[int] | None) -> list[int]:
    if not page_numbers:
        return list(range(total_pages))
    return [page_number - 1 for page_number in sorted(page_numbers) if 1 <= page_number <= total_pages]


def extract_requested_text_pages(reader: PdfReader, page_numbers: set[int] | None) -> dict[int, list[str]]:
    text_pages: dict[int, list[str]] = {}
    for page_index in requested_page_indices(len(reader.pages), page_numbers):
        page_number = page_index + 1
        page_text = reader.pages[page_index].extract_text() or ""
        text_pages[page_number] = [line.strip() for line in page_text.splitlines() if line.strip()]
    return text_pages


def page_side(page_number: int) -> str:
    return "right" if page_number % 2 == 1 else "left"


def page_crop_bounds(page_width: float, page_height: float, page_number: int, crop_settings: dict | None) -> tuple[float, float, float, float]:
    settings = normalize_crop_settings(crop_settings)[page_side(page_number)]
    left = page_width * settings["left"] / 100.0
    right = page_width * (1.0 - settings["right"] / 100.0)
    top = page_height * settings["top"] / 100.0
    bottom = page_height * (1.0 - settings["bottom"] / 100.0)
    return left, top, right, bottom


def bbox_midpoint_in_bounds(x0: float, top: float, x1: float, bottom: float, bounds: tuple[float, float, float, float]) -> bool:
    left, crop_top, right, crop_bottom = bounds
    mid_x = (x0 + x1) / 2.0
    mid_y = (top + bottom) / 2.0
    return left <= mid_x <= right and crop_top <= mid_y <= crop_bottom


def filter_words_by_crop(words: list[dict], bounds: tuple[float, float, float, float] | None) -> list[dict]:
    if bounds is None:
        return words
    return [
        word
        for word in words
        if bbox_midpoint_in_bounds(
            float(word.get("x0") or 0.0),
            float(word.get("top") or 0.0),
            float(word.get("x1") or 0.0),
            float(word.get("bottom") or word.get("top") or 0.0),
            bounds,
        )
    ]


def normalize_words_to_layout_bounds(words: list[dict], bounds: tuple[float, float, float, float] | None) -> list[dict]:
    if bounds is None:
        return words
    left, _, _, _ = bounds
    normalized: list[dict] = []
    for word in words:
        item = dict(word)
        item["x0"] = float(item.get("x0") or 0.0) - left
        item["x1"] = float(item.get("x1") or 0.0) - left
        normalized.append(item)
    return normalized


def layout_width_for_bounds(page_width: float, bounds: tuple[float, float, float, float] | None) -> float:
    if bounds is None:
        return page_width
    left, _, right, _ = bounds
    return max(1.0, right - left)


def is_bold_font(font_name: str) -> bool:
    return any(marker in font_name.lower() for marker in ("bold", "black", "heavy", "demi"))


def is_italic_font(font_name: str) -> bool:
    return any(marker in font_name.lower() for marker in ("italic", "oblique", "slant"))


def dominant_number(values: list[float], fallback: float = 12.0) -> float:
    rounded = [round(value * 2) / 2 for value in values if value > 0]
    if not rounded:
        return fallback
    return max(set(rounded), key=rounded.count)


def normalize_text_spacing(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s+([,.!?;:])", r"\1", value)
    return value.strip()


def fix_ltr_punctuation_in_rtl(value: str) -> str:
    latin = r"A-Za-z0-9][A-Za-z0-9\s.,&;:'\-–—/"
    value = re.sub(rf"\),\s*([{latin}]+)$", r"(\1", value)
    value = re.sub(r"^([A-Za-z0-9][A-Za-z0-9\s.,&;:'\-–—/]*)\.\(", r"\1).", value)
    value = re.sub(rf"\),\s*([{latin}]+)\(", r"(\1)", value)
    value = re.sub(rf"\)([{latin}]+)\(", r"(\1)", value)
    value = re.sub(rf"\)([{latin}]+)", r"(\1", value)
    value = re.sub(rf"([{latin}]+)\(", r"\1)", value)
    value = re.sub(r"\(\s+", "(", value)
    value = re.sub(r"\s+\)", ")", value)
    value = re.sub(r",([A-Za-z])", r", \1", value)
    value = re.sub(r"\(([^()]*[A-Za-z][^()]*)\.\)", r"(\1).", value)
    return value


def repair_split_ltr_citation(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        prefix = normalize_text_spacing(match.group("prefix"))
        hebrew = normalize_text_spacing(match.group("hebrew"))
        author = normalize_text_spacing(match.group("author"))
        year = match.group("year")
        return f"{hebrew} ({prefix} {author}, {year})."

    value = re.sub(
        r"(?P<prefix>[A-Z][A-Za-z0-9\s.,&;:'\-\u2013]+&)\)?\s+"
        r"(?P<hebrew>[\u0590-\u05ff][\u0590-\u05ff\s.,;:'\"\u05f3\u05f4\u05be\\-]+?)\s+"
        r"(?P<year>\d{4}),\s*(?P<author>[A-Z][A-Za-z\s.'\-\u2013]+)\)\.?",
        replacement,
        value,
    )
    return re.sub(
        r"\((?P<first_author>[A-Z][A-Za-z\s.'\-]+)\s+"
        r"(?P<first_year>\d{4})\)\.\s+"
        r"(?P<hebrew>[\u0590-\u05ff][\u0590-\u05ff\s.,;:'\"\u05f3\u05f4\u05be\\-]+?);\s+"
        r"(?P<rest>[A-Z][A-Za-z0-9\s.,&;:'\-\u2013]+,\s*\d{4})(?=\s+[\u0590-\u05ff])",
        lambda match: (
            f"({normalize_text_spacing(match.group('first_author'))}, {match.group('first_year')}; "
            f"{normalize_text_spacing(match.group('rest'))}). {normalize_text_spacing(match.group('hebrew'))}"
        ),
        value,
    )


def clean_extracted_text_line(value: str) -> str:
    return repair_split_ltr_citation(fix_ltr_punctuation_in_rtl(normalize_text_spacing(value)))


def has_hebrew(value: str) -> bool:
    return bool(re.search(r"[\u0590-\u05ff]", value))


def visual_word_to_text(value: str) -> str:
    if has_hebrew(value):
        return value[::-1]
    return value


def visual_line_to_text(value: str) -> str:
    if has_hebrew(value):
        return value[::-1]
    return value


def canonical_match_text(value: str) -> str:
    value = clean_extracted_text_line(value)
    value = re.sub(r"[^\w\u0590-\u05ff]+", "", value, flags=re.UNICODE)
    return value.lower()


def canonical_hebrew_text(value: str) -> str:
    return "".join(re.findall(r"[\u0590-\u05ff]+", clean_extracted_text_line(value)))


def style_line_similarity(correct_text: str, visual_line: dict) -> float:
    correct = canonical_match_text(correct_text)
    visual_text = visual_line_to_text(str(visual_line.get("text", "")))
    visual = canonical_match_text(visual_text)
    if not correct or not visual:
        return 0.0
    if correct == visual:
        return 1.0
    if correct in visual or visual in correct:
        shorter = min(len(correct), len(visual))
        longer = max(len(correct), len(visual))
        return 0.92 * (shorter / longer)
    correct_hebrew = canonical_hebrew_text(correct_text)
    visual_hebrew = canonical_hebrew_text(visual_text)
    if len(correct_hebrew) >= 8 and len(visual_hebrew) >= 8 and (correct_hebrew in visual_hebrew or visual_hebrew in correct_hebrew):
        shorter = min(len(correct_hebrew), len(visual_hebrew))
        longer = max(len(correct_hebrew), len(visual_hebrew))
        return max(0.72, 0.94 * (shorter / longer))
    return difflib.SequenceMatcher(None, correct, visual).ratio()


def best_style_line_match(correct_text: str, visual_lines: list[dict]) -> dict | None:
    best: tuple[float, int, dict] | None = None
    for visual_index, visual_line in enumerate(visual_lines):
        score = style_line_similarity(correct_text, visual_line)
        if best is None or score > best[0]:
            best = (score, visual_index, visual_line)
    if best is None:
        return None
    score, visual_index, visual_line = best
    return {
        "visual_index": visual_index,
        "score": round(score, 3),
        "visual_text": clean_extracted_text_line(visual_line_to_text(str(visual_line.get("text") or ""))),
    }


def line_debug_record(line: dict, index: int | None = None, role: str | None = None) -> dict:
    record = {
        "text": str(line.get("text") or ""),
        "top": round(float(line.get("top") or 0.0), 2),
        "bottom": round(float(line.get("bottom") or 0.0), 2),
        "x0": round(float(line.get("x0") or 0.0), 2),
        "x1": round(float(line.get("x1") or 0.0), 2),
        "width": round(line_width(line), 2),
        "size": round(float(line.get("size") or 0.0), 2),
        "font": str(line.get("font") or ""),
    }
    if index is not None:
        record["index"] = index
    if role:
        record["role"] = role
    if line.get("noise_reason"):
        record["noise_reason"] = str(line.get("noise_reason"))
    return record


def align_text_lines_to_style_lines(correct_text_lines: list[str], visual_lines: list[dict], require_visual_match: bool = False) -> list[dict]:
    corrected_lines: list[dict | None] = [None] * len(correct_text_lines)
    clean_text_lines = [clean_extracted_text_line(line) for line in correct_text_lines]
    candidates: list[tuple[float, int, int]] = []
    for text_index, clean_text in enumerate(clean_text_lines):
        for visual_index, visual_line in enumerate(visual_lines):
            score = style_line_similarity(clean_text, visual_line)
            if score >= 0.58:
                candidates.append((score, text_index, visual_index))

    def candidate_sort_key(item: tuple[float, int, int]) -> tuple[float, int]:
        score, text_index, visual_index = item
        correct_length = len(canonical_match_text(clean_text_lines[text_index]))
        visual_length = len(canonical_match_text(visual_line_to_text(str(visual_lines[visual_index].get("text", "")))))
        return score, min(correct_length, visual_length)

    candidates.sort(key=candidate_sort_key, reverse=True)

    used_text_indexes: set[int] = set()
    used_visual_indexes: set[int] = set()
    for score, text_index, visual_index in candidates:
        if text_index in used_text_indexes or visual_index in used_visual_indexes:
            continue
        line = dict(visual_lines[visual_index])
        line["text"] = clean_text_lines[text_index]
        corrected_lines[text_index] = line
        used_text_indexes.add(text_index)
        used_visual_indexes.add(visual_index)

    last_top = 0.0
    for index, clean_text in enumerate(clean_text_lines):
        if corrected_lines[index] is not None:
            last_top = max(last_top, float(corrected_lines[index].get("bottom") or corrected_lines[index].get("top") or last_top))
            continue

        if require_visual_match:
            continue

        if index < len(visual_lines) and index not in used_visual_indexes:
            line = dict(visual_lines[index])
            used_visual_indexes.add(index)
            last_top = max(last_top, float(line.get("bottom") or line.get("top") or last_top))
        else:
            top = last_top + 18.0
            line = {
                "size": 12.0,
                "font": "",
                "top": top,
                "bottom": top + 12.0,
                "x0": 0.0,
                "x1": 0.0,
                "style_words": [],
            }
            last_top = line["bottom"]

        line["text"] = clean_text
        corrected_lines[index] = line

    complete_lines = [line for line in corrected_lines if line is not None]
    complete_lines.sort(key=lambda line: (float(line.get("top") or 0), float(line.get("x0") or 0)))
    return complete_lines


def visual_lines_to_text_lines(visual_lines: list[dict]) -> list[dict]:
    corrected: list[dict] = []
    for visual_line in visual_lines:
        line = dict(visual_line)
        line["text"] = clean_extracted_text_line(visual_line_to_text(str(line.get("text") or "")))
        corrected.append(line)
    corrected.sort(key=lambda line: (float(line.get("top") or 0), float(line.get("x0") or 0)))
    return corrected


def line_from_words(words: list[dict]) -> dict:
    raw_text = " ".join(word.get("text", "") for word in words)
    text = normalize_text_spacing(raw_text)
    sizes = [float(word.get("size") or 0) for word in words]
    fonts = [str(word.get("fontname") or "") for word in words]
    top = min(float(word.get("top") or 0) for word in words)
    bottom = max(float(word.get("bottom") or top) for word in words)
    x0 = min(float(word.get("x0") or 0) for word in words)
    x1 = max(float(word.get("x1") or x0) for word in words)
    reading_words = list(reversed(words))
    return {
        "text": text,
        "raw_text": raw_text,
        "size": dominant_number(sizes),
        "font": max(set(fonts), key=fonts.count) if fonts else "",
        "top": top,
        "bottom": bottom,
        "x0": x0,
        "x1": x1,
        "style_words": [
            {
                "text": normalize_text_spacing(visual_word_to_text(str(word.get("text", "")))),
                "size": float(word.get("size") or 0),
                "font": str(word.get("fontname") or ""),
            }
            for word in reading_words
        ],
    }


def split_words_by_horizontal_regions(words: list[dict], page_width: float) -> list[list[dict]]:
    if not words:
        return []

    sorted_words = sorted(words, key=lambda item: float(item["x0"]))
    average_size = dominant_number([float(word.get("size") or 0.0) for word in sorted_words])
    gaps = [
        float(sorted_words[index].get("x0") or 0.0) - float(sorted_words[index - 1].get("x1") or 0.0)
        for index in range(1, len(sorted_words))
    ]
    positive_gaps = sorted(gap for gap in gaps if gap > 0)
    baseline_gap = positive_gaps[max(0, len(positive_gaps) // 4)] if positive_gaps else average_size * 0.35
    gap_threshold = max(page_width * 0.02, average_size * 1.4, baseline_gap * 1.8, 12.0)
    regions: list[list[dict]] = []
    current: list[dict] = []
    previous_x1 = 0.0

    for word in sorted_words:
        x0 = float(word.get("x0") or 0.0)
        if current and x0 - previous_x1 > gap_threshold:
            regions.append(current)
            current = []
        current.append(word)
        previous_x1 = max(previous_x1, float(word.get("x1") or x0))

    if current:
        regions.append(current)
    return regions


def cluster_visual_lines(words: list[dict], page_width: float, tolerance: float = 3.2) -> list[dict]:
    sorted_words = sorted(words, key=lambda item: (float(item["top"]), float(item["x0"])))
    clusters: list[list[dict]] = []

    for word in sorted_words:
        top = float(word["top"])
        if clusters:
            cluster_top = sum(float(item["top"]) for item in clusters[-1]) / len(clusters[-1])
            if abs(top - cluster_top) <= tolerance:
                clusters[-1].append(word)
                continue
        clusters.append([word])

    lines: list[dict] = []
    for words_in_line in clusters:
        for words_in_region in split_words_by_horizontal_regions(words_in_line, page_width):
            words_in_region.sort(key=lambda item: float(item["x0"]))
            line = line_from_words(words_in_region)
            if line_width(line) < max(10.0, page_width * 0.025):
                continue
            if line["text"]:
                lines.append(line)
    return lines


def style_attrs(size: float, font: str, body_size: float) -> tuple[list[str], str]:
    classes: list[str] = []
    ratio = min(max(size / body_size, 0.82), 1.35) if body_size and size else 1.0
    if is_bold_font(font):
        classes.append("bold")
    if is_italic_font(font):
        classes.append("italic")
    style_attr = f' style="font-size:{ratio:.2f}em"' if abs(ratio - 1.0) >= 0.06 else ""
    return classes, style_attr


LTR_SEQUENCE_RE = re.compile(r"\(?[A-Za-z0-9][A-Za-z0-9\s.,;:&'\"()\-–/]*[A-Za-z0-9)&]\)?")


def text_with_bidi_isolates(value: str) -> str:
    parts: list[str] = []
    last = 0
    for match in LTR_SEQUENCE_RE.finditer(value):
        start, end = match.span()
        if start > last:
            parts.append(html.escape(value[last:start]))
        parts.append(f'<bdi dir="ltr">{html.escape(match.group(0))}</bdi>')
        last = end
    if last < len(value):
        parts.append(html.escape(value[last:]))
    return "".join(parts)


def wrap_span(value: str, classes: list[str], style_attr: str) -> str:
    class_attr = f' class="{" ".join(classes)}"' if classes else ""
    return f"<span{class_attr}{style_attr}>{text_with_bidi_isolates(value)}</span>"


def styled_line_html(line: dict, body_size: float) -> str:
    text = line["text"]
    line_classes, line_style = style_attrs(line["size"], line["font"], body_size)
    style_words = [
        word for word in line.get("style_words", [])
        if word.get("text") and (is_bold_font(word.get("font", "")) or is_italic_font(word.get("font", "")) or abs((float(word.get("size") or 0) / body_size) - 1.0) >= 0.06)
    ]

    if not style_words:
        return wrap_span(text, line_classes, line_style)

    ranges: list[tuple[int, int, list[str], str]] = []
    cursor = 0
    for word in style_words:
        token = str(word["text"]).strip()
        if not token:
            continue
        position = text.find(token, cursor)
        if position == -1:
            position = text.find(token)
        if position == -1:
            continue
        classes, style_attr = style_attrs(float(word.get("size") or 0), str(word.get("font") or ""), body_size)
        ranges.append((position, position + len(token), classes, style_attr))
        cursor = position + len(token)

    if not ranges:
        return wrap_span(text, line_classes, line_style)

    ranges.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, list[str], str]] = []
    for start, end, classes, style_attr in ranges:
        if merged and start <= merged[-1][1] + 1 and classes == merged[-1][2] and style_attr == merged[-1][3]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), classes, style_attr)
        elif not merged or start >= merged[-1][1]:
            merged.append((start, end, classes, style_attr))

    parts: list[str] = []
    last = 0
    for start, end, classes, style_attr in merged:
        if start > last:
            parts.append(text_with_bidi_isolates(text[last:start]))
        parts.append(wrap_span(text[start:end], classes, style_attr))
        last = end
    if last < len(text):
        parts.append(text_with_bidi_isolates(text[last:]))
    return "".join(parts)


def rtl_indented_paragraph_starts(lines: list[dict], body_size: float, page_width: float) -> set[int]:
    if len(lines) < 3 or page_width <= 0:
        return set()
    hebrew_lines = [line for line in lines if has_hebrew(str(line.get("text") or ""))]
    if len(hebrew_lines) < 3 or len(hebrew_lines) / len(lines) < 0.55:
        return set()

    right_edges = [float(line.get("x1") or 0.0) for line in hebrew_lines if float(line.get("x1") or 0.0) > 0.0]
    if not right_edges:
        return set()
    right_margin = dominant_number(right_edges, fallback=max(right_edges))
    indent_threshold = max(body_size * 0.75, page_width * 0.015)
    max_first_line_indent = max(body_size * 2.8, page_width * 0.065)
    aligned_tolerance = indent_threshold * 0.55
    sentence_end = re.compile(r"[.!?;:\u05c3][\"'\u05f4\u201d)]*\s*$")

    starts: set[int] = set()
    for index in range(1, len(lines) - 1):
        previous = lines[index - 1]
        current = lines[index]
        following = lines[index + 1]
        current_text = str(current.get("text") or "").strip()
        previous_text = str(previous.get("text") or "").strip()
        if not has_hebrew(current_text) or not has_hebrew(str(following.get("text") or "")):
            continue
        gap = float(current.get("top") or 0.0) - float(previous.get("bottom") or 0.0)
        if gap < 0.0 or gap > body_size * 1.25:
            continue
        current_indent = right_margin - float(current.get("x1") or 0.0)
        previous_indent = right_margin - float(previous.get("x1") or 0.0)
        following_indent = right_margin - float(following.get("x1") or 0.0)
        if not (indent_threshold <= current_indent <= max_first_line_indent):
            continue
        if previous_indent > aligned_tolerance or following_indent > aligned_tolerance:
            continue
        if not sentence_end.search(previous_text):
            continue
        starts.add(index)
    return starts


def lines_to_blocks(lines: list[dict], body_size: float, page_width: float = 0.0) -> list[dict]:
    if not lines:
        return []

    gaps = [lines[index]["top"] - lines[index - 1]["bottom"] for index in range(1, len(lines))]
    positive_gaps = [gap for gap in gaps if gap > 0]
    normal_gap = dominant_number(positive_gaps, fallback=body_size * 0.45)
    paragraph_gap = max(normal_gap * 1.85, body_size * 0.8)
    rtl_indent_starts = rtl_indented_paragraph_starts(lines, body_size, page_width)

    blocks: list[dict] = []
    current: list[dict] = []
    current_start_reason = "page_start"
    for index, line in enumerate(lines):
        if current:
            gap = line["top"] - current[-1]["bottom"]
            start_reason = "vertical_gap" if gap >= paragraph_gap else "rtl_first_line_indent" if index in rtl_indent_starts else ""
            starts_new = bool(start_reason)
            if starts_new:
                blocks.append({"lines": current, "paragraph_start_reason": current_start_reason})
                current = []
                current_start_reason = start_reason
        current.append(line)
    if current:
        blocks.append({"lines": current, "paragraph_start_reason": current_start_reason})

    for block in blocks:
        render_body_block(block, body_size)
    return blocks


def render_body_block(block: dict, body_size: float) -> dict:
    block["top"] = min(line["top"] for line in block["lines"])
    block["bottom"] = max(line["bottom"] for line in block["lines"])
    block["tag"] = "p"
    block_text = " ".join(str(line.get("text") or "") for line in block["lines"])
    repaired_block_text = repair_split_ltr_citation(block_text)
    if repaired_block_text != block_text:
        first_line = dict(block["lines"][0])
        first_line["text"] = repaired_block_text
        first_line["style_words"] = []
        block["lines"] = [first_line]
        block["html"] = wrap_span(repaired_block_text, [], "")
    else:
        block["html"] = " ".join(styled_line_html(line, body_size) for line in block["lines"])
    return block


def block_is_heading_like(block: dict, page_width: float, body_size: float) -> bool:
    lines = block.get("lines", [])
    if not lines or len(lines) > 2 or page_width <= 0:
        return False

    text = block_plain_text(block).strip()
    if not text or len(text) > 150 or re.search(r"[.!?؟׃:;״”'\)]\s*$", text):
        return False

    x0, _, x1, _ = block_bounds(block, page_width)
    width_ratio = (x1 - x0) / page_width
    center_ratio = ((x0 + x1) / 2.0) / page_width
    sizes = [float(line.get("size") or body_size) for line in lines]
    avg_size = sum(sizes) / len(sizes)
    visually_heading = avg_size >= body_size * 1.04 or width_ratio <= 0.72
    centered = 0.34 <= center_ratio <= 0.66
    return visually_heading and centered


def body_block_merge_reason(previous: dict, current: dict, page_width: float, body_size: float) -> str | None:
    if previous.get("class") or current.get("class") or page_width <= 0:
        return None
    if current.get("paragraph_start_reason") in {"vertical_gap", "rtl_first_line_indent"}:
        return None
    if block_is_heading_like(previous, page_width, body_size) or block_is_heading_like(current, page_width, body_size):
        return None

    previous_x0, _, previous_x1, previous_bottom = block_bounds(previous, page_width)
    current_x0, current_top, current_x1, _ = block_bounds(current, page_width)
    gap = current_top - previous_bottom
    if gap < 0 or gap > body_size * 3.2:
        return None

    previous_width = max(1.0, previous_x1 - previous_x0)
    current_width = max(1.0, current_x1 - current_x0)
    overlap = max(0.0, min(previous_x1, current_x1) - max(previous_x0, current_x0))
    overlap_ratio = overlap / min(previous_width, current_width)
    previous_zone = horizontal_zone(previous_x0, previous_x1, page_width)
    current_zone = horizontal_zone(current_x0, current_x1, page_width)
    same_region = previous_zone == current_zone or overlap_ratio >= 0.58
    if not same_region:
        return None

    previous_text = block_plain_text(previous).strip()
    current_text = block_plain_text(current).strip()
    if not previous_text or not current_text:
        return None

    current_line_count = len(current.get("lines", []))
    current_is_short_continuation = current_line_count <= 2 and len(current_text) <= 180
    previous_continues = not re.search(r"[.!?؟׃:;״”'\)]\s*$", previous_text)
    small_gap = gap <= body_size * 1.7
    if previous_continues and gap <= body_size * 3.2:
        return "continuing_sentence"
    if small_gap and (overlap_ratio >= 0.72 or current_is_short_continuation):
        return "same_region_small_gap"
    return None


def merge_adjacent_body_blocks(blocks: list[dict], page_width: float, body_size: float) -> list[dict]:
    merged: list[dict] = []
    for block in blocks:
        if not merged:
            merged.append(block)
            continue

        reason = body_block_merge_reason(merged[-1], block, page_width, body_size)
        if not reason:
            merged.append(block)
            continue

        previous = merged[-1]
        merge_reasons = list(previous.get("merge_reasons", []))
        merge_reasons.append(reason)
        previous["lines"] = previous.get("lines", []) + block.get("lines", [])
        previous["merge_reasons"] = merge_reasons
        previous["merged_block_count"] = int(previous.get("merged_block_count", 1)) + int(block.get("merged_block_count", 1))
        render_body_block(previous, body_size)

    return merged


def line_width(line: dict) -> float:
    return max(0.0, float(line.get("x1") or 0.0) - float(line.get("x0") or 0.0))


def is_ltr_reference_fragment(text: str) -> bool:
    normalized = normalize_text_spacing(text)
    if not normalized or has_hebrew(normalized):
        return False
    if not re.search(r"\b(?:19|20)\d{2}\b", normalized):
        return False
    latin_chars = len(re.findall(r"[A-Za-z]", normalized))
    if latin_chars < 4:
        return False
    reference_markers = sum(marker in normalized for marker in ("&", " et al", ",", ";", "("))
    return reference_markers >= 2


def estimate_body_band(lines: list[dict], body_size: float, page_width: float) -> tuple[float, float]:
    candidates = [
        line
        for line in lines
        if line_width(line) >= page_width * 0.45
        and float(line.get("size") or body_size) >= body_size * 0.94
    ]
    if not candidates:
        return page_width * 0.08, page_width * 0.92

    x0_values = sorted(float(line.get("x0") or 0.0) for line in candidates)
    x1_values = sorted(float(line.get("x1") or page_width) for line in candidates)
    middle = len(candidates) // 2
    median_x0 = x0_values[middle]
    median_x1 = x1_values[middle]
    padding = page_width * 0.05
    return max(0.0, median_x0 - padding), min(page_width, median_x1 + padding)


def is_likely_secondary_line(line: dict, body_size: float, page_width: float, body_band: tuple[float, float]) -> bool:
    text = str(line.get("text") or "").strip()
    if len(text) < 8 or page_width <= 0:
        return False

    size = float(line.get("size") or body_size)
    width = line_width(line)
    center = (float(line.get("x0") or 0.0) + float(line.get("x1") or 0.0)) / 2.0
    body_left, body_right = body_band
    outside_body = center < body_left or center > body_right
    small = size <= body_size * 0.94
    very_small = size <= body_size * 0.88
    narrow = width <= page_width * 0.58
    very_narrow = width <= page_width * 0.42
    short = len(text) <= 115
    looks_like_heading = size >= body_size * 1.06 or is_bold_font(str(line.get("font") or ""))

    if looks_like_heading:
        return False
    if not outside_body and is_ltr_reference_fragment(text):
        return False

    score = 0
    if small:
        score += 2
    if very_small:
        score += 1
    if narrow:
        score += 1
    if very_narrow:
        score += 1
    if outside_body:
        score += 2
    if short:
        score += 1
    return score >= 4


def noise_reason_for_line(line: dict, page_width: float, page_height: float, body_size: float) -> str | None:
    text = normalize_text_spacing(str(line.get("text") or ""))
    if not text:
        return "empty"

    top = float(line.get("top") or 0.0)
    bottom = float(line.get("bottom") or top)
    size = float(line.get("size") or body_size)
    top_margin = page_height > 0 and top <= page_height * 0.095
    bottom_margin = page_height > 0 and bottom >= page_height * 0.925

    if "File #" in text or "do not distribute" in text or "belongs to" in text:
        return "watermark"
    if "Book" in text and ".indb" in text:
        return "footer_metadata"
    if re.search(r"\b\d{2}/\d{2}/\d{4}\b", text):
        return "footer_metadata"
    if re.fullmatch(r"[A-Z](?:\s+[A-Z]){4,}", text):
        return "watermark_letters"
    if len(text) >= 8 and re.fullmatch(r"[_\-\s]+", text):
        return "decorative_rule"
    if bottom_margin:
        return "footer"

    has_hebrew_text = has_hebrew(text)
    has_digit = bool(re.search(r"\d", text))
    short_header = len(text) <= 95
    if top_margin and short_header and (has_digit or has_hebrew_text) and size <= body_size * 1.05:
        return "running_header"

    if page_width > 0 and line_width(line) <= page_width * 0.08 and has_digit and (top_margin or bottom_margin):
        return "page_number"
    return None


def split_noise_lines(lines: list[dict], page_width: float, page_height: float, body_size: float) -> tuple[list[dict], list[dict]]:
    content_lines: list[dict] = []
    noise_lines: list[dict] = []
    for line in lines:
        reason = noise_reason_for_line(line, page_width, page_height, body_size)
        if reason:
            item = dict(line)
            item["noise_reason"] = reason
            noise_lines.append(item)
        else:
            content_lines.append(line)
    return content_lines, noise_lines


def secondary_lines_to_blocks(lines: list[dict], body_size: float) -> list[dict]:
    if not lines:
        return []

    blocks: list[dict] = []
    max_gap = body_size * 1.35

    def same_secondary_region(previous: dict, line: dict) -> bool:
        horizontal_gap = max(
            float(line.get("x0") or 0.0) - float(previous.get("x1") or 0.0),
            float(previous.get("x0") or 0.0) - float(line.get("x1") or 0.0),
            0.0,
        )
        overlap = min(float(previous.get("x1") or 0.0), float(line.get("x1") or 0.0)) - max(float(previous.get("x0") or 0.0), float(line.get("x0") or 0.0))
        return overlap > 0 or horizontal_gap <= body_size * 0.75

    for line in sorted(lines, key=lambda item: (float(item["top"]), float(item["x0"]))):
        matching_block = None
        for block in reversed(blocks):
            previous = block["lines"][-1]
            if float(line["top"]) - float(previous["bottom"]) <= max_gap and same_secondary_region(previous, line):
                matching_block = block
                break
        if matching_block is None:
            blocks.append({"lines": [line]})
        else:
            matching_block["lines"].append(line)

    for block in blocks:
        block["top"] = min(line["top"] for line in block["lines"])
        block["bottom"] = max(line["bottom"] for line in block["lines"])
        block["tag"] = "aside"
        block["class"] = "caption"
        block["html"] = " ".join(styled_line_html(line, body_size) for line in block["lines"])
    return blocks


def separate_secondary_text(lines: list[dict], body_size: float, page_width: float) -> tuple[list[dict], list[dict]]:
    body_band = estimate_body_band(lines, body_size, page_width)
    flow_lines: list[dict] = []
    secondary_lines: list[dict] = []
    for line in lines:
        if is_likely_secondary_line(line, body_size, page_width, body_band):
            secondary_lines.append(line)
        else:
            flow_lines.append(line)
    return flow_lines, secondary_lines_to_blocks(secondary_lines, body_size)


def is_question_callout_title(text: str) -> bool:
    return "שאלו" in text and "עצמכם" in text


def question_column_items(lines: list[dict]) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in sorted(lines, key=lambda item: (float(item.get("top") or 0.0), -float(item.get("x1") or 0.0))):
        text = normalize_text_spacing(str(line.get("text") or ""))
        if not text:
            continue
        if text.startswith("•"):
            if current:
                items.append(normalize_text_spacing(" ".join(current)))
            current = [text.lstrip("•").strip()]
        elif current:
            current.append(text)
        else:
            current = [text]
    if current:
        items.append(normalize_text_spacing(" ".join(current)))
    return items


def build_question_callout_block(title_line: dict, content_lines: list[dict], body_size: float) -> dict:
    sorted_content = sorted(content_lines, key=lambda item: float(item.get("x0") or 0.0))
    centers = [
        (float(line.get("x0") or 0.0) + float(line.get("x1") or 0.0)) / 2
        for line in sorted_content
    ]
    split_at = None
    if len(centers) >= 2:
        gaps = [(centers[index] - centers[index - 1], index) for index in range(1, len(centers))]
        largest_gap, gap_index = max(gaps, key=lambda item: item[0])
        if largest_gap >= body_size * 2.5:
            split_at = gap_index

    if split_at is None:
        columns = [sorted_content]
    else:
        columns = [sorted_content[:split_at], sorted_content[split_at:]]
        columns.sort(
            key=lambda column: sum(float(line.get("x1") or 0.0) for line in column) / max(len(column), 1),
            reverse=True,
        )

    column_html: list[str] = []
    plain_lines = [dict(title_line)]
    plain_lines[0]["text"] = normalize_text_spacing(str(title_line.get("text") or ""))
    for column in columns:
        items = question_column_items(column)
        for item in items:
            plain_lines.append({"text": item, "top": title_line["top"], "bottom": title_line["bottom"]})
        items_html = "".join(f"<li>{text_with_bidi_isolates(item)}</li>" for item in items)
        column_html.append(f"<ul>{items_html}</ul>")

    title = normalize_text_spacing(str(title_line.get("text") or ""))
    top = min(float(line.get("top") or 0.0) for line in [title_line] + content_lines)
    bottom = max(float(line.get("bottom") or line.get("top") or 0.0) for line in [title_line] + content_lines)
    return {
        "tag": "aside",
        "class": "question-box",
        "top": top,
        "bottom": bottom,
        "reading_top": top,
        "lines": plain_lines,
        "html": (
            f"<h3>{text_with_bidi_isolates(title)}</h3>"
            f'<div class="question-columns">{"".join(column_html)}</div>'
        ),
    }


def extract_question_callouts(flow_lines: list[dict], visual_lines: list[dict], body_size: float) -> tuple[list[dict], list[dict]]:
    visual_text_lines = visual_lines_to_text_lines(visual_lines)
    callouts: list[dict] = []
    ranges: list[tuple[float, float]] = []

    for title_line in visual_text_lines:
        if not is_question_callout_title(str(title_line.get("text") or "")):
            continue
        title_top = float(title_line.get("top") or 0.0)
        candidates = [
            line
            for line in visual_text_lines
            if title_top < float(line.get("top") or 0.0) <= title_top + body_size * 22
        ]
        candidates.sort(key=lambda item: float(item.get("top") or 0.0))
        content_lines: list[dict] = []
        previous_bottom = float(title_line.get("bottom") or title_top)
        for line in candidates:
            gap = float(line.get("top") or 0.0) - previous_bottom
            if content_lines and gap > body_size * 3.0:
                break
            content_lines.append(line)
            previous_bottom = max(previous_bottom, float(line.get("bottom") or line.get("top") or previous_bottom))
        if not content_lines:
            continue
        block = build_question_callout_block(title_line, content_lines, body_size)
        callouts.append(block)
        ranges.append((float(block["top"]) - body_size * 0.4, float(block["bottom"]) + body_size * 0.4))

    if not ranges:
        return flow_lines, []

    remaining = [
        line
        for line in flow_lines
        if not any(start <= float(line.get("top") or 0.0) <= end for start, end in ranges)
    ]
    return remaining, callouts


def block_bounds(block: dict, page_width: float) -> tuple[float, float, float, float]:
    lines = block.get("lines", [])
    tops = [float(line.get("top") or 0.0) for line in lines]
    bottoms = [float(line.get("bottom") or line.get("top") or 0.0) for line in lines]
    x0_values = [float(line.get("x0") or 0.0) for line in lines if float(line.get("x1") or 0.0) > float(line.get("x0") or 0.0)]
    x1_values = [float(line.get("x1") or 0.0) for line in lines if float(line.get("x1") or 0.0) > float(line.get("x0") or 0.0)]
    top = min(tops) if tops else float(block.get("top") or 0.0)
    bottom = max(bottoms) if bottoms else float(block.get("bottom") or top)
    x0 = min(x0_values) if x0_values else 0.0
    x1 = max(x1_values) if x1_values else page_width
    return x0, top, x1, bottom


def horizontal_zone(x0: float, x1: float, page_width: float) -> str:
    if page_width <= 0:
        return "unknown"
    width_ratio = (x1 - x0) / page_width
    center_ratio = ((x0 + x1) / 2) / page_width
    if width_ratio >= 0.64:
        return "full"
    if center_ratio >= 0.55:
        return "right"
    if center_ratio <= 0.45:
        return "left"
    return "center"


def layout_region_record(region_id: str, kind: str, block: dict, page_width: float, order: int) -> dict:
    x0, top, x1, bottom = block_bounds(block, page_width)
    record = {
        "id": region_id,
        "kind": kind,
        "order": order,
        "top": round(top, 2),
        "bottom": round(bottom, 2),
        "x0": round(x0, 2),
        "x1": round(x1, 2),
        "zone": horizontal_zone(x0, x1, page_width),
        "text": block_plain_text(block),
        "line_count": len(block.get("lines", [])),
    }
    if block.get("class") == "question-box":
        record["internal_flow"] = "rtl_columns"
    return record


def table_region_record(table: dict, page_width: float, order: int) -> dict:
    x0 = float(table.get("x0") or 0.0)
    x1 = float(table.get("x1") or page_width)
    return {
        "id": f"table-{int(table.get('index') or order)}",
        "kind": "table",
        "order": order,
        "top": round(float(table.get("top") or 0.0), 2),
        "bottom": round(float(table.get("bottom") or table.get("top") or 0.0), 2),
        "x0": round(x0, 2),
        "x1": round(x1, 2),
        "zone": horizontal_zone(x0, x1, page_width),
        "rows": int(table.get("rows") or 0),
        "cols": int(table.get("cols") or 0),
        "reconstructed": bool(table.get("reconstructed")),
    }


def box_overlap_ratio(box: tuple[float, float, float, float], other: tuple[float, float, float, float], axis: str) -> float:
    if axis == "x":
        start = max(box[0], other[0])
        end = min(box[2], other[2])
        span = min(max(1.0, box[2] - box[0]), max(1.0, other[2] - other[0]))
    else:
        start = max(box[1], other[1])
        end = min(box[3], other[3])
        span = min(max(1.0, box[3] - box[1]), max(1.0, other[3] - other[1]))
    return max(0.0, end - start) / span


def box_edge_distance(box: tuple[float, float, float, float], other: tuple[float, float, float, float]) -> float:
    dx = max(other[0] - box[2], box[0] - other[2], 0.0)
    dy = max(other[1] - box[3], box[1] - other[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def content_image_records(
    images: list[dict],
    page_width: float,
    page_height: float,
    crop_bounds: tuple[float, float, float, float] | None = None,
) -> list[dict]:
    records: list[dict] = []
    page_area = max(1.0, page_width * page_height)
    crop_left = float(crop_bounds[0]) if crop_bounds else 0.0
    layout_width = layout_width_for_bounds(page_width, crop_bounds)
    for raw_index, image in enumerate(images, start=1):
        x0 = float(image.get("x0") or 0.0)
        x1 = float(image.get("x1") or 0.0)
        top = float(image.get("top") or 0.0)
        bottom = float(image.get("bottom") or 0.0)
        if crop_bounds is not None and not bbox_midpoint_in_bounds(x0, top, x1, bottom, crop_bounds):
            continue
        width = max(0.0, x1 - x0)
        height = max(0.0, bottom - top)
        area = width * height
        if area < 8000 or area >= page_area * 0.70:
            continue
        layout_x0 = x0 - crop_left
        layout_x1 = x1 - crop_left
        records.append(
            {
                "id": f"image-{len(records) + 1}",
                "source_index": raw_index,
                "kind": "image",
                "source_box": [round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)],
                "top": round(top, 2),
                "bottom": round(bottom, 2),
                "x0": round(layout_x0, 2),
                "x1": round(layout_x1, 2),
                "width": round(width, 2),
                "height": round(height, 2),
                "zone": horizontal_zone(layout_x0, layout_x1, layout_width),
            }
        )
    return records


def image_box(image: dict) -> tuple[float, float, float, float]:
    return (
        float(image.get("x0") or 0.0),
        float(image.get("top") or 0.0),
        float(image.get("x1") or 0.0),
        float(image.get("bottom") or 0.0),
    )


def caption_candidate_records(secondary_blocks: list[dict], page_width: float) -> list[dict]:
    candidates: list[dict] = []
    for index, block in enumerate(secondary_blocks, start=1):
        x0, top, x1, bottom = block_bounds(block, page_width)
        text = block_plain_text(block)
        caption_id = f"caption-{index}"
        caption_type = classify_caption_candidate(text, len(block.get("lines", [])), x0, x1, page_width)
        block["id"] = caption_id
        block["caption_type"] = caption_type
        candidates.append(
            {
                "id": caption_id,
                "box": (x0, top, x1, bottom),
                "text": text,
                "line_count": len(block.get("lines", [])),
                "zone": horizontal_zone(x0, x1, page_width),
                "caption_type": caption_type,
            }
        )
    return candidates


def caption_starts_like_figure(text: str) -> bool:
    stripped = normalize_text_spacing(text)
    return bool(re.match(r"^(תרשים|איור|טבלה)\s+\d", stripped))


def classify_caption_candidate(text: str, line_count: int, x0: float, x1: float, page_width: float) -> str:
    normalized = normalize_text_spacing(text)
    text_length = len(normalized)
    width_ratio = max(0.0, x1 - x0) / max(1.0, page_width)
    if caption_starts_like_figure(normalized):
        return "figure_title" if line_count <= 1 and text_length <= 55 else "full_caption"
    if width_ratio <= 0.28 and line_count >= 3:
        return "sidebar"
    if line_count <= 2 and text_length <= 55:
        return "local_label"
    if line_count >= 3 or text_length >= 90:
        return "full_caption"
    return "unknown"


def score_image_caption_match(image: dict, caption: dict, page_width: float) -> dict:
    image_bounds = image_box(image)
    caption_bounds = caption["box"]
    x_overlap = box_overlap_ratio(image_bounds, caption_bounds, "x")
    y_overlap = box_overlap_ratio(image_bounds, caption_bounds, "y")
    distance = box_edge_distance(image_bounds, caption_bounds)
    max_near = max(24.0, page_width * 0.18)
    closeness = max(0.0, 1.0 - min(distance, max_near) / max_near)
    caption_width = max(1.0, caption_bounds[2] - caption_bounds[0])
    image_width = max(1.0, image_bounds[2] - image_bounds[0])
    figure_marker = caption_starts_like_figure(caption.get("text", ""))
    source_caption_type = str(caption.get("caption_type") or "unknown")
    caption_type = source_caption_type
    caption_side = ""
    geometry_confirmed = False
    edge_tolerance = max(12.0, page_width * 0.025)
    edge_aligned = min(
        abs(caption_bounds[0] - image_bounds[0]),
        abs(caption_bounds[2] - image_bounds[2]),
    ) <= edge_tolerance

    if caption_bounds[1] >= image_bounds[3] and x_overlap >= 0.18:
        reason = "below_overlap"
        geometry_confirmed = x_overlap >= 0.55 and distance <= max(32.0, page_width * 0.08)
        confidence = 0.58 + 0.25 * x_overlap + 0.17 * closeness
    elif caption_bounds[3] <= image_bounds[1] and x_overlap >= 0.18:
        reason = "above_overlap"
        narrow_edge_caption = (
            source_caption_type == "sidebar"
            and int(caption.get("line_count") or 0) >= 3
            and edge_aligned
        )
        geometry_confirmed = (
            x_overlap >= 0.55
            and distance <= max(24.0, page_width * 0.05)
            and (caption_width >= image_width * 0.45 or figure_marker or narrow_edge_caption)
        )
        width_penalty = 0.30 if caption_width < image_width * 0.45 and not figure_marker and not geometry_confirmed else 0.0
        confidence = 0.36 + 0.20 * x_overlap + 0.14 * closeness - width_penalty
    elif y_overlap >= 0.14 and distance <= max(page_width * 0.28, 36.0):
        reason = "side_overlap"
        caption_side = "left" if (caption_bounds[0] + caption_bounds[2]) < (image_bounds[0] + image_bounds[2]) else "right"
        geometry_confirmed = y_overlap >= 0.65 and distance <= max(28.0, page_width * 0.08)
        confidence = 0.52 + 0.25 * y_overlap + 0.15 * closeness
    elif figure_marker and distance <= page_width * 0.40:
        reason = "group_caption"
        confidence = 0.50 + 0.20 * closeness
    else:
        reason = "low_confidence_nearest"
        confidence = 0.20 + 0.25 * closeness + 0.10 * max(x_overlap, y_overlap)

    if source_caption_type == "sidebar" and geometry_confirmed:
        caption_type = "full_caption"

    confidence += {
        "full_caption": 0.10,
        "figure_title": 0.08,
        "local_label": 0.0,
        "unknown": 0.0,
        "sidebar": -0.28,
    }.get(caption_type, 0.0)
    confidence = max(0.0, min(0.99, confidence))
    if confidence < 0.45:
        reason = f"low_confidence_{reason}"
    return {
        "caption_id": caption["id"],
        "caption_text": caption.get("text", ""),
        "caption_type": caption_type,
        "source_caption_type": source_caption_type,
        "distance": round(distance, 2),
        "x_overlap": round(x_overlap, 2),
        "y_overlap": round(y_overlap, 2),
        "match_confidence": round(confidence, 2),
        "match_reason": reason,
        "caption_side": caption_side,
        "geometry_confirmed": geometry_confirmed,
        "edge_aligned": edge_aligned,
    }


def attach_image_caption_matches(images: list[dict], caption_candidates: list[dict], page_width: float) -> None:
    ambiguous_candidates = len(caption_candidates) >= 6
    for image in images:
        if ambiguous_candidates:
            image["ambiguous_caption_candidates"] = True
        if not caption_candidates:
            image["caption_match"] = None
            image["match_confidence"] = 0.0
            image["match_reason"] = "no_caption_candidates"
            continue
        matches = [score_image_caption_match(image, caption, page_width) for caption in caption_candidates]
        best = max(matches, key=lambda item: (item["match_confidence"], -item["distance"]))
        if ambiguous_candidates and best["match_confidence"] >= 0.75 and not caption_starts_like_figure(best.get("caption_text", "")):
            best = dict(best)
            best["match_confidence"] = round(max(0.0, best["match_confidence"] - 0.18), 2)
            best["match_reason"] = f"ambiguous_{best['match_reason']}"
        image["caption_match"] = best
        image["nearest_caption_id"] = best["caption_id"]
        image["match_confidence"] = best["match_confidence"]
        image["match_reason"] = best["match_reason"]


def recover_image_caption_continuations(
    images: list[dict],
    caption_candidates: list[dict],
    body_blocks: list[dict],
    secondary_blocks: list[dict],
    page_width: float,
    page_height: float,
) -> None:
    """Promote geometry-confirmed captions and join tiny trailing fragments."""
    for index, block in enumerate(body_blocks, start=1):
        block.setdefault("id", f"body-block-{index}")
    secondary_by_id: dict[str, dict] = {}
    for index, block in enumerate(secondary_blocks, start=1):
        block.setdefault("id", f"caption-{index}")
        secondary_by_id[str(block["id"])] = block
    candidate_by_id = {str(candidate.get("id") or ""): candidate for candidate in caption_candidates}

    for image in images:
        match = image.get("caption_match") or {}
        caption_id = str(match.get("caption_id") or "")
        caption_block = secondary_by_id.get(caption_id)
        match_reason = str(match.get("match_reason") or "")
        if (
            not caption_block
            or str(match.get("caption_type") or "") not in {"full_caption", "figure_title"}
            or float(match.get("match_confidence") or 0.0) < IMAGE_CAPTION_CONFIDENCE_THRESHOLD
            or not bool(match.get("geometry_confirmed"))
            or not any(location in match_reason for location in ("side_overlap", "below_overlap", "above_overlap"))
        ):
            continue

        caption_block["caption_type"] = "full_caption"
        candidate = candidate_by_id.get(caption_id)
        if candidate is not None:
            candidate["caption_type"] = "full_caption"
        image["caption_complete"] = True
        image["caption_source_blocks"] = [caption_id]
        image["caption_merge_reason"] = "geometry_confirmed_caption"
        caption_bounds = block_bounds(caption_block, page_width)
        current_bottom = caption_bounds[3]
        caption_parts = [block_plain_text(caption_block)]
        source_blocks = [caption_id]
        max_gap = max(12.0, page_height * 0.02)
        for block in sorted(body_blocks, key=lambda item: float(item.get("top") or 0.0)):
            if block.get("consumed_by_figure_group") or block.get("consumed_by_image"):
                continue
            bounds = block_bounds(block, page_width)
            gap = bounds[1] - current_bottom
            text = block_plain_text(block)
            if gap < -2.0 or gap > max_gap:
                continue
            if box_overlap_ratio(caption_bounds, bounds, "x") < 0.65:
                continue
            if len(block.get("lines", [])) > 2 or len(text) > 80 or not text:
                continue
            if caption_parts[-1].rstrip().endswith((".", "!", "?", ":", ";")):
                break
            caption_parts.append(text)
            source_blocks.append(str(block.get("id") or ""))
            block["consumed_by_image"] = str(image.get("id") or "")
            caption_block.setdefault("lines", []).extend(block.get("lines", []))
            caption_block["bottom"] = max(float(caption_block.get("bottom") or 0.0), bounds[3])
            current_bottom = bounds[3]

        if len(caption_parts) == 1:
            continue
        recovered_text = normalize_text_spacing(" ".join(caption_parts))
        location = next(location for location in ("side_overlap", "below_overlap", "above_overlap") if location in match_reason)
        prefix = "ambiguous_" if match_reason.startswith("ambiguous_") else ""
        recovered_reason = f"{prefix}{location}_with_continuation"
        match["caption_text"] = recovered_text
        match["match_reason"] = recovered_reason
        image["caption_match"] = match
        image["match_reason"] = recovered_reason
        image["caption_complete"] = True
        image["caption_source_blocks"] = source_blocks
        image["caption_merge_reason"] = "geometry_confirmed_caption_with_adjacent_body_fragment"
        if candidate is not None:
            candidate["text"] = recovered_text


def image_regions_are_neighbors(first: dict, second: dict, page_width: float, page_height: float) -> bool:
    first_box = image_box(first)
    second_box = image_box(second)
    x_overlap = box_overlap_ratio(first_box, second_box, "x")
    y_overlap = box_overlap_ratio(first_box, second_box, "y")
    horizontal_gap = max(0.0, max(first_box[0], second_box[0]) - min(first_box[2], second_box[2]))
    vertical_gap = max(0.0, max(first_box[1], second_box[1]) - min(first_box[3], second_box[3]))
    horizontal_neighbors = y_overlap >= 0.55 and horizontal_gap <= max(18.0, page_width * 0.035)
    vertical_neighbors = x_overlap >= 0.55 and vertical_gap <= max(24.0, page_height * 0.06)
    return horizontal_neighbors or vertical_neighbors


def figure_group_record(
    group_index: int,
    members: list[dict],
    caption_candidates: list[dict],
    page_width: float,
    grouping_reason: str,
) -> dict:
    x0 = min(image_box(image)[0] for image in members)
    top = min(image_box(image)[1] for image in members)
    x1 = max(image_box(image)[2] for image in members)
    bottom = max(image_box(image)[3] for image in members)
    source_boxes = [image.get("source_box") for image in members if image.get("source_box")]
    source_box = [
        round(min(float(box[0]) for box in source_boxes), 2),
        round(min(float(box[1]) for box in source_boxes), 2),
        round(max(float(box[2]) for box in source_boxes), 2),
        round(max(float(box[3]) for box in source_boxes), 2),
    ] if source_boxes else [round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)]
    group: dict = {
        "id": f"figure-group-{group_index}",
        "kind": "figure_group",
        "source_box": source_box,
        "top": round(top, 2),
        "bottom": round(bottom, 2),
        "x0": round(x0, 2),
        "x1": round(x1, 2),
        "width": round(x1 - x0, 2),
        "height": round(bottom - top, 2),
        "zone": horizontal_zone(x0, x1, page_width),
        "member_image_ids": [str(image.get("id") or "") for image in members],
        "member_source_indexes": [int(image.get("source_index") or 0) for image in members],
        "member_count": len(members),
        "grouping_reason": grouping_reason,
    }

    # A shared caption normally sits outside the complete gallery bounds. Local labels
    # between individual panels are intentionally excluded from this group-level pass.
    member_caption_counts: dict[str, int] = {}
    for image in members:
        caption_id = str(image.get("nearest_caption_id") or "")
        if caption_id:
            member_caption_counts[caption_id] = member_caption_counts.get(caption_id, 0) + 1
    outside_candidates = [
        caption
        for caption in caption_candidates
        if (
            float(caption["box"][1]) >= bottom
            or float(caption["box"][3]) <= top
            or float(caption["box"][0]) >= x1
            or float(caption["box"][2]) <= x0
        )
        and (
            str(caption.get("id") or "") not in member_caption_counts
            or member_caption_counts[str(caption.get("id") or "")] >= 2
            or caption_starts_like_figure(str(caption.get("text") or ""))
        )
        and (
            str(caption.get("caption_type") or "unknown") not in {"local_label", "sidebar"}
            or member_caption_counts.get(str(caption.get("id") or ""), 0) >= 2
        )
    ]
    if outside_candidates:
        matches = [score_image_caption_match(group, caption, page_width) for caption in outside_candidates]
        best = max(
            matches,
            key=lambda item: (
                {"full_caption": 3, "figure_title": 2, "unknown": 1}.get(str(item.get("caption_type") or "unknown"), 0),
                item["match_confidence"],
                -item["distance"],
            ),
        )
        group["caption_match"] = best
        group["nearest_caption_id"] = best["caption_id"]
        group["match_confidence"] = best["match_confidence"]
        group["match_reason"] = f"group_{best['match_reason']}"
    else:
        group["caption_match"] = None
        group["match_confidence"] = 0.0
        group["match_reason"] = "no_group_caption_candidates"
    group_caption_id = str((group.get("caption_match") or {}).get("caption_id") or "")
    local_labels = [
        {
            "caption_id": str(caption.get("id") or ""),
            "text": str(caption.get("text") or ""),
            "box": [round(float(value), 2) for value in caption.get("box", ())],
            "caption_type": str(caption.get("caption_type") or "local_label"),
        }
        for caption in sorted(caption_candidates, key=lambda item: (float(item["box"][1]), -float(item["box"][0])))
        if str(caption.get("id") or "") != group_caption_id
        and str(caption.get("caption_type") or "unknown") == "local_label"
        and float(caption["box"][0]) <= x1 + 12.0
        and float(caption["box"][2]) >= x0 - 12.0
        and float(caption["box"][1]) <= bottom + 12.0
        and float(caption["box"][3]) >= top - 12.0
    ]
    if local_labels:
        group["local_labels"] = local_labels
    return group


def cluster_image_regions(
    images: list[dict],
    caption_candidates: list[dict],
    page_width: float,
    page_height: float,
) -> list[dict]:
    if len(images) < 2:
        return []

    adjacency: list[set[int]] = [set() for _ in images]
    for first_index, first in enumerate(images):
        for second_index in range(first_index + 1, len(images)):
            if image_regions_are_neighbors(first, images[second_index], page_width, page_height):
                adjacency[first_index].add(second_index)
                adjacency[second_index].add(first_index)

    components: list[list[dict]] = []
    visited: set[int] = set()
    for start in range(len(images)):
        if start in visited:
            continue
        stack = [start]
        indexes: list[int] = []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            indexes.append(current)
            stack.extend(adjacency[current] - visited)
        if len(indexes) >= 2:
            components.append([images[index] for index in sorted(indexes)])

    groups: list[dict] = []
    for members in components:
        caption_counts: dict[str, int] = {}
        for image in members:
            caption_id = str(image.get("nearest_caption_id") or "")
            if caption_id:
                caption_counts[caption_id] = caption_counts.get(caption_id, 0) + 1
        shared_caption = max(caption_counts.values(), default=0) >= 2
        ambiguous = any(image.get("ambiguous_caption_candidates") for image in members)
        dense_gallery = len(members) >= 4
        independently_captioned = all(
            float(image.get("match_confidence") or 0.0) >= 0.85
            and not image.get("ambiguous_caption_candidates")
            for image in members
        ) and len({str(image.get("nearest_caption_id") or "") for image in members}) == len(members)
        if independently_captioned or not (shared_caption or ambiguous or dense_gallery):
            continue
        grouping_reason = "shared_caption" if shared_caption else "dense_gallery" if dense_gallery else "ambiguous_nearby_images"
        group = figure_group_record(len(groups) + 1, members, caption_candidates, page_width, grouping_reason)
        groups.append(group)
        for image in members:
            image["group_candidate"] = True
            image["figure_group_id"] = group["id"]
    return groups


def recover_figure_group_captions(
    figure_groups: list[dict],
    body_blocks: list[dict],
    secondary_blocks: list[dict],
    page_width: float,
    page_height: float,
) -> None:
    for index, block in enumerate(body_blocks, start=1):
        block.setdefault("id", f"body-block-{index}")
    for index, block in enumerate(secondary_blocks, start=1):
        block.setdefault("id", f"caption-{index}")

    for group in figure_groups:
        existing_match = group.get("caption_match") or {}
        if (
            str(existing_match.get("caption_type") or "") in {"full_caption", "figure_title"}
            and float(group.get("match_confidence") or 0.0) >= IMAGE_CAPTION_CONFIDENCE_THRESHOLD
        ):
            group["caption_complete"] = True
            group["caption_source_blocks"] = [str(existing_match.get("caption_id") or "")]
            group["caption_merge_reason"] = "direct_full_caption_match"
            continue

        group_bounds = image_box(group)
        max_distance = max(42.0, min(page_width * 0.22, page_height * 0.14))
        anchors: list[tuple[float, dict, tuple[float, float, float, float]]] = []
        for block in body_blocks:
            if block.get("consumed_by_figure_group"):
                continue
            text = block_plain_text(block)
            if not caption_starts_like_figure(text):
                continue
            bounds = block_bounds(block, page_width)
            distance = box_edge_distance(group_bounds, bounds)
            x_overlap = box_overlap_ratio(group_bounds, bounds, "x")
            y_overlap = box_overlap_ratio(group_bounds, bounds, "y")
            if distance <= max_distance and max(x_overlap, y_overlap) >= 0.18:
                anchors.append((distance, block, bounds))
        if not anchors:
            group["caption_complete"] = False
            continue

        _, anchor, anchor_bounds = min(anchors, key=lambda item: (item[0], float(item[2][1])))
        source_blocks = [str(anchor.get("id") or "")]
        caption_parts = [block_plain_text(anchor)]
        last_bottom = anchor_bounds[3]
        continuation_gap = max(16.0, page_height * 0.025)
        for block in sorted(secondary_blocks, key=lambda item: float(item.get("top") or 0.0)):
            if block.get("consumed_by_figure_group"):
                continue
            bounds = block_bounds(block, page_width)
            gap = bounds[1] - last_bottom
            if gap < -2.0 or gap > continuation_gap:
                continue
            if box_overlap_ratio(anchor_bounds, bounds, "x") < 0.18:
                continue
            text = block_plain_text(block)
            if not text or caption_starts_like_figure(text) or str(block.get("caption_type") or "") == "sidebar":
                continue
            caption_parts.append(text)
            source_blocks.append(str(block.get("id") or ""))
            block["consumed_by_figure_group"] = str(group.get("id") or "")
            last_bottom = bounds[3]

        recovered_text = normalize_text_spacing(" ".join(caption_parts))
        recovered_id = f"recovered-{group.get('id', 'figure-group')}-caption"
        anchor["consumed_by_figure_group"] = str(group.get("id") or "")
        anchor["caption_type"] = "full_caption"
        group["caption_match"] = {
            "caption_id": recovered_id,
            "caption_text": recovered_text,
            "caption_type": "full_caption",
            "distance": round(box_edge_distance(group_bounds, anchor_bounds), 2),
            "x_overlap": round(box_overlap_ratio(group_bounds, anchor_bounds, "x"), 2),
            "y_overlap": round(box_overlap_ratio(group_bounds, anchor_bounds, "y"), 2),
            "match_confidence": 0.96,
            "match_reason": "recovered_body_caption",
        }
        group["nearest_caption_id"] = recovered_id
        group["match_confidence"] = 0.96
        group["match_reason"] = "recovered_body_caption"
        group["caption_source_blocks"] = source_blocks
        group["caption_complete"] = True
        group["caption_merge_reason"] = "figure_marker_body_block_with_adjacent_continuation"


def render_pdf_page_to_png(pdf_path: Path, page_number: int, output_dir: Path, dpi: int = IMAGE_RENDER_DPI) -> Path:
    if not PDFTOPPM.exists():
        raise FileNotFoundError(f"pdftoppm was not found: {PDFTOPPM}")
    prefix = output_dir / f"page-{page_number}"
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
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pdftoppm failed")
    return prefix.with_suffix(".png")


def validate_image_quality(value: int) -> int:
    quality = int(value)
    if not 35 <= quality <= 95:
        raise ValueError("Image quality must be between 35 and 95")
    return quality


def validate_image_max_width(value: int) -> int:
    width = int(value)
    if not 240 <= width <= 2000:
        raise ValueError("Image maximum width must be between 240 and 2000 pixels")
    return width


def image_crop_data_uri(
    rendered_page_path: Path,
    page_width: float,
    page_height: float,
    source_box: list[float],
    *,
    quality: int = IMAGE_JPEG_QUALITY,
    max_width: int = IMAGE_MAX_EMBED_WIDTH,
) -> str:
    with Image.open(rendered_page_path) as rendered:
        scale_x = rendered.width / max(1.0, page_width)
        scale_y = rendered.height / max(1.0, page_height)
        x0, top, x1, bottom = [float(value) for value in source_box]
        left = max(0, min(rendered.width, int(x0 * scale_x)))
        upper = max(0, min(rendered.height, int(top * scale_y)))
        right = max(left + 1, min(rendered.width, int(x1 * scale_x)))
        lower = max(upper + 1, min(rendered.height, int(bottom * scale_y)))
        crop = rendered.crop((left, upper, right, lower)).convert("RGB")
        if crop.width > max_width:
            ratio = max_width / crop.width
            crop = crop.resize((max_width, max(1, int(crop.height * ratio))), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def embed_page_images(
    pdf_path: Path,
    pages: list[dict],
    *,
    quality: int = IMAGE_JPEG_QUALITY,
    max_width: int = IMAGE_MAX_EMBED_WIDTH,
    progress_callback=None,
) -> None:
    quality = validate_image_quality(quality)
    max_width = validate_image_max_width(max_width)
    pages_with_images = [page for page in pages if page.get("image_regions")]
    if not pages_with_images:
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        for page_index, page in enumerate(pages_with_images, start=1):
            if progress_callback:
                progress_callback(f"Rendering images for page {page['number']} ({page_index}/{len(pages_with_images)})")
            rendered_page = render_pdf_page_to_png(pdf_path, int(page["number"]), tmp_path)
            for image in page.get("image_regions", []):
                source_box = image.get("source_box")
                if not source_box:
                    continue
                image["src"] = image_crop_data_uri(
                    rendered_page,
                    float(page.get("source_width") or page.get("width") or 1.0),
                    float(page.get("source_height") or page.get("height") or 1.0),
                    source_box,
                    quality=quality,
                    max_width=max_width,
                )


def layout_item_bounds(item: dict, kind: str, page_width: float) -> tuple[float, float, float, float]:
    if kind == "table":
        return (
            float(item.get("x0") or 0.0),
            float(item.get("top") or 0.0),
            float(item.get("x1") or page_width),
            float(item.get("bottom") or item.get("top") or 0.0),
        )
    if kind in {"image", "figure_group"}:
        return image_box(item)
    return block_bounds(item, page_width)


def sort_layout_items(items: list[tuple[str, dict]], page_width: float, body_size: float) -> list[tuple[str, dict]]:
    if not items:
        return []

    def top_of(entry: tuple[str, dict]) -> float:
        kind, item = entry
        return float(item.get("reading_top", item.get("top", 0.0))) if kind != "table" else float(item.get("top") or 0.0)

    ordered = sorted(items, key=top_of)
    groups: list[list[tuple[str, dict]]] = []
    tolerance = max(body_size * 1.35, 12.0)
    for entry in ordered:
        top = top_of(entry)
        if not groups or top - min(top_of(group_item) for group_item in groups[-1]) > tolerance:
            groups.append([entry])
        else:
            groups[-1].append(entry)

    zone_rank = {"right": 0, "full": 1, "center": 1, "unknown": 1, "left": 2}
    kind_rank = {"body": 0, "question": 1, "caption": 2, "figure_group": 3, "image": 3, "table": 4}
    result: list[tuple[str, dict]] = []
    for group in groups:
        if len(group) == 1:
            result.extend(group)
            continue

        def group_key(entry: tuple[str, dict]) -> tuple[int, int, float]:
            kind, item = entry
            x0, _, x1, _ = layout_item_bounds(item, kind, page_width)
            zone = horizontal_zone(x0, x1, page_width)
            return zone_rank.get(zone, 1), kind_rank.get(kind, 9), top_of(entry)

        result.extend(sorted(group, key=group_key))
    return result


def build_layout_regions(body_blocks: list[dict], secondary_blocks: list[dict], tables: list[dict], page_width: float, image_regions: list[dict] | None = None, figure_groups: list[dict] | None = None) -> list[dict]:
    items: list[tuple[str, dict]] = []
    for block in body_blocks:
        if block.get("consumed_by_figure_group") or block.get("consumed_by_image"):
            continue
        kind = "question" if block.get("class") == "question-box" else "body"
        items.append((kind, block))
    for block in secondary_blocks:
        if block.get("consumed_by_figure_group") or block.get("consumed_by_image"):
            continue
        items.append(("caption", block))
    for table in tables:
        if table.get("reconstructed") or (table.get("data") and table.get("cols", 0) > 1):
            items.append(("table", table))
    grouped_image_ids = {
        str(image_id)
        for group in figure_groups or []
        for image_id in group.get("member_image_ids", [])
    }
    for image in image_regions or []:
        if str(image.get("id") or "") not in grouped_image_ids:
            items.append(("image", image))
    for group in figure_groups or []:
        items.append(("figure_group", group))

    regions: list[dict] = []
    body_size = dominant_number(
        [
            float(line.get("size") or 0.0)
            for _, item in items
            if isinstance(item, dict)
            for line in item.get("lines", [])
        ],
        fallback=12.0,
    )
    for order, (kind, item) in enumerate(sort_layout_items(items, page_width, body_size), start=1):
        if kind == "table":
            regions.append(table_region_record(item, page_width, order))
        elif kind in {"image", "figure_group"}:
            record = dict(item)
            record["order"] = order
            regions.append(record)
        else:
            regions.append(layout_region_record(f"{kind}-{order}", kind, item, page_width, order))
    return regions


def block_plain_text(block: dict) -> str:
    return normalize_text_spacing(" ".join(str(line.get("text") or "") for line in block.get("lines", [])))


def block_ends_sentence(block: dict) -> bool:
    text = block_plain_text(block).rstrip()
    if not text:
        return True
    return text[-1] in ".!?:;؟׃"


def assign_secondary_reading_order(body_blocks: list[dict], secondary_blocks: list[dict], body_size: float) -> None:
    ordered_body = sorted(body_blocks, key=lambda block: float(block["top"]))
    for block in secondary_blocks:
        top = float(block["top"])
        reading_top = top
        previous_body = next((candidate for candidate in reversed(ordered_body) if float(candidate["top"]) <= top), None)
        following_body = next((candidate for candidate in ordered_body if float(candidate["top"]) > top), None)

        if previous_body and top <= float(previous_body["bottom"]):
            reading_top = float(previous_body["bottom"]) + 0.01
        elif previous_body and following_body:
            gap = float(following_body["top"]) - float(previous_body["bottom"])
            block_height = float(block["bottom"]) - top
            max_interruption_gap = max(body_size * 8.0, block_height + body_size * 4.0)
            if gap <= max_interruption_gap or not block_ends_sentence(previous_body):
                reading_top = float(following_body["bottom"]) + 0.01

        block["reading_top"] = reading_top


def clean_table_cell(value: object) -> str:
    if value is None:
        return ""
    return clean_extracted_text_line(str(value).replace("\n", " "))


def table_data_to_html(rows: list[list[str]], estimated: bool = False) -> str:
    if not rows:
        return ""

    max_cols = max((len(row) for row in rows), default=0)
    colgroup = ""
    if max_cols == 3:
        colgroup = (
            "<colgroup>"
            '<col class="stage-col">'
            '<col class="period-col">'
            '<col class="description-col">'
            "</colgroup>"
        )

    rendered_rows: list[str] = []
    headers = rows[0] if rows else []
    for row_index, row in enumerate(rows):
        tag = "th" if row_index == 0 else "td"
        row_attr = f' data-row="{row_index}"' if row_index > 0 else ""
        cells = ""
        for cell_index, cell in enumerate(row):
            label = headers[cell_index] if row_index > 0 and cell_index < len(headers) else ""
            label_attr = f' data-label="{html.escape(label)}"' if label else ""
            cells += f"<{tag}{label_attr}>{html.escape(cell)}</{tag}>"
        rendered_rows.append(f"<tr{row_attr}>{cells}</tr>")

    estimate_class = " estimated" if estimated else ""
    return f'<div class="table-preview-wrap"><table class="detected-table{estimate_class}" dir="rtl">{colgroup}{"".join(rendered_rows)}</table></div>'


def is_table_period_line(text: str) -> bool:
    text = text.strip()
    return (
        text.startswith("מהלידה")
        or text.startswith("שנתיים")
        or text.startswith("שבע עד")
        or text.startswith("גיל")
        or text.startswith("ואילך")
    )


def is_short_stage_continuation(text: str) -> bool:
    words = text.split()
    return 1 <= len(words) <= 3 and not is_table_period_line(text)


def split_period_stage(text: str) -> tuple[str, str]:
    if "שלב" not in text:
        return text.strip(), ""
    before, after = text.split("שלב", 1)
    return before.strip(), f"שלב {after.strip()}".strip()


def reconstruct_stage_table(lines: list[dict]) -> list[list[str]] | None:
    texts = [line["text"].strip() for line in lines if line["text"].strip()]
    header_index = next(
        (
            index
            for index, text in enumerate(texts)
            if "תיאור" in text and "תקופת" in text and "שלב" in text
        ),
        -1,
    )
    if header_index == -1:
        return None

    content: list[str] = []
    for text in texts[header_index + 1 :]:
        if text.startswith("תרומתה") or text.startswith("ומגבלותיה"):
            break
        content.append(text)

    rows: list[list[str]] = [["שלב", "תקופת התפתחות", "תיאור"]]
    index = 0
    while index < len(content):
        description: list[str] = []
        while index < len(content) and not is_table_period_line(content[index]):
            description.append(content[index])
            index += 1

        if index >= len(content) or not description:
            break

        period_parts: list[str] = []
        stage_parts: list[str] = []
        while index < len(content):
            text = content[index]
            if "שלב" in text:
                period, stage = split_period_stage(text)
                if period:
                    period_parts.append(period)
                if stage:
                    stage_parts.append(stage)
                index += 1
                break
            period_parts.append(text)
            index += 1

        while index < len(content) and is_short_stage_continuation(content[index]):
            stage_parts.append(content[index])
            index += 1

        if description and (period_parts or stage_parts):
            rows.append(
                [
                    normalize_text_spacing(" ".join(stage_parts)),
                    normalize_text_spacing(" ".join(period_parts)),
                    normalize_text_spacing(" ".join(description)),
                ]
            )

    return rows if len(rows) > 1 else None


def split_delimited_table_line(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if "|" in value:
        parts = value.split("|")
    elif "\t" in value:
        parts = value.split("\t")
    else:
        parts = re.split(r"\s{3,}", value)
    cells = [clean_table_cell(part) for part in parts]
    return [cell for cell in cells if cell]


def reconstruct_delimited_text_table(lines: list[dict]) -> list[list[str]] | None:
    candidates: list[list[str]] = []
    for line in lines:
        variants = [str(line.get("raw_text") or ""), str(line.get("text") or "")]
        cells: list[str] = []
        for variant in variants:
            cells = split_delimited_table_line(variant)
            if len(cells) >= 2:
                break
        if len(cells) >= 2:
            candidates.append(cells)
            continue
        if candidates:
            break

    if len(candidates) < 2:
        return None

    column_count = max(set(len(row) for row in candidates), key=[len(row) for row in candidates].count)
    rows = [row for row in candidates if len(row) == column_count]
    if len(rows) < 2:
        return None
    return rows


def text_lines_to_records(text_lines: list[str]) -> list[dict]:
    records: list[dict] = []
    for index, text in enumerate(text_lines):
        top = index * 18.0
        records.append(
            {
                "text": clean_extracted_text_line(text),
                "raw_text": text,
                "top": top,
                "bottom": top + 12.0,
            }
        )
    return records


def reconstruct_table_from_matching_marker(marker_text: str, fallback_lines: list[dict] | None) -> list[list[str]] | None:
    if not fallback_lines:
        return None
    for index, line in enumerate(fallback_lines):
        text = line["text"].strip()
        if text != marker_text and marker_text not in text and text not in marker_text:
            continue
        following = fallback_lines[index : min(len(fallback_lines), index + 44)]
        reconstructed = reconstruct_stage_table(following) or reconstruct_delimited_text_table(following)
        if reconstructed and len(reconstructed) >= 3:
            return reconstructed
    return None


def detect_tables(page: pdfplumber.page.Page, crop_bounds: tuple[float, float, float, float] | None = None) -> list[dict]:
    detected: list[dict] = []
    try:
        tables = page.find_tables()
    except Exception:
        return detected

    for index, table in enumerate(tables, start=1):
        x0, top, x1, bottom = table.bbox
        if crop_bounds is not None and not bbox_midpoint_in_bounds(float(x0), float(top), float(x1), float(bottom), crop_bounds):
            continue
        rows = table.extract() or []
        normalized_rows = [[clean_table_cell(cell) for cell in row] for row in rows]
        column_count = max((len(row) for row in normalized_rows), default=0)
        sample_cells = [
            cell
            for row in normalized_rows[:2]
            for cell in row[:3]
            if cell
        ]
        detected.append(
            {
                "index": index,
                "top": float(top),
                "bottom": float(bottom),
                "x0": float(x0),
                "x1": float(x1),
                "rows": len(normalized_rows),
                "cols": column_count,
                "sample": " | ".join(sample_cells[:4]),
                "data": normalized_rows,
            }
        )
    return detected


def detect_text_table_markers(lines: list[dict], start_index: int = 1, fallback_lines: list[dict] | None = None) -> list[dict]:
    markers: list[dict] = []
    for index, line in enumerate(lines):
        text = line["text"].strip()
        if not re.search(r"\bטבלה\b", text):
            continue

        following = []
        for candidate in lines[index : min(len(lines), index + 44)]:
            candidate_text = candidate["text"].strip()
            if len(following) >= 6 and (candidate_text.startswith("תרומתה") or candidate_text.startswith("ומגבלותיה")):
                break
            following.append(candidate)
        if len(following) < 2:
            continue

        bottom = following[-1]["bottom"]

        reconstructed_rows = reconstruct_stage_table(following) or reconstruct_delimited_text_table(following)
        if not reconstructed_rows or len(reconstructed_rows) < 3:
            reconstructed_rows = reconstruct_table_from_matching_marker(text, fallback_lines)
        preview_rows = reconstructed_rows or [[line["text"]] for line in following]
        column_count = max((len(row) for row in preview_rows), default=0)

        markers.append(
            {
                "index": start_index + len(markers),
                "top": float(line["top"]),
                "bottom": float(bottom),
                "x0": 0.0,
                "x1": 0.0,
                "rows": len(preview_rows),
                "cols": column_count if reconstructed_rows else 0,
                "sample": text,
                "data": preview_rows,
                "estimated": True,
                "reconstructed": bool(reconstructed_rows),
            }
        )
    return markers


def line_overlaps_table(line: dict, tables: list[dict]) -> bool:
    line_mid = (float(line["top"]) + float(line["bottom"])) / 2
    line_text = canonical_match_text(str(line.get("text", "")))
    for table in tables:
        if not (table.get("reconstructed") or (table.get("data") and table.get("cols", 0) > 1)):
            continue
        if table.get("reconstructed") and line_text:
            table_variants = []
            for row in table.get("data", []):
                table_variants.append(" ".join(row))
                table_variants.append(" ".join(reversed(row)))
            for table_variant in table_variants:
                table_text = canonical_match_text(table_variant)
                if len(line_text) >= 4 and (line_text in table_text or table_text in line_text):
                    return True
        if float(table["top"]) <= line_mid <= float(table["bottom"]):
            return True
    return False


def extract_pdf_structured(
    pdf_path: Path,
    crop_settings: dict | None = None,
    page_numbers: set[int] | None = None,
    *,
    embed_images: bool = True,
    image_quality: int = IMAGE_JPEG_QUALITY,
    image_max_width: int = IMAGE_MAX_EMBED_WIDTH,
    progress_callback=None,
) -> tuple[list[dict], str, int, int]:
    pages: list[dict] = []
    all_lines: list[dict] = []
    crop_settings = normalize_crop_settings(crop_settings)
    crop_enabled = crop_settings_enabled(crop_settings)
    pypdf_reader = PdfReader(str(pdf_path))
    text_pages = extract_requested_text_pages(pypdf_reader, page_numbers)

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            if page_numbers and page_number not in page_numbers:
                continue
            crop_bounds = page_crop_bounds(float(page.width), float(page.height), page_number, crop_settings) if crop_enabled else None
            words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=True,
                extra_attrs=["fontname", "size"],
            )
            words = filter_words_by_crop(words, crop_bounds)
            layout_width = layout_width_for_bounds(float(page.width), crop_bounds)
            layout_words = normalize_words_to_layout_bounds(words, crop_bounds)
            lines = cluster_visual_lines(layout_words, layout_width)
            image_regions = content_image_records(page.images, float(page.width), float(page.height), crop_bounds)
            tables = detect_tables(page, crop_bounds)
            correct_text_lines = text_pages.get(page_number, [])
            if crop_enabled:
                corrected_lines = align_text_lines_to_style_lines(correct_text_lines, lines, require_visual_match=True) if correct_text_lines else visual_lines_to_text_lines(lines)
            else:
                corrected_lines = align_text_lines_to_style_lines(correct_text_lines, lines) if correct_text_lines else visual_lines_to_text_lines(lines)

            table_order_lines = text_lines_to_records(correct_text_lines)
            tables.extend(
                detect_text_table_markers(
                    corrected_lines,
                    start_index=len(tables) + 1,
                    fallback_lines=table_order_lines,
                )
            )
            pages.append(
                {
                    "number": page_number,
                    "width": layout_width,
                    "height": float(page.height),
                    "source_width": float(page.width),
                    "source_height": float(page.height),
                    "lines": corrected_lines,
                    "visual_lines": lines,
                    "tables": tables,
                    "image_regions": image_regions,
                }
            )
            all_lines.extend(corrected_lines)

    if page_numbers and not pages:
        raise ValueError(f"No pages matched the requested range: {page_range_label(page_numbers)}")

    body_size = dominant_number([line["size"] for line in all_lines])
    total_chars = sum(len(line["text"]) for line in all_lines)

    for page in pages:
        non_table_lines = [
            line
            for line in page["lines"]
            if not line_overlaps_table(line, page.get("tables", []))
        ]
        content_lines, noise_lines = split_noise_lines(non_table_lines, float(page.get("width") or 0.0), float(page.get("height") or 0.0), body_size)
        flow_lines, secondary_blocks = separate_secondary_text(content_lines, body_size, float(page.get("width") or 0.0))
        flow_lines, question_blocks = extract_question_callouts(flow_lines, page.get("visual_lines", []), body_size)
        page_width = float(page.get("width") or 0.0)
        page["blocks"] = merge_adjacent_body_blocks(lines_to_blocks(flow_lines, body_size, page_width), page_width, body_size)
        page["blocks"].extend(question_blocks)
        caption_candidates = caption_candidate_records(secondary_blocks, float(page.get("width") or 0.0))
        attach_image_caption_matches(page.get("image_regions", []), caption_candidates, float(page.get("width") or 0.0))
        recover_image_caption_continuations(
            page.get("image_regions", []),
            caption_candidates,
            page["blocks"],
            secondary_blocks,
            float(page.get("width") or 0.0),
            float(page.get("height") or 0.0),
        )
        page["figure_groups"] = cluster_image_regions(
            page.get("image_regions", []),
            caption_candidates,
            float(page.get("width") or 0.0),
            float(page.get("height") or 0.0),
        )
        recover_figure_group_captions(
            page["figure_groups"],
            page["blocks"],
            secondary_blocks,
            float(page.get("width") or 0.0),
            float(page.get("height") or 0.0),
        )
        assign_secondary_reading_order(page["blocks"], secondary_blocks, body_size)
        page["secondary_blocks"] = secondary_blocks
        page["noise_lines"] = noise_lines
        page["regions"] = build_layout_regions(page["blocks"], secondary_blocks, page.get("tables", []), float(page.get("width") or 0.0), image_regions=page.get("image_regions", []), figure_groups=page.get("figure_groups", []))

    if embed_images:
        embed_page_images(
            pdf_path,
            pages,
            quality=image_quality,
            max_width=image_max_width,
            progress_callback=progress_callback,
        )

    plain_text_parts: list[str] = []
    for page in pages:
        plain_text_parts.append(f"--- עמוד {page['number']} ---")
        reading_items: list[tuple[str, dict]] = []
        for block in page["blocks"] + page.get("secondary_blocks", []):
            if block.get("consumed_by_figure_group") or block.get("consumed_by_image"):
                continue
            kind = "caption" if block.get("class") == "caption" else "question" if block.get("class") == "question-box" else "body"
            reading_items.append((kind, block))
        for table in page.get("tables", []):
            if table.get("reconstructed") or (table.get("data") and table.get("cols", 0) > 1):
                reading_items.append(("table", table))

        for kind, item in sort_layout_items(reading_items, float(page.get("width") or 0.0), body_size):
            if kind == "table":
                table = item
                for row in table.get("data", []):
                    plain_text_parts.append(" | ".join(row))
            else:
                prefix = "[Caption] " if kind == "caption" else ""
                plain_text_parts.append(prefix + " ".join(line["text"] for line in item["lines"]))
        plain_text_parts.append("")

    return pages, "\n".join(plain_text_parts).strip() + "\n", len(pages), total_chars


def build_layout_debug_report(
    pdf_path: Path,
    crop_settings: dict | None = None,
    page_numbers: set[int] | None = None,
) -> dict:
    pdf_path = pdf_path.expanduser().resolve()
    crop_settings = normalize_crop_settings(crop_settings)
    crop_enabled = crop_settings_enabled(crop_settings)
    pypdf_reader = PdfReader(str(pdf_path))
    text_pages = extract_requested_text_pages(pypdf_reader, page_numbers)
    report_pages: list[dict] = []
    all_corrected_lines: list[dict] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            if page_numbers and page_number not in page_numbers:
                continue
            crop_bounds = page_crop_bounds(float(page.width), float(page.height), page_number, crop_settings) if crop_enabled else None
            raw_words = page.extract_words(
                keep_blank_chars=False,
                use_text_flow=True,
                extra_attrs=["fontname", "size"],
            )
            cropped_words = filter_words_by_crop(raw_words, crop_bounds)
            layout_width = layout_width_for_bounds(float(page.width), crop_bounds)
            layout_words = normalize_words_to_layout_bounds(cropped_words, crop_bounds)
            visual_lines = cluster_visual_lines(layout_words, layout_width)
            image_regions = content_image_records(page.images, float(page.width), float(page.height), crop_bounds)
            logical_lines = text_pages.get(page_number, [])
            if crop_enabled:
                corrected_lines = align_text_lines_to_style_lines(logical_lines, visual_lines, require_visual_match=True) if logical_lines else visual_lines_to_text_lines(visual_lines)
            else:
                corrected_lines = align_text_lines_to_style_lines(logical_lines, visual_lines) if logical_lines else visual_lines_to_text_lines(visual_lines)

            table_order_lines = text_lines_to_records(logical_lines)
            tables = detect_tables(page, crop_bounds)
            tables.extend(
                detect_text_table_markers(
                    corrected_lines,
                    start_index=len(tables) + 1,
                    fallback_lines=table_order_lines,
                )
            )

            report_pages.append(
                {
                    "number": page_number,
                    "page_width": round(float(page.width), 2),
                    "page_height": round(float(page.height), 2),
                    "layout_width": round(layout_width, 2),
                    "crop_bounds": [round(float(value), 2) for value in crop_bounds] if crop_bounds else None,
                    "word_counts": {
                        "raw": len(raw_words),
                        "after_crop": len(cropped_words),
                    },
                    "pypdf_lines": [
                        {
                            "index": index,
                            "text": clean_extracted_text_line(text),
                            "best_visual_match": best_style_line_match(text, visual_lines),
                        }
                        for index, text in enumerate(logical_lines)
                    ],
                    "visual_lines": [
                        line_debug_record(line, index=index, role="visual")
                        for index, line in enumerate(visual_lines)
                    ],
                    "corrected_lines": [
                        line_debug_record(line, index=index, role="corrected")
                        for index, line in enumerate(corrected_lines)
                    ],
                    "tables": [
                        {
                            "index": table.get("index"),
                            "top": round(float(table.get("top") or 0.0), 2),
                            "bottom": round(float(table.get("bottom") or 0.0), 2),
                            "rows": table.get("rows"),
                            "cols": table.get("cols"),
                            "estimated": bool(table.get("estimated")),
                            "reconstructed": bool(table.get("reconstructed")),
                            "sample": table.get("sample", ""),
                        }
                        for table in tables
                    ],
                    "_corrected_lines_for_classification": corrected_lines,
                    "_visual_lines_for_classification": visual_lines,
                    "_tables_for_classification": tables,
                    "_image_regions_for_classification": image_regions,
                }
            )
            all_corrected_lines.extend(corrected_lines)

    body_size = dominant_number([line["size"] for line in all_corrected_lines])
    for page_report in report_pages:
        corrected_lines = page_report.pop("_corrected_lines_for_classification")
        visual_lines = page_report.pop("_visual_lines_for_classification")
        tables = page_report.pop("_tables_for_classification")
        image_regions = page_report.pop("_image_regions_for_classification")
        non_table_lines = [line for line in corrected_lines if not line_overlaps_table(line, tables)]
        content_lines, noise_lines = split_noise_lines(
            non_table_lines,
            float(page_report["layout_width"]),
            float(page_report["page_height"]),
            body_size,
        )
        flow_lines, secondary_blocks = separate_secondary_text(content_lines, body_size, float(page_report["layout_width"]))
        flow_lines, question_blocks = extract_question_callouts(flow_lines, visual_lines, body_size)
        layout_width = float(page_report["layout_width"])
        body_blocks = merge_adjacent_body_blocks(lines_to_blocks(flow_lines, body_size, layout_width), layout_width, body_size)
        body_blocks.extend(question_blocks)
        assign_secondary_reading_order(body_blocks, secondary_blocks, body_size)
        caption_candidates = caption_candidate_records(secondary_blocks, float(page_report["layout_width"]))
        attach_image_caption_matches(image_regions, caption_candidates, float(page_report["layout_width"]))
        recover_image_caption_continuations(
            image_regions,
            caption_candidates,
            body_blocks,
            secondary_blocks,
            float(page_report["layout_width"]),
            float(page_report["page_height"]),
        )
        figure_groups = cluster_image_regions(
            image_regions,
            caption_candidates,
            float(page_report["layout_width"]),
            float(page_report["page_height"]),
        )
        recover_figure_group_captions(
            figure_groups,
            body_blocks,
            secondary_blocks,
            float(page_report["layout_width"]),
            float(page_report["page_height"]),
        )
        page_report["classification"] = {
            "body_lines": [line_debug_record(line, role="body") for line in flow_lines],
            "noise_lines": [line_debug_record(line, role="noise") for line in noise_lines],
            "caption_blocks": [
                {
                    "id": f"caption-{index}",
                    "caption_type": str(block.get("caption_type") or "unknown"),
                    "consumed_by_figure_group": block.get("consumed_by_figure_group"),
                    "consumed_by_image": block.get("consumed_by_image"),
                    "top": round(float(block.get("top") or 0.0), 2),
                    "bottom": round(float(block.get("bottom") or 0.0), 2),
                    "reading_top": round(float(block.get("reading_top", block.get("top", 0.0))), 2),
                    "text": block_plain_text(block),
                    "lines": [line_debug_record(line, role="caption") for line in block.get("lines", [])],
                }
                for index, block in enumerate(secondary_blocks, start=1)
            ],
            "image_regions": image_regions,
            "figure_groups": figure_groups,
            "question_blocks": [
                {
                    "id": str(block.get("id") or ""),
                    "consumed_by_figure_group": block.get("consumed_by_figure_group"),
                    "consumed_by_image": block.get("consumed_by_image"),
                    "top": round(float(block.get("top") or 0.0), 2),
                    "bottom": round(float(block.get("bottom") or 0.0), 2),
                    "text": block_plain_text(block),
                    "lines": [line_debug_record(line, role="question") for line in block.get("lines", [])],
                }
                for block in question_blocks
            ],
            "body_blocks": [
                {
                    "id": str(block.get("id") or ""),
                    "consumed_by_figure_group": block.get("consumed_by_figure_group"),
                    "consumed_by_image": block.get("consumed_by_image"),
                    "top": round(float(block.get("top") or 0.0), 2),
                    "bottom": round(float(block.get("bottom") or 0.0), 2),
                    "ends_sentence": block_ends_sentence(block),
                    "paragraph_start_reason": str(block.get("paragraph_start_reason") or ""),
                    "merged_block_count": int(block.get("merged_block_count", 1)),
                    "merge_reasons": block.get("merge_reasons", []),
                    "text": block_plain_text(block),
                }
                for block in body_blocks
            ],
            "regions": build_layout_regions(body_blocks, secondary_blocks, tables, float(page_report["layout_width"]), image_regions=image_regions, figure_groups=figure_groups),
        }

    return {
        "source": str(pdf_path),
        "requested_pages": page_range_label(page_numbers),
        "crop_enabled": crop_enabled,
        "crop_settings": crop_settings,
        "pages": report_pages,
    }


def write_layout_debug_report(
    pdf_path: Path,
    output_dir: Path,
    crop_settings: dict | None = None,
    page_numbers: set[int] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_layout_debug_report(pdf_path, crop_settings=crop_settings, page_numbers=page_numbers)
    output_path = output_dir / f"{safe_slug(pdf_path.name)}-layout-debug.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def image_figure_html(image: dict, caption_text: str = "") -> str:
    src = str(image.get("src") or "")
    if not src:
        return ""
    confidence = float(image.get("match_confidence") or 0.0)
    reason = str(image.get("match_reason") or "")
    classes = ["embedded-image"]
    match = image.get("caption_match") or {}
    if caption_text and "side_overlap" in reason:
        caption_side = str(match.get("caption_side") or "right")
        classes.extend(["side-caption", f"side-caption-{caption_side}"])
    elif caption_text and "above_overlap" in reason:
        classes.append("caption-above")
    elif caption_text and "below_overlap" in reason:
        classes.append("caption-below")
    if image.get("group_candidate"):
        classes.append("image-group-candidate")
    if image.get("ambiguous_caption_candidates"):
        classes.append("ambiguous-caption")
    img_alt = html.escape(caption_text or f"Image {image.get('id', '')}".strip())
    caption_html = f"<figcaption>{text_with_bidi_isolates(caption_text)}</figcaption>" if caption_text else ""
    return (
        f'<figure class="{" ".join(classes)}" data-match-confidence="{confidence:.2f}" '
        f'data-match-reason="{html.escape(reason)}">'
        f'<img src="{html.escape(src)}" alt="{img_alt}" loading="lazy">'
        f"{caption_html}</figure>"
    )


def usable_image_caption(item: dict) -> bool:
    match = item.get("caption_match") or {}
    caption_type = str(match.get("caption_type") or "unknown")
    allowed_types = (
        {"full_caption", "figure_title"}
        if item.get("kind") == "figure_group"
        else {"full_caption", "figure_title", "local_label", "unknown"}
    )
    return (
        bool(match.get("caption_id"))
        and caption_type in allowed_types
        and float(item.get("match_confidence") or 0.0) >= IMAGE_CAPTION_CONFIDENCE_THRESHOLD
        and not str(item.get("match_reason") or "").startswith("low_confidence")
    )


def figure_group_layout_class(group: dict, members: list[dict]) -> str:
    if len(members) >= 4 or group.get("grouping_reason") == "dense_gallery":
        return "group-layout-gallery"
    centers_x = [(image_box(member)[0] + image_box(member)[2]) / 2.0 for member in members]
    centers_y = [(image_box(member)[1] + image_box(member)[3]) / 2.0 for member in members]
    horizontal_spread = max(centers_x) - min(centers_x) if centers_x else 0.0
    vertical_spread = max(centers_y) - min(centers_y) if centers_y else 0.0
    return "group-layout-stack" if vertical_spread > horizontal_spread * 1.15 else "group-layout-row"


def figure_group_html(group: dict, members: list[dict], caption_text: str = "") -> str:
    renderable_members = [member for member in members if member.get("src")]
    if len(renderable_members) < 2:
        return ""
    group_caption_id = str((group.get("caption_match") or {}).get("caption_id") or "")
    layout_class = figure_group_layout_class(group, renderable_members)
    member_html: list[str] = []
    used_local_caption_ids: set[str] = set()
    for member in renderable_members:
        match = member.get("caption_match") or {}
        local_caption_id = str(match.get("caption_id") or "")
        local_caption_text = ""
        if (
            usable_image_caption(member)
            and local_caption_id != group_caption_id
            and local_caption_id not in used_local_caption_ids
        ):
            local_caption_text = str(match.get("caption_text") or "")
            used_local_caption_ids.add(local_caption_id)
        alt_text = local_caption_text or caption_text or f"Image {member.get('id', '')}".strip()
        local_label_html = (
            f'<span class="figure-local-label">{text_with_bidi_isolates(local_caption_text)}</span>'
            if local_caption_text
            else ""
        )
        member_html.append(
            f'<div class="figure-group-item" data-image-id="{html.escape(str(member.get("id") or ""))}">'
            f'<img src="{html.escape(str(member.get("src") or ""))}" alt="{html.escape(alt_text)}" loading="lazy">'
            f"{local_label_html}</div>"
        )
    remaining_labels = [
        label
        for label in group.get("local_labels", [])
        if str(label.get("caption_id") or "") not in used_local_caption_ids
    ]
    remaining_labels_html = (
        '<div class="figure-group-labels">'
        + "".join(
            f'<span class="figure-local-label">{text_with_bidi_isolates(str(label.get("text") or ""))}</span>'
            for label in remaining_labels
        )
        + "</div>"
        if remaining_labels
        else ""
    )
    caption_html = f"<figcaption>{text_with_bidi_isolates(caption_text)}</figcaption>" if caption_text else ""
    confidence = float(group.get("match_confidence") or 0.0)
    reason = str(group.get("match_reason") or "")
    return (
        f'<figure class="embedded-figure-group {layout_class}" '
        f'data-group-id="{html.escape(str(group.get("id") or ""))}" '
        f'data-match-confidence="{confidence:.2f}" data-match-reason="{html.escape(reason)}">'
        f'<div class="figure-group-grid">{"".join(member_html)}</div>{remaining_labels_html}{caption_html}</figure>'
    )


def pages_to_html(pages: list[dict]) -> str:
    page_html: list[str] = []
    for page in pages:
        page_items = []
        reading_items: list[tuple[str, dict]] = []
        for index, block in enumerate(page.get("secondary_blocks", []), start=1):
            block.setdefault("id", f"caption-{index}")
        image_by_id = {str(image.get("id") or ""): image for image in page.get("image_regions", [])}
        renderable_groups: list[tuple[dict, list[dict]]] = []
        grouped_image_ids: set[str] = set()
        attached_caption_ids: set[str] = set()
        for group in page.get("figure_groups", []):
            members = [image_by_id[image_id] for image_id in group.get("member_image_ids", []) if image_id in image_by_id and image_by_id[image_id].get("src")]
            if len(members) < 2:
                continue
            renderable_groups.append((group, members))
            grouped_image_ids.update(str(member.get("id") or "") for member in members)
            if usable_image_caption(group):
                attached_caption_ids.add(str((group.get("caption_match") or {}).get("caption_id") or ""))
            attached_caption_ids.update(
                str(label.get("caption_id") or "")
                for label in group.get("local_labels", [])
                if label.get("caption_id")
            )
            for member in members:
                if usable_image_caption(member):
                    attached_caption_ids.add(str((member.get("caption_match") or {}).get("caption_id") or ""))
        for image in page.get("image_regions", []):
            if image.get("src") and str(image.get("id") or "") not in grouped_image_ids and usable_image_caption(image):
                attached_caption_ids.add(str((image.get("caption_match") or {}).get("caption_id") or ""))

        consumed_caption_ids: set[str] = set()
        for block in page["blocks"] + page.get("secondary_blocks", []):
            if block.get("consumed_by_figure_group") or block.get("consumed_by_image"):
                continue
            kind = "caption" if block.get("class") == "caption" else "question" if block.get("class") == "question-box" else "body"
            if kind == "caption" and str(block.get("id") or "") in attached_caption_ids:
                continue
            reading_items.append((kind, block))
        for image in page.get("image_regions", []):
            if image.get("src") and str(image.get("id") or "") not in grouped_image_ids:
                reading_items.append(("image", image))
        for group, members in renderable_groups:
            render_group = dict(group)
            render_group["members"] = members
            reading_items.append(("figure_group", render_group))
        for table in page.get("tables", []):
            if table.get("reconstructed") or (table.get("data") and table.get("cols", 0) > 1):
                reading_items.append(("table", table))

        body_size = dominant_number([float(line.get("size") or 0.0) for line in page.get("lines", [])], fallback=12.0)
        for kind, item in sort_layout_items(reading_items, float(page.get("width") or 0.0), body_size):
            if kind == "table":
                table = item
                sample = f'<div class="table-sample">{html.escape(table["sample"])}</div>' if table["sample"] else ""
                if table.get("reconstructed"):
                    certainty = "טבלה משוחזרת"
                else:
                    certainty = "טבלה אפשרית" if table.get("estimated") else "טבלה"
                cols_text = f' · {table["cols"]} עמודות' if table["cols"] else ""
                preview = table_data_to_html(table.get("data", []), estimated=bool(table.get("estimated")))
                page_items.append(
                    {
                        "html": (
                            '<aside class="table-detection">'
                            f'<strong>זוהתה {certainty}</strong>'
                            f'<span>עמוד {page["number"]}, טבלה {table["index"]} · '
                            f'{table["rows"]} שורות{cols_text}</span>'
                            f"{sample}{preview}"
                            "</aside>"
                        ),
                    }
                )
            else:
                if kind == "figure_group":
                    match = item.get("caption_match") or {}
                    caption_id = str(match.get("caption_id") or "")
                    caption_text = ""
                    if usable_image_caption(item) and caption_id not in consumed_caption_ids:
                        caption_text = str(match.get("caption_text") or "")
                        consumed_caption_ids.add(caption_id)
                    figure = figure_group_html(item, item.get("members", []), caption_text=caption_text)
                    if figure:
                        page_items.append({"html": figure})
                elif kind == "image":
                    match = item.get("caption_match") or {}
                    caption_id = str(match.get("caption_id") or "")
                    caption_text = ""
                    if (
                        caption_id
                        and caption_id not in consumed_caption_ids
                        and usable_image_caption(item)
                    ):
                        caption_text = str(match.get("caption_text") or "")
                        consumed_caption_ids.add(caption_id)
                    figure = image_figure_html(item, caption_text=caption_text)
                    if figure:
                        page_items.append({"html": figure})
                else:
                    block = item
                    tag = block["tag"]
                    class_attr = f' class="{html.escape(str(block["class"]))}"' if block.get("class") else ""
                    page_items.append({"html": f"<{tag}{class_attr}>{block['html']}</{tag}>"})
        page_html.append(
            f'<section class="page" id="page-{page["number"]}">'
            f'<div class="page-marker">עמוד {page["number"]}</div>'
            f'<div class="content">{"".join(item["html"] for item in page_items)}</div>'
            f"</section>"
        )
    return "".join(page_html)


def extract_pdf_text(
    pdf_path: Path,
    crop_settings: dict | None = None,
    page_numbers: set[int] | None = None,
) -> tuple[str, int, int]:
    if crop_settings_enabled(crop_settings):
        raise ValueError("Crop bounds require structured PDF extraction")
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    total_chars = 0

    for index, page in enumerate(reader.pages, start=1):
        if page_numbers and index not in page_numbers:
            continue
        page_text = page.extract_text() or ""
        page_text = "\n".join(clean_extracted_text_line(line) for line in page_text.strip().splitlines())
        total_chars += len(page_text)
        parts.append(f"--- עמוד {index} ---\n{page_text}\n")

    if page_numbers and not parts:
        raise ValueError(f"No pages matched the requested range: {page_range_label(page_numbers)}")

    return "\n".join(parts).strip() + "\n", len(parts), total_chars


def guess_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("--- עמוד"):
            return line[:120]
    return fallback


def build_conversion_report(
    pages: list[dict],
    *,
    total_pages: int,
    page_numbers: set[int] | None,
    crop_settings: dict | None,
    structured: bool,
    images_enabled: bool = True,
    image_quality: int = IMAGE_JPEG_QUALITY,
    image_max_width: int = IMAGE_MAX_EMBED_WIDTH,
) -> dict:
    tables = [table for page in pages for table in page.get("tables", [])]
    secondary_blocks = [block for page in pages for block in page.get("secondary_blocks", [])]
    image_regions = [image for page in pages for image in page.get("image_regions", [])]
    figure_groups = [group for page in pages for group in page.get("figure_groups", [])]
    embedded_sources = [str(image.get("src") or "") for image in image_regions if image.get("src")]
    embedded_image_bytes = sum(
        max(0, (len(source.partition(",")[2]) * 3) // 4 - source.partition(",")[2].count("="))
        for source in embedded_sources
    )
    converted_page_numbers = [int(page["number"]) for page in pages]
    return {
        "mode": "structured" if structured else "plain text fallback",
        "source_pages": total_pages,
        "converted_pages": len(converted_page_numbers),
        "converted_page_numbers": converted_page_numbers,
        "requested_range": page_range_label(page_numbers),
        "crop_enabled": crop_settings_enabled(crop_settings),
        "tables_detected": len(tables),
        "tables_reconstructed": sum(1 for table in tables if table.get("reconstructed")),
        "possible_tables": sum(1 for table in tables if table.get("estimated") and not table.get("reconstructed")),
        "secondary_blocks": len(secondary_blocks),
        "image_regions": len(image_regions),
        "figure_groups": len(figure_groups),
        "images_enabled": images_enabled,
        "image_quality": image_quality,
        "image_max_width": image_max_width,
        "embedded_images": len(embedded_sources),
        "embedded_image_bytes": embedded_image_bytes,
    }


def format_byte_size(value: int | float) -> str:
    size = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def conversion_report_to_html(report: dict | None) -> str:
    if not report:
        return ""
    rows = [
        ("Extraction mode", report.get("mode", "")),
        ("Requested pages", report.get("requested_range", "all pages")),
        ("Converted pages", f'{report.get("converted_pages", 0)} of {report.get("source_pages", 0)}'),
        ("Crop bounds", "enabled" if report.get("crop_enabled") else "off"),
        ("Tables detected", report.get("tables_detected", 0)),
        ("Tables reconstructed", report.get("tables_reconstructed", 0)),
        ("Possible tables", report.get("possible_tables", 0)),
        ("Captions / side text", report.get("secondary_blocks", 0)),
        ("Image regions", report.get("image_regions", 0)),
        ("Figure groups", report.get("figure_groups", 0)),
        ("Image embedding", "enabled" if report.get("images_enabled", True) else "disabled"),
        ("JPEG quality", report.get("image_quality", IMAGE_JPEG_QUALITY) if report.get("images_enabled", True) else "n/a"),
        ("Maximum image width", f'{report.get("image_max_width", IMAGE_MAX_EMBED_WIDTH)} px' if report.get("images_enabled", True) else "n/a"),
        ("Images embedded", report.get("embedded_images", 0)),
        ("Embedded image data", format_byte_size(report.get("embedded_image_bytes", 0))),
        ("Estimated HTML size", format_byte_size(report.get("estimated_output_bytes", 0))) if report.get("estimated_output_bytes") is not None else ("Estimated HTML size", "pending"),
    ]
    rows_html = "".join(
        f"<dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd>"
        for label, value in rows
    )
    page_numbers = report.get("converted_page_numbers") or []
    page_list = ", ".join(str(number) for number in page_numbers[:40])
    if len(page_numbers) > 40:
        page_list += f", ... +{len(page_numbers) - 40} more"
    if page_list:
        rows_html += f"<dt>Page list</dt><dd>{html.escape(page_list)}</dd>"
    return f'<details class="conversion-report"><summary>Conversion report</summary><dl>{rows_html}</dl></details>'


def build_reader_html(
    text: str,
    title: str,
    source_name: str,
    reader_html: str | None = None,
    report: dict | None = None,
) -> str:
    source_json = json.dumps(text, ensure_ascii=False)
    reader_html_json = json.dumps(reader_html or "", ensure_ascii=False)
    title_html = html.escape(title)
    source_html = html.escape(source_name)
    report_html = conversion_report_to_html(report)

    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <style>
    :root {{
      --bg: #f6f3ed;
      --paper: #fffdf8;
      --text: #24211d;
      --muted: #6e665c;
      --line: #d9d1c5;
      --accent: #176f68;
      --accent-soft: #d8ebe8;
      --reader-size: 21px;
      --reader-line: 1.9;
      --reader-width: 760px;
    }}
    [data-theme="dark"] {{
      --bg: #161716;
      --paper: #22231f;
      --text: #eee9df;
      --muted: #b9b0a3;
      --line: #3f4039;
      --accent: #73c7bd;
      --accent-soft: #263d3a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 93%, transparent);
      backdrop-filter: blur(14px);
    }}
    .toolbar {{
      width: min(1120px, 100%);
      margin: 0 auto;
      padding: 12px 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.35;
    }}
    .meta {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }}
    button, select {{
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--paper);
      color: var(--text);
      padding: 0 11px;
      font: inherit;
      font-size: 14px;
    }}
    button {{ cursor: pointer; }}
    button:hover, select:hover {{ border-color: var(--accent); }}
    button.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    main {{
      width: min(1120px, 100%);
      margin: 0 auto;
      padding: 28px 18px 72px;
    }}
    .reader {{
      width: min(var(--reader-width), 100%);
      margin: 0 auto;
      background: var(--paper);
      border: 1px solid var(--line);
      box-shadow: 0 18px 50px rgba(52, 43, 32, .12);
      padding: clamp(22px, 5vw, 58px);
    }}
    [data-theme="dark"] .reader {{ box-shadow: none; }}
    .page {{
      padding-block: 8px 34px;
      border-bottom: 1px solid var(--line);
    }}
    .page:last-child {{ border-bottom: 0; }}
    .page-marker {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      margin-bottom: 18px;
      padding: 0 10px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
    }}
    .content {{
      overflow-wrap: anywhere;
      font-family: "Noto Sans Hebrew", "Segoe UI", Arial, sans-serif;
      font-size: var(--reader-size);
      line-height: var(--reader-line);
    }}
    .content p {{
      margin: 0 0 1.05em;
    }}
    .content .caption {{
      margin: .9em 0 1.15em;
      border-inline-start: 4px solid var(--line);
      padding: .55em .8em;
      background: color-mix(in srgb, var(--paper) 82%, var(--accent-soft));
      color: var(--muted);
      font-size: .88em;
      line-height: 1.65;
    }}
    .content .embedded-image {{
      margin: 1.25em 0 1.45em;
      padding: 0;
    }}
    .content .embedded-image img {{
      display: block;
      width: min(100%, 760px);
      height: auto;
      margin-inline: auto;
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 1px 4px rgba(0,0,0,.12);
    }}
    .content .embedded-image figcaption {{
      width: min(100%, 760px);
      margin: .55em auto 0;
      color: var(--muted);
      font-size: .86em;
      line-height: 1.55;
      text-align: right;
    }}
    .content .embedded-image.caption-above {{
      display: flex;
      flex-direction: column;
    }}
    .content .embedded-image.caption-above img {{ order: 2; }}
    .content .embedded-image.caption-above figcaption {{
      order: 1;
      margin: 0 auto .55em;
    }}
    .content .embedded-image.side-caption {{
      width: min(100%, 920px);
      margin-inline: auto;
      display: grid;
      grid-template-columns: minmax(0, 3.3fr) minmax(9.5rem, 1fr);
      gap: clamp(16px, 3vw, 34px);
      align-items: end;
      direction: ltr;
    }}
    .content .embedded-image.side-caption-right {{
      grid-template-areas: "image caption";
    }}
    .content .embedded-image.side-caption-left {{
      grid-template-areas: "caption image";
      grid-template-columns: minmax(9.5rem, 1fr) minmax(0, 3.3fr);
    }}
    .content .embedded-image.side-caption img {{
      grid-area: image;
      width: 100%;
      margin: 0;
    }}
    .content .embedded-image.side-caption figcaption {{
      grid-area: caption;
      width: auto;
      margin: 0;
      direction: rtl;
      padding-block-end: .15em;
    }}
    .content .embedded-image.ambiguous-caption figcaption {{
      border-inline-start: 3px solid var(--line);
      padding-inline-start: .65em;
    }}
    .content .embedded-figure-group {{
      width: min(100%, 820px);
      margin: 1.4em auto 1.65em;
      padding: .8em;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: color-mix(in srgb, var(--paper) 94%, var(--accent-soft));
    }}
    .content .figure-group-grid {{
      display: grid;
      gap: 12px;
      align-items: start;
      direction: rtl;
    }}
    .content .group-layout-gallery .figure-group-grid {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }}
    .content .group-layout-row .figure-group-grid {{
      grid-template-columns: repeat(var(--group-columns, 3), minmax(0, 1fr));
    }}
    .content .group-layout-stack .figure-group-grid {{
      grid-template-columns: minmax(0, 1fr);
      width: min(100%, 520px);
      margin-inline: auto;
    }}
    .content .figure-group-item {{
      min-width: 0;
      text-align: center;
    }}
    .content .figure-group-item img {{
      display: block;
      width: 100%;
      height: auto;
      max-height: 520px;
      object-fit: contain;
      margin-inline: auto;
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 1px 4px rgba(0,0,0,.12);
    }}
    .content .figure-local-label {{
      display: block;
      margin-top: .38em;
      color: var(--muted);
      font-size: .75em;
      line-height: 1.35;
    }}
    .content .figure-group-labels {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: .35em .65em;
      margin-top: .6em;
    }}
    .content .figure-group-labels .figure-local-label {{
      margin: 0;
      padding: .2em .5em;
      border-radius: 999px;
      background: var(--paper);
    }}
    .content .embedded-figure-group > figcaption {{
      margin: .8em auto 0;
      color: var(--muted);
      font-size: .86em;
      line-height: 1.55;
      text-align: right;
    }}
    .content .question-box {{
      margin: 1.1em 0 1.35em;
      border: 1px solid var(--accent-soft);
      border-radius: 7px;
      padding: .9em 1em;
      background: color-mix(in srgb, var(--paper) 72%, var(--accent-soft));
      font-size: .9em;
      line-height: 1.62;
    }}
    .content .question-box h3 {{
      margin: 0 0 .65em;
      color: var(--accent);
      font-size: 1.02em;
      line-height: 1.35;
      text-align: right;
    }}
    .content .question-columns {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1.1em;
    }}
    .content .question-box ul {{
      margin: 0;
      padding-inline-start: 1.1em;
    }}
    .content .question-box li {{
      margin: 0 0 .55em;
    }}
    .content h2 {{
      margin: 0 0 .85em;
      font-size: calc(var(--reader-size) * 1.2);
      line-height: 1.45;
      font-weight: 700;
    }}
    .content span.large {{
      font-size: 1.15em;
    }}
    .content span.small {{
      font-size: .88em;
      color: var(--muted);
    }}
    .content span.bold {{
      font-weight: 700;
    }}
    .content span.italic {{
      font-style: italic;
    }}
    .findbar {{
      width: min(var(--reader-width), 100%);
      margin: 0 auto 14px;
      display: flex;
      gap: 8px;
    }}
    .findbar input {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--paper);
      color: var(--text);
      padding: 0 12px;
      font: inherit;
    }}
    .conversion-report {{
      width: min(var(--reader-width), 100%);
      margin: 0 auto 14px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--paper);
      color: var(--text);
      padding: 12px 14px;
      direction: ltr;
      text-align: left;
      font-size: 14px;
    }}
    .conversion-report summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--accent);
    }}
    .conversion-report dl {{
      display: grid;
      grid-template-columns: minmax(120px, auto) 1fr;
      gap: 8px 14px;
      margin: 12px 0 0;
    }}
    .conversion-report dt {{
      color: var(--muted);
    }}
    .conversion-report dd {{
      margin: 0;
      font-weight: 600;
    }}
    mark {{
      background: #ffe08a;
      color: #1d1b16;
      padding: 0 2px;
    }}
    .table-detection {{
      margin: 1.35em 0;
      border: 1px solid var(--accent);
      border-inline-start-width: 5px;
      border-radius: 7px;
      background: var(--accent-soft);
      color: var(--text);
      padding: 12px 14px;
      font-size: .92em;
      line-height: 1.55;
    }}
    .table-detection strong {{
      display: block;
      margin-bottom: 2px;
      color: var(--accent);
      font-size: 1.02em;
    }}
    .table-detection span {{
      display: block;
    }}
    .table-sample {{
      margin-top: 6px;
      color: var(--muted);
      font-size: .9em;
    }}
    .table-preview-wrap {{
      margin-top: 10px;
      overflow-x: auto;
      max-width: 100%;
    }}
    .table-carousel-nav {{
      display: none;
    }}
    .detected-table {{
      width: 100%;
      min-width: 720px;
      border-collapse: collapse;
      background: var(--paper);
      font-size: .9em;
      line-height: 1.5;
      table-layout: fixed;
      overflow-wrap: normal;
      word-break: normal;
    }}
    .detected-table .stage-col {{
      width: 18%;
    }}
    .detected-table .period-col {{
      width: 18%;
    }}
    .detected-table .description-col {{
      width: 64%;
    }}
    .detected-table th,
    .detected-table td {{
      border: 1px solid var(--line);
      padding: 7px 9px;
      vertical-align: top;
      text-align: start;
      overflow-wrap: normal;
      word-break: normal;
      hyphens: manual;
    }}
    .detected-table th {{
      background: color-mix(in srgb, var(--accent-soft) 72%, var(--paper));
      color: var(--accent);
      font-weight: 700;
    }}
    .detected-table.estimated td,
    .detected-table.estimated th {{
      white-space: normal;
    }}
    .content .embedded-image img,
    .content .figure-group-item img {{
      cursor: zoom-in;
    }}
    .content .embedded-image img:focus-visible,
    .content .figure-group-item img:focus-visible {{
      outline: 3px solid var(--accent);
      outline-offset: 4px;
    }}
    body.lightbox-open {{
      overflow: hidden;
    }}
    .image-lightbox {{
      position: fixed;
      inset: 0;
      z-index: 1000;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      gap: 12px;
      padding: max(14px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right)) max(14px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
      background: rgba(12, 12, 12, .94);
      color: #fff;
    }}
    .image-lightbox[hidden] {{
      display: none;
    }}
    .image-lightbox-close {{
      justify-self: end;
      width: 44px;
      height: 44px;
      border: 1px solid rgba(255,255,255,.55);
      border-radius: 50%;
      padding: 0;
      background: rgba(0,0,0,.35);
      color: #fff;
      font-size: 1.8rem;
      line-height: 1;
    }}
    .image-lightbox img {{
      display: block;
      align-self: center;
      justify-self: center;
      max-width: 100%;
      max-height: 100%;
      width: auto;
      height: auto;
      object-fit: contain;
      border-radius: 6px;
      background: #fff;
      box-shadow: 0 14px 44px rgba(0,0,0,.45);
    }}
    .image-lightbox-caption {{
      max-width: 900px;
      margin: 0 auto;
      color: #f1eee8;
      font-size: clamp(.88rem, 2.3vw, 1.05rem);
      line-height: 1.5;
      text-align: center;
    }}
    .hidden {{ display: none; }}
    @media (max-width: 760px) {{
      .toolbar {{ grid-template-columns: 1fr; }}
      .controls {{ justify-content: stretch; }}
      button, select {{ flex: 1 1 auto; }}
      main {{ padding-inline: 10px; }}
      .reader {{ border-inline: 0; padding-inline: 18px; }}
      .content .question-columns {{ grid-template-columns: 1fr; }}
      .content .embedded-image.side-caption,
      .content .embedded-image.side-caption-left,
      .content .embedded-image.side-caption-right {{
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        grid-template-areas: "image" "caption";
        gap: 0;
        direction: rtl;
      }}
      .content .embedded-image.side-caption figcaption {{
        width: min(100%, 760px);
        margin: .55em auto 0;
      }}
      .content .embedded-figure-group {{ padding: .6em; }}
      .content .group-layout-gallery .figure-group-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .content .group-layout-row .figure-group-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .table-preview-wrap {{
        overflow-x: auto;
        overscroll-behavior-inline: contain;
        scroll-snap-type: x mandatory;
        scroll-padding-inline: 0;
        scrollbar-width: thin;
        padding-bottom: 8px;
      }}
      .table-carousel-nav {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin-top: 10px;
        margin-bottom: 6px;
      }}
      .table-carousel-nav button {{
        flex: 0 0 42px;
        width: 42px;
        height: 42px;
        border-radius: 50%;
        border: 1px solid var(--accent);
        background: var(--paper);
        color: var(--accent);
        font-size: 1.25rem;
        font-weight: 700;
        line-height: 1;
      }}
      .table-carousel-nav button:disabled {{
        opacity: .35;
      }}
      .table-carousel-position {{
        flex: 1 1 auto;
        text-align: center;
        color: var(--muted);
        font-size: .86em;
      }}
      .detected-table {{
        min-width: 0;
        width: 100%;
        border-collapse: collapse;
        border-spacing: 0;
        background: transparent;
        table-layout: auto;
        display: block;
      }}
      .detected-table colgroup,
      .detected-table tr:first-child {{
        display: none;
      }}
      .detected-table tbody {{
        display: flex;
        gap: 14px;
        padding-inline: 0;
      }}
      .detected-table tr,
      .detected-table td {{
        display: block;
      }}
      .detected-table tr {{
        position: relative;
        flex: 0 0 100%;
        width: 100%;
        max-width: 100%;
        scroll-snap-align: start;
        border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--line));
        border-inline-start: 5px solid var(--accent);
        border-radius: 7px;
        background: color-mix(in srgb, var(--paper) 88%, var(--accent-soft));
        padding: 34px 12px 8px;
        box-shadow: 0 8px 18px rgba(52, 43, 32, .08);
      }}
      [data-theme="dark"] .detected-table tr {{
        box-shadow: none;
      }}
      .detected-table tr::before {{
        content: "שורה " attr(data-row);
        position: absolute;
        top: 8px;
        inset-inline-start: 12px;
        color: var(--accent);
        font-size: .82em;
        font-weight: 700;
      }}
      .detected-table td {{
        width: 100%;
        border: 0;
        border-bottom: 1px solid var(--line);
        padding: 9px 0;
        overflow-wrap: anywhere;
      }}
      .detected-table td:last-child {{
        border-bottom: 0;
      }}
      .detected-table td::before {{
        content: attr(data-label);
        display: block;
        margin-bottom: 3px;
        color: var(--accent);
        font-size: .82em;
        font-weight: 700;
      }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="toolbar">
      <div>
        <h1>{title_html}</h1>
        <div class="meta" id="stats">מקור: {source_html}</div>
      </div>
      <div class="controls" aria-label="הגדרות קריאה">
        <button type="button" id="decrease" title="הקטנת טקסט">A-</button>
        <button type="button" id="increase" title="הגדלת טקסט">A+</button>
        <button type="button" id="spacing" title="שינוי מרווח שורות">מרווח</button>
        <select id="width" title="רוחב טור" aria-label="רוחב טור">
          <option value="680px">צר</option>
          <option value="760px" selected>רגיל</option>
          <option value="900px">רחב</option>
        </select>
        <button type="button" id="theme" title="מצב כהה או בהיר">כהה</button>
        <button type="button" id="findToggle" title="חיפוש בטקסט">חיפוש</button>
      </div>
    </div>
  </header>
  <main>
    <form class="findbar hidden" id="findbar" role="search">
      <input id="query" type="search" placeholder="חיפוש בטקסט" autocomplete="off">
      <button type="button" id="clear">נקה</button>
    </form>
    {report_html}
    <article class="reader" id="reader" aria-live="polite"></article>
  </main>
  <div class="image-lightbox" id="imageLightbox" role="dialog" aria-modal="true" aria-label="Image viewer" hidden>
    <button class="image-lightbox-close" id="imageLightboxClose" type="button" aria-label="Close image viewer">&times;</button>
    <img id="imageLightboxImage" alt="">
    <p class="image-lightbox-caption" id="imageLightboxCaption"></p>
  </div>
  <script type="application/json" id="source-text">{source_json}</script>
  <script type="application/json" id="source-html">{reader_html_json}</script>
  <script>
    const source = JSON.parse(document.getElementById('source-text').textContent);
    const preservedHtml = JSON.parse(document.getElementById('source-html').textContent);
    const reader = document.getElementById('reader');
    const stats = document.getElementById('stats');
    const query = document.getElementById('query');
    const root = document.documentElement;
    const imageLightbox = document.getElementById('imageLightbox');
    const imageLightboxImage = document.getElementById('imageLightboxImage');
    const imageLightboxCaption = document.getElementById('imageLightboxCaption');
    const imageLightboxClose = document.getElementById('imageLightboxClose');
    let imageLightboxTrigger = null;
    let fontSize = Number(localStorage.getItem('readerFontSize')) || 21;
    let lineMode = Number(localStorage.getItem('readerLineMode')) || 0;
    const lineHeights = [1.9, 2.15, 1.65];

    function escapeHtml(value) {{
      return value.replace(/[&<>"]/g, char => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }}[char]));
    }}

    function splitPages(text) {{
      return text.split(/--- עמוד (\\d+) ---/).slice(1).reduce((pages, part, index, parts) => {{
        if (index % 2 === 0) pages.push({{ number: part, body: parts[index + 1].trim() }});
        return pages;
      }}, []);
    }}

    function highlightInPlace(needle) {{
      const safeNeedle = needle.trim().replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
      if (!safeNeedle) return;
      const regex = new RegExp(safeNeedle, 'gi');
      const walker = document.createTreeWalker(reader, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const node of nodes) {{
        const value = node.nodeValue;
        regex.lastIndex = 0;
        if (!regex.test(value)) continue;
        regex.lastIndex = 0;
        const fragment = document.createDocumentFragment();
        let lastIndex = 0;
        value.replace(regex, (match, offset) => {{
          fragment.append(document.createTextNode(value.slice(lastIndex, offset)));
          const mark = document.createElement('mark');
          mark.textContent = match;
          fragment.append(mark);
          lastIndex = offset + match.length;
        }});
        fragment.append(document.createTextNode(value.slice(lastIndex)));
        node.parentNode.replaceChild(fragment, node);
      }}
    }}

    function enhanceTableCarousels() {{
      document.querySelectorAll('.table-preview-wrap').forEach(wrap => {{
        const rows = Array.from(wrap.querySelectorAll('.detected-table tr[data-row]'));
        if (rows.length < 2 || wrap.previousElementSibling?.classList.contains('table-carousel-nav')) return;

        let currentIndex = 0;
        const nav = document.createElement('div');
        nav.className = 'table-carousel-nav';
        const previous = document.createElement('button');
        previous.type = 'button';
        previous.textContent = '‹';
        previous.setAttribute('aria-label', 'Previous table row');
        const position = document.createElement('span');
        position.className = 'table-carousel-position';
        const next = document.createElement('button');
        next.type = 'button';
        next.textContent = '›';
        next.setAttribute('aria-label', 'Next table row');
        nav.append(previous, position, next);
        wrap.before(nav);

        const closestIndex = () => {{
          const wrapRect = wrap.getBoundingClientRect();
          return rows.reduce((best, row, index) => {{
            const distance = Math.abs(row.getBoundingClientRect().left - wrapRect.left);
            return distance < best.distance ? {{ index, distance }} : best;
          }}, {{ index: 0, distance: Infinity }}).index;
        }};

        function update() {{
          position.textContent = `${{currentIndex + 1}} / ${{rows.length}}`;
          previous.disabled = currentIndex === 0;
          next.disabled = currentIndex === rows.length - 1;
        }}

        function setIndex(index) {{
          currentIndex = Math.max(0, Math.min(rows.length - 1, index));
          rows[currentIndex].scrollIntoView({{ behavior: 'smooth', block: 'nearest', inline: 'start' }});
          update();
        }}

        previous.addEventListener('click', () => setIndex(currentIndex - 1));
        next.addEventListener('click', () => setIndex(currentIndex + 1));
        wrap.addEventListener('scroll', () => {{
          window.clearTimeout(wrap.carouselTimer);
          wrap.carouselTimer = window.setTimeout(() => {{
            currentIndex = closestIndex();
            update();
          }}, 80);
        }}, {{ passive: true }});
        update();
      }});
    }}

    function closeImageLightbox() {{
      if (imageLightbox.hidden) return;
      imageLightbox.hidden = true;
      imageLightboxImage.removeAttribute('src');
      imageLightboxCaption.textContent = '';
      document.body.classList.remove('lightbox-open');
      imageLightboxTrigger?.focus();
      imageLightboxTrigger = null;
    }}

    function openImageLightbox(image) {{
      imageLightboxTrigger = image;
      imageLightboxImage.src = image.currentSrc || image.src;
      imageLightboxImage.alt = image.alt || '';
      const figure = image.closest('figure');
      const localLabel = image.closest('.figure-group-item')?.querySelector('.figure-local-label');
      const sharedCaption = figure?.querySelector(':scope > figcaption');
      imageLightboxCaption.textContent = localLabel?.textContent?.trim() || sharedCaption?.textContent?.trim() || image.alt || '';
      imageLightbox.hidden = false;
      document.body.classList.add('lightbox-open');
      imageLightboxClose.focus();
    }}

    function enhanceImageLightbox() {{
      reader.querySelectorAll('.embedded-image img, .figure-group-item img').forEach(image => {{
        if (image.dataset.lightboxEnhanced) return;
        image.dataset.lightboxEnhanced = 'true';
        image.tabIndex = 0;
        image.setAttribute('role', 'button');
        image.setAttribute('aria-label', `${{image.alt || 'Image'}} - open enlarged image`);
        image.addEventListener('click', () => openImageLightbox(image));
        image.addEventListener('keydown', event => {{
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          openImageLightbox(image);
        }});
      }});
    }}

    function render() {{
      const pages = splitPages(source);
      const needle = query.value;
      if (preservedHtml) {{
        reader.innerHTML = preservedHtml;
        highlightInPlace(needle);
      }} else {{
        reader.innerHTML = pages.map(page => {{
          const lines = page.body.split('\\n');
          const first = lines.shift() || '';
          const body = escapeHtml(lines.join('\\n').trim());
          return `<section class="page" id="page-${{page.number}}">
            <div class="page-marker">עמוד ${{page.number}}</div>
            <div class="content"><h2>${{escapeHtml(first)}}</h2><p>${{body}}</p></div>
          </section>`;
        }}).join('');
        highlightInPlace(needle);
      }}
      enhanceTableCarousels();
      enhanceImageLightbox();
      const wordCount = source.split(/\\s+/).filter(Boolean).length;
      stats.textContent = `${{pages.length}} עמודים · כ-${{wordCount.toLocaleString('he-IL')}} מילים · מקור: {source_html}`;
    }}

    function applySettings() {{
      root.style.setProperty('--reader-size', `${{fontSize}}px`);
      root.style.setProperty('--reader-line', lineHeights[lineMode]);
      localStorage.setItem('readerFontSize', fontSize);
      localStorage.setItem('readerLineMode', lineMode);
    }}

    document.getElementById('increase').addEventListener('click', () => {{
      fontSize = Math.min(32, fontSize + 1);
      applySettings();
    }});
    document.getElementById('decrease').addEventListener('click', () => {{
      fontSize = Math.max(16, fontSize - 1);
      applySettings();
    }});
    document.getElementById('spacing').addEventListener('click', event => {{
      lineMode = (lineMode + 1) % lineHeights.length;
      event.currentTarget.classList.toggle('active', lineMode !== 0);
      applySettings();
    }});
    document.getElementById('width').addEventListener('change', event => {{
      root.style.setProperty('--reader-width', event.target.value);
      localStorage.setItem('readerWidth', event.target.value);
    }});
    document.getElementById('theme').addEventListener('click', event => {{
      const dark = document.body.dataset.theme !== 'dark';
      document.body.dataset.theme = dark ? 'dark' : 'light';
      event.currentTarget.textContent = dark ? 'בהיר' : 'כהה';
      localStorage.setItem('readerTheme', dark ? 'dark' : 'light');
    }});
    document.getElementById('findToggle').addEventListener('click', () => {{
      document.getElementById('findbar').classList.toggle('hidden');
      query.focus();
    }});
    document.getElementById('clear').addEventListener('click', () => {{
      query.value = '';
      render();
      query.focus();
    }});
    query.addEventListener('input', render);
    imageLightboxClose.addEventListener('click', closeImageLightbox);
    imageLightbox.addEventListener('click', event => {{
      if (event.target === imageLightbox) closeImageLightbox();
    }});
    document.addEventListener('keydown', event => {{
      if (event.key === 'Escape') closeImageLightbox();
    }});

    const savedWidth = localStorage.getItem('readerWidth');
    if (savedWidth) {{
      document.getElementById('width').value = savedWidth;
      root.style.setProperty('--reader-width', savedWidth);
    }}
    if (localStorage.getItem('readerTheme') === 'dark') {{
      document.body.dataset.theme = 'dark';
      document.getElementById('theme').textContent = 'בהיר';
    }}

    applySettings();
    render();
  </script>
</body>
</html>
"""


def create_reader(
    pdf_path: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    crop_settings: dict | None = None,
    page_numbers: set[int] | None = None,
    *,
    embed_images: bool = True,
    image_quality: int = IMAGE_JPEG_QUALITY,
    image_max_width: int = IMAGE_MAX_EMBED_WIDTH,
    progress_callback=None,
) -> Path:
    pdf_path = pdf_path.expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF")
    image_quality = validate_image_quality(image_quality)
    image_max_width = validate_image_max_width(image_max_width)

    output_dir.mkdir(parents=True, exist_ok=True)
    total_source_pages = len(PdfReader(str(pdf_path)).pages)
    if page_numbers and not any(page <= total_source_pages for page in page_numbers):
        raise ValueError(f"No pages matched the requested range: {page_range_label(page_numbers)}")
    try:
        pages, text, page_count, char_count = extract_pdf_structured(
            pdf_path,
            crop_settings=crop_settings,
            page_numbers=page_numbers,
            embed_images=embed_images,
            image_quality=image_quality,
            image_max_width=image_max_width,
            progress_callback=progress_callback,
        )
        reader_html = pages_to_html(pages)
        report = build_conversion_report(
            pages,
            total_pages=total_source_pages,
            page_numbers=page_numbers,
            crop_settings=crop_settings,
            structured=True,
            images_enabled=embed_images,
            image_quality=image_quality,
            image_max_width=image_max_width,
        )
    except Exception:
        text, page_count, char_count = extract_pdf_text(
            pdf_path,
            crop_settings=crop_settings,
            page_numbers=page_numbers,
        )
        reader_html = None
        converted_page_numbers = sorted(page_numbers) if page_numbers else list(range(1, page_count + 1))
        converted_page_numbers = [number for number in converted_page_numbers if number <= total_source_pages]
        report = build_conversion_report(
            [{"number": number, "tables": []} for number in converted_page_numbers],
            total_pages=total_source_pages,
            page_numbers=page_numbers,
            crop_settings=crop_settings,
            structured=False,
            images_enabled=embed_images,
            image_quality=image_quality,
            image_max_width=image_max_width,
        )
    if char_count == 0:
        raise ValueError("No text layer was found in this PDF. OCR is required for scanned-only PDFs.")

    title = guess_title(text, pdf_path.stem)
    html_doc = build_reader_html(text, title=title, source_name=pdf_path.name, reader_html=reader_html, report=report)
    report["estimated_output_bytes"] = len(html_doc.encode("utf-8-sig"))
    html_doc = build_reader_html(text, title=title, source_name=pdf_path.name, reader_html=reader_html, report=report)
    output_path = output_dir / f"{safe_slug(pdf_path.name)}-reader.html"
    output_path.write_text(html_doc, encoding="utf-8-sig")
    return output_path


def parse_multipart_form(body: bytes, content_type: str) -> tuple[str, bytes, dict[str, str]]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type)
    if not match:
        raise ValueError("Missing multipart boundary")
    boundary = match.group("boundary").strip().strip('"').encode()
    delimiter = b"--" + boundary
    filename = "uploaded.pdf"
    pdf_data = b""
    fields: dict[str, str] = {}

    for part in body.split(delimiter):
        if b"Content-Disposition:" not in part:
            continue
        header, _, data = part.partition(b"\r\n\r\n")
        if not data:
            continue
        data = data.rsplit(b"\r\n", 1)[0]
        name_match = re.search(rb'name="([^"]*)"', header)
        if not name_match:
            continue
        name = name_match.group(1).decode("utf-8", "replace")
        filename_match = re.search(rb'filename="([^"]*)"', header)
        if filename_match:
            filename = filename_match.group(1).decode("utf-8", "replace") or filename
            pdf_data = data
        else:
            fields[name] = data.decode("utf-8", "replace").strip()

    if not pdf_data:
        raise ValueError("No PDF file was uploaded")
    return filename, pdf_data, fields


def crop_settings_from_fields(fields: dict[str, str]) -> dict:
    return normalize_crop_settings(
        {
            side: {
                edge: fields.get(f"crop_{side}_{edge}", "0")
                for edge in ("top", "right", "bottom", "left")
            }
            for side in ("right", "left")
        }
    )


def render_pdf_previews(pdf_data: bytes) -> dict:
    if not PDFTOPPM.exists():
        raise FileNotFoundError(f"pdftoppm was not found: {PDFTOPPM}")

    previews = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / "preview.pdf"
        input_path.write_bytes(pdf_data)
        with pdfplumber.open(str(input_path)) as pdf:
            page_count = len(pdf.pages)

        for page_number, side in ((1, "right"), (2, "left")):
            if page_number > page_count:
                continue
            prefix = tmp_path / f"page-{page_number}"
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
                    "72",
                    str(input_path),
                    str(prefix),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pdftoppm failed")
            image_path = prefix.with_suffix(".png")
            image_bytes = image_path.read_bytes()
            previews.append(
                {
                    "page": page_number,
                    "side": side,
                    "image": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
                }
            )

    return {"pages": previews}


class ReaderToolHandler(BaseHTTPRequestHandler):
    server_version = "PDFReaderTool/1.0"

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(UPLOAD_PAGE)
            return
        if parsed.path.startswith("/readers/"):
            name = unquote(parsed.path.removeprefix("/readers/"))
            path = (DEFAULT_OUTPUT_DIR / name).resolve()
            if DEFAULT_OUTPUT_DIR.resolve() not in path.parents or not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/convert", "/preview"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
                raise ValueError("PDF is empty or larger than 80 MB")
            body = self.rfile.read(content_length)
            filename, data, fields = parse_multipart_form(body, self.headers.get("Content-Type", ""))
            if not data.startswith(b"%PDF"):
                raise ValueError("Uploaded file does not look like a PDF")
            if path == "/preview":
                self.send_json(render_pdf_previews(data))
                return
            crop_settings = crop_settings_from_fields(fields)
            page_numbers = parse_page_ranges(fields.get("page_range"))
            embed_images = fields.get("no_images") not in {"on", "true", "1"}
            image_quality = validate_image_quality(int(fields.get("image_quality") or IMAGE_JPEG_QUALITY))
            image_max_width = validate_image_max_width(int(fields.get("image_max_width") or IMAGE_MAX_EMBED_WIDTH))

            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = Path(tmpdir) / f"{uuid.uuid4()}.pdf"
                input_path.write_bytes(data)
                output_path = create_reader(
                    input_path,
                    DEFAULT_OUTPUT_DIR,
                    crop_settings=crop_settings,
                    page_numbers=page_numbers,
                    embed_images=embed_images,
                    image_quality=image_quality,
                    image_max_width=image_max_width,
                )
                final_path = output_path.with_name(f"{safe_slug(filename)}-reader.html")
                if final_path != output_path:
                    output_path.replace(final_path)

            link = f"/readers/{quote(final_path.name)}"
            self.send_html(SUCCESS_PAGE.format(link=html.escape(link), name=html.escape(final_path.name)))
        except Exception as exc:
            self.send_html(ERROR_PAGE.format(error=html.escape(str(exc))), HTTPStatus.BAD_REQUEST)


UPLOAD_PAGE = """<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF לדף קריאה</title>
  <style>
    body {
      margin: 0;
      background: #f6f3ed;
      color: #24211d;
      font-family: "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }
    main {
      width: min(720px, 100%);
      margin: 0 auto;
      padding: 48px 18px;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 30px;
      line-height: 1.25;
    }
    p {
      margin: 0 0 24px;
      color: #61594f;
      font-size: 17px;
      line-height: 1.7;
    }
    form {
      background: #fffdf8;
      border: 1px solid #d9d1c5;
      border-radius: 8px;
      padding: 22px;
    }
    input[type=file] {
      width: 100%;
      padding: 14px;
      border: 1px dashed #9f9588;
      border-radius: 8px;
      background: #fbf8f1;
      font: inherit;
    }
    .field {
      display: grid;
      gap: 7px;
      margin-top: 16px;
      color: #4c463f;
      font-size: 15px;
    }
    .field input {
      min-height: 42px;
      border: 1px solid #cfc5b8;
      border-radius: 7px;
      background: #fbf8f1;
      color: #24211d;
      padding: 0 12px;
      font: inherit;
      direction: ltr;
      text-align: left;
    }
    .field span {
      color: #61594f;
      font-size: 13px;
    }
    .field.checkbox {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .field.checkbox input {
      min-height: 0;
      width: 20px;
      height: 20px;
      margin: 0;
    }
    .crop-tools {
      margin-top: 20px;
      border-top: 1px solid #e0d7ca;
      padding-top: 18px;
    }
    .crop-tools h2 {
      margin: 0 0 8px;
      font-size: 20px;
    }
    .crop-tools > p {
      margin-bottom: 16px;
      font-size: 15px;
    }
    .crop-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .crop-card {
      border: 1px solid #d9d1c5;
      border-radius: 8px;
      padding: 14px;
      background: #fbf8f1;
    }
    .crop-card h3 {
      margin: 0 0 10px;
      font-size: 17px;
    }
    .crop-preview {
      position: relative;
      margin-bottom: 12px;
      border: 1px solid #9f9588;
      border-radius: 6px;
      background: #ede5d8;
      min-height: 170px;
      overflow: hidden;
      user-select: none;
      touch-action: none;
    }
    .crop-preview img {
      display: block;
      width: 100%;
      height: auto;
    }
    .crop-preview.loading::after {
      content: "טוען תצוגה...";
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: #61594f;
    }
    .crop-rect {
      position: absolute;
      inset: var(--crop-top, 0%) var(--crop-right, 0%) var(--crop-bottom, 0%) var(--crop-left, 0%);
      border: 2px solid #176f68;
      border-radius: 4px;
      background: rgba(23, 111, 104, .10);
    }
    .crop-handle {
      position: absolute;
      background: #176f68;
      opacity: .95;
    }
    .crop-handle.top,
    .crop-handle.bottom {
      left: 0;
      right: 0;
      height: 12px;
      cursor: ns-resize;
    }
    .crop-handle.top { top: -6px; }
    .crop-handle.bottom { bottom: -6px; }
    .crop-handle.left,
    .crop-handle.right {
      top: 0;
      bottom: 0;
      width: 12px;
      cursor: ew-resize;
    }
    .crop-handle.left { left: -6px; }
    .crop-handle.right { right: -6px; }
    .crop-control {
      display: grid;
      grid-template-columns: 70px 1fr 58px;
      gap: 8px;
      align-items: center;
      margin-top: 8px;
      color: #4c463f;
      font-size: 14px;
    }
    .crop-control input[type=range] {
      width: 100%;
    }
    .crop-control input[type=number] {
      width: 58px;
      padding: 5px;
      border: 1px solid #cfc5b8;
      border-radius: 5px;
      font: inherit;
    }
    .secondary-button {
      margin-top: 12px;
      min-height: 36px;
      border: 1px solid #176f68;
      background: transparent;
      color: #176f68;
      padding: 0 12px;
      font-size: 14px;
    }
    button {
      margin-top: 16px;
      min-height: 44px;
      border: 0;
      border-radius: 7px;
      background: #176f68;
      color: white;
      padding: 0 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    @media (max-width: 680px) {
      .crop-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>PDF לדף קריאה נוח</h1>
    <p>בחר קובץ PDF עם שכבת טקסט. הכלי יחלץ את הטקסט וייצור דף HTML עצמאי עם תצוגת עברית, מצב כהה, חיפוש ושליטה בגודל הטקסט.</p>
    <form action="/convert" method="post" enctype="multipart/form-data">
      <input id="pdfInput" type="file" name="pdf" accept="application/pdf,.pdf" required>
      <label class="field">Page range
        <input type="text" name="page_range" placeholder="all pages, or 1-5,8,12" inputmode="numeric">
        <span>Leave empty to convert the full PDF. Use this for quick tests before converting the whole book.</span>
      </label>
      <label class="field checkbox"><input type="checkbox" name="no_images"> Skip embedded images</label>
      <label class="field">JPEG image quality
        <input type="number" name="image_quality" min="35" max="95" value="84" inputmode="numeric">
        <span>Lower values create smaller standalone HTML files. Recommended range: 65-84.</span>
      </label>
      <label class="field">Maximum image width (pixels)
        <input type="number" name="image_max_width" min="240" max="2000" value="900" inputmode="numeric">
        <span>Use 600-900 for mobile reading; larger values preserve more detail.</span>
      </label>
      <section class="crop-tools" aria-label="גבולות סריקה">
        <h2>גבולות סריקה</h2>
        <p>הערכים הם אחוזים מהעמוד שייחתכו לפני חילוץ הטקסט. עמודים ימניים הם עמודים אי־זוגיים, ושמאליים הם זוגיים.</p>
        <div class="crop-grid">
          <div class="crop-card" data-side="right">
            <h3>עמודים ימניים</h3>
            <div class="crop-preview"><div class="crop-rect"><span class="crop-handle top" data-edge="top"></span><span class="crop-handle right" data-edge="right"></span><span class="crop-handle bottom" data-edge="bottom"></span><span class="crop-handle left" data-edge="left"></span></div></div>
            <label class="crop-control">עליון <input type="range" min="0" max="45" step="1" name="crop_right_top" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">ימין <input type="range" min="0" max="45" step="1" name="crop_right_right" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">תחתון <input type="range" min="0" max="45" step="1" name="crop_right_bottom" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">שמאל <input type="range" min="0" max="45" step="1" name="crop_right_left" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <button class="secondary-button" type="button" id="copyRightCrop">העתק לשמאליים</button>
          </div>
          <div class="crop-card" data-side="left">
            <h3>עמודים שמאליים</h3>
            <div class="crop-preview"><div class="crop-rect"><span class="crop-handle top" data-edge="top"></span><span class="crop-handle right" data-edge="right"></span><span class="crop-handle bottom" data-edge="bottom"></span><span class="crop-handle left" data-edge="left"></span></div></div>
            <label class="crop-control">עליון <input type="range" min="0" max="45" step="1" name="crop_left_top" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">ימין <input type="range" min="0" max="45" step="1" name="crop_left_right" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">תחתון <input type="range" min="0" max="45" step="1" name="crop_left_bottom" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
            <label class="crop-control">שמאל <input type="range" min="0" max="45" step="1" name="crop_left_left" value="0"><input type="number" min="0" max="45" step="1" value="0"></label>
          </div>
        </div>
      </section>
      <button type="submit">צור דף קריאה</button>
    </form>
  </main>
  <script>
    const edgeFromName = name => name.split('_').at(-1);
    function clamp(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return 0;
      return Math.min(Math.max(number, 0), 45);
    }
    function setEdgeValue(card, edge, value) {
      const safeValue = clamp(value);
      const range = card.querySelector(`[name="crop_${card.dataset.side}_${edge}"]`);
      const number = range.closest('.crop-control').querySelector('input[type=number]');
      range.value = safeValue;
      number.value = safeValue;
      updateCard(card);
    }
    function updateCard(card) {
      card.querySelectorAll('.crop-control').forEach(control => {
        const range = control.querySelector('input[type=range]');
        const number = control.querySelector('input[type=number]');
        const value = clamp(range.value);
        range.value = value;
        number.value = value;
        card.style.setProperty(`--crop-${edgeFromName(range.name)}`, `${value}%`);
      });
    }
    async function loadPreview(file) {
      const previews = document.querySelectorAll('.crop-preview');
      previews.forEach(preview => preview.classList.add('loading'));
      const formData = new FormData();
      formData.append('pdf', file);
      try {
        const response = await fetch('/preview', { method: 'POST', body: formData });
        if (!response.ok) throw new Error(await response.text());
        const data = await response.json();
        for (const page of data.pages || []) {
          const card = document.querySelector(`[data-side="${page.side}"]`);
          if (!card) continue;
          const preview = card.querySelector('.crop-preview');
          preview.querySelector('img')?.remove();
          const image = document.createElement('img');
          image.alt = page.side === 'right' ? 'עמוד ימני לדוגמה' : 'עמוד שמאלי לדוגמה';
          image.src = page.image;
          preview.prepend(image);
        }
      } catch (error) {
        console.error(error);
        alert('לא הצלחתי לטעון תצוגה מקדימה של העמודים.');
      } finally {
        previews.forEach(preview => preview.classList.remove('loading'));
      }
    }
    function installDragHandlers(card) {
      const preview = card.querySelector('.crop-preview');
      preview.querySelectorAll('.crop-handle').forEach(handle => {
        handle.addEventListener('pointerdown', event => {
          event.preventDefault();
          handle.setPointerCapture(event.pointerId);
          const edge = handle.dataset.edge;
          const move = moveEvent => {
            const rect = preview.getBoundingClientRect();
            const xPercent = ((moveEvent.clientX - rect.left) / rect.width) * 100;
            const yPercent = ((moveEvent.clientY - rect.top) / rect.height) * 100;
            if (edge === 'top') setEdgeValue(card, edge, yPercent);
            if (edge === 'bottom') setEdgeValue(card, edge, 100 - yPercent);
            if (edge === 'left') setEdgeValue(card, edge, xPercent);
            if (edge === 'right') setEdgeValue(card, edge, 100 - xPercent);
          };
          const stop = () => {
            handle.removeEventListener('pointermove', move);
            handle.removeEventListener('pointerup', stop);
            handle.removeEventListener('pointercancel', stop);
          };
          handle.addEventListener('pointermove', move);
          handle.addEventListener('pointerup', stop);
          handle.addEventListener('pointercancel', stop);
        });
      });
    }
    document.querySelectorAll('.crop-card').forEach(card => {
      card.querySelectorAll('.crop-control').forEach(control => {
        const range = control.querySelector('input[type=range]');
        const number = control.querySelector('input[type=number]');
        range.addEventListener('input', () => {
          number.value = clamp(range.value);
          updateCard(card);
        });
        number.addEventListener('input', () => {
          range.value = clamp(number.value);
          updateCard(card);
        });
      });
      installDragHandlers(card);
      updateCard(card);
    });
    document.getElementById('pdfInput').addEventListener('change', event => {
      const file = event.target.files?.[0];
      if (file) loadPreview(file);
    });
    document.getElementById('copyRightCrop').addEventListener('click', () => {
      ['top', 'right', 'bottom', 'left'].forEach(edge => {
        const source = document.querySelector(`[name="crop_right_${edge}"]`);
        const target = document.querySelector(`[name="crop_left_${edge}"]`);
        target.value = source.value;
        target.closest('.crop-control').querySelector('input[type=number]').value = source.value;
      });
      updateCard(document.querySelector('[data-side="left"]'));
    });
  </script>
</body>
</html>
"""

SUCCESS_PAGE = """<!doctype html>
<html lang="he" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>נוצר דף קריאה</title></head>
<body style="font-family: Segoe UI, Arial, sans-serif; background:#f6f3ed; color:#24211d; padding:36px">
  <main style="max-width:680px; margin:auto">
    <h1>דף הקריאה נוצר</h1>
    <p>הקובץ נשמר בשם: <code>{name}</code></p>
    <p><a href="{link}" style="font-size:20px">פתח את דף הקריאה</a></p>
    <p><a href="/">המר PDF נוסף</a></p>
  </main>
</body>
</html>
"""

ERROR_PAGE = """<!doctype html>
<html lang="he" dir="rtl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>שגיאה</title></head>
<body style="font-family: Segoe UI, Arial, sans-serif; background:#f6f3ed; color:#24211d; padding:36px">
  <main style="max-width:680px; margin:auto">
    <h1>לא הצלחתי להמיר את ה־PDF</h1>
    <p>{error}</p>
    <p><a href="/">נסה שוב</a></p>
  </main>
</body>
</html>
"""


def serve(host: str, port: int) -> None:
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), ReaderToolHandler)
    print(f"PDF reader tool running at http://{host}:{port}/")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a text-based PDF into a standalone readable HTML page.")
    parser.add_argument("pdf", nargs="?", help="Path to a PDF file")
    parser.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated HTML files")
    parser.add_argument("--serve", action="store_true", help="Run a local upload web app")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve")
    parser.add_argument("--right-crop", help="Odd/right page crop as top,right,bottom,left percentages")
    parser.add_argument("--left-crop", help="Even/left page crop as top,right,bottom,left percentages")
    parser.add_argument("--pages", help="Optional page range to convert, for example: 1-5,8,12")
    parser.add_argument("--no-images", action="store_true", help="Do not render or embed PDF images")
    parser.add_argument("--image-quality", type=int, default=IMAGE_JPEG_QUALITY, help="Embedded JPEG quality from 35 to 95 (default: 84)")
    parser.add_argument("--image-max-width", type=int, default=IMAGE_MAX_EMBED_WIDTH, help="Maximum embedded image width in pixels from 240 to 2000 (default: 900)")
    parser.add_argument("--debug-layout", action="store_true", help="Write a JSON layout debug report instead of a reader")
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.host, args.port)
        return 0

    if not args.pdf:
        parser.error("provide a PDF path, or use --serve")

    try:
        crop_settings = normalize_crop_settings(
            {
                "right": parse_crop_argument(args.right_crop),
                "left": parse_crop_argument(args.left_crop),
            }
        )
        page_numbers = parse_page_ranges(args.pages)
        if args.debug_layout:
            output_path = write_layout_debug_report(
                Path(args.pdf),
                Path(args.output_dir),
                crop_settings=crop_settings,
                page_numbers=page_numbers,
            )
        else:
            output_path = create_reader(
                Path(args.pdf),
                Path(args.output_dir),
                crop_settings=crop_settings,
                page_numbers=page_numbers,
                embed_images=not args.no_images,
                image_quality=args.image_quality,
                image_max_width=args.image_max_width,
                progress_callback=lambda message: print(message, file=sys.stderr),
            )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
