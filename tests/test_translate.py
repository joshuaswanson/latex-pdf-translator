import pytest

from translator import translate
from translator.extract import Span, TranslatableLine
from translator.translate import (
    MAX_GROUP_CHARS, _fix_terminology, _group_paragraphs, _postprocess_translation,
    translate_lines,
)


def make_line(template: str, y: float) -> TranslatableLine:
    span = Span(text=template, font="SFRM1000", size=10, bbox=(100, y, 500, y + 10),
                ink_bbox=(100, y, 500, y + 10), origin=(100, y + 8), is_text=True)
    return TranslatableLine(
        page_idx=0, spans=[span], bbox=span.bbox, max_x1=500, template=template,
        math_spans=[], is_toc=False, toc_content="", toc_page_num="",
        font_style="regular", text_styles=[(len(template), "regular")],
    )


@pytest.mark.parametrize("text, expected", [
    ("every compact open set is closed", "every compact open set is closed"),
    ("the restriction to a compact open.", "the restriction to a compact open set."),
    ("a compact open {M0}", "a compact open set {M0}"),
    ("compact open subgroups", "compact open subgroups"),
    ("whatever the case", "whatever the case"),
    ("we have {M0} whatever {M1}", "we have {M0} for all {M1}"),
    ("such that, whatever the {M1} family", "such that, for all the {M1} family"),
    ("f(x) tends to infinity", "f(x) tends to infinity"),
    ("Growth conditions to infinity", "Growth conditions at infinity"),
    ("Demonstration. — We have", "Proof. — We have"),
    ("VARIABLE {M0}-ADIC", "{M0}-ADIC VARIABLE"),
    ("functions of a variable {M3}-adic", "functions of a {M3}-adic variable"),
    ("2. Measurements", "2. Measures"),
    ("Analytical functions", "Analytic functions"),
    ("Lemme 3", "Lemma 3"),
])
def test_terminology_fixes(text, expected):
    assert _fix_terminology(text) == expected


def test_terminology_fixes_apply_only_to_english():
    assert _postprocess_translation("Demonstration XXXM0XXX", "en") == "Proof {M0}"
    assert _postprocess_translation("Demonstration XXXM0XXX", "es") == "Demonstration {M0}"


def test_paragraph_groups_stay_under_request_limit():
    lines = [make_line("mot " * 100, y=100 + 11 * i) for i in range(40)]
    groups = _group_paragraphs(lines)

    assert len(groups) > 1
    for group in groups:
        merged_len = sum(len(lines[i].template) for i in group) + len(group) - 1
        assert merged_len <= MAX_GROUP_CHARS


def test_cache_is_keyed_by_language_pair(fake_translator, tmp_path):
    cache_path = tmp_path / "cache.json"
    lines = [make_line("Soit une fonction continue", y=100)]

    translate_lines(lines, cache_path=cache_path, source="fr", target="en")
    translate_lines(lines, cache_path=cache_path, source="fr", target="en")
    translate_lines(lines, cache_path=cache_path, source="fr", target="es")

    assert [(s, t) for s, t, _ in fake_translator.calls] == [("fr", "en"), ("fr", "es")]


def test_failed_translations_are_not_cached(monkeypatch, tmp_path):
    class FailingTranslator:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            raise ConnectionError("offline")

    monkeypatch.setattr(translate, "GoogleTranslator", FailingTranslator)
    monkeypatch.setattr(translate.time, "sleep", lambda seconds: None)
    cache_path = tmp_path / "cache.json"
    lines = [make_line("Soit une fonction continue", y=100)]

    result = translate_lines(lines, cache_path=cache_path, source="fr", target="en")

    assert result == ["Soit une fonction continue"]
    assert cache_path.read_text() == "{}"


def test_body_text_in_12pt_documents_merges_into_paragraphs():
    lines = [make_line("mot " * 20, y=100 + 13 * i) for i in range(3)]
    for line in lines:
        line.spans[0].size = 12

    assert _group_paragraphs(lines) == [[0, 1, 2]]
