import re
import unicodedata
from dataclasses import dataclass


# -- Configuration ----------------------------------------------------------

# cm-super fonts (T1 encoding) only ever hold text.
CM_SUPER_TEXT_FONT_RE = re.compile(r"SF(?:RM|BX|BI|TI|SL)\d")

# Computer Modern and Latin Modern roman fonts typeset body text in documents
# without cm-super, but they also typeset digits, operators and operator names
# inside math.
SHARED_ROMAN_FONT_RE = re.compile(
    r"CM(?:R|BX|TI|BXTI|SL)\d|LMRoman(?:Slant)?\d+-(?:Regular|Bold|Italic|BoldItalic)\b"
)

# Whitespace and punctuation in a shared roman font belong to whichever of
# text or math follows them. Other shared roman spans without words (digits,
# single letters, operators) are text only between text on both sides.
SEPARATOR_RE = re.compile(r"[\s,.;:!?]*")

# OT1 fonts have no accented letters, so TeX stacks a spacing accent glyph on
# the base letter and text extraction yields e.g. "d´eriv´ee".
SPACING_TO_COMBINING_ACCENT = {
    "\u00b4": "\u0301",  # acute
    "`": "\u0300",       # grave
    "\u02c6": "\u0302",  # circumflex
    "\u00a8": "\u0308",  # diaeresis
    "\u02dc": "\u0303",  # tilde
    "\u02c7": "\u030c",  # caron
    "\u02d8": "\u0306",  # breve
    "\u02da": "\u030a",  # ring
    "\u00af": "\u0304",  # macron
    "\u02d9": "\u0307",  # dot above
}
SPACING_ACCENT_RE = re.compile(
    "([" + "".join(SPACING_TO_COMBINING_ACCENT) + "])([A-Za-z\u0131\u0237])"
)
CEDILLA_RE = re.compile("\u00b8([cCsStT])|([cCsStT])\u00b8")
DOTLESS_TO_DOTTED = {"\u0131": "i", "\u0237": "j"}

EXTENSION_FONT_RE = re.compile(r"CMEX|LMMathExtension")

BOLD_FONT_RE = re.compile(r"SFB[XI]|CMBX|-Bold")
ITALIC_FONT_RE = re.compile(r"SF(?:BI|TI|SL)|CM(?:BX)?TI|CMSL|Italic|Slant")

MATH_OPERATOR_NAMES = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "liminf",
    "limsup", "sup", "inf", "max", "min", "det", "dim", "ker", "Ker", "deg",
    "arg", "gcd", "lcm", "hom", "Hom", "End", "Aut", "Gal", "Im", "Re", "Id",
    "id", "mod", "Pr", "tr", "Tr", "rank", "rk", "card", "Card", "Spec", "Res",
    "Ind", "supp", "Supp", "Frac", "GL", "SL", "diag", "sgn", "vol", "grad",
    "div", "rot", "curl", "dist", "diam", "ord",
}

ENGLISH_FUNCTION_WORDS = {
    "the", "and", "of", "is", "are", "that", "this", "these", "with", "for",
    "we", "which", "by", "from", "have", "has", "its", "where", "then", "such",
    "there", "be", "not", "can", "our", "their", "into", "it", "to",
}


# -- Font classification ----------------------------------------------------

def is_text_span(font: str, text: str, roman_is_text: bool) -> bool:
    """True if a span holds translatable text (vs math notation)."""
    if CM_SUPER_TEXT_FONT_RE.search(font):
        return True
    return roman_is_text and bool(SHARED_ROMAN_FONT_RE.search(font)) and _has_words(text)


def _has_words(text: str) -> bool:
    words = re.findall(r"[^\W\d_]{2,}", text)
    return any(word not in MATH_OPERATOR_NAMES for word in words)


def _resolve_wordless_spans(spans: list["Span"]):
    """Decide text or math for shared roman spans without words from their neighbors."""
    wordless = [bool(SHARED_ROMAN_FONT_RE.search(s.font)) and not s.is_text for s in spans]
    separator = [w and bool(SEPARATOR_RE.fullmatch(s.text)) for w, s in zip(wordless, spans)]

    for i, span in enumerate(spans):
        if not wordless[i] or separator[i]:
            continue
        prev = next((spans[j] for j in range(i - 1, -1, -1) if not wordless[j]), None)
        following = next((spans[j] for j in range(i + 1, len(spans)) if not wordless[j]), None)
        neighbors = [n for n in (prev, following) if n is not None]
        span.is_text = bool(neighbors) and all(n.is_text for n in neighbors)

    for i, span in enumerate(spans):
        if not separator[i]:
            continue
        following = next((spans[j] for j in range(i + 1, len(spans)) if not separator[j]), None)
        prev = next((spans[j] for j in range(i - 1, -1, -1) if not separator[j]), None)
        neighbor = following or prev
        span.is_text = neighbor is not None and neighbor.is_text


def _compose_accents(text: str) -> str:
    def acute_etc(m):
        base = DOTLESS_TO_DOTTED.get(m.group(2), m.group(2))
        return unicodedata.normalize("NFC", base + SPACING_TO_COMBINING_ACCENT[m.group(1)])

    def cedilla(m):
        return unicodedata.normalize("NFC", (m.group(1) or m.group(2)) + "\u0327")

    return CEDILLA_RE.sub(cedilla, SPACING_ACCENT_RE.sub(acute_etc, text))


