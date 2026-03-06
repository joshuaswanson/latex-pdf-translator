"""Translate a French math LaTeX PDF to English.

For each line in the PDF:
1. Classify spans as text (translatable) or math (preserve)
2. Build full line text with XXXM0XXX markers for math symbols
3. Translate via Google Translate (free, handles word reordering naturally)
4. Re-render: white-out original line, place translated text + math glyph images
"""

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from deep_translator import GoogleTranslator

# -- Configuration ----------------------------------------------------------

TEXT_FONT_PREFIXES = ("SFRM", "SFBX", "SFBI", "SFTI")

# Font prefix -> style mapping
BOLD_FONT_PREFIXES = ("SFBX", "SFBI")
ITALIC_FONT_PREFIXES = ("SFTI", "SFBI")

FONT_DIR = Path(__file__).parent

FONT_FILES = {
    "regular": FONT_DIR / "cmunrm.otf",
    "bold": FONT_DIR / "cmunbx.otf",
    "italic": FONT_DIR / "cmunti.otf",
    "bolditalic": FONT_DIR / "cmunbi.otf",
}

FONT_NAMES = {
    "regular": "CMUSerif",
    "bold": "CMUSerifBold",
    "italic": "CMUSerifItalic",
    "bolditalic": "CMUSerifBoldItalic",
}

FONT_OBJECTS = {
    style: pymupdf.Font(fontfile=str(path))
    for style, path in FONT_FILES.items()
}

# Latin Modern Math: comprehensive math font from the CM family.
# Has 99.9% coverage of all math symbols used in LaTeX PDFs.
MATH_FONT_FILE = FONT_DIR / "latinmodern-math.otf"
MATH_FONT_NAME = "LMMath"
MATH_FONT = pymupdf.Font(fontfile=str(MATH_FONT_FILE))

# Map LaTeX math font prefixes to rendering style.
# "math" = use Latin Modern Math (covers symbols, Greek, operators)
# "italic"/"bold"/"regular" = use CMU text font (for plain letters/numbers)
MATH_FONT_STYLE = {
    "CMMI": "math",      # Computer Modern Math Italic (italic letters + Greek)
    "CMBX": "bold",      # Computer Modern Bold Extended
    "CMR1": "regular",   # Computer Modern Roman 10pt
    "CMR5": "regular",   # Computer Modern Roman 5pt
    "CMR7": "regular",   # Computer Modern Roman 7pt
    "CMR8": "regular",   # Computer Modern Roman 8pt
    "CMBS": "math",      # Computer Modern Bold Symbols
    "CMSY": "math",      # Computer Modern Symbols
    "CMEX": "math",      # Computer Modern Extensions (big delimiters)
    "rsfs": "math",      # Ralph Smith Formal Script
    "EUFM": "math",      # Euler Fraktur
}

# CMEX control characters -> Unicode equivalents
CMEX_CHAR_MAP = {
    "\x00": "(", "\x01": ")", "\x02": "[", "\x03": "]",
    "\x04": "\u230A", "\x05": "\u230B",  # floor brackets
    "\x06": "\u2308", "\x07": "\u2309",  # ceiling brackets
    "\x08": "{", "\x09": "}",
    "\x0A": "\u27E8", "\x0B": "\u27E9",  # angle brackets
    "\x0C": "|",
    "\x10": "\u239B", "\x11": "\u239D",  # left paren top/bottom
    "\x12": "\u239E", "\x13": "\u23A0",  # right paren top/bottom
    "(": "(", " ": " ",
    "P": "\u2211",  # summation (text size)
    "Q": "\u220F",  # product (text size)
    "R": "\u222B",  # integral (text size)
    "X": "\u2211",  # summation (display size)
    "Y": "\u220F",  # product (display size)
    "Z": "\u222B",  # integral (display size)
    "\uf8f1": "\u23A7",  # left curly brace upper
    "\uf8f2": "\u23A8",  # left curly brace middle
    "\uf8f3": "\u23A9",  # left curly brace lower
    "\uf8f4": "\u23AB",  # right curly brace upper
}

# rsfs script letter mapping (rsfs extracts as plain letters, need Unicode script)
RSFS_CHAR_MAP = {
    "A": "\U0001D49C", "B": "\u212C", "C": "\U0001D49E",
    "D": "\U0001D49F", "E": "\u2130", "F": "\u2131",
    "G": "\U0001D4A2", "H": "\u210B", "I": "\u2110",
    "J": "\U0001D4A5", "K": "\U0001D4A6", "L": "\u2112",
    "M": "\u2133", "N": "\U0001D4A9", "O": "\U0001D4AA",
    "P": "\U0001D4AB", "Q": "\U0001D4AC", "R": "\u211B",
    "S": "\U0001D4AE", "T": "\U0001D4AF", "U": "\U0001D4B0",
    "V": "\U0001D4B1", "W": "\U0001D4B2", "X": "\U0001D4B3",
    "Y": "\U0001D4B4", "Z": "\U0001D4B5",
}

# Build math italic letter mapping (a-z -> U+1D44E..., A-Z -> U+1D434...)
# These are the Unicode "Mathematical Italic" code points
MATH_ITALIC_MAP = {}
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    cp = 0x1D434 + i
    MATH_ITALIC_MAP[ch] = chr(cp)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    cp = 0x1D44E + i
    if cp == 0x1D455:  # 'h' is at a different position (planck constant)
        MATH_ITALIC_MAP[ch] = "\u210E"
    else:
        MATH_ITALIC_MAP[ch] = chr(cp)

# Math italic Greek mapping
_GREEK_ITALIC_START = 0x1D6FC  # alpha
_GREEK_LOWER = "αβγδεζηθικλμνξοπρςστυφχψω"
for i, ch in enumerate(_GREEK_LOWER):
    MATH_ITALIC_MAP[ch] = chr(_GREEK_ITALIC_START + i)
# Additional Greek variants
MATH_ITALIC_MAP["ϕ"] = "\U0001D719"  # phi variant
MATH_ITALIC_MAP["µ"] = "\U0001D707"  # mu (from micro sign)

# Math bold letter mapping
MATH_BOLD_MAP = {}
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    MATH_BOLD_MAP[ch] = chr(0x1D400 + i)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    MATH_BOLD_MAP[ch] = chr(0x1D41A + i)
for i, ch in enumerate("0123456789"):
    MATH_BOLD_MAP[ch] = chr(0x1D7CE + i)

# Euler Fraktur (EUFM) letter mapping
EUFM_CHAR_MAP = {}
_FRAKTUR_UPPER = 0x1D504
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    cp = _FRAKTUR_UPPER + i
    # Unicode assigns some Fraktur letters to different code points
    if ch == "C": cp = 0x212D
    elif ch == "H": cp = 0x210C
    elif ch == "I": cp = 0x2111
    elif ch == "R": cp = 0x211C
    elif ch == "Z": cp = 0x2128
    EUFM_CHAR_MAP[ch] = chr(cp)
_FRAKTUR_LOWER = 0x1D51E
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    EUFM_CHAR_MAP[ch] = chr(_FRAKTUR_LOWER + i)

# Google Translate batch size (max 5000 chars per request)
BATCH_CHARS = 4800

