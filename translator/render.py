import re
from pathlib import Path

import pymupdf

from translator.charmap import (
    RSFS_CHAR_MAP, MATH_ITALIC_MAP, MATH_BOLD_MAP,
    EUFM_CHAR_MAP,
)
from translator.extract import Span, TranslatableLine


FONT_DIR = Path(__file__).parent / "fonts"

FONT_FILES = {
    "regular": FONT_DIR / "cmunrm.otf",
    "bold": FONT_DIR / "cmunbx.otf",
    "italic": FONT_DIR / "cmunti.otf",
    "bolditalic": FONT_DIR / "cmunbi.otf",
}

FONT_OBJECTS = {
    style: pymupdf.Font(fontfile=str(path))
    for style, path in FONT_FILES.items()
}

# Latin Modern Math: comprehensive math font from the CM family.
# Has 99.9% coverage of all math symbols used in LaTeX PDFs.
MATH_FONT_FILE = FONT_DIR / "latinmodern-math.otf"
MATH_FONT = pymupdf.Font(fontfile=str(MATH_FONT_FILE))

# Map LaTeX math fonts (Computer Modern and their Latin Modern equivalents)
# to a canonical kind. Order matters: earlier patterns win.
MATH_FONT_KINDS = [
    (re.compile(r"CMMI|LMMathItalic"), "CMMI"),
    (re.compile(r"CMBSY|LMMathSymbols\d+-Bold"), "CMBS"),
    (re.compile(r"CMSY|LMMathSymbols"), "CMSY"),
    (re.compile(r"CMEX|LMMathExtension"), "CMEX"),
    (re.compile(r"CMBXTI|LMRoman\d+-BoldItalic"), "CMBXTI"),
    (re.compile(r"CMBX|LMRoman\d+-Bold"), "CMBX"),
    (re.compile(r"CMTI|LMRoman\d+-Italic"), "CMTI"),
    (re.compile(r"CMR\d|LMRoman\d+-Regular"), "CMR"),
    (re.compile(r"rsfs"), "rsfs"),
    (re.compile(r"EUFM"), "EUFM"),
]

# Map math font kinds to rendering style.
# "math" = use Latin Modern Math (covers symbols, Greek, operators)
# "italic"/"bold"/"regular" = use CMU text font (for plain letters/numbers)
# Spans in any other font are copied from the original page.
MATH_FONT_STYLE = {
    "CMMI": "math",          # Math Italic (italic letters + Greek)
    "CMBS": "math",          # Bold Symbols
    "CMSY": "math",          # Symbols
    "CMEX": "math",          # Extensions (big delimiters)
    "CMBXTI": "bolditalic",  # Bold Extended Text Italic
    "CMBX": "bold",          # Bold Extended
    "CMTI": "italic",        # Text Italic
    "CMR": "regular",        # Roman
    "rsfs": "math",          # Ralph Smith Formal Script
    "EUFM": "math",          # Euler Fraktur
}

# Padding (x, top, bottom) around copied glyphs. Script fonts have flourishes
# outside their character boxes; other fonts' character boxes already span the
# full ascender-to-descender height.
SCRIPT_GLYPH_PADDING = (1.5, 2.5, 1.5)
TIGHT_GLYPH_PADDING = (0.3, 0.3, 0.3)

# Opening delimiters, including CMEX's big ones. Sub- and superscripts never
# attach to them, so a stacked pair right after one is a fraction.
OPENING_DELIMITERS = set("([{\u27e8") | {"\x00", "\x02", "\x04", "\x06", "\x08", "\x0a",
                                          "\x10", "\x11"}

# Fraction bars and radical overlines are thin filled rectangles, or in older
# dvips output thin inline images
RULE_MAX_THICKNESS = 1.5
RULE_MIN_WIDTH = 2

# Translated text that would overflow its block is condensed horizontally,
# down to this fraction of its natural width.
MIN_TEXT_SCALE = 0.7


