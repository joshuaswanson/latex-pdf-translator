import pytest

from conftest import FONT_VARIANTS, open_fixture
from translator.extract import (
    Span, _compose_accents, _is_english_block, _resolve_wordless_spans, extract_lines,
)


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_extracts_french_text_for_every_font_setup(variant):
    templates = [line.template for line in extract_lines(open_fixture(variant))]

    assert templates[0] == "Fonctions continues"
    assert "Théorème. Toute fonction continue sur un compact est bornée." in templates
    assert any(t.startswith("La dérivée vérifie{M0}") for t in templates)


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_math_stays_out_of_translatable_text(variant):
    lines = extract_lines(open_fixture(variant))
    soit = next(line for line in lines if line.template.startswith("Soit"))

    assert soit.template.count("{M") == 4
    assert "sin" not in soit.template
    math_text = "".join(s.text for group in soit.math_spans for s in group)
    assert "R" in math_text  # \mathbb{R}
    assert "sin" in math_text


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_font_styles(variant):
    lines = extract_lines(open_fixture(variant))
    assert lines[0].font_style == "bold"
    theorem = next(line for line in lines if line.template.startswith("Théorème"))
    assert [style for _, style in theorem.text_styles] == ["bold", "italic"]


def test_composes_ot1_spacing_accents():
    assert _compose_accents("d´eriv´ee") == "dérivée"
    assert _compose_accents("Th´eor`eme") == "Théorème"
    assert _compose_accents("na¨ıf") == "naïf"
    assert _compose_accents("fa¸cade") == "façade"


def test_detects_english_blocks():
    english = "In this paper we study the dual of the space of locally analytic functions."
    french = "Dans ce texte, nous étudions le dual de l'espace des fonctions localement analytiques."
    german = "In dieser Arbeit untersuchen wir den Dualraum der lokal analytischen Funktionen."
    assert _is_english_block(english)
    assert not _is_english_block(french)
    assert not _is_english_block(german)


def test_wordless_roman_spans_follow_their_neighbors():
    def span(text, font, is_text):
        return Span(text=text, font=font, size=10, bbox=(0, 0, 1, 1),
                    ink_bbox=(0, 0, 1, 1), origin=(0, 1), is_text=is_text)

    spans = [
        span("along with", "CMR10", True), span(" ", "CMR10", False),
        span("a", "CMR10", False), span(" ", "CMR10", False),
        span("correction to", "CMR10", True), span(" ", "CMR10", False),
        span("f", "CMMI10", False), span("(", "CMR10", False),
        span("x", "CMMI10", False), span(")", "CMR10", False),
    ]
    _resolve_wordless_spans(spans)

    assert [s.is_text for s in spans] == [
        True, True, True, True, True, False, False, False, False, False,
    ]
