import pytest
from fastapi.testclient import TestClient

from conftest import fixture_pdf_bytes
from server import app

client = TestClient(app)


def upload(**form):
    files = {"file": ("paper.pdf", fixture_pdf_bytes("cmsuper"), "application/pdf")}
    return client.post("/translate?source=fr&target=en", files=files, data=form)


@pytest.mark.parametrize("engine", ["ollama", "apple"])
def test_local_engines_are_rejected(engine):
    response = upload(engine=engine)
    assert response.status_code == 400
    assert "command-line" in response.json()["detail"]


def test_engines_that_need_keys_reject_missing_keys():
    response = upload(engine="deepl")
    assert response.status_code == 400
    assert response.json()["detail"] == "DeepL needs an API key."


def test_claude_ignores_server_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
    response = upload(engine="claude")
    assert response.status_code == 400
    assert "API key" in response.json()["detail"]


def test_unknown_engine_is_rejected():
    response = upload(engine="babelfish")
    assert response.status_code == 400
