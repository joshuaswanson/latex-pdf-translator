import json

import pytest

from conftest import FakeEngine
from translator import translate
from translator.engines import EngineError, RateLimitedError
from translator.extract import Span, TranslatableLine
from translator.translate import (
    MAX_GROUP_CHARS, _fix_terminology, _group_paragraphs, translate_lines,
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
    lines = [make_line("Démonstration de la proposition", y=100)]

    class Literal(FakeEngine):
        apply_term_fixes = True

        def translate_batch(self, texts, context=("", "")):
            return ["Demonstration of the proposition" for _ in texts]

    assert translate_lines(lines, Literal(target="en")) == ["Proof of the proposition"]
    assert translate_lines(lines, Literal(target="es")) == ["Demonstration of the proposition"]


def test_paragraph_groups_stay_under_request_limit():
    lines = [make_line("mot " * 100, y=100 + 11 * i) for i in range(40)]
    groups = _group_paragraphs(lines)

    assert len(groups) > 1
    for group in groups:
        merged_len = sum(len(lines[i].template) for i in group) + len(group) - 1
        assert merged_len <= MAX_GROUP_CHARS


def test_cache_is_keyed_by_engine_and_language_pair(tmp_path):
    cache_path = tmp_path / "cache.json"
    lines = [make_line("Soit une fonction continue", y=100)]
    english, spanish = FakeEngine(target="en"), FakeEngine(target="es")

    translate_lines(lines, english, cache_path=cache_path)
    translate_lines(lines, english, cache_path=cache_path)
    translate_lines(lines, spanish, cache_path=cache_path)

    assert len(english.batches) == 1
    assert len(spanish.batches) == 1


def test_requests_are_batched_within_engine_limits():
    lines = [make_line(f"ligne {i}", y=100 + 30 * i) for i in range(25)]
    engine = FakeEngine()

    translate_lines(lines, engine)

    assert [len(batch) for batch in engine.batches] == [10, 10, 5]


def test_failed_translations_are_not_cached(monkeypatch, tmp_path):
    class FailingEngine(FakeEngine):
        def translate_batch(self, texts, context=("", "")):
            raise ConnectionError("offline")

    monkeypatch.setattr(translate.time, "sleep", lambda seconds: None)
    cache_path = tmp_path / "cache.json"
    lines = [make_line("Soit une fonction continue", y=100)]

    result = translate_lines(lines, FailingEngine(), cache_path=cache_path)

    assert result == ["Soit une fonction continue"]
    assert cache_path.read_text() == "{}"


def test_rate_limiting_stops_the_run_and_keeps_finished_translations(monkeypatch, tmp_path):
    class RateLimitedEngine(FakeEngine):
        max_batch_items = 1

        def translate_batch(self, texts, context=("", "")):
            if self.batches:
                raise RateLimitedError()
            return super().translate_batch(texts, context)

    sleeps = []
    monkeypatch.setattr(translate.time, "sleep", sleeps.append)
    cache_path = tmp_path / "cache.json"
    lines = [make_line(f"ligne {i}", y=100 + 30 * i) for i in range(5)]

    with pytest.raises(EngineError, match="rate limiting"):
        translate_lines(lines, RateLimitedEngine(), cache_path=cache_path)

    assert len(sleeps) == translate.MAX_ATTEMPTS - 1
    assert len(json.loads(cache_path.read_text())) == 1


def test_body_text_in_12pt_documents_merges_into_paragraphs():
    lines = [make_line("mot " * 20, y=100 + 13 * i) for i in range(3)]
    for line in lines:
        line.spans[0].size = 12

    assert _group_paragraphs(lines) == [[0, 1, 2]]


def test_batches_run_in_parallel_up_to_the_engine_limit():
    import threading
    import time as clock

    class ParallelEngine(FakeEngine):
        max_batch_items = 1
        max_concurrency = 4

        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def translate_batch(self, texts, context=("", "")):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            clock.sleep(0.05)
            with self.lock:
                self.active -= 1
            return [text.upper() for text in texts]

    lines = [make_line(f"ligne {i}", y=100 + 30 * i) for i in range(12)]
    engine = ParallelEngine()

    result = translate_lines(lines, engine)

    assert result == [f"LIGNE {i}" for i in range(12)]
    assert engine.peak == 4
