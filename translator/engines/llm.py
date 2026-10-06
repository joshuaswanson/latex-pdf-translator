"""Large language model engines.

Each request sends a JSON list of templates and asks for a JSON object with
one translation per template. A translation whose placeholders differ from its
template is retried on its own with the required placeholders spelled out,
then given up on.
"""

import asyncio
import json
import re
from collections import Counter
from collections.abc import Sequence

import requests

from translator.engines.base import (
    PLACEHOLDER_RE, Engine, EngineError, RateLimitedError, language_name, placeholders,
    post_json, retry_after_seconds,
)

TRANSLATIONS_SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array", "items": {"type": "string"}}},
    "required": ["translations"],
    "additionalProperties": False,
}


class LLMEngine(Engine):
    default_model = ""
    max_batch_items = 40
    max_batch_chars = 6000
    retries_failures = True

    def __init__(self, source, target, **kwargs):
        super().__init__(source, target, **kwargs)
        self.model = self.model or self.default_model

    @property
    def cache_id(self) -> str:
        return f"{self.name}:{self.model}"

    def instructions(self, note: str = "") -> str:
        source, target = language_name(self.source), language_name(self.target)
        return (
            f"You translate excerpts of a {source} mathematics paper into {target}. "
            "The input is a JSON object whose \"items\" are text taken from consecutive lines "
            "of the PDF. \"before\" and \"after\", when present, hold the neighboring text "
            "for context only and must not be translated. "
            "Items may start or end mid-sentence. Translate each item separately, never move "
            "words between items, and return exactly one translation per item, in the same order. "
            "Tokens of the form {M0}, {M1}, ... stand for mathematical formulas: copy every "
            "token exactly once and unchanged, placed where the formula belongs in the "
            "translated sentence. Leave proper names, numbering, labels, and citations "
            "unchanged. "
            f"Use standard {target} mathematical terminology. {note}"
        ).rstrip()

    def translate_batch(self, texts, context=("", "")):
        results = self._complete_checked(texts, context)
        out = []
        for i, (text, result) in enumerate(zip(texts, results)):
            if result is None:
                tokens = ", ".join(placeholders(text))
                note = (f"The translation must contain each of these tokens exactly once: {tokens}."
                        if tokens else "")
                neighbors = (texts[i - 1] if i > 0 else context[0],
                             texts[i + 1] if i + 1 < len(texts) else context[1])
                result = self._complete_checked([text], neighbors, note)[0]
            out.append(result)
        return out

    def _complete_checked(self, texts: list[str], context: tuple[str, str],
                          note: str = "") -> list[str | None]:
        """Translations with missing or altered placeholders replaced by None."""
        translations = self.complete(request_json(texts, context), len(texts), note)
        if translations is None or len(translations) != len(texts):
            return [None] * len(texts)
        checked = []
        for text, translation in zip(texts, translations):
            if translation is not None:
                translation = _drop_extra_placeholders(translation, text)
                if placeholders(translation) != placeholders(text):
                    translation = None
            checked.append(translation)
        return checked

    def complete(self, request: str, count: int, note: str = "") -> Sequence[str | None] | None:
        """One model request for `count` items, with `note` appended to the instructions.

        Returns None when the response is unusable.
        """
        raise NotImplementedError


class ClaudeEngine(LLMEngine):
    name = "claude"
    label = "Claude"
    default_model = "claude-opus-5-5"
    max_concurrency = 4
    max_batch_chars = 8000

    # Models that accept server-side refusal fallbacks
    FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

    def __init__(self, source, target, *, ambient_credentials=False, **kwargs):
        import anthropic

        self._anthropic = anthropic
        if not kwargs.get("api_key") and not ambient_credentials:
            raise EngineError("Claude needs an Anthropic API key.")
        super().__init__(source, target, **kwargs)
        try:
            # Without an explicit key the SDK reads ANTHROPIC_API_KEY or an `ant auth login` profile
            self._client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
        except anthropic.AnthropicError:
            raise EngineError("No Anthropic credentials found. Set ANTHROPIC_API_KEY.")

    def complete(self, request, count, note=""):
        anthropic = self._anthropic
        output_config: dict = {"format": {"type": "json_schema", "schema": TRANSLATIONS_SCHEMA}}
        if "haiku" not in self.model:
            output_config["effort"] = "low"
        request = dict(
            model=self.model,
            max_tokens=16000,
            system=self.instructions(note),
            messages=[{"role": "user", "content": request}],
            output_config=output_config,
        )
        try:
            if self.model in self.FALLBACK_MODELS:
                response = self._client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                response = self._client.messages.create(**request)
        except anthropic.RateLimitError as e:
            raise RateLimitedError(retry_after_seconds(e.response.headers))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise EngineError("Anthropic rejected the API key.")
        except anthropic.NotFoundError:
            raise EngineError(f"Claude model {self.model} was not found.")
        except anthropic.BadRequestError as e:
            raise EngineError(f"Claude rejected the request: {e.message}")

        if response.stop_reason != "end_turn":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        return _parse_translations(text)


