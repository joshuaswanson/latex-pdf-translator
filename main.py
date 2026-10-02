"""Translate a LaTeX-typeset PDF while preserving its mathematical notation.

1. Classify PDF text spans as translatable text or math by their font
2. Translate each line or paragraph via Google Translate, with XXXM0XXX
   placeholders standing in for math
3. Redact the original text and re-render the translation as vector text,
   with math in Latin Modern Math
"""

import argparse
from pathlib import Path

from translator.pipeline import NoTranslatableTextError, translate_pdf

STAGE_MESSAGES = {
    "extract": "Extracting translatable lines...",
    "translate": "Translating via Google Translate...",
    "render": "Rendering translations...",
}


def main():
    parser = argparse.ArgumentParser(description="Translate math LaTeX PDFs between languages")
    parser.add_argument("input", help="Input PDF file")
    parser.add_argument("--source", "-s", required=True, help="Source language code (e.g. fr, de)")
    parser.add_argument("--target", "-t", default="en", help="Target language code (default: en)")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = input_path.with_name(f"{input_path.stem}-{args.target}.pdf")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")

    current_stage = None

    def print_progress(stage, completed, total):
        nonlocal current_stage
        if stage != current_stage:
            if current_stage == "translate":
                print()  # newline after \r progress
            print(f"\n{STAGE_MESSAGES[stage]}")
            current_stage = stage
        if stage == "translate" and total:
            print(f"  {completed}/{total} groups translated", end="\r")

    try:
        result = translate_pdf(input_path.read_bytes(), args.source, args.target,
                               cache_path=input_path.with_suffix(".cache.json"),
                               on_progress=print_progress)
    except NoTranslatableTextError as e:
        raise SystemExit(str(e))

    output_path.write_bytes(result)
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
