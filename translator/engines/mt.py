"""Machine translation services."""

import html
import re

import httpx
from deep_translator.constants import GOOGLE_LANGUAGES_TO_CODES

from translator.engines.base import (
    Engine, EngineError, RateLimitedError, post_json, retry_after_seconds,
)

GOOGLE_MARKER_RE = re.compile(r"XXXM(\d+)XXX", re.IGNORECASE)


class GoogleFreeEngine(Engine):
    """The keyless Google Translate endpoint. It treats XXXM0XXX as an opaque token."""

    name = "google"
    label = "Google Translate"
    apply_term_fixes = True

    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, source, target, **kwargs):
        super().__init__(source, target, **kwargs)
        codes = set(GOOGLE_LANGUAGES_TO_CODES.values())
        for code in (source, target):
            if code.lower() not in codes:
                raise EngineError(f"Google Translate does not support the language {code!r}.")
        # Google answers requests from the `requests` library with HTTP 429
        # where it serves httpx
        self._client = httpx.Client(timeout=60)

    def translate_batch(self, texts, context=("", "")):
        return [self._translate(text) for text in texts]

    def _translate(self, text: str) -> str:
        response = self._client.post(
            self.URL,
            params={"client": "gtx", "sl": self.source, "tl": self.target, "dt": "t"},
            data={"q": self._to_markers(text)},
            timeout=60,
        )
        if response.status_code == 429:
            raise RateLimitedError(retry_after_seconds(response.headers))
        response.raise_for_status()
        segments = response.json()[0] or []
        return self._from_markers("".join(segment[0] for segment in segments if segment[0]))

    @staticmethod
    def _to_markers(text: str) -> str:
        text = re.sub(r"\{M(\d+)\}", r"XXXM\1XXX", text)
        # Spaces around markers make Google see them as separate tokens
        text = re.sub("([a-zA-ZÀ-ÿ])(XXXM\\d+XXX)", r"\1 \2", text)
        return re.sub("(XXXM\\d+XXX)([a-zA-ZÀ-ÿ])", r"\1 \2", text)

    @staticmethod
    def _from_markers(text: str) -> str:
        return GOOGLE_MARKER_RE.sub(r"{M\1}", text)


class DeepLEngine(Engine):
    name = "deepl"
    label = "DeepL"
    requires_key = True
    max_concurrency = 4
    max_batch_items = 50
    max_batch_chars = 30000
    apply_term_fixes = True

    # DeepL requires a regional variant for these target languages
    TARGET_VARIANTS = {"en": "EN-US", "pt": "PT-PT", "zh": "ZH-HANS"}

    def translate_batch(self, texts, context=("", "")):
        # Free API keys end in ":fx" and use a separate host
        host = "api-free.deepl.com" if self.api_key.endswith(":fx") else "api.deepl.com"
        body = {
            "text": [to_xml(t) for t in texts],
            "source_lang": self.source.upper(),
            "target_lang": self.TARGET_VARIANTS.get(self.target.lower(), self.target.upper()),
            "tag_handling": "xml",
        }
        data = post_json(
            self.label, f"https://{host}/v2/translate", json=body,
            headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
            error_messages={456: "The DeepL character quota for this month is used up."},
        )
        return [from_xml(t["text"]) for t in data["translations"]]


class AzureEngine(Engine):
    name = "azure"
    label = "Azure Translator"
    requires_key = True
    max_concurrency = 4
    max_batch_items = 100
    max_batch_chars = 20000
    apply_term_fixes = True

    def translate_batch(self, texts, context=("", "")):
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        params = {"api-version": "3.0", "from": self.source, "to": self.target, "textType": "html"}
        data = post_json(self.label, "https://api.cognitive.microsofttranslator.com/translate",
                         headers=headers, params=params,
                         json=[{"Text": to_html(t, 'class="notranslate"')} for t in texts])
        return [from_html(item["translations"][0]["text"]) for item in data]


class GoogleCloudEngine(Engine):
    name = "google-cloud"
    label = "Google Cloud Translation"
    requires_key = True
    max_concurrency = 4
    max_batch_items = 100
    max_batch_chars = 25000
    apply_term_fixes = True

    def translate_batch(self, texts, context=("", "")):
        body = {
            "q": [to_html(t, 'translate="no"') for t in texts],
            "source": self.source,
            "target": self.target,
            "format": "html",
        }
        data = post_json(self.label, "https://translation.googleapis.com/language/translate/v2",
                         headers={"X-Goog-Api-Key": self.api_key}, json=body)
        return [from_html(t["translatedText"]) for t in data["data"]["translations"]]


def to_xml(text: str) -> str:
    return re.sub(r"\{M(\d+)\}", r'<m i="\1"/>', html.escape(text, quote=False))


def from_xml(text: str) -> str:
    return html.unescape(re.sub(r'<m i="(\d+)"\s*/>', r"{M\1}", text))


def to_html(text: str, no_translate_attribute: str) -> str:
    return re.sub(r"(\{M\d+\})", rf"<span {no_translate_attribute}>\1</span>",
                  html.escape(text, quote=False))


def from_html(text: str) -> str:
    return html.unescape(re.sub(r"<span[^>]*>\s*(\{M\d+\})\s*</span>", r"\1", text))