# Math placeholder format: XXXM0XXX, XXXM1XXX, etc.
# Google Translate preserves these as opaque tokens
MATH_MARKER_RE = re.compile(r'XXXM(\d+)XXX', re.IGNORECASE)

# Post-translation terminology fixes (applied after {M0} markers restored)
TERM_FIXES = {
    # Title word order fix
    "VARIABLE {M0}-ADIC": "{M0}-ADIC VARIABLE",
    "Variable {M0}-adic": "{M0}-adic Variable",
    "variable {M0}-adic": "{M0}-adic variable",
    # Math terminology
    "temperate distributions": "tempered distributions",
    "Temperate distributions": "Tempered distributions",
    "temperate distribution": "tempered distribution",
    "Temperate distribution": "Tempered distribution",
    "temperature distributions": "tempered distributions",
    "Temperature distributions": "Tempered distributions",
    "temperature distribution": "tempered distribution",
    "Temperature distribution": "Tempered distribution",
    "measurements": "measures",
    "Measurements": "Measures",
    "Table of contents": "Table of Contents",
    "table of contents": "Table of Contents",
    "TABLE OF CONTENTS": "TABLE OF CONTENTS",
    "Class functions": "Functions of class",
    "class functions": "functions of class",
    "Summary. \u2014": "Abstract. \u2014",
    "mirabolous": "mirabolic",
    "Mirabolous": "Mirabolic",
    "mirabolique": "mirabolic",
    "Mirabolique": "Mirabolic",
    "to infinity": "at infinity",
    "demonstrate the results": "prove the results",
    "Locally analytical": "Locally analytic",
    "locally analytical": "locally analytic",
    "Analytical functions": "Analytic functions",
    "analytical functions": "analytic functions",
    "analytical function": "analytic function",
    "Distribution operations": "Operations on distributions",
    "Point Support Distributions": "Distributions with point support",
    "point support distributions": "distributions with point support",
    "Point support distributions": "Distributions with point support",
    "compact open": "compact open set",
    "let us demonstrate": "let us prove",
    "let's demonstrate": "let us prove",
    "we demonstrate": "we prove",
    "one demonstrates": "one proves",
    "whatever ": "for all ",
    "Whatever ": "For all ",
    "Demonstration": "Proof",
    "demonstration": "proof",
    "Th\u00e9or\u00e8me": "Theorem",
    "th\u00e9or\u00e8me": "theorem",
    "Corollaire": "Corollary",
    "corollaire": "corollary",
    "Remarque": "Remark",
    "remarque": "remark",
    "Proposition": "Proposition",
    "D\u00e9finition": "Definition",
    "d\u00e9finition": "definition",
    "Lemme": "Lemma",
    "lemme": "lemma",
}


# -- Font classification ----------------------------------------------------

def is_text_font(fontname: str) -> bool:
    """True if font contains translatable text (vs math notation)."""
    return any(prefix in fontname for prefix in TEXT_FONT_PREFIXES)


def _get_font_style(fontname: str) -> str:
    """Determine font style from the original font name."""
    is_bold = any(prefix in fontname for prefix in BOLD_FONT_PREFIXES)
    is_italic = any(prefix in fontname for prefix in ITALIC_FONT_PREFIXES)
    if is_bold and is_italic:
        return "bolditalic"
    if is_bold:
        return "bold"
    if is_italic:
        return "italic"
    return "regular"


# -- Data structures --------------------------------------------------------

@dataclass
class Span:
    text: str
    font: str
    size: float
    bbox: tuple  # (x0, y0, x1, y1)
    origin: tuple  # (x, y) baseline point
    is_text: bool


@dataclass
class TranslatableLine:
    page_idx: int
    spans: list[Span]
    bbox: tuple
    template: str  # text with {M0} placeholders for math
    math_spans: list[list[Span]]  # groups of consecutive math spans per placeholder
    is_toc: bool  # has dot leaders
    toc_content: str  # text portion of TOC (without dots/page number)
    toc_page_num: str  # trailing page number for TOC lines
    font_style: str  # dominant font style for the line
    text_styles: list  # [(char_count, style), ...] style runs in template text


# -- Extraction -------------------------------------------------------------

def _line_core_y(line):
    """Get the core y-range of a line, excluding tall math symbols (CMEX).

    Uses non-CMEX span bboxes to avoid tall summation/integral signs from
    inflating the y-range. For CMEX-only lines, uses origin y with tight range.
    """
    core_spans = [s for s in line["spans"] if "CMEX" not in s["font"]]
    if not core_spans:
        # All CMEX: use origin y with tight range to prevent false merges
        origins = [s["origin"][1] for s in line["spans"]]
        mid = sum(origins) / len(origins)
        return mid - 3, mid + 3
    y0 = min(s["bbox"][1] for s in core_spans)
    y1 = max(s["bbox"][3] for s in core_spans)
    return y0, y1


def _is_cmex_only(line):
    """Check if a line contains only CMEX (tall delimiter/operator) spans."""
    return all("CMEX" in s["font"] for s in line["spans"])


def _merge_same_y_lines(lines, max_x_gap=8):
    """Merge raw PDF lines that are on the same visual line (y-ranges overlap).

    This handles cases like d/dx fractions where the numerator, denominator,
    and surrounding text are separate PDF "lines" at the same y level.
    Without merging, translated text can overflow into adjacent line areas.
    Only merges lines that are also close in x (gap < max_x_gap points).
    Uses core y-range (excluding tall CMEX symbols) for overlap detection.
    CMEX-only lines are placed into y-groups by x-adjacency to avoid
    ambiguous y-overlap pulling them into the wrong visual line.
    """
    if len(lines) <= 1:
        return lines

    # Separate CMEX-only lines (tall delimiters/summations with ambiguous y)
    regular_lines = []
    cmex_only_lines = []
    for line in lines:
        if _is_cmex_only(line):
            cmex_only_lines.append(line)
        else:
            regular_lines.append(line)

    # Group regular lines by overlapping core y-ranges
    y_groups = []
    for line in regular_lines:
        placed = False
        ly0, ly1 = _line_core_y(line)
        for group in y_groups:
            gy0, gy1 = _line_core_y(group[0])
            y_overlap = min(ly1, gy1) - max(ly0, gy0)
            min_height = min(ly1 - ly0, gy1 - gy0)
            if min_height > 0 and y_overlap / min_height >= 0.5:
                group.append(line)
                placed = True
                break
        if not placed:
            y_groups.append([line])

    # Place CMEX-only lines into y-groups by x-adjacency (not y-overlap).
    # This prevents tall CMEX symbols from being pulled into the wrong
    # visual line due to ambiguous vertical extent.
    for cmex_line in cmex_only_lines:
        cx0 = cmex_line["bbox"][0]
        cx1 = cmex_line["bbox"][2]
        best_group = None
        best_gap = float('inf')
        for group in y_groups:
            for gline in group:
                gx1 = gline["bbox"][2]
                gx0 = gline["bbox"][0]
                # Check if CMEX line sits right after or before a group line
                gap = min(abs(cx0 - gx1), abs(gx0 - cx1))
                if gap < best_gap:
                    best_gap = gap
                    best_group = group
        if best_group is not None and best_gap < 20:
            best_group.append(cmex_line)
        else:
            y_groups.append([cmex_line])

    result = []
    for y_group in y_groups:
        if len(y_group) == 1:
            result.append(y_group[0])
            continue

        # Within each y-group, cluster by x-proximity
        y_group.sort(key=lambda l: l["bbox"][0])
        x_clusters = [[y_group[0]]]
        for line in y_group[1:]:
            prev_x1 = x_clusters[-1][-1]["bbox"][2]
            curr_x0 = line["bbox"][0]
            if curr_x0 - prev_x1 < max_x_gap:
                x_clusters[-1].append(line)
            else:
                x_clusters.append([line])

        for cluster in x_clusters:
            if len(cluster) == 1:
                result.append(cluster[0])
                continue

            # Only merge if cluster is small and has translatable text
            has_text = any(
                any(p in s["font"] for p in ("SFRM", "SFBX", "SFBI", "SFTI"))
                for line in cluster for s in line["spans"]
            )
            # Don't merge if multiple text lines start at left margin
            # (these are consecutive visual lines, not fragments)
            left_margin = min(l["bbox"][0] for l in cluster)
            margin_text_lines = sum(
                1 for line in cluster
                if line["bbox"][0] < left_margin + 15
                and any(p in s["font"] for p in ("SFRM", "SFBX", "SFBI", "SFTI")
                        for s in line["spans"])
            )
            if len(cluster) > 8 or not has_text or margin_text_lines > 1:
                result.extend(cluster)
            else:
                # Merge: combine spans sorted by x, union bboxes
                merged_spans = []
                for line in cluster:
                    merged_spans.extend(line["spans"])
                merged_spans.sort(key=lambda s: s["bbox"][0])
                all_bboxes = [l["bbox"] for l in cluster]
                merged_bbox = [
                    min(b[0] for b in all_bboxes),
                    min(b[1] for b in all_bboxes),
                    max(b[2] for b in all_bboxes),
                    max(b[3] for b in all_bboxes),
                ]
                result.append({"spans": merged_spans, "bbox": merged_bbox})

    return result


