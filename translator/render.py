import re
from pathlib import Path

import pymupdf

from translator.charmap import (
    CMEX_CHAR_MAP, RSFS_CHAR_MAP, MATH_ITALIC_MAP, MATH_BOLD_MAP,
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

    # CMSY combining characters (e.g. U+0338 "not" slash) need original glyph
    # because they overlay the next character and can't render standalone
    if prefix == "CMSY" and any(ord(ch) < 0x20 or ch == '\u0338' for ch in ms.text if ch.strip()):
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
