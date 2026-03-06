# latex-pdf-translator

Translate French math LaTeX PDFs to English while preserving all mathematical notation as crisp vector text.

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

## Usage

```bash
uv run main.py                          # translate fonctionsdunevariable.pdf
uv run main.py path/to/french-math.pdf  # translate any French math PDF
```

Output is saved as `<input>-en.pdf`. A `.cache.json` file is created alongside for fast re-runs.

## Dependencies

```
pymupdf          # PDF reading, text extraction, redaction, rendering
deep-translator  # Free Google Translate API wrapper
```

## Fonts

The following fonts are included (all open source):

| Font                  | File                   | Purpose                                        |
| --------------------- | ---------------------- | ---------------------------------------------- |
| CMU Serif Roman       | `cmunrm.otf`           | Regular translated text                        |
| CMU Serif Bold        | `cmunbx.otf`           | Bold text (headings)                           |
| CMU Serif Italic      | `cmunti.otf`           | Italic text                                    |
| CMU Serif Bold Italic | `cmunbi.otf`           | Bold italic text                               |
| Latin Modern Math     | `latinmodern-math.otf` | Math symbols, operators, Greek, script letters |

## Limitations

- Line-level translation for math-containing lines (Google Translate can't reorder words around opaque markers perfectly)
- Some CMEX extensible delimiter fragments use image fallback when Unicode mapping unavailable
- Assumes French source language (configurable in code)