def extract_lines(doc) -> list[TranslatableLine]:
    """Extract all lines containing translatable French text."""
    result = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        raw = page.get_text("dict")

        for block in raw["blocks"]:
            if block["type"] != 0:
                continue

            # Skip blocks that are already in English (e.g. Abstract)
            block_text = ""
            for line in block["lines"]:
                for s in line["spans"]:
                    if is_text_font(s["font"]):
                        block_text += s["text"]
            if _is_english_block(block_text):
                continue

            # Index page numbers that are standalone lines (for TOC)
            # These appear as separate lines at the same y as the TOC entry
            line_page_nums = {}  # int(y) -> (page_num_str, right_edge)
            for ld in block["lines"]:
                all_text = "".join(s["text"] for s in ld["spans"]).strip()
                if re.match(r'^\d{1,3}$', all_text):
                    y = ld["bbox"][1]
                    entry = (all_text, ld["bbox"][2])
                    line_page_nums[int(y)] = entry
                    line_page_nums[int(y) + 1] = entry  # tolerance

            math_only_lines = []  # [(bbox, [Span, ...]), ...] for fraction numerators etc.
            block_lines_start = len(result)  # track where this block's lines start

            # Merge lines at the same y-level (e.g. fraction parts + surrounding text)
            merged_block_lines = _merge_same_y_lines(block["lines"])

            for line_data in merged_block_lines:
                spans = []
                for s in line_data["spans"]:
                    spans.append(Span(
                        text=s["text"],
                        font=s["font"],
                        size=s["size"],
                        bbox=tuple(s["bbox"]),
                        origin=tuple(s["origin"]),
                        is_text=is_text_font(s["font"]),
                    ))
                if not spans:
                    continue

                # Build template with math placeholders.
                # Merge consecutive math spans into single placeholders so
                # Google Translate sees them as one token (e.g. " p" not " "+"p")
                # Also track font style per character for mixed-style rendering.
                parts = []
                math_spans = []  # list of lists (merged groups)
                text_styles = []  # [(char_count, style), ...]
                in_math = False
                for span in spans:
                    if span.is_text:
                        in_math = False
                        parts.append(span.text)
                        style = _get_font_style(span.font)
                        if text_styles and text_styles[-1][1] == style:
                            text_styles[-1] = (text_styles[-1][0] + len(span.text), style)
                        else:
                            text_styles.append((len(span.text), style))
                    else:
                        if in_math:
                            # Extend current math group
                            math_spans[-1].append(span)
                        else:
                            # Start new math group
                            in_math = True
                            idx = len(math_spans)
                            placeholder = f"{{M{idx}}}"
                            parts.append(placeholder)
                            math_spans.append([span])
                            # Math placeholders don't contribute to text_styles
                template = "".join(parts)

                # Skip lines with no meaningful text (but remember math-only
                # lines so we can attach them to nearby translatable lines)
                text_only = "".join(s.text for s in spans if s.is_text).strip()
                if not text_only:
                    if math_spans:
                        # Math-only line (e.g. fraction numerator) - save for later
                        math_only_lines.append((line_data["bbox"], math_spans[0]))
                    continue
                # Skip pure numbers, punctuation, dots
                if re.match(r'^[\s\d.,;:!?()\[\]/*+=\-]+$', text_only):
                    continue

                # Detect TOC lines (dot leaders)
                is_toc = bool(re.search(r'(\.\s){3,}', template))
                toc_content = ""
                toc_page_num = ""
                if is_toc:
                    toc_content, toc_page_num = _parse_toc_line(template)
                    # Page number may be in a separate line at same y
                    if not toc_page_num:
                        y_key = int(line_data["bbox"][1])
                        pn_entry = line_page_nums.get(y_key)
                        if pn_entry:
                            toc_page_num = pn_entry[0]
                            # Extend bbox to include page number's right edge
                            bbox = list(line_data["bbox"])
                            bbox[2] = pn_entry[1]
                            line_data = dict(line_data, bbox=bbox)

                # Determine dominant font style from text spans
                font_style = _dominant_font_style(spans)

                result.append(TranslatableLine(
                    page_idx=page_idx,
                    spans=spans,
                    bbox=tuple(line_data["bbox"]),
                    template=template,
                    math_spans=math_spans,
                    is_toc=is_toc,
                    toc_content=toc_content,
                    toc_page_num=toc_page_num,
                    font_style=font_style,
                    text_styles=text_styles,
                ))

            # Attach math-only lines (fraction numerators) to nearby translatable lines
            for mo_bbox, mo_spans in math_only_lines:
                mo_y = (mo_bbox[1] + mo_bbox[3]) / 2
                mo_x = mo_bbox[0]
                best_line = None
                best_dist = 20  # max vertical distance to consider
                for tl in result[block_lines_start:]:
                    if not tl.math_spans:
                        continue
                    tl_y = (tl.bbox[1] + tl.bbox[3]) / 2
                    dist = abs(mo_y - tl_y)
                    first_math_x = tl.math_spans[0][0].bbox[0]
                    if dist < best_dist and abs(mo_x - first_math_x) < 15:
                        best_dist = dist
                        best_line = tl
                if best_line:
                    # Prepend to the first math group
                    best_line.math_spans[0] = mo_spans + best_line.math_spans[0]

    return result


