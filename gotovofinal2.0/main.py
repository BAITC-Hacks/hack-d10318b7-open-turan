"""Local meeting transcription, review and Word export."""
from __future__ import annotations

import importlib.util
import logging
import os
import tempfile
import uuid
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from docx_generator import generate_protocol_docx
from utils import (MAX_TRANSCRIPT_CHARS, OLLAMA_MODEL, OLLAMA_URL, ProcessingError,
                   UNKNOWN, clean_text, normalize_tasks, ollama_status,
                   structure_meeting, transcribe_audio)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("meeting_notes")
app = FastAPI(title="Meeting Notes", version="4.0.0",
              description="Локальная расшифровка встреч и редактирование протоколов.")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
MAX_FILE_SIZE_BYTES = 25_000_000
UPLOAD_CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
MAX_TASKS_IN_MEMORY = 20
MAX_ACTIVE_TASKS = 2
tasks: dict[str, dict] = {}
task_lock = Lock()
audio_lock = Lock()


def make_room_for_task() -> None:
    """Call under task_lock, immediately before reserving a task."""
    if sum(task.get("status") == "pending" for task in tasks.values()) >= MAX_ACTIVE_TASKS:
        raise HTTPException(503, "Уже обрабатываются две встречи. Дождитесь завершения.")
    while len(tasks) >= MAX_TASKS_IN_MEMORY:
        completed_id = next((key for key, value in tasks.items()
                             if value.get("status") != "pending"), None)
        if completed_id is None:
            raise HTTPException(503, "Все места заняты. Подождите завершения обработки.")
        tasks.pop(completed_id)


def reserve_task() -> str:
    with task_lock:
        make_room_for_task()
        task_id = str(uuid.uuid4())
        tasks[task_id] = {"status": "pending", "stage": "queued"}
        return task_id


def update_task(task_id: str, **value) -> None:
    with task_lock:
        tasks[task_id] = value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MeetingMetadata(StrictModel):
    meeting_title: str = Field(min_length=1, max_length=200)
    meeting_date: str = Field(min_length=10, max_length=10)
    output_language: Literal["ru", "kk"] = "kk"

    @field_validator("meeting_title")
    @classmethod
    def required_text(cls, value: str) -> str:
        text = clean_text(value, "")
        if not text:
            raise ValueError("Заполните название встречи.")
        return text

    @field_validator("meeting_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError("Укажите существующую дату в формате ГГГГ-ММ-ДД.") from exc


class DocumentTask(StrictModel):
    task: str = Field(min_length=1, max_length=2000)
    responsible: str = Field(default=UNKNOWN, max_length=300)
    deadline: str = Field(default=UNKNOWN, max_length=300)
    evidence: str = Field(default="", max_length=2000)

    @field_validator("task", "responsible", "deadline", "evidence")
    @classmethod
    def sanitize(cls, value: str) -> str:
        return clean_text(value, "")

    @field_validator("task")
    @classmethod
    def required_task(cls, value: str) -> str:
        if not value:
            raise ValueError("Заполните текст задачи.")
        return value


ShortText = Annotated[str, Field(max_length=2000)]


class DocumentRequest(MeetingMetadata):
    summary: str = Field(min_length=1, max_length=6000)
    decisions: list[ShortText] = Field(default_factory=list, max_length=100)
    speakers: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=100)
    tasks: list[DocumentTask] = Field(default_factory=list, max_length=200)
    transcript_text: str | None = Field(default=None, max_length=MAX_TRANSCRIPT_CHARS)

    @field_validator("summary")
    @classmethod
    def required_summary(cls, value: str) -> str:
        text = clean_text(value, "")
        if not text:
            raise ValueError("Заполните краткое резюме перед скачиванием.")
        return text


class TranscriptRequest(MeetingMetadata):
    transcript_text: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARS)

    @field_validator("transcript_text")
    @classmethod
    def required_transcript(cls, value: str) -> str:
        text = clean_text(value, "")
        if not text:
            raise ValueError("Вставьте текст встречи.")
        return text


def finish_transcript(task_id, text, segments, warning, title, meeting_date, output_language):
    update_task(task_id, status="pending", stage="analysis")
    try:
        analysis = structure_meeting(text, meeting_date, output_language=output_language)
    except ProcessingError as exc:
        # Preserve the transcript if the summary model or evidence check fails.
        analysis = {"status": "manual", "summary": "", "decisions": [], "speakers": [], "tasks": []}
        warning = " ".join(filter(None, [warning, str(exc),
                              "Расшифровка сохранена. Заполните протокол вручную по тексту ниже."]))
    analysis.update(meeting_title=title, meeting_date=meeting_date,
                    output_language=output_language, transcript_text=text,
                    transcript_segments=segments, quality_warning=warning,
                    transcription_model=os.getenv("WHISPER_MODEL", "small"), summary_model=OLLAMA_MODEL)
    update_task(task_id, status="done", result=analysis)


