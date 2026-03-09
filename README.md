# latex-pdf-translator

Translate math LaTeX PDFs to English from any language while preserving all mathematical notation as crisp vector text.

## How it works

1. **Extract** - Parse PDF text spans, classify as translatable text (SFRM, SFBX, SFTI fonts) or math notation (CMMI, CMSY, CMEX, etc.)
2. **Translate** - Send text to Google Translate (free) with `XXXM0XXX` placeholders for math spans, which Google preserves as opaque tokens
3. **Render** - Remove original text via PDF redaction, re-render translated text using CMU Serif fonts and math symbols using Latin Modern Math with proper Unicode math italic/bold code points

Key features:

- Math symbols rendered as vector text (not images) using Latin Modern Math font
- Proper italic math variables via Unicode Mathematical Italic code points
- Bold/italic text style preservation
- TOC dot leaders and page numbers regenerated
- Hyperlink annotations preserved
- Translation cache for fast re-runs
- Paragraph-level translation for pure-text blocks
- Any source language supported by Google Translate

**[Try it online](https://joshuaswanson.github.io/latex-pdf-translator/)** | [Source on GitHub](https://github.com/joshuaswanson/latex-pdf-translator) | [Buy me a coffee](https://buymeacoffee.com/swanson)

## Usage

```bash
git clone https://github.com/joshuaswanson/latex-pdf-translator
cd latex-pdf-translator
uv sync
uv run main.py path/to/math.pdf --source fr             # French to English
uv run main.py path/to/math.pdf --source de --target es  # German to Spanish
```

Output is saved as `<input>-<target>.pdf`. A `.cache.json` file is created alongside for fast re-runs.

## Limitations

- Line-level translation for math-containing lines (Google Translate can't reorder words around opaque markers perfectly)
- Some CMEX extensible delimiter fragments use image fallback when Unicode mapping unavailable
- Optimized for LaTeX-generated PDFs with standard Computer Modern fonts
