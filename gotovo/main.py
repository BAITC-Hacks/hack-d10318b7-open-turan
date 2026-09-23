"""Локальный сервис записи встреч, расшифровки и проверки протокола."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from docx_generator import generate_protocol_docx
from utils import (
    MAX_TRANSCRIPT_CHARS,
    OLLAMA_MODEL,
    OLLAMA_URL,
    ProcessingError,
    UNKNOWN,
    clean_text,
    normalize_tasks,
    ollama_status,
    structure_meeting,
    transcribe_audio,
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Meeting Notes — локальный протокол",
    description="Аудио и транскрипт обрабатываются только локальными моделями.",
    version="3.0.0",
)

MAX_FILE_SIZE_BYTES = 25_000_000
UPLOAD_CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
MAX_TASKS_IN_MEMORY = 20
tasks: dict[str, dict] = {}


def make_room_for_task() -> None:
    while len(tasks) >= MAX_TASKS_IN_MEMORY:
        completed_id = next(
            (task_id for task_id, task in tasks.items() if task.get("status") != "pending"),
            None,
        )
        if completed_id is None:
            raise HTTPException(
                status_code=503,
                detail="Слишком много записей обрабатывается одновременно. Подождите и повторите.",
            )
        tasks.pop(completed_id, None)


class DocumentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task: str = Field(min_length=1, max_length=2000)
    responsible: str = Field(default=UNKNOWN, max_length=300)
    deadline: str = Field(default=UNKNOWN, max_length=300)
    evidence: str = Field(default="", max_length=2000)

    @field_validator("task", "responsible", "deadline", "evidence")
    @classmethod
    def remove_xml_controls(cls, value: str) -> str:
        return clean_text(value, "")


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    meeting_title: str = Field(min_length=1, max_length=200)
    meeting_date: str = Field(min_length=10, max_length=10)
    summary: str = Field(min_length=1, max_length=6000)
    decisions: list[str] = Field(default_factory=list, max_length=100)
    speakers: list[str] = Field(default_factory=list, max_length=100)
    tasks: list[DocumentTask] = Field(default_factory=list, max_length=200)
    transcript_text: str | None = Field(default=None, max_length=MAX_TRANSCRIPT_CHARS)

    @field_validator("meeting_title", "summary")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        return clean_text(value, "")

    @field_validator("meeting_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД.") from exc


def process_audio(
    task_id: str,
    tmp_path: str,
    meeting_title: str,
    meeting_date: str,
    language: str,
    output_language: str,
) -> None:
    try:
        transcript = transcribe_audio(tmp_path, language)
        analysis = structure_meeting(
            transcript.text,
            meeting_date,
            output_language=output_language,
        )
        analysis.update(
            meeting_title=meeting_title,
            meeting_date=meeting_date,
            transcript_text=transcript.text,
            transcript_segments=transcript.segments,
            quality_warning=transcript.warning,
            transcription_model=os.getenv("WHISPER_MODEL", "small"),
            summary_model=OLLAMA_MODEL,
        )
        tasks[task_id] = {"status": "done", "result": analysis}
    except ProcessingError as exc:
        tasks[task_id] = {"status": "error", "error": str(exc)}
    except Exception:
        tasks[task_id] = {
            "status": "error",
            "error": "Не удалось обработать запись. Проверьте локальные модели и попробуйте снова.",
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
async def health():
    local_llm = ollama_status()
    return {
        "status": "ok" if local_llm["online"] and local_llm["model_available"] else "setup_required",
        "privacy": "local_only",
        "faster_whisper_installed": importlib.util.find_spec("faster_whisper") is not None,
        "ollama_online": local_llm["online"],
        "ollama_model_available": local_llm["model_available"],
        "ollama_model": OLLAMA_MODEL,
        "ollama_url": OLLAMA_URL,
        "whisper_model": os.getenv("WHISPER_MODEL", "small"),
    }


@app.post("/transcribe", status_code=202)
async def transcribe(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    meeting_title: str = Form(default="Протокол совещания"),
    meeting_date: str = Form(default=date.today().isoformat()),
    language: Literal["auto", "ru", "kk"] = Form(default="auto"),
    output_language: Literal["ru", "kk"] = Form(default="kk"),
):
    filename = Path(file.filename or "meeting.webm").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        await file.close()
        raise HTTPException(
            status_code=400,
            detail="Формат не поддерживается. Используйте MP3, WAV, M4A, MP4, WebM, OGG или FLAC.",
        )
    try:
        clean_title = clean_text(meeting_title, "")
        if not clean_title or len(clean_title) > 200:
            raise ValueError("Название встречи должно содержать от 1 до 200 символов.")
        clean_date = date.fromisoformat(meeting_date).isoformat()
    except ValueError as exc:
        await file.close()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        make_room_for_task()
    except HTTPException:
        await file.close()
        raise
    tmp_path = None
    total_size = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(status_code=400, detail="Файл больше 25 МБ.")
                tmp.write(chunk)
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if not total_size:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Загружен пустой файл.")

    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "pending"}
    background_tasks.add_task(
        process_audio,
        task_id,
        tmp_path,
        clean_title,
        clean_date,
        language,
        output_language,
    )
    return {"task_id": task_id}


@app.get("/transcribe/{task_id}")
async def get_transcription(task_id: str):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Задача не найдена. Возможно, сервер перезапускался.")
    return task


@app.post("/transcribe/{task_id}/document")
async def download_document(task_id: str, request: DocumentRequest):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Запись не найдена. Обработайте файл ещё раз.")
    if task["status"] != "done" or not task.get("result"):
        raise HTTPException(status_code=409, detail="Сначала дождитесь расшифровки.")

    try:
        rows = normalize_tasks([item.model_dump() for item in request.tasks])
        document = generate_protocol_docx(
            meeting_title=request.meeting_title,
            meeting_date=request.meeting_date,
            summary=request.summary,
            decisions=request.decisions,
            speakers=request.speakers,
            tasks=rows,
            transcript_text=request.transcript_text,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось подготовить DOCX: {exc}") from exc

    filename = f"meeting_minutes_{request.meeting_date.replace('-', '')}.docx"
    return StreamingResponse(
        document,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

