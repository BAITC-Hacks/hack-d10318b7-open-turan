"""
app.py
Streamlit интерфейсі: "Кеңестерді автохаттамалау және
тапсырмаларды бекіту жүйесі"
"""

import streamlit as st
from datetime import date

from utils import get_client, transcribe_audio, structure_meeting
from docx_generator import generate_protocol_docx

st.set_page_config(
    page_title="Кеңес хаттамалау жүйесі",
    page_icon="🗂️",
    layout="wide",
)

st.title("🗂️ Кеңестерді автохаттамалау және тапсырмаларды бекіту жүйесі")
st.caption(
    "Аудионы мәтінге түрлендіру (STT) → Қорытынды мен тапсырмаларды бөліп алу (GPT-4o) → DOCX хаттама"
)

# ---------------------------------------------------------------------------
# Sidebar: баптаулар
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Баптаулар")

    api_key = st.text_input("OpenAI API кілті (API key)", type="password")

    language_labels = {
        "Авто-анықтау (аралас / шала-қазақ)": None,
        "Орысша": "ru",
        "Қазақша": "kk",
    }
    language_choice = st.selectbox("Аудио тілі", options=list(language_labels.keys()))
    language_hint = language_labels[language_choice]

    meeting_title = st.text_input(
        "Кеңестің атауы", value="Іскерлік кеңестің хаттамасы"
    )
    meeting_date = st.date_input("Кеңес күні", value=date.today())
    include_transcript = st.checkbox(
        "DOCX ішіне толық транскриптті қосу", value=True
    )

    st.divider()
    st.caption(
        "ℹ️ Whisper API дыбыс бойынша спикерлерді өзі ажыратпайды. "
        "Бұл жүйе спикерлерді мәтіннің мазмұнына қарап GPT-4o арқылы "
        "болжамдап бөліп шығарады."
    )

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "result" not in st.session_state:
    st.session_state.result = None
if "transcript" not in st.session_state:
    st.session_state.transcript = None
if "docx_buffer" not in st.session_state:
    st.session_state.docx_buffer = None

# ---------------------------------------------------------------------------
# Аудио жүктеу және өңдеу
# ---------------------------------------------------------------------------
uploaded_file = st.file_uploader(
    "Аудиофайлды жүктеңіз (MP3 / WAV / M4A)", type=["mp3", "wav", "m4a", "mp4"]
)

process_btn = st.button("🚀 Өңдеу", type="primary", use_container_width=False)

if process_btn:
    if not api_key:
        st.error("Өтінеміз, OpenAI API кілтін енгізіңіз.")
    elif not uploaded_file:
        st.error("Өтінеміз, аудиофайл жүктеңіз.")
    else:
        try:
            client = get_client(api_key)

            with st.spinner("1/2 — Аудио мәтінге түрлендірілуде (транскрипция)..."):
                transcript_text = transcribe_audio(
                    client, uploaded_file, language_hint=language_hint
                )
                st.session_state.transcript = transcript_text

            with st.spinner("2/2 — Қорытынды мен тапсырмалар жасалуда (GPT-4o)..."):
                result = structure_meeting(client, transcript_text)
                st.session_state.result = result
                st.session_state.docx_buffer = None  # алдыңғы DOCX-ты тазалаймыз

            st.success("Дайын! Төменнен нәтижені тексере аласыз.")
        except Exception as e:
            st.error(f"Қате кетті: {e}")

# ---------------------------------------------------------------------------
# Транскриптті көрсету
# ---------------------------------------------------------------------------
if st.session_state.transcript:
    with st.expander("📄 Толық транскрипт (мәтінге түсірілген нұсқа)"):
        st.text_area(
            "Транскрипт",
            st.session_state.transcript,
            height=250,
            label_visibility="collapsed",
        )

# ---------------------------------------------------------------------------
# Нәтижелерді көрсету және бекіту
# ---------------------------------------------------------------------------
if st.session_state.result:
    result = st.session_state.result

    st.subheader("📝 Саммари")
    st.write(result.get("summary", "—"))

    st.subheader("🎙️ Спикерлер")
    speakers = result.get("speakers", [])
    st.write(", ".join(speakers) if speakers else "Анықталмады")

    st.subheader("✅ Тапсырмалар (бекіту үшін өзгертуге болады)")
    tasks = result.get("tasks", [])

    if tasks:
        edited_tasks = st.data_editor(
            tasks,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "task": st.column_config.TextColumn("Тапсырма", width="large"),
                "responsible": st.column_config.TextColumn(
                    "Жауапты (Ответственный)"
                ),
                "deadline": st.column_config.TextColumn("Мерзімі (Срок)"),
            },
            key="tasks_editor",
        )
    else:
        st.info("Тапсырмалар табылмады.")
        edited_tasks = []

    st.divider()
    st.subheader("📥 DOCX хаттаманы дайындау және жүктеп алу")

    if st.button("✅ Тапсырмаларды бекіту және DOCX дайындау"):
        docx_buffer = generate_protocol_docx(
            meeting_title=meeting_title,
            meeting_date=str(meeting_date),
            summary=result.get("summary", ""),
            speakers=speakers,
            tasks=edited_tasks,
            transcript_text=st.session_state.transcript
            if include_transcript
            else None,
        )
        st.session_state.docx_buffer = docx_buffer.getvalue()
        st.success("DOCX файл дайын!")

    if st.session_state.docx_buffer:
        st.download_button(
            label="⬇️ Хаттаманы .docx түрінде жүктеп алу",
            data=st.session_state.docx_buffer,
            file_name=f"protocol_{meeting_date}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=False,
        )
else:
    st.info("Аудиофайлды жүктеп, 'Өңдеу' батырмасын басыңыз.")
