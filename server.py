"""FastAPI server for translating PDFs via the web UI."""

import threading
import traceback
import uuid
from dataclasses import dataclass
from time import time

from fastapi import FastAPI, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from translator.engines import LOCAL_ENGINES, Engine, EngineError, create_engine
from translator.pipeline import NoTranslatableTextError, translate_pdf

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
JOB_TTL_SECONDS = 600

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


jobs: dict[str, Job] = {}
active_count = 0
lock = threading.Lock()


def _cleanup_old_jobs():
    """Remove jobs that finished more than JOB_TTL_SECONDS ago."""
    now = time()
    expired = [jid for jid, j in jobs.items()
               if j.finished is not None and now - j.finished > JOB_TTL_SECONDS]
    for jid in expired:
        del jobs[jid]


def _run_pipeline(job_id: str, pdf_bytes: bytes, engine: Engine):
    global active_count
    job = jobs[job_id]

    def on_progress(stage, completed, total):
        label = STAGE_LABELS[stage]
        job.progress = completed
        job.total = total
        job.stage = f"{label}... ({completed}/{total})" if total else f"{label}..."

    try:
        job.result = translate_pdf(pdf_bytes, engine, on_progress=on_progress)
        job.status = "done"
    except (NoTranslatableTextError, EngineError) as e:
        job.status = "error"
        job.error = str(e)
    except Exception:
        # Log the real error server-side, but don't expose internals to clients.
        traceback.print_exc()
        job.status = "error"
        job.error = "Translation failed. Please try again."
    finally:
        job.finished = time()
        with lock:
            active_count -= 1


@app.post("/translate")
async def start_translation(file: UploadFile, source: str = "fr", target: str = "en",
                            engine: str = Form("google"), api_key: str = Form(""),
                            region: str = Form(""), model: str = Form("")):
    """Start a translation job.

    Credentials arrive as form fields, are used only for this job, and are
    never stored or logged.
    """
    global active_count

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")
    if engine in LOCAL_ENGINES:
        raise HTTPException(400, f"The {engine} engine runs only in the command-line tool.")
    try:
        translation_engine = create_engine(engine, source, target, api_key=api_key or None,
                                           region=region or None, model=model or None)
    except EngineError as e:
        raise HTTPException(400, str(e))

    pdf_bytes = await file.read(MAX_FILE_SIZE + 1)

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
                              args=(job_id, pdf_bytes, translation_engine),
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
