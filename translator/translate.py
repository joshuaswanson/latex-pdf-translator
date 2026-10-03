import hashlib
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from translator.charmap import TERM_FIXES
from translator.engines import ENGLISH_CODES, Engine, EngineError, RateLimitedError
from translator.extract import TranslatableLine, Span


# Google Translate rejects requests of 5000 characters or more
MAX_GROUP_CHARS = 4500
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 60

# Lines this much larger than the body text are treated as headings
HEADING_SIZE_RATIO = 1.1

TERM_FIX_PATTERNS = [
    (re.compile(rf"\b{pattern}", re.IGNORECASE), replacement)
    for pattern, replacement in TERM_FIXES.items()
]


def _postprocess_translation(text: str, apply_term_fixes: bool) -> str:
    """Normalize spacing around math placeholders and fix known terminology mistakes."""
    text = re.sub(r'\s+(\{M\d+\})\s+', r' \1 ', text)
    text = re.sub(r'\s+(\{M\d+\})-', r' \1-', text)
    if apply_term_fixes:
        text = _fix_terminology(text)
    return text


def _fix_terminology(text: str) -> str:
    """Fix known Google Translate mistakes for English math terminology."""
    for pattern, replacement in TERM_FIX_PATTERNS:
        text = pattern.sub(lambda m: _match_case(m.expand(replacement), m.group(0)), text)
    return text


def _match_case(replacement: str, matched: str) -> str:
    if matched.isupper():
        return replacement.upper()
    if matched[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def _group_paragraphs(lines: list[TranslatableLine]) -> list[list[int]]:
    """Group consecutive body-text lines into paragraphs for better translation.

    Returns list of groups, where each group is a list of indices into `lines`.
    Only merges lines that have NO math spans (pure text) -- lines with math
    markers stay standalone to avoid marker redistribution issues.
    TOC lines and headings also stay as single-line groups. A group grows to
    at most MAX_GROUP_CHARS characters.
    """
    groups = []
    current_group = []
    current_len = 0
    heading_size = _body_font_size(lines) * HEADING_SIZE_RATIO

    def _is_mergeable(line):
        """A line can be merged into a paragraph only if it has no math."""
        if line.is_toc:
            return False
        if line.font_style == "bold":
            return False
        if line.spans and line.spans[0].size > heading_size:
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
            fits = current_len + 1 + len(line.template) <= MAX_GROUP_CHARS

            if same_page and similar_x and consecutive_y and same_size and fits:
                current_group.append(i)
                current_len += 1 + len(line.template)
            else:
                groups.append(current_group)
                current_group = [i]
                current_len = len(line.template)
        else:
            current_group = [i]
            current_len = len(line.template)

    if current_group:
        groups.append(current_group)

    return groups


def _body_font_size(lines: list[TranslatableLine]) -> float:
    """Most common line font size, weighted by text length."""
    sizes = Counter()
    for line in lines:
        if line.spans:
            sizes[round(line.spans[0].size, 1)] += len(line.template)
    return sizes.most_common(1)[0][0] if sizes else 10.0


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


def _cache_key(engine: Engine, text: str) -> str:
    key = f"{engine.cache_id}:{engine.source}:{engine.target}:{text}"
    return hashlib.md5(key.encode()).hexdigest()


def _batches(indices: list[int], texts: list[str], engine: Engine):
    """Split indices into batches that respect the engine's request limits."""
    batch, size = [], 0
    for i in indices:
        if batch and (len(batch) >= engine.max_batch_items
                      or size + len(texts[i]) > engine.max_batch_chars):
            yield batch
            batch, size = [], 0
        batch.append(i)
        size += len(texts[i])
    if batch:
        yield batch


def _translate_with_retry(engine: Engine, texts: list[str],
                          context: tuple[str, str]) -> list[str | None]:
    """Translate one batch, with None for texts that could not be translated.

    Raises EngineError when the engine keeps rate limiting, so a blocked
    service stops the run quickly.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return engine.translate_batch(texts, context)
        except EngineError:
            raise
        except RateLimitedError as e:
            if attempt == MAX_ATTEMPTS:
                raise EngineError(f"{engine.label} is rate limiting requests. Try again later.")
            wait = min(e.retry_after or 2 ** attempt, MAX_RETRY_WAIT)
            reason = "rate limited"
        except Exception as e:
            if attempt == MAX_ATTEMPTS:
                print(f"    WARNING: translation failed after {MAX_ATTEMPTS} attempts: {e}")
                return [None] * len(texts)
            wait = 2 ** attempt
            reason = str(e)
        print(f"    Retry {attempt}/{MAX_ATTEMPTS - 1} after {wait:.0f}s: {reason}")
        time.sleep(wait)


def translate_lines(lines: list[TranslatableLine], engine: Engine,
                    cache_path: Path | None = None,
                    progress_callback=None) -> list[str]:
    """Translate all lines with the given engine.

    Groups consecutive body-text lines into paragraphs for better translation
    quality, then splits results back to per-line for rendering.
    Uses a disk cache to avoid re-translating on subsequent runs.
    """
    groups = _group_paragraphs(lines)
    cache = _load_cache(cache_path) if cache_path else {}

    group_texts = []
    group_math = []
    for group in groups:
        if len(group) == 1:
            line = lines[group[0]]
            group_texts.append(line.toc_content if line.is_toc else line.template)
            group_math.append(line.math_spans)
        else:
            merged, merged_ms = _merge_paragraph_templates(lines, group)
            group_texts.append(merged)
            group_math.append(merged_ms)

    keys = [_cache_key(engine, text) for text in group_texts]
    translated = [cache.get(key) for key in keys]
    pending = [i for i, t in enumerate(translated) if t is None]

    if pending:
        print(f"  {len(groups) - len(pending)} cached, {len(pending)} to translate")
    else:
        print(f"  All {len(groups)} translations cached")

    if progress_callback and pending:
        progress_callback(0, len(pending))

    # Set when the engine fails for good, so queued batches stop being sent
    stopped = threading.Event()

    def translate_batch(batch: list[int]) -> list[str | None]:
        if stopped.is_set():
            return [None] * len(batch)
        context = (group_texts[batch[0] - 1] if batch[0] > 0 else "",
                   group_texts[batch[-1] + 1] if batch[-1] + 1 < len(group_texts) else "")
        try:
            return _translate_with_retry(engine, [group_texts[i] for i in batch], context)
        except EngineError:
            stopped.set()
            raise

    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=engine.max_concurrency) as pool:
            futures = {pool.submit(translate_batch, batch): batch
                       for batch in _batches(pending, group_texts, engine)}
            try:
                for future in as_completed(futures):
                    batch = futures[future]
                    for i, result in zip(batch, future.result()):
                        if result is None:
                            # Fall back to the untranslated text without caching
                            # it, so the next run retries this group.
                            translated[i] = group_texts[i]
                        else:
                            translated[i] = result
                            cache[keys[i]] = result
                    completed += len(batch)
                    if progress_callback:
                        progress_callback(completed, len(pending))
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    finally:
        if cache_path:
            _save_cache(cache_path, cache)

    apply_term_fixes = engine.apply_term_fixes and engine.target.lower() in ENGLISH_CODES

    # Split paragraph translations back to per-line
    translations = [""] * len(lines)
    for group_idx, (translated_text, group) in enumerate(zip(translated, groups)):
        processed = _postprocess_translation(translated_text, apply_term_fixes)

        if len(group) == 1:
            translations[group[0]] = processed
        else:
            per_line = _split_translation(processed, lines, group,
                                          group_math[group_idx])
            for idx, text in zip(group, per_line):
                translations[idx] = text

    return translations
