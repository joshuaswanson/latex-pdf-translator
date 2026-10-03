# latex-pdf-translator

Translates LaTeX-typeset PDFs to English while preserving mathematical notation.

## How it works

1. **Extract** - Parse PDF text spans, classify as translatable text or math notation (CMMI, CMSY, CMEX, etc.) by font. Text fonts are cm-super (SFRM, SFBX, SFTI), Latin Modern (LMRoman), or Computer Modern (CMR, CMBX, CMTI); in the last two, spans that contain only digits, operators, or operator names like `sin` count as math
2. **Translate** - Send text to the chosen engine with `{M0}` placeholders for math spans. Each engine protects the placeholders in its own way (opaque tokens, XML or HTML tags, or instructions to an LLM), and translations whose placeholders come back altered are retried
3. **Render** - Remove original text via PDF redaction, re-render translated text using CMU Serif fonts and math symbols using Latin Modern Math with proper Unicode math italic/bold code points

Key features:

- Math symbols rendered as vector text (not images) using Latin Modern Math font
- Proper italic math variables via Unicode Mathematical Italic code points
- Bold/italic text style preservation
- TOC dot leaders and page numbers regenerated
- Hyperlink annotations preserved
- Translation cache for fast re-runs
- Paragraph-level translation for pure-text blocks
- Eight translation engines, including free local models (web UI currently limited to French and German)

**[Try it online](https://joshuaswanson.github.io/latex-pdf-translator/)** | [Buy me a coffee](https://buymeacoffee.com/swanson)

<img src="assets/bmc_qr.png" alt="Buy Me a Coffee QR" width="200">

## Usage

```bash
git clone https://github.com/joshuaswanson/latex-pdf-translator
cd latex-pdf-translator
uv sync
uv run main.py path/to/math.pdf --source fr             # French to English
uv run main.py path/to/math.pdf --source de --target es  # German to Spanish
```

Output is saved next to the input as `<input>-<target>.pdf`. A `.cache.json` file is created alongside for fast re-runs.

## Translation engines

Choose an engine with `--engine` (default `google`). Engines that need credentials read them from environment variables or from a `.env` file in the repository root. Copy `.env.template` to `.env` and fill in the keys you have.

| Engine | Needs | Notes |
|---|---|---|
| `google` | nothing | Free Google Translate endpoint. Google blocks it for some networks, including cloud servers. |
| `deepl` | `DEEPL_API_KEY` | DeepL API Free gives 500,000 characters a month. |
| `azure` | `AZURE_TRANSLATOR_KEY`, `AZURE_TRANSLATOR_REGION` | The free F0 tier gives 2 million characters a month. |
| `google-cloud` | `GOOGLE_CLOUD_API_KEY` | Official Google Cloud Translation API. |
| `claude` | `ANTHROPIC_API_KEY` | Defaults to `claude-opus-5-5`. |
| `gemini` | `GEMINI_API_KEY` | Defaults to `gemini-3.5-flash-lite`. |
| `ollama` | a running Ollama server | Local and free. Defaults to `qwen2.5:14b`. Set `OLLAMA_HOST` for a server other than `localhost:11434`. |
| `apple` | macOS 26+ with Apple Intelligence, `uv sync --extra apple` | Apple's on-device model. Local and free. |

```bash
uv run main.py paper.pdf --source de --engine ollama --model qwen2.5:32b
uv sync --extra apple && uv run main.py paper.pdf --source fr --engine apple
```

The website offers the engines that run on the server and asks for the key in the browser. Google blocks the free endpoint for the server's cloud IP addresses, so with the free Google option the visitor's browser translates the extracted text and sends it back for rendering. Ollama and the Apple model run only in the command-line tool.

`--fallback ENGINE` sends text the main engine fails to translate to a second engine, for example `--engine ollama --fallback google`. It is off by default, so a local engine never sends text anywhere unless asked to. The keyed engines send up to four requests at once. LLM engines also receive the text around each batch as context. If an engine keeps rate limiting requests, the run stops with a message and keeps the translations finished so far in the cache.

Run the tests with `uv run pytest`. They use small LaTeX fixtures in `tests/fixtures` and a fake engine, so they need no network access.

## Limitations

- Lines that contain math are translated one line at a time, so sentences that span several lines lose context
- Big delimiters and operators (CMEX), script and Fraktur letters, small caps, and glyphs in unknown fonts are copied from the original page
- Translated lines wider than their text column are condensed horizontally to at most 70% of their natural width; beyond that they overflow
- Terminology fixes for common machine translation mistakes in math prose apply only to English output from the google, deepl, azure and google-cloud engines