def _dominant_font_style(spans: list[Span]) -> str:
    """Find the most common font style among text spans by character count."""
    style_counts = {}
    for s in spans:
        if s.is_text and s.text.strip():
            style = _get_font_style(s.font)
            style_counts[style] = style_counts.get(style, 0) + len(s.text)
    if not style_counts:
        return "regular"
    return max(style_counts, key=style_counts.get)


def _is_english_block(text: str) -> bool:
    """Check if a block of text is already in English."""
    words = re.findall(r'[a-zA-Z]{3,}', text.lower())
    if len(words) < 5:
        return False
    english_markers = {
        'the', 'this', 'text', 'compilation', 'basic', 'results',
        'functional', 'analysis', 'over', 'stress', 'mainly', 'put',
        'space', 'functions', 'its', 'dual', 'distributions', 'order',
        'point', 'being', 'that', 'these', 'spaces', 'have', 'come',
        'play', 'important', 'role', 'theory', 'abstract',
    }
    count = sum(1 for w in words if w in english_markers)
    return count / len(words) > 0.3


def _parse_toc_line(template: str) -> tuple[str, str]:
    """Extract (content_text, page_number) from a TOC line template."""
    dot_match = re.search(r'(\.\s){3,}', template)
    if not dot_match:
        return template.strip(), ""

    content = template[:dot_match.start()].strip()
    after = template[dot_match.end():].strip()

    page_num = ""
    m = re.match(r'[.\s]*(\d+)\s*$', after)
    if m:
        page_num = m.group(1)

    return content, page_num


# -- Translation (Google Translate, free) -----------------------------------

def _prepare_gt_text(text: str) -> str:
    """Convert a template string to Google Translate input."""
    gt_text = re.sub(r'\{M(\d+)\}', r'XXXM\1XXX', text)
    # Ensure spaces around markers so Google sees them as separate tokens
    gt_text = re.sub(r'([a-zA-ZÀ-ÿ])(XXXM\d+XXX)', r'\1 \2', gt_text)
    gt_text = re.sub(r'(XXXM\d+XXX)([a-zA-ZÀ-ÿ])', r'\1 \2', gt_text)
    return gt_text


def _postprocess_translation(text: str) -> str:
    """Convert markers back and apply fixes to a translated string."""
    text = MATH_MARKER_RE.sub(r'{M\1}', text)
    # Clean up extra spaces around markers
    text = re.sub(r'\s+(\{M\d+\})\s+', r' \1 ', text)
    text = re.sub(r'\s+(\{M\d+\})-', r' \1-', text)
    text = _fix_terminology(text)
    return text


def _group_paragraphs(lines: list[TranslatableLine]) -> list[list[int]]:
    """Group consecutive body-text lines into paragraphs for better translation.

    Returns list of groups, where each group is a list of indices into `lines`.
    Only merges lines that have NO math spans (pure text) -- lines with math
    markers stay standalone to avoid marker redistribution issues.
    TOC lines and headings also stay as single-line groups.
    """
    groups = []
    current_group = []

    def _is_mergeable(line):
        """A line can be merged into a paragraph only if it has no math."""
        if line.is_toc:
            return False
        if line.font_style == "bold":
            return False
        if line.spans and line.spans[0].size > 11:
            return False
        if line.math_spans:
            return False
        return True

    for i, line in enumerate(lines):
        if not _is_mergeable(line):
            if current_group:
                groups.append(current_group)
                current_group = []
            groups.append([i])
            continue

        # Check if this line continues the current group
        if current_group:
            prev = lines[current_group[-1]]
            same_page = line.page_idx == prev.page_idx
            similar_x = abs(line.bbox[0] - prev.bbox[0]) < 20
            consecutive_y = (line.bbox[1] - prev.bbox[3]) < prev.spans[0].size
            same_size = abs(line.spans[0].size - prev.spans[0].size) < 0.5

            if same_page and similar_x and consecutive_y and same_size:
                current_group.append(i)
            else:
                groups.append(current_group)
                current_group = [i]
        else:
            current_group = [i]

    if current_group:
        groups.append(current_group)

    return groups


def _merge_paragraph_templates(lines: list[TranslatableLine],
                                indices: list[int]) -> tuple[str, list[list[Span]]]:
    """Merge multiple line templates into one paragraph template.

    Renumbers math markers sequentially and merges math_spans lists.
    Returns (merged_template, merged_math_spans).
    """
    merged_parts = []
    merged_math = []

    for idx in indices:
        line = lines[idx]
        text = line.toc_content if line.is_toc else line.template
        # Renumber {M0}, {M1}, ... relative to current merged_math length
        offset = len(merged_math)
        renumbered = re.sub(
            r'\{M(\d+)\}',
            lambda m: f"{{M{int(m.group(1)) + offset}}}",
            text,
        )
        merged_parts.append(renumbered)
        merged_math.extend(line.math_spans)

    return " ".join(merged_parts), merged_math


def _split_translation(translated: str, lines: list[TranslatableLine],
                       indices: list[int],
                       merged_math: list[list[Span]]) -> list[str]:
    """Split a paragraph translation back into per-line translations.

    Distributes words proportionally based on original line text lengths,
    preserving math markers on the correct lines.
    """
    if len(indices) == 1:
        return [translated]

    # Calculate target character count per line (proportional to original)
    orig_lengths = []
    for idx in indices:
        line = lines[idx]
        text = line.toc_content if line.is_toc else line.template
        orig_lengths.append(len(text))
    total = sum(orig_lengths)
    if total == 0:
        return [translated] + [""] * (len(indices) - 1)

    # Split translated text into tokens (words and math markers)
    tokens = re.split(r'(\s+)', translated)
    tokens = [t for t in tokens if t]  # remove empty

    # Distribute tokens across lines
    result_lines = []
    token_idx = 0
    for i, idx in enumerate(indices):
        target_len = orig_lengths[i]
        target_frac = target_len / total
        target_chars = int(len(translated) * target_frac)

        line_tokens = []
        line_len = 0

        while token_idx < len(tokens):
            tok = tokens[token_idx]
            # Always put at least one token per line
            if not line_tokens or (line_len + len(tok) <= target_chars * 1.3):
                line_tokens.append(tok)
                line_len += len(tok)
                token_idx += 1
            else:
                break

            # Don't exceed target too much (except for last line)
            if i < len(indices) - 1 and line_len >= target_chars:
                break

        result_lines.append("".join(line_tokens).strip())

    # Last line gets remaining tokens
    if token_idx < len(tokens):
        remaining = "".join(tokens[token_idx:]).strip()
        if result_lines:
            result_lines[-1] = (result_lines[-1] + " " + remaining).strip()
        else:
            result_lines.append(remaining)

    # Ensure we have exactly the right number of lines
    while len(result_lines) < len(indices):
        result_lines.append("")

    # Remap math markers back to per-line numbering
    for i, idx in enumerate(indices):
        line = lines[idx]
        n_math = len(line.math_spans)
        # Calculate the offset for this line's markers in the merged numbering
        offset = sum(len(lines[indices[j]].math_spans) for j in range(i))

        line_text = result_lines[i]
        # Replace merged marker numbers with local numbers
        for merged_idx in range(offset, offset + n_math):
            local_idx = merged_idx - offset
            line_text = line_text.replace(
                f"{{M{merged_idx}}}",
                f"{{MLOCAL{local_idx}}}",
            )
        # Clean up: remove markers that belong to other lines
        line_text = re.sub(r'\{M\d+\}', '', line_text)
        # Rename local markers back
        line_text = re.sub(r'\{MLOCAL(\d+)\}', r'{M\1}', line_text)
        result_lines[i] = line_text.strip()

    return result_lines


