"""Кеңес аудиосынан тексерілетін хаттама жобасын дайындау."""

import hashlib
import json
from datetime import date

import pandas as pd
import streamlit as st

from docx_generator import generate_protocol_docx
from utils import (
    MAX_AUDIO_BYTES, TASK_FIELDS, ProcessingError, clean_text,
    get_client, normalize_tasks, structure_meeting, transcribe_audio,
)

st.set_page_config(page_title="Кеңес хаттамасы", page_icon="🗂️", layout="wide")


def invalidate_docx():
    st.session_state.docx_buffer = None
    st.session_state.docx_signature = None


def reset_analysis():
    st.session_state.result = None
    st.session_state.transcript = ""
    st.session_state.quality_warning = ""
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
    for key in list(st.session_state):
        if key.startswith("tasks_editor_"):
            del st.session_state[key]
    invalidate_docx()


for key, value in {
    "result": None, "transcript": "", "quality_warning": "",
    "editor_version": 0, "docx_buffer": None,
    "docx_signature": None, "input_signature": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = value

st.title("Кеңестерді автохаттамалау және тапсырмаларды бекіту")
st.info("Демонстрациялық нұсқа: аудио мен мәтін OpenAI API-ге жіберіледі. Құпия жазбаларды жүктемеңіз.")

with st.sidebar:
    st.header("Баптаулар")
    api_key = st.text_input("OpenAI API кілті", type="password")
    languages = {"Автоматты анықтау / аралас": None, "Орысша": "ru", "Қазақша": "kk"}
    language = languages[st.selectbox("Аудио тілі", list(languages))]
    title = st.text_input("Кеңес атауы", "Кеңес хаттамасы", on_change=invalidate_docx)
    meeting_date = st.date_input("Кеңес күні", date.today())
    include_transcript = st.checkbox("Толық транскриптті қосу", True, on_change=invalidate_docx)
    st.caption("Акустикалық диаризация қосылмаған. Аттар тек мәтіндегі анық белгілерден алынады.")

uploaded = st.file_uploader(
    "Аудиофайл: MP3, WAV, M4A немесе MP4 (25 МБ-қа дейін)",
    type=["mp3", "wav", "m4a", "mp4"], key="audio_upload", on_change=reset_analysis,
)
audio_hash = hashlib.sha256(uploaded.getvalue()).hexdigest() if uploaded is not None else None
input_signature = (audio_hash, uploaded.name if uploaded is not None else None, language, str(meeting_date))
if input_signature != st.session_state.input_signature:
    reset_analysis()
    st.session_state.input_signature = input_signature

if st.button("Өңдеу", type="primary"):
    reset_analysis()
    try:
        if uploaded is None:
            raise ProcessingError("Алдымен аудиофайл жүктеңіз.")
        if uploaded.size > MAX_AUDIO_BYTES:
            raise ProcessingError("Файл 25 МБ-тан үлкен. Жазбаны бөліктерге бөліңіз.")
        with get_client(api_key) as client:
            with st.spinner("1/2 — Аудио мәтінге түрлендірілуде…"):
                transcript = transcribe_audio(client, uploaded, language)
            with st.spinner("2/2 — Қорытынды мен тапсырмалар алынуда…"):
                result = structure_meeting(client, transcript.text, str(meeting_date))
        if result["status"] == "error":
            raise ProcessingError(result["summary"])
        # Барлық кезең өткеннен кейін ғана нәтижені бірге сақтаймыз.
        st.session_state.update(
            result=result, transcript=transcript.text, quality_warning=transcript.warning,
        )
    except ProcessingError as exc:
        reset_analysis()
        st.error(str(exc))
    except Exception:
        reset_analysis()
        st.error("Өңдеу аяқталмады. Нәтиже сақталмады. Файл мен баптауларды тексеріңіз.")

result = st.session_state.result
if result is not None:
    if result["status"] == "unclear":
        st.warning(result["summary"])
    else:
        st.success("Өңдеу аяқталды. Хаттаманы бекітпес бұрын деректерді тексеріңіз.")
    if st.session_state.quality_warning:
        st.warning(st.session_state.quality_warning)
    with st.expander("Транскрипт"):
        st.text_area("Танылған мәтін", st.session_state.transcript, height=250, disabled=True)
    st.subheader("Қысқаша қорытынды")
    st.write(result["summary"])
    st.subheader("Мәтінде расталған сөйлеушілер")
    st.write(", ".join(result["speakers"]) or "Анықталмады")
    st.subheader("Тапсырмалар")
    st.caption("Жолдарды түзетуге, қосуға және өшіруге болады. Дәлел үзіндісі автоматты талдаудан алынған.")
    frame = pd.DataFrame(result["tasks"], columns=TASK_FIELDS).astype("string")
    edited = st.data_editor(
        frame, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"tasks_editor_{st.session_state.editor_version}",
        on_change=invalidate_docx, disabled=["evidence"],
        column_config={
            "task": st.column_config.TextColumn("Тапсырма", width="large"),
            "responsible": st.column_config.TextColumn("Жауапты тұлға"),
            "deadline": st.column_config.TextColumn("Мерзім"),
            "evidence": st.column_config.TextColumn("Бастапқы дәлел үзіндісі", width="large"),
        },
    )
    try:
        payload = {
            "meeting_title": clean_text(title, "Кеңес хаттамасы"),
            "meeting_date": str(meeting_date), "summary": result["summary"],
            "speakers": result["speakers"], "tasks": normalize_tasks(edited),
            "transcript_text": st.session_state.transcript if include_transcript else None,
        }
        signature = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode("utf-8")).hexdigest()
        if signature != st.session_state.docx_signature:
            invalidate_docx()
        if st.button("Тапсырмаларды бекіту және DOCX дайындау"):
            invalidate_docx()
            buffer = generate_protocol_docx(**payload)
            st.session_state.docx_buffer = buffer.getvalue()
            st.session_state.docx_signature = signature
            st.success("DOCX дайын.")
        if st.session_state.docx_buffer is not None:
            st.download_button(
                "Хаттаманы жүктеу", data=st.session_state.docx_buffer,
                file_name=f"protocol_{meeting_date}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
    except Exception:
        invalidate_docx()
        st.error("DOCX дайындалмады. Кестедегі мәндерді тексеріп, қайта көріңіз.")
else:
    st.caption("Аудионы жүктеп, «Өңдеу» батырмасын басыңыз.")
