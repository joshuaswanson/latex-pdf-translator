import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from conftest import FakeEngine, fixture_pdf_bytes
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


def wait_for(job_id, *statuses):
    for _ in range(200):
        status = client.get(f"/status/{job_id}").json()
        if status["status"] in statuses:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job stayed in {status}")


def test_browser_translation_flow():
    files = {"file": ("paper.pdf", fixture_pdf_bytes("cmsuper"), "application/pdf")}
    job_id = client.post("/extract?source=fr&target=en", files=files).json()["job_id"]
    wait_for(job_id, "awaiting_translation")

    segments = client.get(f"/segments/{job_id}").json()
    assert segments["source"] == "fr"
    assert segments["segments"][0] == "Fonctions continues"

    translations = FakeEngine().translate_batch(segments["segments"])
    translations[-1] = None
    response = client.post(f"/render/{job_id}", json={"translations": translations})
    assert response.status_code == 200
    assert wait_for(job_id, "done", "error")["status"] == "done"

    with pymupdf.open("pdf", client.get(f"/download/{job_id}").content) as doc:
        text = doc[0].get_text()
    assert "Continuous functions" in text
    assert "de cette section." in text


def test_render_rejects_wrong_number_of_translations():
    files = {"file": ("paper.pdf", fixture_pdf_bytes("cmsuper"), "application/pdf")}
    job_id = client.post("/extract?source=fr&target=en", files=files).json()["job_id"]
    wait_for(job_id, "awaiting_translation")

    response = client.post(f"/render/{job_id}", json={"translations": ["only one"]})

    assert response.status_code == 400


def test_rejects_pdfs_over_the_page_limit():
    with pymupdf.open() as doc:
        for _ in range(151):
            doc.new_page()
        long_pdf = doc.tobytes()
    response = client.post("/extract?source=fr&target=en",
                           files={"file": ("long.pdf", long_pdf, "application/pdf")})
    assert response.status_code == 400
    assert "150 pages" in response.json()["detail"]


def test_rejects_files_that_are_not_pdfs():
    response = client.post("/extract?source=fr&target=en",
                           files={"file": ("fake.pdf", b"not a pdf", "application/pdf")})
    assert response.status_code == 400
