import re
from dataclasses import dataclass


# -- Configuration ----------------------------------------------------------

TEXT_FONT_PREFIXES = ("SFRM", "SFBX", "SFBI", "SFTI")

# Font prefix -> style mapping
BOLD_FONT_PREFIXES = ("SFBX", "SFBI")
ITALIC_FONT_PREFIXES = ("SFTI", "SFBI")


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
