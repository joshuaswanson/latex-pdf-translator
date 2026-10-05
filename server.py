"""FastAPI server for translating PDFs via the web UI.

Keyed engines translate on the server through /translate. For the free Google
engine the visitor's browser translates, because Google blocks requests from
the server's cloud IP addresses: /extract returns the text segments, the
browser translates them, and /render turns the translations into the PDF.
"""

import os
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from time import time

import pymupdf
from fastapi import FastAPI, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from translator.engines import LOCAL_ENGINES, EngineError, create_engine
from translator.extract import TranslatableLine
from translator.pipeline import (
    NoTranslatableTextError, extract_pdf_lines, render_pdf, translate_pdf,
)
from translator.translate import Segments, apply_translations, segment_lines, uses_term_fixes

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

# Rendering takes about 0.8 MB of memory per page. The defaults keep two
# concurrent jobs inside the free server's 512 MB; a larger server can raise
# them through environment variables.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_JOBS", 2))
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 25))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_PAGES = int(os.environ.get("MAX_PAGES", 150))
JOB_TTL_SECONDS = 600
# Long documents take the browser several minutes to translate
AWAITING_TRANSLATION_TTL_SECONDS = 3600
MAX_SEGMENT_TRANSLATION_CHARS = 20000

STAGE_LABELS = {
    "extract": "Extracting text",
    "translate": "Translating",
    "render": "Rendering",
}


@dataclass
class Job:
    status: str = "processing"
    stage: str = "Starting..."
    progress: int = 0
    total: int = 0
    result: bytes | None = None
    error: str | None = None
    finished: float | None = None
    filename: str = ""
    # Kept between /extract and /render for browser translation
    source: str = ""
    target: str = ""
    pdf_bytes: bytes | None = None
    lines: list[TranslatableLine] = field(default_factory=list)
    segments: Segments | None = None
    awaiting_since: float | None = None

    def report(self, stage: str, completed: int, total: int):
        label = STAGE_LABELS[stage]
        self.progress = completed
        self.total = total
        self.stage = f"{label}... ({completed}/{total})" if total else f"{label}..."


class RenderRequest(BaseModel):
    translations: list[str | None]


jobs: dict[str, Job] = {}
active_count = 0
lock = threading.Lock()


def _cleanup_old_jobs():
    """Remove finished jobs and jobs whose browser translation never arrived."""
    now = time()
    expired = [
        jid for jid, j in jobs.items()
        if (j.finished is not None and now - j.finished > JOB_TTL_SECONDS)
        or (j.awaiting_since is not None and now - j.awaiting_since > AWAITING_TRANSLATION_TTL_SECONDS)
    ]
    for jid in expired:
        del jobs[jid]


def _start(job: Job, work: Callable[[Job], None]):
    """Run `work` in a background thread, holding one of the concurrency slots."""
    global active_count
    with lock:
        if active_count >= MAX_CONCURRENT:
            raise HTTPException(503, "Server is busy. Please try again in a minute.")
        active_count += 1
    job.status = "processing"
    threading.Thread(target=_run, args=(job, work), daemon=True).start()


def _run(job: Job, work: Callable[[Job], None]):
    global active_count
    try:
        work(job)
    except (NoTranslatableTextError, EngineError) as e:
        _fail(job, str(e))
    except Exception:
        # Log the real error server-side, but don't expose internals to clients.
        traceback.print_exc()
        _fail(job, "Translation failed. Please try again.")
    finally:
        with lock:
            active_count -= 1


def _fail(job: Job, message: str):
    job.status = "error"
    job.error = message
    job.finished = time()
    job.pdf_bytes = None
    job.lines = []