def process_audio(task_id, tmp_path, meeting_title, meeting_date, language, output_language):
    try:
        with audio_lock:
            update_task(task_id, status="pending", stage="transcription")
            transcript = transcribe_audio(tmp_path, language)
        finish_transcript(task_id, transcript.text, transcript.segments, transcript.warning,
                          meeting_title, meeting_date, output_language)
    except ProcessingError as exc:
        update_task(task_id, status="error", error=str(exc))
    except Exception:
        logger.exception("Audio processing failed")
        update_task(task_id, status="error", error="Не удалось обработать запись. Проверьте файл и повторите.")
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove temporary audio")


def process_text(task_id, request):
    try:
        finish_transcript(task_id, request.transcript_text, [], "", request.meeting_title,
                          request.meeting_date, request.output_language)
    except Exception:
        logger.exception("Text processing failed")
        update_task(task_id, status="error", error="Не удалось обработать текст. Повторите попытку.")


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    local_llm = ollama_status()
    whisper_installed = importlib.util.find_spec("faster_whisper") is not None
    summary_ready = local_llm["online"] and local_llm["model_available"]
    return {"status": "ok" if summary_ready and whisper_installed else "setup_required",
            "privacy": "local_only", "faster_whisper_installed": whisper_installed,
            "transcription_ready": whisper_installed, "summarization_ready": summary_ready,
            "ollama_online": local_llm["online"], "ollama_model_available": local_llm["model_available"],
            "ollama_model": OLLAMA_MODEL, "ollama_url": OLLAMA_URL,
            "whisper_model": os.getenv("WHISPER_MODEL", "small")}


@app.post("/transcript", status_code=202)
def submit_transcript(request: TranscriptRequest, background_tasks: BackgroundTasks):
    task_id = reserve_task()
    background_tasks.add_task(process_text, task_id, request)
    return {"task_id": task_id}


@app.post("/transcribe", status_code=202)
async def transcribe(background_tasks: BackgroundTasks, file: UploadFile = File(...),
                     meeting_title: str = Form(default="Протокол совещания"),
                     meeting_date: str | None = Form(default=None),
                     language: Literal["auto", "ru", "kk"] = Form(default="auto"),
                     output_language: Literal["ru", "kk"] = Form(default="kk")):
    tmp_path = None
    task_id = None
    try:
        suffix = Path(file.filename or "meeting.webm").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, "Используйте MP3, WAV, M4A, MP4, WebM, OGG или FLAC.")
        try:
            metadata = MeetingMetadata(meeting_title=meeting_title,
                                       meeting_date=meeting_date or date.today().isoformat(),
                                       output_language=output_language)
        except ValueError:
            raise HTTPException(400, "Проверьте название встречи (1–200 символов) и дату.")
        total_size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(400, "Файл больше 25 МБ. Разделите запись на части.")
                tmp.write(chunk)
        if not total_size:
            raise HTTPException(400, "Загружен пустой файл.")
        task_id = reserve_task()
        background_tasks.add_task(process_audio, task_id, tmp_path, metadata.meeting_title,
                                  metadata.meeting_date, language, output_language)
        return {"task_id": task_id}
    finally:
        await file.close()
        if tmp_path and task_id is None:
            Path(tmp_path).unlink(missing_ok=True)


@app.get("/transcribe/{task_id}")
def get_transcription(task_id: str):
    with task_lock:
        task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Запись больше недоступна: сервер перезапущен или обработано более 20 встреч.")
    return task


@app.post("/transcribe/{task_id}/document")
def download_document(task_id: str, request: DocumentRequest):
    task = get_transcription(task_id)
    if task["status"] != "done" or not task.get("result"):
        raise HTTPException(409, "Сначала дождитесь расшифровки.")
    try:
        document = generate_protocol_docx(
            meeting_title=request.meeting_title, meeting_date=request.meeting_date,
            summary=request.summary, decisions=request.decisions, speakers=request.speakers,
            tasks=normalize_tasks([item.model_dump() for item in request.tasks]),
            transcript_text=request.transcript_text, output_language=request.output_language)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Не удалось подготовить DOCX: {exc}") from exc
    filename = f"meeting_minutes_{request.meeting_date.replace('-', '')}.docx"
    return StreamingResponse(document,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