def _uses_cm_super(doc) -> bool:
    return any(
        CM_SUPER_TEXT_FONT_RE.search(font[3])
        for page in doc
        for font in page.get_fonts()
    )


def is_extension_font(font: str) -> bool:
    """True for fonts with tall extensible delimiters and big operators."""
    return bool(EXTENSION_FONT_RE.search(font))


def _get_font_style(fontname: str) -> str:
    """Determine font style from the original font name."""
    is_bold = bool(BOLD_FONT_RE.search(fontname))
    is_italic = bool(ITALIC_FONT_RE.search(fontname))
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
    ink_bbox: tuple  # bbox of the non-whitespace characters
    origin: tuple  # (x, y) baseline point
    is_text: bool


@dataclass
class TranslatableLine:
    page_idx: int
    spans: list[Span]
    bbox: tuple
    max_x1: float  # right edge of the text column containing the line
    template: str  # text with {M0} placeholders for math
    math_spans: list[list[Span]]  # groups of consecutive math spans per placeholder
    is_toc: bool  # has dot leaders
    toc_content: str  # text portion of TOC (without dots/page number)
    toc_page_num: str  # trailing page number for TOC lines
    font_style: str  # dominant font style for the line
    text_styles: list  # [(char_count, style), ...] style runs in template text


# -- Extraction -------------------------------------------------------------

def _add_span_text(raw: dict):
    """Add "text" and "ink_bbox" to every span of a rawdict page."""
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                span["text"] = "".join(c["c"] for c in span["chars"])
                ink = [c["bbox"] for c in span["chars"] if c["c"].strip()]
                span["ink_bbox"] = (
                    (min(b[0] for b in ink), min(b[1] for b in ink),
                     max(b[2] for b in ink), max(b[3] for b in ink))
                    if ink else span["bbox"]
                )


def _line_core_y(line):
    """Get the core y-range of a line, excluding tall math symbols (CMEX).

    Uses non-CMEX span bboxes to avoid tall summation/integral signs from
    inflating the y-range. For CMEX-only lines, uses origin y with tight range.
    """
    core_spans = [s for s in line["spans"] if not is_extension_font(s["font"])]
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
    return all(is_extension_font(s["font"]) for s in line["spans"])


def _merge_same_y_lines(lines, roman_is_text: bool, max_x_gap=8):
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

            def has_text(line):
                return any(is_text_span(s["font"], s["text"], roman_is_text)
                           for s in line["spans"])

            # Don't merge if multiple text lines start at left margin
            # (these are consecutive visual lines, not fragments)
            left_margin = min(l["bbox"][0] for l in cluster)
            margin_text_lines = sum(
                1 for line in cluster
                if line["bbox"][0] < left_margin + 15 and has_text(line)
            )
            if len(cluster) > 8 or not any(map(has_text, cluster)) or margin_text_lines > 1:
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
    """Extract all lines containing translatable text."""
    result = []
    roman_is_text = not _uses_cm_super(doc)

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        raw = page.get_text("rawdict")
        _add_span_text(raw)
        paragraph_bboxes = [
            b["bbox"] for b in raw["blocks"]
            if b["type"] == 0 and len(b["lines"]) >= 2
        ]

        for block in raw["blocks"]:
            if block["type"] != 0:
                continue

            # Skip blocks that are already in English (e.g. Abstract)
            block_text = ""
            for line in block["lines"]:
                for s in line["spans"]:
                    if is_text_span(s["font"], s["text"], roman_is_text):
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
            merged_block_lines = _merge_same_y_lines(block["lines"], roman_is_text)

            for line_data in merged_block_lines:
                spans = []
                for s in line_data["spans"]:
                    text = s["text"]
                    if roman_is_text and SHARED_ROMAN_FONT_RE.search(s["font"]):
                        text = _compose_accents(text)
                    is_text = is_text_span(s["font"], text, roman_is_text)
                    spans.append(Span(
                        text=text,
                        font=s["font"],
                        size=s["size"],
                        bbox=tuple(s["bbox"]),
                        ink_bbox=tuple(s["ink_bbox"]),
                        origin=tuple(s["origin"]),
                        is_text=is_text,
                    ))
                if not spans:
                    continue
                if roman_is_text:
                    _resolve_wordless_spans(spans)

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
                    max_x1=_column_right_edge(line_data["bbox"], block["bbox"],
                                              paragraph_bboxes),
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


def _column_right_edge(line_bbox, block_bbox, paragraph_bboxes) -> float:
    """Right edge of the text column a line sits in.

    Uses the vertically nearest multi-line paragraph that spans the line's
    left edge, so one-line blocks such as headings get the full column width.
    """
    x0, y0, x1, y1 = line_bbox
    own_edge = max(block_bbox[2], x1)
    candidates = [b for b in paragraph_bboxes if b[0] - 2 <= x0 <= b[2]]
    if not candidates:
        return own_edge

    def vertical_gap(b):
        return max(b[1] - y1, y0 - b[3], 0)

    return max(own_edge, min(candidates, key=vertical_gap)[2])


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
    words = re.findall(r'[^\W\d_]{2,}', text.lower())
    if len(words) < 8:
        return False
    count = sum(1 for w in words if w in ENGLISH_FUNCTION_WORDS)
    return count / len(words) > 0.15


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