class PageText:
    """Collects a page's text and writes it in one go.

    page.insert_text rescans every font resource on the page on each call,
    which dominated rendering time on pages with many copied glyphs.
    """

    def __init__(self, rect: pymupdf.Rect):
        self._rect = rect
        self._writer = pymupdf.TextWriter(rect)
        self._has_text = False
        self._condensed = []

    def add(self, point: tuple, text: str, font: pymupdf.Font, fontsize: float):
        self._writer.append(point, text, font=font, fontsize=fontsize)
        self._has_text = True

    def condensed(self, fixpoint: pymupdf.Point, scale: float) -> pymupdf.TextWriter:
        """A writer whose text is condensed horizontally by `scale` around `fixpoint`."""
        writer = pymupdf.TextWriter(self._rect)
        self._condensed.append((writer, (fixpoint, pymupdf.Matrix(scale, 1))))
        return writer

    def write(self, page):
        if self._has_text:
            self._writer.write_text(page)
        for writer, morph in self._condensed:
            writer.write_text(page, morph=morph)


def render_all(work_doc, orig_doc, lines: list[TranslatableLine],
               translations: list[str], progress_callback=None):
    """Apply all translations to the work document."""
    # Group lines by page, separating unchanged ones
    page_lines = {}
    unchanged_lines = {}
    for line, translated in zip(lines, translations):
        original = line.toc_content if line.is_toc else line.template
        if translated.strip() == original.strip():
            unchanged_lines.setdefault(line.page_idx, []).append((line, original))
        else:
            page_lines.setdefault(line.page_idx, []).append((line, translated))
    for page_idx, changed_lines in page_lines.items():
        changed_lines.extend(_lines_hit_by_redaction(
            work_doc[page_idx], changed_lines, unchanged_lines.get(page_idx, [])))

    # Track rendered text extents for link rectangle adjustment
    # (page_idx, round(y_mid)) -> [(orig_x0, orig_x1, new_x0, text_end_x), ...]
    rendered_extents = {}

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

    # Copied glyphs come from pages of this copy with the translatable text removed
    glyph_doc = pymupdf.open()
    glyph_doc.insert_pdf(orig_doc)

    changed = 0
    pages_to_render = sorted(page_lines)
    total_pages = len(pages_to_render)
    if progress_callback:
        progress_callback(0, total_pages)
    for page_num, page_idx in enumerate(pages_to_render):
        page = work_doc[page_idx]
        lines_on_page = [line for line in lines if line.page_idx == page_idx]
        orig_page = glyph_doc[page_idx]
        _remove_translatable_text(orig_page, lines_on_page)

        # Save link annotations before redaction removes them
        saved_links = list(page.get_links())

        # Phase 1: Add redaction annotations for all lines on this page
        rects = [_get_whiteout_rect(page, line) for line, _ in page_lines[page_idx]]
        rules = _thin_rules(orig_doc[page_idx])
        collateral = _collateral_glyph_boxes(orig_doc[page_idx], rects, lines_on_page)
        for rect in rects:
            page.add_redact_annot(rect, fill=(1, 1, 1))

        # Apply all redactions at once (actually removes underlying content)
        # Pixel mode removes rules drawn as images inside the rects and leaves
        # other images alone
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)

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

        # Phase 2: Re-render translated text + math glyphs
        page_text = PageText(page.rect)
        for line, translated in page_lines[page_idx]:
            whiteout = _get_whiteout_rect(page, line)
            line_rules = [rule for rule in rules if rule.intersects(whiteout)]
            text_end_x = _render_line_content(page, orig_page, line, translated, page_text,
                                              line_rules)
            # Record rendered text extent for link rectangle adjustment
            y_mid = (line.bbox[1] + line.bbox[3]) / 2
            orig_x0, orig_x1 = line.bbox[0], line.bbox[2]
            rendered_extents.setdefault((page_idx, round(y_mid)), []).append(
                (orig_x0, orig_x1, orig_x0, text_end_x))
            changed += 1

        page_text.write(page)
        for box in collateral:
            page.show_pdf_page(box, glyph_doc, page_idx, clip=box)

        if progress_callback:
            progress_callback(page_num + 1, total_pages)

    print(f"  {changed} lines modified")
    return all_annot_colors, rendered_extents, all_link_texts


