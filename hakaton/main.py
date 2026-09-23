"""Meeting recorder backend with transcription, summaries, and DOCX export."""

import json
import os
import tempfile
import uuid
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Optional

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Meeting Notes", version="2.0")

default_origins = (
    "http://localhost:3000,http://127.0.0.1:3000,"
    "http://localhost:5173,http://127.0.0.1:5173"
)
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", default_origins).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = {
    ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"
}
MAX_FILE_SIZE_MB = 25
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gpt-transcribe")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """Ты готовишь протокол совещания по расшифровке аудиозаписи.
Верни ответ строго как JSON без markdown и пояснений:

{
  "title": "краткое содержательное название совещания",
  "summary": "краткое резюме совещания в 2-4 предложениях",
  "key_points": ["важный обсуждённый вопрос"],
  "decisions": ["принятое решение"],
  "action_items": ["конкретное действие, исполнитель и срок, если они названы"],
  "raw_cleaned": "полная расшифровка без слов-паразитов и повторов"
}

Правила:
- Не придумывай участников, решения, исполнителей или сроки.
- Если раздел не упоминался, верни для него пустой массив.
- Сохраняй язык совещания.
- Не сокращай raw_cleaned до резюме: сохрани содержание разговора.
- Если запись неразборчива или пуста, используй title "Не удалось распознать".
"""


class TaskStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    ERROR = "error"


class Task(BaseModel):
    status: TaskStatus
    result: Optional[dict] = None
    error: Optional[str] = None


tasks: dict[str, Task] = {}


def create_api_client() -> OpenAI:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        raise RuntimeError("Не задан ключ OPENAI_API_KEY. Проверь файл .env.")
    return OpenAI(api_key=openai_key)


def parse_llm_json(value: str) -> dict:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Модель вернула JSON неправильного формата")
    return parsed


def normalize_result(data: dict, raw_text: str) -> dict:
    normalized = {
        "title": str(data.get("title") or "Протокол совещания").strip(),
        "summary": str(data.get("summary") or "").strip(),
        "key_points": data.get("key_points") or [],
        "decisions": data.get("decisions") or [],
        "action_items": data.get("action_items") or [],
        "raw_cleaned": str(data.get("raw_cleaned") or raw_text).strip(),
    }
    for field in ("key_points", "decisions", "action_items"):
        value = normalized[field]
        if not isinstance(value, list):
            value = [value]
        normalized[field] = [str(item).strip() for item in value if str(item).strip()]
    return normalized


def process_audio(task_id: str, tmp_path: str) -> None:
    try:
        openai_client = create_api_client()
        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model=TRANSCRIPTION_MODEL,
                file=audio_file,
            )
        raw_text = transcript.text.strip()
        if not raw_text:
            raise ValueError("В аудиозаписи не удалось обнаружить речь")

        completion = openai_client.chat.completions.create(
            model=SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "meeting_minutes",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "key_points": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "decisions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "action_items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "raw_cleaned": {"type": "string"},
                        },
                        "required": [
                            "title",
                            "summary",
                            "key_points",
                            "decisions",
                            "action_items",
                            "raw_cleaned",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        )
        choice = completion.choices[0]
        if choice.message.refusal:
            raise ValueError(f"Модель отказалась подготовить протокол: {choice.message.refusal}")
        llm_output = choice.message.content
        if not llm_output:
            raise ValueError("Модель не вернула протокол")
        structured = normalize_result(parse_llm_json(llm_output), raw_text)
        tasks[task_id] = Task(status=TaskStatus.DONE, result=structured)
    except Exception as exc:
        tasks[task_id] = Task(status=TaskStatus.ERROR, error=str(exc))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def set_cell_fill(cell, color: str) -> None:
    cell_properties = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), color)
    cell_properties.append(shading)


def set_cell_borders(cell, color: str = "D9D9D9") -> None:
    cell_properties = cell._tc.get_or_add_tcPr()
    borders = cell_properties.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        cell_properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = "w:" + edge
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:color"), color)


def configure_text_style(style, font_name: str, size: int) -> None:
    style.font.name = font_name
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor(0, 0, 0)
    run_properties = style.element.get_or_add_rPr()
    fonts = run_properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run_properties.insert(0, fonts)
    fonts.set(qn("w:ascii"), font_name)
    fonts.set(qn("w:hAnsi"), font_name)
    fonts.set(qn("w:eastAsia"), font_name)
    for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn("w:" + attribute), None)
    color = run_properties.find(qn("w:color"))
    if color is None:
        color = OxmlElement("w:color")
        run_properties.append(color)
    color.set(qn("w:val"), "000000")
    for attribute in ("themeColor", "themeTint", "themeShade"):
        color.attrib.pop(qn("w:" + attribute), None)


def format_run(run, size: int, color: str = "000000", bold: Optional[bool] = None) -> None:
    run.font.name = "Arial"
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        run.font.bold = bold
    run_properties = run._element.get_or_add_rPr()
    fonts = run_properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run_properties.insert(0, fonts)
    for attribute in ("ascii", "hAnsi", "eastAsia"):
        fonts.set(qn("w:" + attribute), "Arial")
    for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn("w:" + attribute), None)
    color_element = run_properties.find(qn("w:color"))
    if color_element is None:
        color_element = OxmlElement("w:color")
        run_properties.append(color_element)
    color_element.set(qn("w:val"), color)
    for attribute in ("themeColor", "themeTint", "themeShade"):
        color_element.attrib.pop(qn("w:" + attribute), None)


def build_meeting_document(result: dict) -> BytesIO:
    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.8)
    section.bottom_margin = Inches(0.8)
    section.left_margin = Inches(0.85)
    section.right_margin = Inches(0.85)

    styles = document.styles
    normal = styles["Normal"]
    configure_text_style(normal, "Arial", 11)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.12
    for style_name, size in (("Title", 24), ("Heading 1", 16), ("Heading 2", 13)):
        style = styles[style_name]
        configure_text_style(style, "Arial", size)
    title_style_properties = styles["Title"].element.pPr
    if title_style_properties is not None:
        title_border = title_style_properties.find(qn("w:pBdr"))
        if title_border is not None:
            title_style_properties.remove(title_border)

    title = str(result.get("title") or "Протокол совещания")
    title_paragraph = document.add_paragraph(title, style="Title")
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    intro = document.add_paragraph(
        "Документ содержит краткое резюме, основные вопросы, решения, задачи и очищенную расшифровку совещания."
    )
    intro.paragraph_format.space_after = Pt(16)

    document.add_heading("Резюме", level=1)
    document.add_paragraph(result.get("summary") or "Резюме не сформировано.")

    def add_list_section(heading: str, items: list[str], empty_text: str) -> None:
        document.add_heading(heading, level=1)
        if items:
            for item in items:
                document.add_paragraph(str(item), style="List Bullet")
        else:
            document.add_paragraph(empty_text)

    add_list_section("Основные вопросы", result.get("key_points") or [], "Основные вопросы не выделены.")
    add_list_section("Принятые решения", result.get("decisions") or [], "Явные решения не зафиксированы.")

    document.add_heading("Задачи", level=1)
    action_items = result.get("action_items") or []
    if action_items:
        table = document.add_table(rows=1, cols=2)
        table.autofit = False
        table.columns[0].width = Inches(0.6)
        table.columns[1].width = Inches(6.0)
        headers = table.rows[0].cells
        headers[0].text = "№"
        headers[1].text = "Действие"
        for cell in headers:
            set_cell_fill(cell, "1F4E78")
            set_cell_borders(cell)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for run in cell.paragraphs[0].runs:
                run.font.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)
        for index, item in enumerate(action_items, start=1):
            cells = table.add_row().cells
            cells[0].text = str(index)
            cells[1].text = str(item)
            cells[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            for cell in cells:
                set_cell_borders(cell)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if index % 2 == 0:
                for cell in cells:
                    set_cell_fill(cell, "F2F6FA")
    else:
        document.add_paragraph("Конкретные задачи не зафиксированы.")

    document.add_heading("Расшифровка", level=1)
    document.add_paragraph(str(result.get("raw_cleaned") or "Расшифровка отсутствует."))

    for paragraph in document.paragraphs:
        if paragraph.style.name == "Title":
            size, bold = 24, False
        elif paragraph.style.name.startswith("Heading"):
            size, bold = 16, True
        else:
            size, bold = 11, None
        for run in paragraph.runs:
            format_run(run, size=size, color="000000", bold=bold)
    for table in document.tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        format_run(
                            run,
                            size=10,
                            color="FFFFFF" if row_index == 0 else "000000",
                            bold=True if row_index == 0 else None,
                        )

    document.core_properties.title = title
    document.core_properties.subject = "Протокол совещания"
    document.core_properties.author = "Meeting Notes"

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/transcribe", status_code=202)
async def transcribe(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    filename = file.filename or "meeting.webm"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Неподдерживаемый формат {suffix or '(без расширения)'}. Допустимы: {allowed}",
        )

    tmp_path: Optional[str] = None
    total_size = 0
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(status_code=400, detail=f"Файл больше {MAX_FILE_SIZE_MB} МБ")
                tmp.write(chunk)
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if total_size == 0:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Загружен пустой файл")

    task_id = str(uuid.uuid4())
    tasks[task_id] = Task(status=TaskStatus.PENDING)
    background_tasks.add_task(process_audio, task_id, tmp_path)
    return {"task_id": task_id}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "transcription_model": TRANSCRIPTION_MODEL,
        "summary_model": SUMMARY_MODEL,
    }


@app.get("/transcribe/{task_id}")
async def get_transcription(task_id: str):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Задача не найдена")
    return task


@app.get("/transcribe/{task_id}/document")
async def download_document(task_id: str):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Задача не найдена")
    if task.status != TaskStatus.DONE or not task.result:
        raise HTTPException(409, "Документ ещё не готов")

    document = build_meeting_document(task.result)
    filename = f"meeting_notes_{task_id[:8]}.docx"
    return StreamingResponse(
        document,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