def _load_cache(cache_path: Path) -> dict:
    """Load translation cache from disk."""
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def _save_cache(cache_path: Path, cache: dict):
    """Save translation cache to disk."""
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))


def translate_lines(lines: list[TranslatableLine],
                    cache_path: Path | None = None) -> list[str]:
    """Translate all lines via Google Translate (free).

    Groups consecutive body-text lines into paragraphs for better translation
    quality, then splits results back to per-line for rendering.
    Uses a disk cache to avoid re-translating on subsequent runs.
    """
    translator = GoogleTranslator(source='fr', target='en')
    groups = _group_paragraphs(lines)

    # Load translation cache
    cache = _load_cache(cache_path) if cache_path else {}

    # Prepare paragraph texts
    group_texts = []
    group_math = []
    for group in groups:
        if len(group) == 1:
            line = lines[group[0]]
            text = line.toc_content if line.is_toc else line.template
            group_texts.append(_prepare_gt_text(text))
            group_math.append(line.math_spans)
        else:
            merged, merged_ms = _merge_paragraph_templates(lines, group)
            group_texts.append(_prepare_gt_text(merged))
            group_math.append(merged_ms)

    # Separate cached vs uncached
    to_translate = []  # (group_idx, text)
    translated_groups = []  # (group_idx, result)

    for i, text in enumerate(group_texts):
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in cache:
            translated_groups.append((i, cache[cache_key]))
        else:
            to_translate.append((i, text, cache_key))

    if to_translate:
        print(f"  {len(translated_groups)} cached, {len(to_translate)} to translate")
    else:
        print(f"  All {len(translated_groups)} translations cached")

    # Translate uncached in batches with retry
    batch = []
    batch_len = 0

    for i, text, cache_key in to_translate:
        batch.append((i, text, cache_key))
        batch_len += len(text)

        if batch_len >= BATCH_CHARS or (i, text, cache_key) == to_translate[-1]:
            batch_texts = [t for _, t, _ in batch]
            results = None

            for attempt in range(3):
                try:
                    results = translator.translate_batch(batch_texts)
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** (attempt + 1)
                        print(f"    Retry {attempt + 1}/3 after {wait}s: {e}")
                        time.sleep(wait)
                    else:
                        print(f"    WARNING: translation failed after 3 attempts: {e}")
                        results = batch_texts

            for (idx, _orig, ck), result in zip(batch, results):
                if result is None:
                    result = batch_texts[idx - batch[0][0]]
                translated_groups.append((idx, result))
                cache[ck] = result

            batch = []
            batch_len = 0
            time.sleep(0.3)

    # Save updated cache
    if cache_path:
        _save_cache(cache_path, cache)

    # Sort and postprocess
    translated_groups.sort(key=lambda x: x[0])

    # Split paragraph translations back to per-line
    translations = [""] * len(lines)
    for (group_idx, translated_text), group in zip(translated_groups, groups):
        processed = _postprocess_translation(translated_text)

        if len(group) == 1:
            translations[group[0]] = processed
        else:
            # Split merged translation back to individual lines
            per_line = _split_translation(processed, lines, group,
                                          group_math[group_idx])
            for idx, text in zip(group, per_line):
                translations[idx] = text

    return translations


def _fix_terminology(text: str) -> str:
    """Fix known Google Translate mistakes for math terminology."""
    for wrong, right in TERM_FIXES.items():
        text = text.replace(wrong, right)
    return text


# -- Rendering --------------------------------------------------------------

def render_all(work_doc, orig_doc, lines: list[TranslatableLine],
               translations: list[str]):
    """Apply all translations to the work document."""
    # Group lines by page, filtering unchanged ones
    page_lines = {}
    for line, translated in zip(lines, translations):
        original = line.toc_content if line.is_toc else line.template
        if translated.strip() == original.strip():
            continue
        page_lines.setdefault(line.page_idx, []).append((line, translated))

    # Track rendered text extents for link rectangle adjustment
    rendered_extents = {}  # (page_idx, round(y_mid)) -> (x0, text_end_x)

    # Collect link annotation colors and text from ALL pages before any redaction
    all_annot_colors = {}  # (page_idx, round_x0, round_y0) -> (r, g, b)
    all_link_texts = {}  # (page_idx, round_x0, round_y0) -> text under link
    for page_idx in range(len(work_doc)):
        page = work_doc[page_idx]
        for link in page.get_links():
            xref = link.get("xref")
            if not xref:
                continue
            key = (page_idx, round(link["from"].x0, 1), round(link["from"].y0, 1))
            # Read color from raw xref (annot.colors often returns None)
            raw = work_doc.xref_object(xref)
            m = re.search(r'/C\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]', raw)
            if m:
                all_annot_colors[key] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            else:
                all_annot_colors[key] = (1, 0, 0)  # default red
            # Collect text under link for search-based repositioning
            link_text = page.get_text("text", clip=link["from"]).strip()
            if link_text:
                all_link_texts[key] = link_text

    changed = 0
    for page_idx in sorted(page_lines):
        page = work_doc[page_idx]
        orig_page = orig_doc[page_idx]

        # Save link annotations before redaction removes them
        saved_links = list(page.get_links())

        # Phase 1: Add redaction annotations for all lines on this page
        for line, _translated in page_lines[page_idx]:
            rect = _get_whiteout_rect(page, line)
            page.add_redact_annot(rect, fill=(1, 1, 1))

        # Apply all redactions at once (actually removes underlying content)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        # Restore links removed by redaction and ensure all have colored borders
        surviving = {
            (round(l["from"].x0, 1), round(l["from"].y0, 1))
            for l in page.get_links()
        }
        for link in saved_links:
            key = (round(link["from"].x0, 1), round(link["from"].y0, 1))
            if key not in surviving:
                page.insert_link(link)

        # (Link colors are fixed in a post-processing pass after save/reload)

        # Register fonts AFTER redactions (redactions can remove font resources)
        for style, name in FONT_NAMES.items():
            page.insert_font(
                fontname=name,
                fontfile=str(FONT_FILES[style]),
            )
        page.insert_font(
            fontname=MATH_FONT_NAME,
            fontfile=str(MATH_FONT_FILE),
        )

        # Phase 2: Re-render translated text + math glyphs
        for line, translated in page_lines[page_idx]:
            text_end_x = _render_line_content(page, orig_page, line, translated)
            # Record rendered text extent for link rectangle adjustment
            y_mid = (line.bbox[1] + line.bbox[3]) / 2
            orig_x0, orig_x1 = line.bbox[0], line.bbox[2]
            rendered_extents[(page_idx, round(y_mid))] = (orig_x0, orig_x1, line.bbox[0], text_end_x)
            changed += 1

    print(f"  {changed} lines modified")
    return all_annot_colors, rendered_extents, all_link_texts


