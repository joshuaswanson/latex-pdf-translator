from collections.abc import Callable
from pathlib import Path

import pymupdf

from translator.extract import extract_lines
from translator.render import fix_link_annotations, render_all
from translator.translate import translate_lines

# Called as on_progress(stage, completed, total) with stage one of
# "extract", "translate", "render".
ProgressCallback = Callable[[str, int, int], None]


class NoTranslatableTextError(ValueError):
    pass


def translate_pdf(pdf_bytes: bytes, source: str, target: str,
                  cache_path: Path | None = None,
                  on_progress: ProgressCallback | None = None) -> bytes:
    """Translate a LaTeX-typeset PDF, returning the translated PDF bytes."""
    report = on_progress or (lambda stage, completed, total: None)

    with pymupdf.open("pdf", pdf_bytes) as orig_doc, pymupdf.open("pdf", pdf_bytes) as work_doc:
        report("extract", 0, 0)
        lines = extract_lines(orig_doc)
        if not lines:
            raise NoTranslatableTextError("No translatable text found in this PDF.")

        report("translate", 0, 0)
        translations = translate_lines(
            lines, cache_path=cache_path, source=source, target=target,
            progress_callback=lambda completed, total: report("translate", completed, total),
        )
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
