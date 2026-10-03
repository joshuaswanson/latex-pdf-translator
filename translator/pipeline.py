from collections.abc import Callable
from pathlib import Path

import pymupdf

from translator.engines import Engine
from translator.extract import TranslatableLine, extract_lines
from translator.render import fix_link_annotations, render_all
from translator.translate import translate_lines

# Called as on_progress(stage, completed, total) with stage one of
# "extract", "translate", "render".
ProgressCallback = Callable[[str, int, int], None]


class NoTranslatableTextError(ValueError):
    pass


def translate_pdf(pdf_bytes: bytes, engine: Engine,
                  cache_path: Path | None = None,
                  on_progress: ProgressCallback | None = None) -> bytes:
    """Translate a LaTeX-typeset PDF, returning the translated PDF bytes.

    Raises EngineError when the translation engine fails in a way the user
    has to fix, such as a rejected API key.
    """
    report = on_progress or (lambda stage, completed, total: None)
    report("extract", 0, 0)
    lines = extract_pdf_lines(pdf_bytes)
    report("translate", 0, 0)
    translations = translate_lines(
        lines, engine, cache_path=cache_path,
        progress_callback=lambda completed, total: report("translate", completed, total),
    )
    return render_pdf(pdf_bytes, lines, translations, report)


def extract_pdf_lines(pdf_bytes: bytes) -> list[TranslatableLine]:
    with pymupdf.open("pdf", pdf_bytes) as doc:
        lines = extract_lines(doc)
    if not lines:
        raise NoTranslatableTextError("No translatable text found in this PDF.")
    return lines


def render_pdf(pdf_bytes: bytes, lines: list[TranslatableLine], translations: list[str],
               on_progress: ProgressCallback | None = None) -> bytes:
    """Render per-line translations into the PDF."""
    report = on_progress or (lambda stage, completed, total: None)
    with pymupdf.open("pdf", pdf_bytes) as orig_doc, pymupdf.open("pdf", pdf_bytes) as work_doc:
        link_info = render_all(
            work_doc, orig_doc, lines, translations,
            progress_callback=lambda completed, total: report("render", completed, total),
        )
        rendered = work_doc.tobytes(garbage=4, deflate=True)

    # insert_link creates links with xref=0, so reload to get real xrefs
    # before fixing link borders and rectangles.
    with pymupdf.open("pdf", rendered) as doc:
        fix_link_annotations(doc, *link_info)
        return doc.tobytes(garbage=4, deflate=True)
