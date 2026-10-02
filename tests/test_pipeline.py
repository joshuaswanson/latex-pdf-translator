import pymupdf
import pytest

from conftest import FONT_VARIANTS, fixture_pdf_bytes
from translator.pipeline import NoTranslatableTextError, translate_pdf
from translator.render import _find_extent


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_translates_fixture_end_to_end(fake_translator, variant):
    result = translate_pdf(fixture_pdf_bytes(variant), "fr", "en")

    with pymupdf.open("pdf", result) as doc:
        text = doc[0].get_text()
    assert "Continuous functions" in text
    assert "Theorem. Every function continuous on a compact is bounded." in text
    assert "Soit" not in text
    assert "XXXM" not in text and "{M" not in text


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_translated_text_stays_inside_the_text_block(fake_translator, variant):
    with pymupdf.open("pdf", fixture_pdf_bytes(variant)) as doc:
        right_margin = max(b[2] for b in doc[0].get_text("blocks"))

    result = translate_pdf(fixture_pdf_bytes(variant), "fr", "en")

    with pymupdf.open("pdf", result) as doc:
        words = doc[0].get_text("words")
    assert max(w[2] for w in words) <= right_margin + 1


def test_rejects_pdf_without_text(fake_translator):
    with pymupdf.open() as doc:
        doc.new_page()
        blank = doc.tobytes()

    with pytest.raises(NoTranslatableTextError):
        translate_pdf(blank, "fr", "en")


def test_link_extent_matches_the_column_containing_the_link():
    left = (50, 290, 50, 280)
    right = (320, 560, 320, 540)
    extents = {(0, 100): [left, right]}

    assert _find_extent(extents, 0, pymupdf.Rect(400, 95, 450, 105)) == right
    assert _find_extent(extents, 0, pymupdf.Rect(60, 95, 90, 105)) == left
