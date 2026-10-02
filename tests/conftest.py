import re
from pathlib import Path

import pymupdf
import pytest

from translator import translate

FIXTURES = Path(__file__).parent / "fixtures"

# The fixtures are the same French document compiled with cm-super (T1),
# Latin Modern, and the default OT1 Computer Modern fonts.
FONT_VARIANTS = ["cmsuper", "lmodern", "cm"]

FRENCH_TO_ENGLISH = {
    "Fonctions": "Continuous", "continues": "functions", "Soit": "Let",
    "une": "be a", "fonction": "function", "continue": "continuous",
    "sur": "on", "telle": "such", "que": "that", "pour": "for",
    "tout": "all", "Théorème": "Theorem", "Toute": "Every", "un": "a",
    "est": "is", "bornée": "bounded", "La": "The", "dérivée": "derivative",
    "vérifie": "satisfies", "et": "and", "nous": "we", "avons": "have",
    "donc": "therefore", "démontré": "proved", "le": "the",
    "résultat": "result", "prin-": "main", "cipal": "", "de": "of",
    "cette": "this",
}


class FakeTranslator:
    """Word-by-word stand-in for GoogleTranslator that records its calls."""

    calls: list[tuple[str, str, str]] = []

    def __init__(self, source, target):
        self.source = source
        self.target = target

    def translate(self, text):
        FakeTranslator.calls.append((self.source, self.target, text))
        return re.sub(r"[^\W\d_][\w-]*",
                      lambda m: FRENCH_TO_ENGLISH.get(m.group(0), m.group(0)), text)


@pytest.fixture
def fake_translator(monkeypatch):
    FakeTranslator.calls = []
    monkeypatch.setattr(translate, "GoogleTranslator", FakeTranslator)
    return FakeTranslator


def fixture_pdf_bytes(variant: str) -> bytes:
    return (FIXTURES / f"{variant}.pdf").read_bytes()


def open_fixture(variant: str) -> pymupdf.Document:
    return pymupdf.open(FIXTURES / f"{variant}.pdf")
