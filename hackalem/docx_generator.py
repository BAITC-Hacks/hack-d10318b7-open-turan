"""
docx_generator.py
Кеңестің хаттамасын (.docx) дайын форматта жасайтын модуль.
"""

import io
from datetime import datetime

from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn


def _set_cell_shading(cell, color_hex: str = "D9E2F3"):
    """Кестенің ұяшығына фон түсін қосады (тақырып жолын бөлектеу үшін)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.makeelement(
        qn("w:shd"),
        {
            qn("w:val"): "clear",
            qn("w:color"): "auto",
            qn("w:fill"): color_hex,
        },
    )
    tc_pr.append(shd)


def generate_protocol_docx(
    meeting_title: str,
    meeting_date: str,
    summary: str,
    speakers: list,
    tasks: list,
    transcript_text: str = None,
) -> io.BytesIO:
    """
    Кеңестің хаттамасын .docx файл ретінде жасайды және BytesIO буферін қайтарады.

    :param meeting_title: Кеңестің атауы
    :param meeting_date: Кеңес өткен күн (жол)
    :param summary: Қысқаша қорытынды (саммари)
    :param speakers: Спикерлер тізімі (list[str])
    :param tasks: Тапсырмалар тізімі
                  (list[dict], әр dict: task, responsible, deadline)
    :param transcript_text: Толық транскрипт (міндетті емес, қосымша ретінде кіреді)
    :return: io.BytesIO — дайын .docx файлдың байттары
    """
    doc = Document()

    # Негізгі стиль
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    # Атауы
    title = doc.add_heading(meeting_title or "Кеңестің хаттамасы", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Күні
    date_p = doc.add_paragraph()
    date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    date_run = date_p.add_run(
        f"Күні: {meeting_date or datetime.now().strftime('%Y-%m-%d')}"
    )
    date_run.italic = True

    # Қатысушылар
    doc.add_heading("Қатысушылар (спикерлер)", level=1)
    if speakers:
        for sp in speakers:
            doc.add_paragraph(str(sp), style="List Bullet")
    else:
        doc.add_paragraph("Анықталмады")

    # Саммари
    doc.add_heading("Қысқаша қорытынды (Саммари)", level=1)
    doc.add_paragraph(summary or "—")

    # Тапсырмалар кестесі
    doc.add_heading("Тапсырмалар кестесі", level=1)

    if tasks:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        headers = ["№", "Тапсырма", "Жауапты (Ответственный)", "Мерзімі (Срок)"]
        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            hdr_cells[i].text = h
            for p in hdr_cells[i].paragraphs:
                for r in p.runs:
                    r.bold = True
            _set_cell_shading(hdr_cells[i])

        col_widths = [Cm(1.2), Cm(8), Cm(4), Cm(3)]
        for row in table.rows:
            for idx, cell in enumerate(row.cells):
                cell.width = col_widths[idx]

        for i, task in enumerate(tasks, start=1):
            row_cells = table.add_row().cells
            row_cells[0].text = str(i)
            row_cells[1].text = str(task.get("task", ""))
            row_cells[2].text = str(task.get("responsible", "Анықталмады"))
            row_cells[3].text = str(task.get("deadline", "Анықталмады"))
            for cell in row_cells:
                cell.width = col_widths[row_cells.index(cell)]
    else:
        doc.add_paragraph("Тапсырмалар табылмады.")

    # Толық транскриптті қосымша ретінде қосу (міндетті емес)
    if transcript_text:
        doc.add_page_break()
        doc.add_heading("Толық транскрипт (қосымша)", level=1)
        p = doc.add_paragraph(transcript_text)
        for run in p.runs:
            run.font.size = Pt(10)

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer
