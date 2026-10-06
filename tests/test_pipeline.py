import pymupdf
import pytest

from conftest import FONT_VARIANTS, OTHER_TYPEFACES, FakeEngine, fixture_pdf_bytes
from translator.pipeline import NoTranslatableTextError, translate_pdf
from translator.extract import Span, TranslatableLine
from translator.render import _find_extent, _find_stacked_spans, _lines_hit_by_redaction


@pytest.mark.parametrize("variant", FONT_VARIANTS + OTHER_TYPEFACES)
def test_translates_fixture_end_to_end(variant):
    result = translate_pdf(fixture_pdf_bytes(variant), FakeEngine())

    with pymupdf.open("pdf", result) as doc:
        text = doc[0].get_text()
    assert "Continuous functions" in text
    assert "Theorem. Every function continuous on a compact is bounded." in text
    assert "Soit" not in text
    assert "XXXM" not in text and "{M" not in text


@pytest.mark.parametrize("variant, typeface", [
    ("cm", "CMU"), ("times", "Times"), ("palatino", "Pagella"),
])
def test_translated_text_is_set_in_the_original_typeface(variant, typeface):
    result = translate_pdf(fixture_pdf_bytes(variant), FakeEngine())

    with pymupdf.open("pdf", result) as doc:
        spans = [s for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", [])
                 for s in l["spans"]]
    theorem_fonts = {s["font"] for s in spans if "bounded" in s["text"] or "Theorem" in s["text"]}
    assert theorem_fonts and all(typeface in font for font in theorem_fonts)


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_translated_text_stays_inside_the_text_block(variant):
    with pymupdf.open("pdf", fixture_pdf_bytes(variant)) as doc:
        right_margin = max(b[2] for b in doc[0].get_text("blocks"))

    result = translate_pdf(fixture_pdf_bytes(variant), FakeEngine())

    with pymupdf.open("pdf", result) as doc:
        words = doc[0].get_text("words")
    assert max(w[2] for w in words) <= right_margin + 1


def test_rejects_pdf_without_text():
    with pymupdf.open() as doc:
        doc.new_page()
        blank = doc.tobytes()

    with pytest.raises(NoTranslatableTextError):
        translate_pdf(blank, FakeEngine())


def test_link_extent_matches_the_column_containing_the_link():
    left = (50, 290, 50, 280)
    right = (320, 560, 320, 540)
    extents = {(0, 100): [left, right]}

    assert _find_extent(extents, 0, pymupdf.Rect(400, 95, 450, 105)) == right
    assert _find_extent(extents, 0, pymupdf.Rect(60, 95, 90, 105)) == left


def test_fraction_after_an_opening_delimiter_is_stacked():
    def span(text, font, bbox, baseline):
        return Span(text=text, font=font, size=7, bbox=bbox, ink_bbox=bbox,
                    origin=(bbox[0], baseline), is_text=False)

    group = [
        span("(", "CMEX10", (312.1, 171.6, 316.7, 181.5), 172.2),
        span("16", "CMR7", (316.7, 166.8, 325.8, 177.9), 172.2),
        span("t", "CMMI7", (320.3, 178.3, 323.4, 185.2), 183.0),
    ]

    assert _find_stacked_spans(group) == {1, 2}


def test_unchanged_line_touched_by_a_neighbor_redaction_is_redrawn():
    def line(top, bottom, glyph_top=None):
        spans = [Span(text="mot", font="SFRM1000", size=10, bbox=(100, top, 400, bottom),
                      ink_bbox=(100, top, 400, bottom), origin=(100, bottom - 2), is_text=True)]
        if glyph_top is not None:
            radical = (200, glyph_top, 210, bottom)
            spans.append(Span(text="√", font="CMSY10", size=10, bbox=radical, ink_bbox=radical,
                              origin=(200, glyph_top + 8), is_text=False))
        return TranslatableLine(
            page_idx=0, spans=spans, bbox=(100, top, 400, bottom), max_x1=400, template="mot",
            math_spans=[], is_toc=False, toc_content="", toc_page_num="",
            font_style="regular", text_styles=[(3, "regular")],
        )

    translated = line(100, 112)
    with_radical = line(114, 126, glyph_top=104)
    far_below = line(140, 152)

    with pymupdf.open() as doc:
        hit = _lines_hit_by_redaction(doc.new_page(), [(translated, "word")],
                                      [(with_radical, "mot"), (far_below, "mot")])

    assert [line for line, _ in hit] == [with_radical]


class LongWindedEngine(FakeEngine):
    def translate_batch(self, texts, context=("", "")):
        return [f"{t} {t}" if "{M" not in t else t for t in super().translate_batch(texts)]


@pytest.mark.parametrize("variant", FONT_VARIANTS + OTHER_TYPEFACES)
def test_text_twice_as_long_stays_inside_the_text_block(variant):
    with pymupdf.open("pdf", fixture_pdf_bytes(variant)) as doc:
        right_margin = max(b[2] for b in doc[0].get_text("blocks"))

    result = translate_pdf(fixture_pdf_bytes(variant), LongWindedEngine())

    with pymupdf.open("pdf", result) as doc:
        words = doc[0].get_text("words")
    assert max(w[2] for w in words) <= right_margin + 1