def _thin_rules(page) -> list[pymupdf.Rect]:
    """Horizontal rules on the page, from both vector drawings and images."""
    candidates = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    candidates += [drawing["rect"] for drawing in page.get_drawings()]
    return [rect for rect in candidates
            if rect.height < RULE_MAX_THICKNESS and rect.width > RULE_MIN_WIDTH]


def _remove_translatable_text(page, lines_on_page: list[TranslatableLine]):
    """Remove the translatable text from a copy of the original page.

    Copied math glyphs come from this page, so a tall glyph's clip carries no
    pieces of neighboring words. Each text span is removed through a thin band
    across its middle, which spares math glyphs that only reach toward it.
    """
    for line in lines_on_page:
        for span in line.spans:
            if span.is_text and span.text.strip():
                middle = (span.bbox[1] + span.bbox[3]) / 2
                page.add_redact_annot(
                    pymupdf.Rect(span.bbox[0], middle - 0.25, span.bbox[2], middle + 0.25),
                    fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)


def _collateral_glyph_boxes(orig_page, rects: list[pymupdf.Rect],
                            lines_on_page: list[TranslatableLine]) -> list[pymupdf.Rect]:
    """Areas of glyphs outside every translatable line that the redactions will remove.

    These belong to content the tool does not re-render, such as display
    equations, and are copied back from the glyph source after redaction.
    """
    rect_boxes = [tuple(rect) for rect in rects]
    line_boxes = [span.bbox for line in lines_on_page for span in line.spans]
    boxes = []
    for block in orig_page.get_text("rawdict")["blocks"]:
        for raw_line in block.get("lines", []):
            for span in raw_line["spans"]:
                touched = [r for r in rect_boxes if _overlaps(span["bbox"], r)]
                if not touched:
                    continue
                nearby = [box for box in line_boxes if _overlaps(box, span["bbox"])]
                hit = [
                    char["bbox"] for char in span["chars"]
                    if char["c"].strip()
                    and any(_overlaps(char["bbox"], r) for r in touched)
                    and not any(_contains(box, _center(char["bbox"])) for box in nearby)
                ]
                if hit:
                    boxes.append(pymupdf.Rect(
                        min(b[0] for b in hit) - 0.3, min(b[1] for b in hit) - 0.3,
                        max(b[2] for b in hit) + 0.3, max(b[3] for b in hit) + 0.3,
                    ))
    return boxes


def _lines_hit_by_redaction(page, changed: list[tuple], unchanged: list[tuple]) -> list[tuple]:
    """Unchanged lines with glyphs that touch a changed line's redaction.

    Redaction removes every character touching its rectangle, so a tall glyph
    such as a radical reaching into the line above disappears with that line.
    These lines are redrawn with their original text.
    """
    rects = [_get_whiteout_rect(page, line) for line, _ in changed]
    hit = []
    remaining = list(unchanged)
    while True:
        newly_hit = [
            (line, text) for line, text in remaining
            if any(_overlaps(span.bbox, tuple(rect)) for span in line.spans for rect in rects)
        ]
        if not newly_hit:
            return hit
        hit.extend(newly_hit)
        rects.extend(_get_whiteout_rect(page, line) for line, _ in newly_hit)
        hit_ids = {id(line) for line, _ in newly_hit}
        remaining = [(line, text) for line, text in remaining if id(line) not in hit_ids]


def _center(bbox: tuple) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2


