from types import SimpleNamespace

import pytest

from translator.engines import ENGINES, EngineError, RateLimitedError, create_engine
from translator.engines import base
from translator.engines.llm import ClaudeEngine, LLMEngine
from translator.engines.mt import (
    DeepLEngine, GoogleFreeEngine, from_html, from_xml, to_html, to_xml,
)

TEMPLATE = "Soit {M0} & {M1} < 3, alors {M12}."


def test_xml_placeholders_round_trip():
    assert to_xml(TEMPLATE) == 'Soit <m i="0"/> &amp; <m i="1"/> &lt; 3, alors <m i="12"/>.'
    assert from_xml(to_xml(TEMPLATE)) == TEMPLATE


@pytest.mark.parametrize("attribute", ['class="notranslate"', 'translate="no"'])
def test_html_placeholders_round_trip(attribute):
    assert from_html(to_html(TEMPLATE, attribute)) == TEMPLATE


def test_google_markers_round_trip():
    marked = GoogleFreeEngine._to_markers("Soit{M0}une fonction")
    assert marked == "Soit XXXM0XXX une fonction"
    assert GoogleFreeEngine._from_markers("Let xxxm0xxx be a function") == "Let {M0} be a function"


@pytest.mark.parametrize("name", ["deepl", "azure", "google-cloud", "gemini", "claude"])
def test_engines_that_need_keys_reject_missing_keys(name):
    with pytest.raises(EngineError, match="API key"):
        create_engine(name, "fr", "en")


def test_unknown_engine():
    with pytest.raises(EngineError, match="Unknown"):
        create_engine("babelfish", "fr", "en")


def test_every_engine_is_registered_under_its_name():
    assert all(engine.name == name for name, engine in ENGINES.items())


class FakeResponse:
    def __init__(self, status_code, data=None, headers=None, text=""):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 500:
            raise ConnectionError(f"server error {self.status_code}")


@pytest.fixture
def http(monkeypatch):
    """Replace requests.post and record the requests made."""
    calls = []

    def respond_with(response):
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return response
        monkeypatch.setattr(base.requests, "post", post)
        return calls

    return respond_with


def test_deepl_request_uses_free_endpoint_and_restores_placeholders(http):
    calls = http(FakeResponse(200, {"translations": [{"text": 'Let <m i="0"/> be &amp; so'}]}))
    engine = create_engine("deepl", "fr", "en", api_key="secret:fx")

    assert engine.translate_batch(["Soit {M0} & donc"]) == ["Let {M0} be & so"]
    url, request = calls[0]
    assert url == "https://api-free.deepl.com/v2/translate"
    assert request["json"]["target_lang"] == "EN-US"
    assert request["json"]["tag_handling"] == "xml"
    assert request["headers"]["Authorization"] == "DeepL-Auth-Key secret:fx"


@pytest.mark.parametrize("response, error, message", [
    (FakeResponse(429, headers={"retry-after": "7"}), RateLimitedError, None),
    (FakeResponse(403), EngineError, "rejected the API key"),
    (FakeResponse(456), EngineError, "quota"),
    (FakeResponse(400, text="bad target_lang"), EngineError, "bad target_lang"),
])
def test_http_errors_map_to_engine_errors(http, response, error, message):
    http(response)
    engine = DeepLEngine("fr", "en", api_key="secret")

    with pytest.raises(error) as raised:
        engine.translate_batch(["Bonjour"])

    if message:
        assert message in str(raised.value)
    else:
        assert raised.value.retry_after == 7


class ScriptedLLM(LLMEngine):
    name = "scripted"
    label = "Scripted"
    default_model = "test"

    def __init__(self, responses):
        super().__init__("fr", "en")
        self.responses = list(responses)
        self.requests = []

    def complete(self, texts):
        self.requests.append(texts)
        return self.responses.pop(0)


def test_llm_retries_items_with_broken_placeholders_individually():
    engine = ScriptedLLM([
        ["Let {M0} be", "dropped the formula", "and {M1} {M2}"],
        ["then {M1} holds"],
    ])

    result = engine.translate_batch(["Soit {M0}", "alors {M1} vaut", "et {M1} {M2}"])

    assert result == ["Let {M0} be", "then {M1} holds", "and {M1} {M2}"]
    assert engine.requests[1] == ["alors {M1} vaut"]


def test_llm_gives_up_on_items_that_stay_broken():
    engine = ScriptedLLM([["missing", "ok"], ["still missing"]])

    assert engine.translate_batch(["a {M0}", "ok"]) == [None, "ok"]


def test_llm_wrong_item_count_retries_each_item():
    engine = ScriptedLLM([["only one"], ["first"], ["second"]])

    assert engine.translate_batch(["un", "deux"]) == ["first", "second"]


def test_llm_instructions_name_the_languages():
    instructions = ScriptedLLM([]).instructions()
    assert "French mathematics paper into English" in instructions
    assert "{M0}" in instructions


def claude_response(stop_reason, text):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)])


@pytest.mark.parametrize("stop_reason, text, expected", [
    ("end_turn", '{"translations": ["Let {M0} be"]}', ["Let {M0} be"]),
    ("refusal", "", None),
    ("max_tokens", '{"translations": ["Let', None),
])
def test_claude_reads_structured_output(stop_reason, text, expected):
    engine = ClaudeEngine("fr", "en", api_key="secret")
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return claude_response(stop_reason, text)

    engine._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert engine.complete(["Soit {M0}"]) == expected
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default"
    assert sent["output_config"]["format"]["type"] == "json_schema"
