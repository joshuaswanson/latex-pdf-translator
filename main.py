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
BOLD_FONT_PREFIXES = ("SFBX",)
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
    "\x10": "\u239B", "\x11": "\u239D",  # left paren top/bottom
    "\x12": "\u239E", "\x13": "\u23A0",  # right paren top/bottom
}

# rsfs script letter mapping (rsfs extracts as plain letters, need Unicode script)
RSFS_CHAR_MAP = {
    "C": "\U0001D49E", "D": "\U0001D49F", "O": "\U0001D4AA",
    "R": "\u211B", "S": "\U0001D4AE", "L": "\u2112",
    "F": "\u2131", "B": "\u212C", "H": "\u210B", "I": "\u2110",
    "M": "\u2133", "P": "\U0001D4AB", "T": "\U0001D4AF",
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

# Google Translate batch size (max 5000 chars per request)
BATCH_CHARS = 4800

# Math placeholder format: XXXM0XXX, XXXM1XXX, etc.
# Google Translate preserves these as opaque tokens
MATH_MARKER_RE = re.compile(r'XXXM(\d+)XXX')

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

            for line_data in block["lines"]:
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

                # Skip lines with no meaningful text
                text_only = "".join(s.text for s in spans if s.is_text).strip()
                if not text_only:
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

    changed = 0
    for page_idx in sorted(page_lines):
        page = work_doc[page_idx]
        orig_page = orig_doc[page_idx]

        # Save link annotations with their visual properties before redaction
        saved_links = []
        for link in page.get_links():
            saved_links.append(link)
        # Save annotation border colors (get_links doesn't include these)
        annot_colors = {}
        for annot in page.annots():
            if annot.type[0] == 2:  # Link annotation
                key = (round(annot.rect.x0, 1), round(annot.rect.y0, 1))
                annot_colors[key] = annot.colors.get("stroke", (1, 0, 0))

        # Phase 1: Add redaction annotations for all lines on this page
        for line, _translated in page_lines[page_idx]:
            rect = _get_whiteout_rect(page, line)
            page.add_redact_annot(rect, fill=(1, 1, 1))

        # Apply all redactions at once (actually removes underlying content)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        # Restore link annotations with visible colored borders
        for link in saved_links:
            key = (round(link["from"].x0, 1), round(link["from"].y0, 1))
            stroke = annot_colors.get(key, (1, 0, 0))
            link["colors"] = {"stroke": stroke}
            link["border"] = {"width": 1}
            page.insert_link(link)

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
            _render_line_content(page, orig_page, line, translated)
            changed += 1

    print(f"  {changed} lines modified")


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


def _render_line_content(page, orig_page, line: TranslatableLine,
                         translated: str):
    """Render translated text + math glyphs onto the page."""
    x0, y0, x1, y1 = line.bbox
    fontsize = line.spans[0].size

    # Find baseline from first text span's origin
    baseline_y = y1
    for s in line.spans:
        if s.is_text and s.text.strip():
            baseline_y = s.origin[1]
            break

    # Build styled segments from translated text
    styled_segments = _build_style_map(line, translated)

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
            for ms in line.math_spans[idx]:
                math_rect = pymupdf.Rect(ms.bbox)
                if math_rect.is_empty or math_rect.width < 0.5:
                    continue
                rendered = _render_math_span(page, orig_page, ms, x, baseline_y)
                x += rendered
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

    # For TOC lines, add dot leaders and page number
    if line.is_toc:
        toc_font_name = FONT_NAMES[line.font_style]
        toc_font_obj = FONT_OBJECTS[line.font_style]
        _render_toc_dots(page, x, baseline_y, fontsize, toc_font_name,
                         toc_font_obj, line)


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
    return text


def _render_math_span(page, orig_page, ms: Span, x: float,
                      baseline_y: float) -> float:
    """Render a single math span as vector text, returning width consumed."""
    prefix = _math_font_prefix(ms.font)
    style = MATH_FONT_STYLE.get(prefix)

    if style is None:
        # Unknown font - use original bbox width as spacing
        return pymupdf.Rect(ms.bbox).width

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


def _render_toc_dots(page, x_after_text: float, baseline_y: float,
                     fontsize: float, font_name: str, font_obj,
                     line: TranslatableLine):
    """Render dot leaders (and right-aligned page number if inline)."""
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
    render_all(work_doc, orig_doc, lines, translations)

    # Save
    work_doc.save(output_path, garbage=4, deflate=True)
    work_doc.close()
    orig_doc.close()
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