def _contains(bbox: tuple, point: tuple[float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _overlaps(a: tuple, b: tuple) -> bool:
    return (min(a[2], b[2]) - max(a[0], b[0]) > 0.1
            and min(a[3], b[3]) - max(a[1], b[1]) > 0.1)


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
    if not orig_text_len:
        return [(translated, line.font_style)]

    # Extract just the text portions from translated (skip math markers)
    trans_text_len = len(re.sub(r'\{M\d+\}', '', translated))
    if not trans_text_len:
        return [(translated, line.font_style)]

    # Character offset in the translated text where each style run ends.
    # The last run ends exactly at trans_text_len.
    run_ends = []
    cumulative = 0
    for count, style in line.text_styles:
        cumulative += count
        run_ends.append((int(cumulative / orig_text_len * trans_text_len), style))

    result = []
    pos = 0
    for seg in _parse_segments(translated):
        if seg["type"] == "math":
            result.append((f"{{M{seg['index']}}}", "math"))
            continue
        text = seg["text"]
        seg_start = pos
        seg_end = pos + len(text)
        for run_end, style in run_ends:
            chunk_end = min(run_end, seg_end)
            if chunk_end > pos:
                result.append((text[pos - seg_start:chunk_end - seg_start], style))
                pos = chunk_end

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
                         translated: str, page_text: PageText,
                         rules: list[pymupdf.Rect]) -> float:
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

    toc_font_obj = FONT_OBJECTS[line.font_style]
    right_limit = line.max_x1
    if line.is_toc:
        right_limit = _toc_text_limit(line, fontsize, toc_font_obj)
    text_scale = _fit_text_scale(line, styled_segments, fontsize, right_limit - x0)

    # Render from left to right. Condensed text goes to a writer scaled around
    # the line start, so each segment is placed at its unscaled position.
    condensed = None
    x = x0
    for text, style in styled_segments:
        if style == "math":
            m = re.match(r'\{M(\d+)\}', text)
            if not m:
                continue
            idx = int(m.group(1))
            if idx >= len(line.math_spans):
                continue
            x += _render_math_group(page, orig_page, line.math_spans[idx], x, page_text, rules)
        else:
            if not text:
                continue
            font_obj = FONT_OBJECTS[style]
            if text_scale < 1:
                if condensed is None:
                    condensed = page_text.condensed(pymupdf.Point(x0, baseline_y), text_scale)
                condensed.append((x0 + (x - x0) / text_scale, baseline_y), text,
                                 font=font_obj, fontsize=fontsize)
            else:
                page_text.add((x, baseline_y), text, font_obj, fontsize)
            x += font_obj.text_length(text, fontsize=fontsize) * text_scale

    text_end_x = x  # Position after title text, before dots

    # For TOC lines, add dot leaders and page number
    if line.is_toc:
        _render_toc_dots(page_text, x, baseline_y, fontsize, toc_font_obj, line)

    return text_end_x


def _fit_text_scale(line: TranslatableLine, styled_segments: list,
                    fontsize: float, available: float) -> float:
    """Horizontal scale for text segments so the line fits in `available`."""
    text_width = 0
    math_width = 0
    for text, style in styled_segments:
        if style == "math":
            m = re.match(r'\{M(\d+)\}', text)
            if m and int(m.group(1)) < len(line.math_spans):
                math_width += _render_math_group(None, None, line.math_spans[int(m.group(1))], 0,
                                                 None)
        else:
            text_width += FONT_OBJECTS[style].text_length(text, fontsize=fontsize)
    if text_width == 0 or text_width + math_width <= available + 1:
        return 1.0
    return max(MIN_TEXT_SCALE, (available - math_width) / text_width)


def _toc_text_limit(line: TranslatableLine, fontsize: float, font_obj) -> float:
    """Rightmost x for TOC entry text, leaving room for dots and page number."""
    right_edge = line.bbox[2]
    if line.toc_page_num:
        right_edge -= font_obj.text_length(line.toc_page_num, fontsize=fontsize) + 4
    return right_edge - 4 - 3 * font_obj.text_length(". ", fontsize=fontsize)


def _render_math_group(page, orig_page, group: list[Span], x: float,
                       page_text: PageText | None,
                       rules: list[pymupdf.Rect] | None = None) -> float:
    """Render one math placeholder's spans starting at x, returning width consumed.

    With page=None nothing is drawn and only the width is computed.
    """
    stacked = _find_stacked_spans(group)
    attached = _find_attached_spans(group)
    absorbed = {j for extras in attached.values() for j in extras}

    # Render: stacked spans at same x, sequential spans advance x
    start_x = x
    placed = {}  # span index -> (rendered x0, rendered x1)
    frac_x_start = None
    frac_max_width = 0
    for gi, ms in enumerate(group):
        if gi in absorbed:
            continue
        math_rect = pymupdf.Rect(ms.bbox)
        if math_rect.is_empty or math_rect.width < 0.5:
            continue
        extra_spans = [group[j] for j in attached.get(gi, []) if group[j].text.strip()]
        if gi in stacked:
            if frac_x_start is None:
                frac_x_start = x
            rendered = _render_math_span(page, orig_page, ms, frac_x_start, extra_spans, page_text)
            placed[gi] = (frac_x_start, frac_x_start + rendered)
            frac_max_width = max(frac_max_width, rendered)
        else:
            # Flush any pending fraction width
            if frac_x_start is not None:
                _draw_fraction_bars(page, group, stacked, frac_x_start,
                                    frac_x_start + frac_max_width)
                x = frac_x_start + frac_max_width + 1.5
                frac_x_start = None
                frac_max_width = 0
            rendered = _render_math_span(page, orig_page, ms, x, extra_spans, page_text)
            placed[gi] = (x, x + rendered)
            x += rendered
    # Flush final fraction (if stacked spans are at end of group)
    if frac_x_start is not None:
        _draw_fraction_bars(page, group, stacked, frac_x_start,
                            frac_x_start + frac_max_width)
        x = frac_x_start + frac_max_width + 1.5
    if page is not None and rules:
        _redraw_group_rules(page, group, stacked, placed, rules)
    return x - start_x


def _redraw_group_rules(page, group: list[Span], stacked: set[int],
                        placed: dict[int, tuple[float, float]], rules: list[pymupdf.Rect]):
    """Redraw the rules over a math group at the new positions of the spans beneath them.

    Redaction removes rules such as radical overlines along with the line.
    Fraction bars are skipped because _draw_fraction_bars draws them. Rules
    that belong to this group are removed from `rules`.
    """
    spans = [group[i] for i in placed]
    if not spans:
        return
    gy0, gy1 = min(s.bbox[1] for s in spans), max(s.bbox[3] for s in spans)
    fraction = sorted((group[i] for i in stacked), key=lambda s: s.bbox[1])
    for rule in list(rules):
        beneath = sorted(
            (i for i in placed
             if min(group[i].bbox[2], rule.x1) - max(group[i].bbox[0], rule.x0) > 0.5),
            key=lambda i: group[i].bbox[0],
        )
        if not beneath or not gy0 - 2 <= rule.y0 <= gy1 + 2:
            continue
        rules.remove(rule)
        if len(fraction) >= 2 and fraction[0].bbox[3] - 1 <= rule.y0 <= fraction[-1].bbox[1] + 1:
            continue
        first, last = group[beneath[0]], group[beneath[-1]]
        x0 = placed[beneath[0]][0] + (rule.x0 - first.bbox[0])
        x1 = placed[beneath[-1]][1] + (rule.x1 - last.bbox[2])
        page.draw_rect(pymupdf.Rect(x0, rule.y0, max(x1, x0 + 1), max(rule.y1, rule.y0 + 0.4)),
                       color=None, fill=(0, 0, 0), width=0)


def _find_stacked_spans(group: list[Span]) -> set[int]:
    """Indices of spans that are fraction parts.

    Fraction parts overlap in x but differ in y. Their x-centers must be close
    (fractions are centered), which rules out most subscript/superscript pairs.
    """
    stacked = set()
    for gi in range(len(group)):
        for gj in range(gi + 1, len(group)):
            s1, s2 = group[gi], group[gj]
            x_overlap = min(s1.bbox[2], s2.bbox[2]) - max(s1.bbox[0], s2.bbox[0])
            if x_overlap <= 0 or abs(s1.origin[1] - s2.origin[1]) <= 3:
                continue
            c1 = (s1.bbox[0] + s1.bbox[2]) / 2
            c2 = (s2.bbox[0] + s2.bbox[2]) / 2
            min_w = min(s1.bbox[2] - s1.bbox[0], s2.bbox[2] - s2.bbox[0])
            if abs(c1 - c2) >= max(min_w * 0.7, 2.0):
                continue
            # Reject sub/superscripts of a base character: both start right
            # at the right edge of another span
            left_edge = min(s1.bbox[0], s2.bbox[0])
            is_sub_super = any(
                abs(group[gk].bbox[2] - left_edge) < 1.5
                and group[gk].text.strip()
                and group[gk].text.strip() not in OPENING_DELIMITERS
                for gk in range(len(group)) if gk not in (gi, gj)
            )
            if not is_sub_super:
                stacked.add(gi)
                stacked.add(gj)
    return stacked


def _find_attached_spans(group: list[Span]) -> dict[int, list[int]]:
    """Map rsfs/EUFM span indices to the super/subscript spans that follow them.

    These get copied together from the original as one compound glyph, which
    preserves e.g. C^r where the script C and superscript r belong together.
    """
    attached = {}
    for gi in range(len(group) - 1):
        ms_cur = group[gi]
        if _math_font_kind(ms_cur.font) not in ("rsfs", "EUFM"):
            continue
        if not ms_cur.text.strip():
            continue
        extras = []
        for gj in range(gi + 1, len(group)):
            ms_next = group[gj]
            # origin[1] can be identical for superscripts in some PDFs (e.g.
            # rsfs C^r both report the same baseline), so only size and
            # x-adjacency identify a super/subscript.
            if (not ms_next.text.strip()
                    or (ms_next.size < ms_cur.size
                        and abs(ms_next.bbox[0] - ms_cur.bbox[2]) < 3)):
                extras.append(gj)
            else:
                break
        if extras:
            attached[gi] = extras
    return attached


def _math_font_kind(font: str) -> str | None:
    """Canonical math font kind (e.g. 'CMMI' for 'FITVLG+CMMI10'), or None."""
    name = font.split("+")[-1]
    for pattern, kind in MATH_FONT_KINDS:
        if pattern.search(name):
            return kind
    return None


def _map_math_text(text: str, font_kind: str) -> str:
    """Map math span text to Unicode characters renderable by Latin Modern Math."""
    char_map = {
        "rsfs": RSFS_CHAR_MAP,
        "CMMI": MATH_ITALIC_MAP,
        "CMBX": MATH_BOLD_MAP,
        "EUFM": EUFM_CHAR_MAP,
    }.get(font_kind)
    if char_map is None:
        return text
    return "".join(char_map.get(ch, ch) for ch in text)


def _copy_original_glyph(page, orig_page, ms: Span, x: float,
                         extra_spans: list[Span] | None = None,
                         padding: tuple = SCRIPT_GLYPH_PADDING) -> float:
    """Copy a glyph from the original page to preserve its exact appearance.

    Used for glyphs whose Unicode equivalents would look different or land in
    the wrong place, such as rsfs script and EUFM Fraktur letters.

    If extra_spans is provided, the source/dest rects are expanded to include
    those spans (e.g. superscripts attached to a script letter like C^r).
    Only the inked area is copied, so neighboring glyphs inside a span's
    leading or trailing whitespace stay behind.
    """
    full_rect = pymupdf.Rect(ms.bbox)
    if full_rect.is_empty or full_rect.width < 0.5:
        return 0

    ink_rect = pymupdf.Rect(ms.ink_bbox)
    for es in extra_spans or []:
        full_rect |= pymupdf.Rect(es.bbox)
        ink_rect |= pymupdf.Rect(es.ink_bbox)

    pad_x, pad_top, pad_bot = padding
    src_rect = pymupdf.Rect(
        ink_rect.x0 - pad_x, ink_rect.y0 - pad_top,
        ink_rect.x1 + pad_x, ink_rect.y1 + pad_bot,
    )

    # Destination keeps the original size and vertical position
    dst_x0 = x + (ink_rect.x0 - full_rect.x0)
    dst_rect = pymupdf.Rect(
        dst_x0 - pad_x, ink_rect.y0 - pad_top,
        dst_x0 + ink_rect.width + pad_x, ink_rect.y1 + pad_bot,
    )

    if page is not None:
        page.show_pdf_page(dst_rect, orig_page.parent, orig_page.number, clip=src_rect)
    return full_rect.width


def _render_math_span(page, orig_page, ms: Span, x: float,
                      extra_spans: list[Span], page_text: PageText | None) -> float:
    """Render a single math span as vector text, returning width consumed."""
    kind = _math_font_kind(ms.font)
    style = MATH_FONT_STYLE.get(kind)

    if style is None:
        if not ms.text.strip():
            return pymupdf.Rect(ms.bbox).width
        return _copy_original_glyph(page, orig_page, ms, x, padding=TIGHT_GLYPH_PADDING)

    # Latin Modern Math's script and Fraktur letters look different from rsfs
    # and EUFM, so copy the original glyph
    if kind in ("rsfs", "EUFM") and ms.text.strip():
        return _copy_original_glyph(page, orig_page, ms, x, extra_spans)

    # CMEX glyphs and radicals hang below their baseline and come in many
    # sizes, so a Unicode equivalent typeset on that baseline lands too high.
    # Copying the original keeps the exact size and position.
    hangs_below_baseline = ms.origin[1] < (ms.bbox[1] + ms.bbox[3]) / 2
    if (kind == "CMEX" or hangs_below_baseline) and ms.text.strip():
        return _copy_original_glyph(page, orig_page, ms, x)

    # CMSY combining characters (e.g. U+0338 "not" slash) need original glyph
    # because they overlay the next character and can't render standalone
    if kind == "CMSY" and any(ord(ch) < 0x20 or ch == '\u0338' for ch in ms.text if ch.strip()):
        return _copy_original_glyph(page, orig_page, ms, x)

    # Determine font to use
    if style == "math":
        m_font_obj = MATH_FONT
        text = _map_math_text(ms.text, kind)
    else:
        m_font_obj = FONT_OBJECTS[style]
        text = ms.text

    if not text or text.isspace():
        return m_font_obj.text_length(text, fontsize=ms.size) if text else 0

    # The math span keeps its original baseline (sub/superscripts sit off the line)
    if page_text is not None:
        page_text.add((x, ms.origin[1]), text, m_font_obj, ms.size)
    return m_font_obj.text_length(text, fontsize=ms.size)


def _draw_fraction_bars(page, group: list, stacked: set,
                        frac_x_start: float, frac_x_end: float):
    """Draw fraction bars for stacked spans within a math group."""
    if page is None or not stacked:
        return
    stacked_spans = [group[i] for i in sorted(stacked)]
    if len(stacked_spans) < 2:
        return
    stacked_spans.sort(key=lambda s: s.bbox[1])
    upper = stacked_spans[0]
    lower = stacked_spans[-1]
    bar_y = (upper.bbox[3] + lower.bbox[1]) / 2
    shape = page.new_shape()
    shape.draw_line(
        pymupdf.Point(frac_x_start, bar_y),
        pymupdf.Point(frac_x_end, bar_y),
    )
    shape.finish(color=(0, 0, 0), width=0.4)
    shape.commit()


def _render_toc_dots(page_text: PageText, x_after_text: float, baseline_y: float,
                     fontsize: float, font_obj, line: TranslatableLine):
    """Render dot leaders and right-aligned page number."""
    right_edge = line.bbox[2]
    dot_unit = font_obj.text_length(". ", fontsize=fontsize)
    dots_start = x_after_text + 4

    if line.toc_page_num:
        pn_width = font_obj.text_length(line.toc_page_num, fontsize=fontsize)
        pn_x = right_edge - pn_width
        dots_end = pn_x - 4
        page_text.add((pn_x, baseline_y), line.toc_page_num, font_obj, fontsize)
    else:
        dots_end = right_edge - 2

    if dots_end > dots_start + dot_unit * 3:
        n_dots = int((dots_end - dots_start) / dot_unit)
        page_text.add((dots_start, baseline_y), ". " * n_dots, font_obj, fontsize)


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


def _find_extent(rendered_extents: dict, page_idx: int, link_rect) -> tuple | None:
    """Rendered line extent for the line a link sits on."""
    mid_y = round((link_rect.y0 + link_rect.y1) / 2)
    # Try exact y match, then +/- 1 for rounding tolerance
    for y in (mid_y, mid_y + 1, mid_y - 1):
        for extent in rendered_extents.get((page_idx, y), []):
            orig_x0, orig_x1 = extent[0], extent[1]
            if orig_x0 - 5 <= link_rect.x0 <= orig_x1:
                return extent
    return None


def fix_link_annotations(doc, annot_colors: dict, rendered_extents: dict,
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
            extent = _find_extent(rendered_extents, page_idx, lr)
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
                ratio = new_width / orig_width
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
