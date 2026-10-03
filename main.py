"""Translate a LaTeX-typeset PDF while preserving its mathematical notation.

1. Classify PDF text spans as translatable text or math by their font
2. Translate each line or paragraph with the chosen engine, with {M0}
   placeholders standing in for math
3. Redact the original text and re-render the translation as vector text,
   with math in Latin Modern Math
"""

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values

from translator.engines import ENGINES, EngineError, create_engine
from translator.pipeline import NoTranslatableTextError, translate_pdf

STAGE_MESSAGES = {
    "extract": "Extracting translatable lines...",
    "translate": "Translating...",
    "render": "Rendering translations...",
}

# Environment variables (also read from .env) holding each engine's credentials
ENGINE_ENV_VARS = {
    "deepl": {"api_key": "DEEPL_API_KEY"},
    "azure": {"api_key": "AZURE_TRANSLATOR_KEY", "region": "AZURE_TRANSLATOR_REGION"},
    "google-cloud": {"api_key": "GOOGLE_CLOUD_API_KEY"},
    "claude": {"api_key": "ANTHROPIC_API_KEY"},
    "gemini": {"api_key": "GEMINI_API_KEY"},
    "ollama": {"host": "OLLAMA_HOST"},
}


def engine_options(engine: str) -> dict:
    settings = {**dotenv_values(Path(__file__).with_name(".env")), **os.environ}
    return {
        option: settings[var]
        for option, var in ENGINE_ENV_VARS.get(engine, {}).items()
        if settings.get(var)
    }


def main():
    parser = argparse.ArgumentParser(description="Translate math LaTeX PDFs between languages")
    parser.add_argument("input", help="Input PDF file")
    parser.add_argument("--source", "-s", required=True, help="Source language code (e.g. fr, de)")
    parser.add_argument("--target", "-t", default="en", help="Target language code (default: en)")
    parser.add_argument("--engine", "-e", default="google", choices=list(ENGINES),
                        help="Translation engine (default: google)")
    parser.add_argument("--model", "-m", help="Model for the claude, gemini and ollama engines")
    parser.add_argument("--fallback", "-f", choices=list(ENGINES),
                        help="Engine for text the main engine fails to translate (default: none)")
    args = parser.parse_args()

    try:
        engine = create_engine(args.engine, args.source, args.target, model=args.model,
                               ambient_credentials=True, **engine_options(args.engine))
        fallback = args.fallback and create_engine(
            args.fallback, args.source, args.target, ambient_credentials=True,
            **engine_options(args.fallback))
    except EngineError as e:
        raise SystemExit(str(e))

    input_path = Path(args.input)
    output_path = input_path.with_name(f"{input_path.stem}-{args.target}.pdf")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Engine: {engine.label}" + (f" ({engine.model})" if engine.model else ""))
    if fallback:
        print(f"Fallback: {fallback.label}")

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
        result = translate_pdf(input_path.read_bytes(), engine,
                               cache_path=input_path.with_suffix(".cache.json"),
                               on_progress=print_progress, fallback=fallback or None)
    except (NoTranslatableTextError, EngineError) as e:
        raise SystemExit(f"\n{e}")

    output_path.write_bytes(result)
    print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