def _get_whiteout_rect(page, line: TranslatableLine) -> pymupdf.Rect:
    """Compute the rectangle to white-out for a line."""
    x0, y0, x1, y1 = line.bbox
    rect = pymupdf.Rect(x0 - 1, y0 - 1, x1 + 2, y1 + 1)
    if line.is_toc:
        # Extend TOC lines to right margin
        rect.x1 = page.rect.width - 30
    return rect


def _build_style_map(line: TranslatableLine, translated: str) -> list:
    """Map font styles from original line onto translated text.

    Returns a list of (text_segment, style) pairs covering the translated text.
    Distributes original style runs proportionally across the translated text.
    """
    # Total original text length (excluding math markers)
    orig_text_len = sum(count for count, _ in line.text_styles)
    if not orig_text_len or not line.text_styles:
        return [(translated, line.font_style)]

    # Extract just the text portions from translated (skip math markers)
    text_only = re.sub(r'\{M\d+\}', '', translated)
    trans_text_len = len(text_only)
    if not trans_text_len:
        return [(translated, line.font_style)]

    # Build character-level style array for translated text proportionally
    style_breaks = []  # (fraction, style)
    cumulative = 0
    for count, style in line.text_styles:
        cumulative += count
        style_breaks.append((cumulative / orig_text_len, style))

    # Now split translated text into styled segments
    # We work on the full translated text (with markers), tracking text position
    result = []
    current_style_idx = 0
    text_chars_seen = 0

    # Parse into segments first, then assign styles
    segments = _parse_segments(translated)
    for seg in segments:
        if seg["type"] == "math":
            # Math markers pass through with current style
            result.append((f"{{M{seg['index']}}}", "math"))
        else:
            text = seg["text"]
            # Split this text segment at style boundaries
            remaining = text
            while remaining:
                if current_style_idx >= len(style_breaks):
                    current_style = line.text_styles[-1][1]
                else:
                    current_style = style_breaks[current_style_idx][1] if current_style_idx < len(style_breaks) else line.font_style

                # How many more text chars until next style break?
                if current_style_idx < len(style_breaks):
                    break_at_frac = style_breaks[current_style_idx][0]
                    break_at_chars = int(break_at_frac * trans_text_len)
                    chars_until_break = break_at_chars - text_chars_seen
                else:
                    chars_until_break = len(remaining)

                if chars_until_break <= 0:
                    current_style_idx += 1
                    continue

                take = min(len(remaining), chars_until_break)
                chunk = remaining[:take]
                remaining = remaining[take:]
                text_chars_seen += take
                result.append((chunk, current_style))

                if text_chars_seen >= break_at_chars if current_style_idx < len(style_breaks) else False:
                    current_style_idx += 1

    return result


def _fix_style_boundaries(segments):
    """Fix style boundaries to respect bracket groups and label patterns."""
    # Flatten to character-level, tracking math marker positions
    chars = []  # [[char, style], ...]
    items = []  # [("char", idx_in_chars) | ("math", marker_text), ...]

    for text, style in segments:
        if style == "math":
            items.append(("math", text))
        else:
            for ch in text:
                items.append(("char", len(chars)))
                chars.append([ch, style])

    if len(chars) < 2:
        return segments

    full_text = "".join(ch for ch, _ in chars)

    # Fix 1: Unify style within square brackets [...]
    for m in re.finditer(r'\[[^\]]*\]', full_text):
        s, e = m.start(), m.end()
        styles = {}
        for k in range(s, min(e, len(chars))):
            st = chars[k][1]
            styles[st] = styles.get(st, 0) + 1
        if styles:
            dominant = max(styles, key=styles.get)
            for k in range(s, min(e, len(chars))):
                chars[k][1] = dominant

    # Fix 2: Extend style through trailing periods/digits at style boundaries
    # This fixes labels like "I.1.1." where proportional mapping splits mid-label
    i = 0
    while i < len(chars) - 1:
        if chars[i][1] != chars[i + 1][1]:
            prev_style = chars[i][1]
            j = i + 1
            while j < len(chars) and chars[j][0] in '.,;:0123456789':
                j += 1
            if j > i + 1:
                for k in range(i + 1, j):
                    chars[k][1] = prev_style
                i = j
            else:
                i += 1
        else:
            i += 1

    # Rebuild segments from items list
    result = []
    current_text = ""
    current_style = None

    for item_type, item_data in items:
        if item_type == "math":
            if current_text:
                result.append((current_text, current_style))
                current_text = ""
                current_style = None
            result.append((item_data, "math"))
        else:
            char_idx = item_data
            ch, style = chars[char_idx]
            if style != current_style:
                if current_text:
                    result.append((current_text, current_style))
                current_text = ch
                current_style = style
            else:
                current_text += ch

    if current_text:
        result.append((current_text, current_style))

    return result


def _render_line_content(page, orig_page, line: TranslatableLine,
                         translated: str) -> float:
    """Render translated text + math glyphs onto the page.
    Returns x position after rendering content (before TOC dots)."""
    x0, y0, x1, y1 = line.bbox
    # Use font size from the first text span (not math, which may be subscript-sized)
    fontsize = line.spans[0].size
    for s in line.spans:
        if s.is_text and s.text.strip():
            fontsize = s.size
            break

    # Find baseline from first text span's origin
    baseline_y = y1
    for s in line.spans:
        if s.is_text and s.text.strip():
            baseline_y = s.origin[1]
            break

    # Build styled segments from translated text
    styled_segments = _build_style_map(line, translated)
    styled_segments = _fix_style_boundaries(styled_segments)

    # Render from left to right
    x = x0
    for text, style in styled_segments:
        if style == "math":
            # Parse math marker
            m = re.match(r'\{M(\d+)\}', text)
            if not m:
                continue
            idx = int(m.group(1))
            if idx >= len(line.math_spans):
                continue
            group = line.math_spans[idx]
            # Pre-process: identify fraction components (stacked spans)
            # by checking for spans that overlap in x but differ in y.
            # Require x-centers to be close (fractions are centered) to avoid
            # false positives on subscript/superscript pairs.
            stacked = set()  # indices of spans that are fraction parts
            for gi in range(len(group)):
                for gj in range(gi + 1, len(group)):
                    s1, s2 = group[gi], group[gj]
                    x_overlap = min(s1.bbox[2], s2.bbox[2]) - max(s1.bbox[0], s2.bbox[0])
                    if x_overlap > 0 and abs(s1.origin[1] - s2.origin[1]) > 3:
                        # Check x-centers are aligned (fraction, not sub/superscript)
                        c1 = (s1.bbox[0] + s1.bbox[2]) / 2
                        c2 = (s2.bbox[0] + s2.bbox[2]) / 2
                        min_w = min(s1.bbox[2] - s1.bbox[0], s2.bbox[2] - s2.bbox[0])
                        if abs(c1 - c2) < max(min_w * 0.7, 2.0):
                            # Reject if these are sub/superscripts of a base character:
                            # both start right at the right edge of a preceding span
                            left_edge = min(s1.bbox[0], s2.bbox[0])
                            is_sub_super = False
                            for gk in range(len(group)):
                                if gk == gi or gk == gj:
                                    continue
                                base = group[gk]
                                if abs(base.bbox[2] - left_edge) < 1.5:
                                    is_sub_super = True
                                    break
                            if not is_sub_super:
                                stacked.add(gi)
                                stacked.add(gj)

            # Render: stacked spans at same x, sequential spans advance x
            frac_x_start = None
            frac_max_width = 0
            for gi, ms in enumerate(group):
                math_rect = pymupdf.Rect(ms.bbox)
                if math_rect.is_empty or math_rect.width < 0.5:
                    continue
                if gi in stacked:
                    if frac_x_start is None:
                        frac_x_start = x
                    rendered = _render_math_span(page, orig_page, ms, frac_x_start, baseline_y)
                    frac_max_width = max(frac_max_width, rendered)
                else:
                    # Flush any pending fraction width
                    if frac_x_start is not None:
                        _draw_fraction_bars(page, group, stacked,
                                            frac_x_start,
                                            frac_x_start + frac_max_width,
                                            baseline_y)
                        x = frac_x_start + frac_max_width + 1.5
                        frac_x_start = None
                        frac_max_width = 0
                    rendered = _render_math_span(page, orig_page, ms, x, baseline_y)
                    x += rendered
            # Flush final fraction (if stacked spans are at end of group)
            if frac_x_start is not None:
                _draw_fraction_bars(page, group, stacked,
                                    frac_x_start,
                                    frac_x_start + frac_max_width,
                                    baseline_y)
                x = frac_x_start + frac_max_width + 1.5
        else:
            if not text:
                continue
            font_name = FONT_NAMES[style]
            font_obj = FONT_OBJECTS[style]
            page.insert_text(
                pymupdf.Point(x, baseline_y),
                text,
                fontname=font_name,
                fontsize=fontsize,
                color=(0, 0, 0),
            )
            x += font_obj.text_length(text, fontsize=fontsize)

    text_end_x = x  # Position after title text, before dots

    # For TOC lines, add dot leaders and page number
    if line.is_toc:
        toc_font_name = FONT_NAMES[line.font_style]
        toc_font_obj = FONT_OBJECTS[line.font_style]
        _render_toc_dots(page, x, baseline_y, fontsize, toc_font_name,
                         toc_font_obj, line)

    return text_end_x


def _math_font_prefix(font: str) -> str:
    """Extract the base font prefix (e.g., 'CMMI' from 'FITVLG+CMMI10')."""
    name = font.split("+")[-1] if "+" in font else font
    # Match known prefixes
    for prefix in ("CMMI", "CMBX", "CMEX", "CMSY", "CMBS", "rsfs", "EUFM"):
        if name.startswith(prefix):
            return prefix
    # CMR with size suffix (CMR10, CMR7, CMR5, CMR8)
    if name.startswith("CMR"):
        return name[:4]  # CMR1, CMR5, CMR7, CMR8
    return name[:4]


def _map_math_text(text: str, font_prefix: str) -> str:
    """Map math span text to Unicode characters renderable by Latin Modern Math."""
    if font_prefix == "CMEX":
        return "".join(CMEX_CHAR_MAP.get(ch, ch) for ch in text)
    if font_prefix == "rsfs":
        return "".join(RSFS_CHAR_MAP.get(ch, ch) for ch in text)
    if font_prefix == "CMMI":
        # Math italic: map letters to Unicode math italic code points
        return "".join(MATH_ITALIC_MAP.get(ch, ch) for ch in text)
    if font_prefix == "CMBX":
        # Math bold: map letters to Unicode math bold code points
        return "".join(MATH_BOLD_MAP.get(ch, ch) for ch in text)
    if font_prefix == "EUFM":
        return "".join(EUFM_CHAR_MAP.get(ch, ch) for ch in text)
    return text


def _copy_original_glyph(page, orig_page, ms: Span, x: float,
                         baseline_y: float) -> float:
    """Copy a glyph from the original page to preserve its exact appearance.

    Used for fonts like rsfs and EUFM where pymupdf can't render the Unicode
    equivalents via insert_text (supplementary plane limitation).
    """
    orig_rect = pymupdf.Rect(ms.bbox)
    if orig_rect.is_empty or orig_rect.width < 0.5:
        return 0

    # Add padding to avoid clipping glyph edges (especially cursive ascenders)
    pad_x = 1.5
    pad_top = 2.5  # extra for ascenders/flourishes
    pad_bot = 1.5
    src_rect = pymupdf.Rect(
        orig_rect.x0 - pad_x, orig_rect.y0 - pad_top,
        orig_rect.x1 + pad_x, orig_rect.y1 + pad_bot,
    )

    # Calculate destination rectangle preserving size (with matching padding)
    y_offset = ms.bbox[1] - baseline_y
    dst_rect = pymupdf.Rect(
        x - pad_x, baseline_y + y_offset - pad_top,
        x + orig_rect.width + pad_x, baseline_y + y_offset + orig_rect.height + pad_bot,
    )

    # Copy from original page
    page.show_pdf_page(dst_rect, orig_page.parent, orig_page.number, clip=src_rect)
    return orig_rect.width


def _render_math_span(page, orig_page, ms: Span, x: float,
                      baseline_y: float) -> float:
    """Render a single math span as vector text, returning width consumed."""
    prefix = _math_font_prefix(ms.font)
    style = MATH_FONT_STYLE.get(prefix)

    if style is None:
        # Unknown font - use original bbox width as spacing
        return pymupdf.Rect(ms.bbox).width

    # For rsfs (script) and EUFM (fraktur) fonts, copy the original glyph
    # because pymupdf can't render supplementary plane Unicode via insert_text
    if prefix in ("rsfs", "EUFM") and ms.text.strip():
        return _copy_original_glyph(page, orig_page, ms, x, baseline_y)

    # For CMEX characters not in the mapping, copy from original
    if prefix == "CMEX" and any(ch not in CMEX_CHAR_MAP for ch in ms.text if ch.strip()):
        return _copy_original_glyph(page, orig_page, ms, x, baseline_y)

    # Determine font to use
    if style == "math":
        m_font_name = MATH_FONT_NAME
        m_font_obj = MATH_FONT
        text = _map_math_text(ms.text, prefix)
    else:
        m_font_name = FONT_NAMES[style]
        m_font_obj = FONT_OBJECTS[style]
        text = ms.text

    if not text or text.isspace():
        return m_font_obj.text_length(text, fontsize=ms.size) if text else 0

    # Use the math span's original baseline for vertical positioning
    math_baseline = ms.origin[1]
    y_offset = math_baseline - baseline_y

    page.insert_text(
        pymupdf.Point(x, baseline_y + y_offset),
        text,
        fontname=m_font_name,
        fontsize=ms.size,
        color=(0, 0, 0),
    )
    return m_font_obj.text_length(text, fontsize=ms.size)


def _draw_fraction_bars(page, group: list, stacked: set,
                        frac_x_start: float, frac_x_end: float,
                        baseline_y: float):
    """Draw fraction bars for stacked spans within a math group."""
    if not stacked:
        return
    stacked_spans = [group[i] for i in sorted(stacked)]
    if len(stacked_spans) < 2:
        return
    stacked_spans.sort(key=lambda s: s.bbox[1])
    upper = stacked_spans[0]
    lower = stacked_spans[-1]
    # Bar y: midpoint between bottom of upper and top of lower, offset from baseline
    bar_y_orig = (upper.bbox[3] + lower.bbox[1]) / 2
    bar_y = baseline_y + (bar_y_orig - baseline_y)
    shape = page.new_shape()
    shape.draw_line(
        pymupdf.Point(frac_x_start, bar_y),
        pymupdf.Point(frac_x_end, bar_y),
    )
    shape.finish(color=(0, 0, 0), width=0.4)
    shape.commit()