async def _read_pdf(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")
    pdf_bytes = await file.read(MAX_FILE_SIZE + 1)
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File too large. Maximum size is {MAX_FILE_SIZE_MB} MB.")
    try:
        with pymupdf.open("pdf", pdf_bytes) as doc:
            page_count = len(doc)
    except Exception:
        raise HTTPException(400, "This file could not be read as a PDF.")
    if page_count > MAX_PAGES:
        raise HTTPException(400, f"PDFs over {MAX_PAGES} pages are too large for this server. "
                                 "Please use the command-line tool.")
    return pdf_bytes


def _new_job(filename: str, **fields) -> tuple[str, Job]:
    _cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    job = Job(filename=filename, **fields)
    jobs[job_id] = job
    return job_id, job


def _get_job(job_id: str) -> Job:
    if job_id not in jobs:
        raise HTTPException(404, "Job not found.")
    return jobs[job_id]


@app.post("/translate")
async def start_translation(file: UploadFile, source: str = "fr", target: str = "en",
                            engine: str = Form("google"), api_key: str = Form(""),
                            region: str = Form(""), model: str = Form("")):
    """Start a job that translates on the server.

    Credentials arrive as form fields, are used only for this job, and are
    never stored or logged.
    """
    if engine in LOCAL_ENGINES:
        raise HTTPException(400, f"The {engine} engine runs only in the command-line tool.")
    try:
        translation_engine = create_engine(engine, source, target, api_key=api_key or None,
                                           region=region or None, model=model or None)
    except EngineError as e:
        raise HTTPException(400, str(e))
    pdf_bytes = await _read_pdf(file)
    job_id, job = _new_job(file.filename)

    def work(job: Job):
        job.result = translate_pdf(pdf_bytes, translation_engine, on_progress=job.report)
        job.status = "done"
        job.finished = time()

    _start(job, work)
    return {"job_id": job_id}


@app.post("/extract")
async def start_extraction(file: UploadFile, source: str = "fr", target: str = "en"):
    """Start a job whose text segments the browser will translate."""
    pdf_bytes = await _read_pdf(file)
    job_id, job = _new_job(file.filename, source=source, target=target, pdf_bytes=pdf_bytes)

    def work(job: Job):
        job.report("extract", 0, 0)
        job.lines = extract_pdf_lines(pdf_bytes)
        job.segments = segment_lines(job.lines)
        job.status = "awaiting_translation"
        job.awaiting_since = time()

    _start(job, work)
    return {"job_id": job_id}


@app.get("/segments/{job_id}")
async def get_segments(job_id: str):
    job = _get_job(job_id)
    if job.status != "awaiting_translation":
        raise HTTPException(400, "This job has no segments to translate.")
    return {"source": job.source, "target": job.target, "segments": job.segments.texts}


@app.post("/render/{job_id}")
async def start_render(job_id: str, request: RenderRequest):
    """Render the browser's segment translations, with null for untranslated segments."""
    job = _get_job(job_id)
    if job.status != "awaiting_translation":
        raise HTTPException(400, "This job is not waiting for translations.")
    if len(request.translations) != len(job.segments.texts):
        raise HTTPException(400, "Expected one translation per segment.")
    if any(t is not None and len(t) > MAX_SEGMENT_TRANSLATION_CHARS for t in request.translations):
        raise HTTPException(400, "A translation is too long.")

    def work(job: Job):
        translations = apply_translations(job.lines, job.segments, request.translations,
                                          uses_term_fixes(True, job.target))
        job.result = render_pdf(job.pdf_bytes, job.lines, translations, on_progress=job.report)
        job.status = "done"
        job.finished = time()
        job.pdf_bytes = None
        job.lines = []

    _start(job, work)
    job.awaiting_since = None
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = _get_job(job_id)
    resp = {"status": job.status, "stage": job.stage}
    if job.total > 0:
        resp["progress"] = job.progress
        resp["total"] = job.total
    if job.error:
        resp["error"] = job.error
    return resp


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    job = _get_job(job_id)
    if job.status != "done" or job.result is None:
        raise HTTPException(400, "Translation not ready yet.")

    out_name = job.filename.rsplit(".", 1)[0] + "-translated.pdf"
    return Response(
        content=job.result,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )
