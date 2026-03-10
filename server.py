"""FastAPI server for translating PDFs via the web UI."""

import threading
import uuid
from dataclasses import dataclass, field
from time import time

import pymupdf
from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from translator.extract import extract_lines
from translator.translate import translate_lines
from translator.render import render_all, _fix_link_annotations

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://joshuaswanson.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_CONCURRENT = 2
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@dataclass
class Job:
    status: str = "processing"
    stage: str = "Starting..."
    progress: int = 0
    total: int = 0
    result: bytes | None = None
    error: str | None = None
    created: float = field(default_factory=time)
    filename: str = ""


jobs: dict[str, Job] = {}
active_count = 0
lock = threading.Lock()


def _cleanup_old_jobs():
    """Remove jobs older than 10 minutes."""
    now = time()
    expired = [jid for jid, j in jobs.items() if now - j.created > 600]
    for jid in expired:
        del jobs[jid]


def _run_pipeline(job_id: str, pdf_bytes: bytes, source: str, target: str):
    global active_count
    job = jobs[job_id]
    try:
        job.stage = "Extracting text..."
        orig_doc = pymupdf.open("pdf", pdf_bytes)
        work_doc = pymupdf.open("pdf", pdf_bytes)

        lines = extract_lines(orig_doc)
        if not lines:
            job.status = "error"
            job.error = "No translatable text found in this PDF."
            return

        job.stage = f"Translating {len(lines)} lines..."

        def on_progress(completed, total):
            job.progress = completed
            job.total = total
            job.stage = f"Translating... ({completed}/{total})"

        translations = translate_lines(lines, cache_path=None,
                                       source=source, target=target,
                                       progress_callback=on_progress)

        job.stage = "Rendering translated PDF..."
        annot_colors, rendered_extents, link_texts = render_all(
            work_doc, orig_doc, lines, translations
        )

        out_bytes = work_doc.tobytes(garbage=4, deflate=True)
        work_doc.close()
        orig_doc.close()

        doc = pymupdf.open("pdf", out_bytes)
        _fix_link_annotations(doc, annot_colors, rendered_extents, link_texts)
        result_bytes = doc.tobytes(garbage=4, deflate=True)
        doc.close()

        job.result = result_bytes
        job.status = "done"

    except Exception as e:
        job.status = "error"
        job.error = str(e)

    finally:
        with lock:
            active_count -= 1


@app.post("/translate")
async def translate_pdf(file: UploadFile, source: str = "fr", target: str = "en"):
    global active_count

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    pdf_bytes = await file.read()

    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(400, "File too large. Maximum size is 50 MB.")

    _cleanup_old_jobs()

    with lock:
        if active_count >= MAX_CONCURRENT:
            raise HTTPException(503, "Server is busy. Please try again in a minute.")
        active_count += 1

    job_id = str(uuid.uuid4())
    jobs[job_id] = Job(filename=file.filename)

    thread = threading.Thread(target=_run_pipeline,
                              args=(job_id, pdf_bytes, source, target),
                              daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")
    job = jobs[job_id]
    resp = {"status": job.status, "stage": job.stage}
    if job.total > 0:
        resp["progress"] = job.progress
        resp["total"] = job.total
    if job.error:
        resp["error"] = job.error
    return resp


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")
    job = jobs[job_id]
    if job.status != "done" or job.result is None:
        raise HTTPException(400, "Translation not ready yet.")

    out_name = job.filename.rsplit(".", 1)[0] + "-translated.pdf"
    return Response(
        content=job.result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
