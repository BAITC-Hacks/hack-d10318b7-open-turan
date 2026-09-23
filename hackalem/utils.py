"""
utils.py
Аудионы мәтінге түрлендіру (STT) және
мәтіннен қорытынды + тапсырмаларды JSON форматында бөліп алу логикасы.
"""

import json
from openai import OpenAI

DEFAULT_MODEL_TRANSCRIBE = "whisper-1"
DEFAULT_MODEL_CHAT = "gpt-4o"


def get_client(api_key: str) -> OpenAI:
    """OpenAI клиентін жасайды."""
    return OpenAI(api_key=api_key)


def transcribe_audio(client: OpenAI, audio_file, language_hint: str = None) -> str:
    """
    Аудиофайлды (MP3/WAV/M4A) мәтінге түрлендіреді.

    :param audio_file: Streamlit-тен келген UploadedFile объектісі
                        (аты .name арқылы болуы керек).
    :param language_hint: "ru", "kk" немесе None.
                           None болса — Whisper тілді өзі анықтайды,
                           бұл аралас ("шала-қазақ") сөйлеу үшін көбіне жақсы жұмыс істейді.
    :return: транскрипцияланған мәтін (str)
    """
    kwargs = {
        "model": DEFAULT_MODEL_TRANSCRIBE,
        "file": audio_file,
        "response_format": "text",
    }
    if language_hint:
        kwargs["language"] = language_hint

    transcript = client.audio.transcriptions.create(**kwargs)
    # response_format="text" болғанда, жауап тікелей жол (str) болып келеді
    return transcript if isinstance(transcript, str) else str(transcript)


# GPT-4o-ға жіберілетін жүйелік промпт:
# - қорытынды (саммари) жасайды
# - спикерлерді контекске қарап болжамдап бөледі (псевдо-диаризация)
# - тапсырмаларды, жауаптыларды және мерзімдерді JSON форматында шығарады
TASKS_SYSTEM_PROMPT = """
Сен кеңес (жиналыс) хаттамасын дайындайтын көмекшісің.
Саған кеңестің транскрипті беріледі — орыс, қазақ немесе аралас
("шала-қазақ") тілде жазылған болуы мүмкін.

Сенің міндетің — берілген мәтінді талдап, ТЕК ТАЗА JSON форматында
жауап қайтару (қосымша мәтін, markdown немесе ``` белгілерін қолданба):

{
  "summary": "Кеңестің 3-5 сөйлемнен тұратын қысқаша қорытындысы",
  "speakers": ["Спикер 1", "Спикер 2", "..."],
  "tasks": [
    {
      "task": "Тапсырманың нақты сипаттамасы",
      "responsible": "Жауапты адамның аты (мәтіннен табылса), немесе 'Анықталмады'",
      "deadline": "Мерзімі (мәтіннен табылса, мыс. '2026-10-01' немесе 'дүйсенбіге дейін'), немесе 'Анықталмады'"
    }
  ]
}

Ережелер:
1. Спикерлерді нақты ажырата алмасаң (аудио диаризациясыз, тек мәтіннен),
   сөйлеу стиліне, қаратпаларға ("Айгерім айтты", "мен ұсынамын" т.б.)
   қарап болжа; аты табылмаса "Спикер 1", "Спикер 2" деп шартты түрде белгіле.
2. Егер ешбір тапсырма табылмаса, "tasks" массивін бос ([]) қалдыр.
3. Әр тапсырма жеке, қысқа әрі нақты болуы керек.
4. Жауапты адам немесе мерзім мәтіннен анық көрінбесе, "Анықталмады" деп жаз,
   өзің ойдан шығарма.
5. Жауап ТЕК JSON объект болуы керек, басқа ештеңе қоспа.
"""


def structure_meeting(client: OpenAI, transcript_text: str, model: str = DEFAULT_MODEL_CHAT) -> dict:
    """
    Транскриптті GPT-4o арқылы өңдеп, қорытынды, спикерлер және
    тапсырмалар тізімін JSON (dict) түрінде қайтарады.
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TASKS_SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # GPT дұрыс емес JSON қайтарса, қауіпсіз fallback
        data = {
            "summary": "⚠️ Нәтижені JSON форматына түрлендіру кезінде қате кетті.",
            "speakers": [],
            "tasks": [],
        }

    # Кілттердің бар екенін қамтамасыз етеміз
    data.setdefault("summary", "")
    data.setdefault("speakers", [])
    data.setdefault("tasks", [])

    return data