class GeminiEngine(LLMEngine):
    name = "gemini"
    label = "Gemini"
    requires_key = True
    default_model = "gemini-3.5-flash-lite"
    max_concurrency = 4

    def complete(self, request, count, note=""):
        body = {
            "systemInstruction": {"parts": [{"text": self.instructions(note)}]},
            "contents": [{"role": "user",
                          "parts": [{"text": request}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": TRANSLATIONS_SCHEMA,
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        data = post_json(self.label, url, headers={"x-goog-api-key": self.api_key}, json=body)
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        return _parse_translations("".join(p.get("text", "") for p in parts))


class OllamaEngine(LLMEngine):
    name = "ollama"
    label = "Ollama"
    default_model = "qwen2.5:14b"
    max_batch_chars = 4000

    def complete(self, request, count, note=""):
        host = (self.host or "http://localhost:11434").rstrip("/")
        body = {
            "model": self.model,
            "stream": False,
            "format": TRANSLATIONS_SCHEMA,
            # Ollama's default context window is too small for a batch and its translation
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": self.instructions(note)},
                {"role": "user", "content": request},
            ],
        }
        try:
            data = post_json(self.label, f"{host}/api/chat", headers={}, json=body, timeout=900,
                             error_messages={404: f"Ollama has no model {self.model}. "
                                                  f"Run `ollama pull {self.model}`."})
        except requests.ConnectionError:
            raise EngineError(f"Cannot reach Ollama at {host}. Start it with `ollama serve`.")
        return _parse_translations(data.get("message", {}).get("content"))


class AppleEngine(LLMEngine):
    """Apple's on-device foundation model (macOS 26 or later with Apple Intelligence)."""

    name = "apple"
    label = "Apple on-device model"
    default_model = "system"
    # The small on-device model shifts translations between items in a batch,
    # so every item gets its own request
    max_batch_items = 1

    def __init__(self, source, target, **kwargs):
        super().__init__(source, target, **kwargs)
        try:
            import apple_fm_sdk as fm
        except ImportError:
            raise EngineError("The apple engine needs the apple-fm-sdk package. "
                              "Install it with `uv sync --extra apple`.")
        self._fm = fm
        self._system_model = fm.SystemLanguageModel()
        available, reason = self._system_model.is_available()
        if not available:
            raise EngineError(f"Apple's on-device model is unavailable: {reason}")

        self._schemas = {}

    def _schema(self, count: int):
        """Output type with exactly `count` translations."""
        if count not in self._schemas:
            fm = self._fm

            @fm.generable("Translations")
            class Translations:
                translations: list[str] = fm.guide("One translation per input item, in order",
                                                   count=count)

            self._schemas[count] = Translations
        return self._schemas[count]

    def complete(self, request, count, note=""):
        return asyncio.run(self._respond(request, count, note))

    async def _respond(self, request: str, count: int, note: str) -> list[str] | None:
        fm = self._fm
        session = fm.LanguageModelSession(instructions=self.instructions(note),
                                          model=self._system_model)
        try:
            result = await session.respond(request, generating=self._schema(count))
        except fm.RateLimitedError:
            raise RateLimitedError()
        except fm.UnsupportedLanguageOrLocaleError:
            raise EngineError("Apple's on-device model does not support this language.")
        except (fm.ExceededContextWindowSizeError, fm.GuardrailViolationError, fm.RefusalError,
                fm.DecodingFailureError):
            return None
        return list(result.translations)


def _drop_extra_placeholders(translation: str, template: str) -> str:
    """Remove placeholders the model invented or repeated.

    A token missing from the translation still fails the check, because that
    formula would be lost.
    """
    allowed = Counter(PLACEHOLDER_RE.findall(template))
    seen = Counter()

    def keep_allowed(match):
        token = match.group(0)
        seen[token] += 1
        return token if seen[token] <= allowed[token] else ""

    cleaned = PLACEHOLDER_RE.sub(keep_allowed, translation)
    return re.sub(r" {2,}", " ", cleaned).strip() if cleaned != translation else translation


def request_json(texts: list[str], context: tuple[str, str]) -> str:
    before, after = context
    request: dict = {"items": texts}
    if before:
        request["before"] = before
    if after:
        request["after"] = after
    return json.dumps(request, ensure_ascii=False)


def _parse_translations(text: str | None) -> list[str] | None:
    if text is None:
        return None
    try:
        translations = json.loads(text)["translations"]
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(translations, list) or not all(isinstance(t, str) for t in translations):
        return None
    return translations
