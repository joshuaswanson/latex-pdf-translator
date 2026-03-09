"""Translate a French math LaTeX PDF to English.

For each line in the PDF:
1. Classify spans as text (translatable) or math (preserve)
2. Build full line text with XXXM0XXX markers for math symbols
3. Translate via Google Translate (free, handles word reordering naturally)
4. Re-render: white-out original line, place translated text + math glyph images
"""

import sys
from pathlib import Path

import pymupdf

from translator.extract import extract_lines
from translator.translate import translate_lines
from translator.render import render_all, _fix_link_annotations


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.pdf>")
        sys.exit(1)
    input_path = sys.argv[1]

    output_path = Path(input_path).stem + "-en.pdf"
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")

    orig_doc = pymupdf.open(input_path)
    work_doc = pymupdf.open(input_path)

    # Step 1: Extract
    print("\nExtracting translatable lines...")
    lines = extract_lines(orig_doc)
    print(f"Found {len(lines)} translatable lines across {len(orig_doc)} pages")

    # Step 2: Translate (with disk cache for fast re-runs)
    cache_path = Path(input_path).with_suffix(".cache.json")
    print("\nTranslating via Google Translate...")
    translations = translate_lines(lines, cache_path=cache_path)

    # Step 3: Render
    print("\nRendering translations...")
    annot_colors, rendered_extents, link_texts = render_all(work_doc, orig_doc, lines, translations)

    # Save to bytes, reload, and fix link border colors
    # (insert_link creates links with xref=0; need save/reload to get real xrefs)
    pdf_bytes = work_doc.tobytes(garbage=4, deflate=True)
    work_doc.close()
    orig_doc.close()

    doc = pymupdf.open("pdf", pdf_bytes)
    _fix_link_annotations(doc, annot_colors, rendered_extents, link_texts)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