def _render_toc_dots(page, x_after_text: float, baseline_y: float,
                     fontsize: float, font_name: str, font_obj,
                     line: TranslatableLine):
    """Render dot leaders and right-aligned page number."""
    right_edge = line.bbox[2]
    dot_unit = font_obj.text_length(". ", fontsize=fontsize)
    dots_start = x_after_text + 4

    if line.toc_page_num:
        pn_width = font_obj.text_length(line.toc_page_num, fontsize=fontsize)
        pn_x = right_edge - pn_width
        dots_end = pn_x - 4

        if dots_end > dots_start + dot_unit * 3:
            n_dots = int((dots_end - dots_start) / dot_unit)
            page.insert_text(
                pymupdf.Point(dots_start, baseline_y),
                ". " * n_dots,
                fontname=font_name,
                fontsize=fontsize,
                color=(0, 0, 0),
            )

        page.insert_text(
            pymupdf.Point(pn_x, baseline_y),
            line.toc_page_num,
            fontname=font_name,
            fontsize=fontsize,
            color=(0, 0, 0),
        )
    else:
        dots_end = right_edge - 2
        if dots_end > dots_start + dot_unit * 3:
            n_dots = int((dots_end - dots_start) / dot_unit)
            page.insert_text(
                pymupdf.Point(dots_start, baseline_y),
                ". " * n_dots,
                fontname=font_name,
                fontsize=fontsize,
                color=(0, 0, 0),
            )


def _search_link_text(page, text: str, orig_rect) -> pymupdf.Rect | None:
    """Search for text on the page, returning the best matching rect near orig_rect."""
    rects = page.search_for(text)
    if not rects:
        return None
    best = None
    best_dist = float('inf')
    for r in rects:
        # Must be on a similar line (y within tolerance)
        if abs(r.y0 - orig_rect.y0) > 15:
            continue
        dist = abs(r.x0 - orig_rect.x0) + abs(r.y0 - orig_rect.y0)
        if dist < best_dist:
            best_dist = dist
            best = r
    return best


def _fix_link_annotations(doc, annot_colors: dict, rendered_extents: dict,
                          link_texts: dict):
    """Fix link border colors and adjust rectangles to match rendered text."""
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_height = page.rect.height

        for link in page.get_links():
            xref = link.get("xref")
            if not xref:
                continue
            raw_obj = doc.xref_object(xref)

            # Fix border colors
            if "/C " not in raw_obj:
                key = (page_idx, round(link["from"].x0, 1), round(link["from"].y0, 1))
                stroke = annot_colors.get(key, (1, 0, 0))
                r, g, b = stroke
                doc.xref_set_key(xref, "C", f"[{r} {g} {b}]")
            if "/W 0" in raw_obj:
                doc.xref_set_key(xref, "BS", "<< /W 1 >>")

            # Adjust link rectangle to match rendered text extent
            lr = link["from"]
            mid_y = (lr.y0 + lr.y1) / 2
            # Try exact y match, then +/- 1 for rounding tolerance
            extent = (rendered_extents.get((page_idx, round(mid_y)))
                      or rendered_extents.get((page_idx, round(mid_y) + 1))
                      or rendered_extents.get((page_idx, round(mid_y) - 1)))
            if not extent:
                continue

            orig_x0, orig_x1, new_x0, new_text_end = extent
            orig_width = orig_x1 - orig_x0
            new_width = new_text_end - new_x0

            if orig_width <= 0:
                continue

            # For inline links, try search-based positioning (precise for citations)
            key = (page_idx, round(lr.x0, 1), round(lr.y0, 1))
            orig_text = link_texts.get(key, "")

            if orig_text and abs(lr.x0 - orig_x0) >= 5 and len(orig_text) <= 20:
                found = _search_link_text(page, orig_text, lr)
                if found:
                    adj_x0 = found.x0 - 0.5
                    adj_x1 = found.x1 + 0.5
                    pdf_y0 = page_height - lr.y1
                    pdf_y1 = page_height - lr.y0
                    doc.xref_set_key(xref, "Rect",
                                     f"[{adj_x0:.3f} {pdf_y0:.3f} {adj_x1:.3f} {pdf_y1:.3f}]")
                    continue

            # Check if link starts near the line start (TOC-style) or is inline
            if abs(lr.x0 - orig_x0) < 5:
                # Link starts at line start -> adjust to cover rendered text extent
                adj_x0 = new_x0
                adj_x1 = new_text_end
            else:
                # Inline link: proportionally scale position within the line
                ratio = new_width / orig_width if orig_width > 0 else 1.0
                rel_x0 = lr.x0 - orig_x0
                rel_x1 = lr.x1 - orig_x0
                adj_x0 = new_x0 + rel_x0 * ratio
                adj_x1 = new_x0 + rel_x1 * ratio

            pdf_y0 = page_height - lr.y1
            pdf_y1 = page_height - lr.y0
            doc.xref_set_key(xref, "Rect",
                             f"[{adj_x0:.3f} {pdf_y0:.3f} {adj_x1:.3f} {pdf_y1:.3f}]")


def _parse_segments(translated: str) -> list[dict]:
    """Parse translated text into text and math placeholder segments."""
    segments = []
    last = 0
    for m in re.finditer(r'\{M(\d+)\}', translated):
        if m.start() > last:
            segments.append({"type": "text", "text": translated[last:m.start()]})
        segments.append({"type": "math", "index": int(m.group(1))})
        last = m.end()
    if last < len(translated):
        segments.append({"type": "text", "text": translated[last:]})
    return segments


# -- Main pipeline ----------------------------------------------------------

def main():
    if len(sys.argv) >= 2:
        input_path = sys.argv[1]
    else:
        input_path = "fonctionsdunevariable.pdf"

    output_path = Path(input_path).stem + "-en.pdf"
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")

    orig_doc = pymupdf.open(input_path)
    work_doc = pymupdf.open(input_path)

    # Step 1: Extract
    print("\nExtracting translatable lines...")
    lines = extract_lines(orig_doc)
    print(f"Found {len(lines)} translatable lines across {len(orig_doc)} pages")

    # Step 2: Translate (with disk cache for fast re-runs)
    cache_path = Path(input_path).with_suffix(".cache.json")
    print("\nTranslating via Google Translate...")
    translations = translate_lines(lines, cache_path=cache_path)

    # Step 3: Render
    print("\nRendering translations...")
    annot_colors, rendered_extents, link_texts = render_all(work_doc, orig_doc, lines, translations)

    # Save to bytes, reload, and fix link border colors
    # (insert_link creates links with xref=0; need save/reload to get real xrefs)
    pdf_bytes = work_doc.tobytes(garbage=4, deflate=True)
    work_doc.close()
    orig_doc.close()

    doc = pymupdf.open("pdf", pdf_bytes)
    _fix_link_annotations(doc, annot_colors, rendered_extents, link_texts)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
