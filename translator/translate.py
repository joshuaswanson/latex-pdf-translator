import hashlib
import json
import re
import time
from pathlib import Path

from deep_translator import GoogleTranslator

from translator.charmap import TERM_FIXES
from translator.extract import TranslatableLine, Span


# Google Translate batch size (max 5000 chars per request)
BATCH_CHARS = 4800

# Math placeholder format: XXXM0XXX, XXXM1XXX, etc.
# Google Translate preserves these as opaque tokens
MATH_MARKER_RE = re.compile(r'XXXM(\d+)XXX', re.IGNORECASE)


def _prepare_gt_text(text: str) -> str:
    """Convert a template string to Google Translate input."""
    gt_text = re.sub(r'\{M(\d+)\}', r'XXXM\1XXX', text)
    # Ensure spaces around markers so Google sees them as separate tokens
    gt_text = re.sub('([a-zA-Z\u00C0-\u00ff])(XXXM\\d+XXX)', r'\1 \2', gt_text)
    gt_text = re.sub('(XXXM\\d+XXX)([a-zA-Z\u00C0-\u00ff])', r'\1 \2', gt_text)
    return gt_text


def _postprocess_translation(text: str) -> str:
    """Convert markers back and apply fixes to a translated string."""
    text = MATH_MARKER_RE.sub(r'{M\1}', text)
    # Clean up extra spaces around markers
    text = re.sub(r'\s+(\{M\d+\})\s+', r' \1 ', text)
    text = re.sub(r'\s+(\{M\d+\})-', r' \1-', text)
    text = _fix_terminology(text)
    return text


def _group_paragraphs(lines: list[TranslatableLine]) -> list[list[int]]:
    """Group consecutive body-text lines into paragraphs for better translation.

    Returns list of groups, where each group is a list of indices into `lines`.
    Only merges lines that have NO math spans (pure text) -- lines with math
    markers stay standalone to avoid marker redistribution issues.
    TOC lines and headings also stay as single-line groups.
    """
    groups = []
    current_group = []

    def _is_mergeable(line):
        """A line can be merged into a paragraph only if it has no math."""
        if line.is_toc:
            return False
        if line.font_style == "bold":
            return False
        if line.spans and line.spans[0].size > 11:
            return False
        if line.math_spans:
            return False
        return True

    for i, line in enumerate(lines):
        if not _is_mergeable(line):
            if current_group:
                groups.append(current_group)
                current_group = []
            groups.append([i])
            continue

        # Check if this line continues the current group
        if current_group:
            prev = lines[current_group[-1]]
            same_page = line.page_idx == prev.page_idx
            similar_x = abs(line.bbox[0] - prev.bbox[0]) < 20
            consecutive_y = (line.bbox[1] - prev.bbox[3]) < prev.spans[0].size
            same_size = abs(line.spans[0].size - prev.spans[0].size) < 0.5

            if same_page and similar_x and consecutive_y and same_size:
                current_group.append(i)
            else:
                groups.append(current_group)
                current_group = [i]
        else:
            current_group = [i]

    if current_group:
        groups.append(current_group)

    return groups


def _merge_paragraph_templates(lines: list[TranslatableLine],
                                indices: list[int]) -> tuple[str, list[list[Span]]]:
    """Merge multiple line templates into one paragraph template.

    Renumbers math markers sequentially and merges math_spans lists.
    Returns (merged_template, merged_math_spans).
    """
    merged_parts = []
    merged_math = []

    for idx in indices:
        line = lines[idx]
        text = line.toc_content if line.is_toc else line.template
        # Renumber {M0}, {M1}, ... relative to current merged_math length
        offset = len(merged_math)
        renumbered = re.sub(
            r'\{M(\d+)\}',
            lambda m: f"{{M{int(m.group(1)) + offset}}}",
            text,
        )
        merged_parts.append(renumbered)
        merged_math.extend(line.math_spans)

    return " ".join(merged_parts), merged_math


