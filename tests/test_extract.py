import pytest

from conftest import FONT_VARIANTS, OTHER_TYPEFACES, open_fixture
from translator.extract import (
    Span, TranslatableLine, _compose_accents, _is_english_block, _mark_math_variables,
    _merge_same_y_lines, _merge_split_lines,
    _resolve_wordless_spans, extract_lines,
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


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_words_hyphenated_across_lines_are_joined(variant):
    templates = [line.template for line in extract_lines(open_fixture(variant))]

    assert templates[-2].endswith("le résultat principal")
    assert templates[-1] == "de cette section."


@pytest.mark.parametrize("variant", FONT_VARIANTS)
def test_line_split_by_a_fraction_is_merged(variant):
    lines = extract_lines(open_fixture(variant))
    line = next(line for line in lines if line.template.startswith("La dérivée"))

    assert line.template == "La dérivée vérifie{M0}, et nous avons donc démontré le résultat principal"
    assert [s.text for s in line.math_spans[0] if s.text.strip()][:2] == ["df", "dx"]


def test_lines_on_different_visual_lines_are_not_merged():
    def line(template, bbox):
        span = Span(text=template, font="SFRM1000", size=10, bbox=bbox, ink_bbox=bbox,
                    origin=(bbox[0], bbox[3] - 2), is_text=True)
        return TranslatableLine(
            page_idx=0, spans=[span], bbox=bbox, max_x1=480, template=template,
            math_spans=[], is_toc=False, toc_content="", toc_page_num="",
            font_style="regular", text_styles=[(len(template), "regular")],
        )

    tall_line = line("tous nuls. Par construction", (113, 344, 446, 363))
    next_paragraph = line("Si x, définissons", (125, 355, 480, 370))
    continuation = line("et la suite", (448, 346, 480, 360))

    merged = _merge_split_lines([tall_line, next_paragraph, continuation])

    assert [m.template for m in merged] == ["tous nuls. Par construction et la suite",
                                            "Si x, définissons"]


def test_overlapping_text_lines_merge_only_on_a_shared_baseline():
    def raw_line(text, bbox, baseline):
        span = {"text": text, "font": "SFRM1000", "size": 10, "bbox": bbox,
                "ink_bbox": bbox, "origin": (bbox[0], baseline)}
        return {"spans": [span], "bbox": bbox}

    same_line = [raw_line("posons", (113, 298, 216, 312), 310),
                 raw_line(". Les", (205, 300, 482, 314), 310)]
    radical_and_next_line = [raw_line("müssen, wo", (72, 356, 540, 368), 366),
                             raw_line("arbeiten", (465, 342, 540, 363), 354)]

    assert len(_merge_same_y_lines(same_line, text_shares_fonts=False)) == 1
    assert len(_merge_same_y_lines(radical_and_next_line, text_shares_fonts=False)) == 2


@pytest.mark.parametrize("variant", OTHER_TYPEFACES)
def test_typefaces_that_set_math_letters_in_the_text_italic(variant):
    templates = [line.template for line in extract_lines(open_fixture(variant))]

    assert templates[0] == "Fonctions continues"
    assert templates[1] == "Soit{M0} une fonction continue sur{M1} telle que{M2} pour tout{M3}"
    assert templates[3].startswith(
        "La dérivée vérifie{M0}, et nous avons donc démontré le résultat principal")


def test_italic_phrases_are_not_math_variables():
    def raw(text, font):
        return {"text": text, "font": font, "flags": 0}

    spans = [raw("qui ne semblent", "LMRoman10-Regular"), raw(" ", "LMRoman10-Regular"),
             raw("a", "LMRoman10-Italic"), raw(" ", "LMRoman10-Italic"),
             raw("priori", "LMRoman10-Italic"), raw(" couvrir une fonction", "LMRoman10-Regular"),
             raw("f", "LMRoman10-Italic"), raw(" continue", "LMRoman10-Regular")]
    _mark_math_variables(spans)

    assert [s["text"] for s in spans if s["is_variable"]] == ["f"]
