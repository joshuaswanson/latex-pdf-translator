import re

import requests
from deep_translator.constants import GOOGLE_LANGUAGES_TO_CODES

PLACEHOLDER_RE = re.compile(r"\{M\d+\}")
ENGLISH_CODES = {"en", "english"}
LANGUAGE_NAMES = {code: name.title() for name, code in GOOGLE_LANGUAGES_TO_CODES.items()}


class EngineError(Exception):
    """Translation cannot continue. The message is safe to show to users."""


class RateLimitedError(Exception):
    def __init__(self, retry_after: float | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class Engine:
    """Translates batches of templates whose {M0}, {M1}, ... placeholders stand for math.

    translate_batch returns one result per input, or None for an input that
    could not be translated. `context` holds the text just before and after the
    batch, which engines may use to understand it but must not translate.
    """

    name = ""
    label = ""
    requires_key = False
    max_batch_items = 1
    max_batch_chars = 4500
    # Requests in flight at once. The free Google endpoint and local models
    # gain nothing from parallel requests.
    max_concurrency = 1
    # Google Translate style engines make known mistakes in English math prose
    apply_term_fixes = False
    # Engines whose failures are worth a second pass. An LLM that mangled a
    # line often gets it right when asked again.
    retries_failures = False

    def __init__(self, source: str, target: str, *, api_key: str | None = None,
                 region: str | None = None, model: str | None = None,
                 host: str | None = None, ambient_credentials: bool = False):
        if self.requires_key and not api_key:
            raise EngineError(f"{self.label} needs an API key.")
        self.source = source
        self.target = target
        self.api_key = api_key
        self.region = region
        self.model = model
        self.host = host

    @property
    def cache_id(self) -> str:
        return self.name

    def translate_batch(self, texts: list[str],
                        context: tuple[str, str] = ("", "")) -> list[str | None]:
        raise NotImplementedError


def placeholders(text: str) -> list[str]:
    return sorted(PLACEHOLDER_RE.findall(text))


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


def post_json(label: str, url: str, *, headers: dict, json, params: dict | None = None,
              timeout: float = 120, error_messages: dict[int, str] | None = None):
    """POST a JSON request and map HTTP failures to engine errors.

    Server errors and connection failures raise their requests exception, which
    the caller treats as transient.
    """
    response = requests.post(url, headers=headers, json=json, params=params, timeout=timeout)
    if error_messages and response.status_code in error_messages:
        raise EngineError(error_messages[response.status_code])
    if response.status_code == 429:
        raise RateLimitedError(retry_after_seconds(response.headers))
    if response.status_code in (401, 403):
        raise EngineError(f"{label} rejected the API key.")
    if 400 <= response.status_code < 500:
        raise EngineError(f"{label} rejected the request ({response.status_code}): "
                          f"{response.text[:200]}")
    response.raise_for_status()
    return response.json()


def retry_after_seconds(headers) -> float | None:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