def _split_translation(translated: str, lines: list[TranslatableLine],
                       indices: list[int],
                       merged_math: list[list[Span]]) -> list[str]:
    """Split a paragraph translation back into per-line translations.

    Distributes words proportionally based on original line text lengths,
    preserving math markers on the correct lines.
    """
    if len(indices) == 1:
        return [translated]

    # Calculate target character count per line (proportional to original)
    orig_lengths = []
    for idx in indices:
        line = lines[idx]
        text = line.toc_content if line.is_toc else line.template
        orig_lengths.append(len(text))
    total = sum(orig_lengths)
    if total == 0:
        return [translated] + [""] * (len(indices) - 1)

    # Split translated text into tokens (words and math markers)
    tokens = re.split(r'(\s+)', translated)
    tokens = [t for t in tokens if t]  # remove empty

    # Distribute tokens across lines
    result_lines = []
    token_idx = 0
    for i, idx in enumerate(indices):
        target_len = orig_lengths[i]
        target_frac = target_len / total
        target_chars = int(len(translated) * target_frac)

        line_tokens = []
        line_len = 0

        while token_idx < len(tokens):
            tok = tokens[token_idx]
            # Always put at least one token per line
            if not line_tokens or (line_len + len(tok) <= target_chars * 1.3):
                line_tokens.append(tok)
                line_len += len(tok)
                token_idx += 1
            else:
                break

            # Don't exceed target too much (except for last line)
            if i < len(indices) - 1 and line_len >= target_chars:
                break

        result_lines.append("".join(line_tokens).strip())

    # Last line gets remaining tokens
    if token_idx < len(tokens):
        remaining = "".join(tokens[token_idx:]).strip()
        if result_lines:
            result_lines[-1] = (result_lines[-1] + " " + remaining).strip()
        else:
            result_lines.append(remaining)

    # Ensure we have exactly the right number of lines
    while len(result_lines) < len(indices):
        result_lines.append("")

    # Remap math markers back to per-line numbering
    for i, idx in enumerate(indices):
        line = lines[idx]
        n_math = len(line.math_spans)
        # Calculate the offset for this line's markers in the merged numbering
        offset = sum(len(lines[indices[j]].math_spans) for j in range(i))

        line_text = result_lines[i]
        # Replace merged marker numbers with local numbers
        for merged_idx in range(offset, offset + n_math):
            local_idx = merged_idx - offset
            line_text = line_text.replace(
                f"{{M{merged_idx}}}",
                f"{{MLOCAL{local_idx}}}",
            )
        # Clean up: remove markers that belong to other lines
        line_text = re.sub(r'\{M\d+\}', '', line_text)
        # Rename local markers back
        line_text = re.sub(r'\{MLOCAL(\d+)\}', r'{M\1}', line_text)
        result_lines[i] = line_text.strip()

    return result_lines


def _load_cache(cache_path: Path) -> dict:
    """Load translation cache from disk."""
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def _save_cache(cache_path: Path, cache: dict):
    """Save translation cache to disk."""
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))


def translate_lines(lines: list[TranslatableLine],
                    cache_path: Path | None = None,
                    source: str = 'fr',
                    target: str = 'en',
                    progress_callback=None) -> list[str]:
    """Translate all lines via Google Translate (free).

    Groups consecutive body-text lines into paragraphs for better translation
    quality, then splits results back to per-line for rendering.
    Uses a disk cache to avoid re-translating on subsequent runs.
    """
    translator = GoogleTranslator(source=source, target=target)
    groups = _group_paragraphs(lines)

    # Load translation cache
    cache = _load_cache(cache_path) if cache_path else {}

    # Prepare paragraph texts
    group_texts = []
    group_math = []
    for group in groups:
        if len(group) == 1:
            line = lines[group[0]]
            text = line.toc_content if line.is_toc else line.template
            group_texts.append(_prepare_gt_text(text))
            group_math.append(line.math_spans)
        else:
            merged, merged_ms = _merge_paragraph_templates(lines, group)
            group_texts.append(_prepare_gt_text(merged))
            group_math.append(merged_ms)

    # Separate cached vs uncached
    to_translate = []  # (group_idx, text)
    translated_groups = []  # (group_idx, result)

    for i, text in enumerate(group_texts):
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in cache:
            translated_groups.append((i, cache[cache_key]))
        else:
            to_translate.append((i, text, cache_key))

    if to_translate:
        print(f"  {len(translated_groups)} cached, {len(to_translate)} to translate")
    else:
        print(f"  All {len(translated_groups)} translations cached")

    # Translate uncached in batches with retry
    batch = []
    batch_len = 0
    completed = 0
    total_to_translate = len(to_translate)

    for i, text, cache_key in to_translate:
        batch.append((i, text, cache_key))
        batch_len += len(text)

        if batch_len >= BATCH_CHARS or (i, text, cache_key) == to_translate[-1]:
            batch_texts = [t for _, t, _ in batch]
            results = None

            for attempt in range(3):
                try:
                    results = translator.translate_batch(batch_texts)
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** (attempt + 1)
                        print(f"    Retry {attempt + 1}/3 after {wait}s: {e}")
                        time.sleep(wait)
                    else:
                        print(f"    WARNING: translation failed after 3 attempts: {e}")
                        results = batch_texts

            for (idx, _orig, ck), result in zip(batch, results):
                if result is None:
                    result = batch_texts[idx - batch[0][0]]
                translated_groups.append((idx, result))
                cache[ck] = result

            completed += len(batch)
            if progress_callback:
                progress_callback(completed, total_to_translate)

            batch = []
            batch_len = 0
            time.sleep(0.3)

    # Save updated cache
    if cache_path:
        _save_cache(cache_path, cache)

    # Sort and postprocess
    translated_groups.sort(key=lambda x: x[0])

    # Split paragraph translations back to per-line
    translations = [""] * len(lines)
    for (group_idx, translated_text), group in zip(translated_groups, groups):
        processed = _postprocess_translation(translated_text)

        if len(group) == 1:
            translations[group[0]] = processed
        else:
            # Split merged translation back to individual lines
            per_line = _split_translation(processed, lines, group,
                                          group_math[group_idx])
            for idx, text in zip(group, per_line):
                translations[idx] = text

    return translations


def _fix_terminology(text: str) -> str:
    """Fix known Google Translate mistakes for math terminology."""
    for wrong, right in TERM_FIXES.items():
        text = text.replace(wrong, right)
    return text
