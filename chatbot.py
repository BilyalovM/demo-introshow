"""
AI чат-бот для мессенджеров (WhatsApp / Telegram / Instagram).

Логика:
- Входящее сообщение сохраняется в ChatMessage.
- Если бот включён, канал активен и сейчас рабочее время по графику —
  генерируется ответ через Gemini на основе базы знаний (KnowledgeItem)
  и последних сообщений диалога.
- Вне графика отправляется настроенный автоответ (один раз за диалог подряд).
- Бот отвечает "как человек": короткие живые фразы, без упоминания, что он бот.
"""
import datetime
import os

import requests
from sqlalchemy.orm import Session

from database import BotSettings, ChatMessage, KnowledgeItem

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Модели пробуются по порядку: если у одной исчерпана квота — берётся следующая
GEMINI_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-flash-latest,gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash",
    ).split(",") if m.strip()
]

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SCHEDULE = {day: {"on": True, "start": "10:00", "end": "22:00"} for day in DAY_KEYS}
DEFAULT_CHANNELS = {"whatsapp": True, "telegram": True, "instagram": True}
DEFAULT_PERSONA = (
    "Ты — менеджер компании Intro Show (аренда звукового, светового и сценического "
    "оборудования в Алматы). Общайся вежливо, дружелюбно и по-человечески, как живой "
    "сотрудник: короткие фразы, без канцелярита. Отвечай только на основе базы знаний "
    "компании. Если не знаешь ответа — скажи, что уточнишь у коллег, и предложи оставить "
    "контакты. Никогда не говори, что ты бот или ИИ."
)
DEFAULT_OFF_HOURS = (
    "Здравствуйте! Сейчас мы не на связи (график: Пн–Вс 10:00–22:00). "
    "Напишите ваш вопрос — ответим, как только начнётся рабочий день."
)


def get_bot_settings(db: Session) -> BotSettings:
    settings = db.query(BotSettings).first()
    if not settings:
        settings = BotSettings(
            enabled=True,
            schedule=DEFAULT_SCHEDULE,
            channels=DEFAULT_CHANNELS,
            persona=DEFAULT_PERSONA,
            off_hours_message=DEFAULT_OFF_HOURS,
            timezone="Asia/Almaty",
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _now_local(settings: BotSettings) -> datetime.datetime:
    tz_name = settings.timezone or "Asia/Almaty"
    if ZoneInfo:
        try:
            return datetime.datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.datetime.now()


def is_within_schedule(settings: BotSettings) -> bool:
    now = _now_local(settings)
    schedule = settings.schedule or DEFAULT_SCHEDULE
    day_cfg = schedule.get(DAY_KEYS[now.weekday()]) or {}
    if not day_cfg.get("on", False):
        return False
    try:
        start_h, start_m = map(int, (day_cfg.get("start") or "00:00").split(":"))
        end_h, end_m = map(int, (day_cfg.get("end") or "23:59").split(":"))
    except ValueError:
        return True
    minutes = now.hour * 60 + now.minute
    return (start_h * 60 + start_m) <= minutes <= (end_h * 60 + end_m)


def channel_enabled(settings: BotSettings, channel: str) -> bool:
    channels = settings.channels or DEFAULT_CHANNELS
    return bool(channels.get(channel, True))


def _build_knowledge_text(db: Session) -> str:
    items = db.query(KnowledgeItem).order_by(KnowledgeItem.id).all()
    if not items:
        return "База знаний пока пуста."
    parts = []
    for item in items:
        parts.append(f"### {item.title}\n{item.content}")
    text = "\n\n".join(parts)
    # Ограничиваем размер контекста
    return text[:30000]


def _recent_history(db: Session, channel: str, chat_id: str, limit: int = 12):
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.channel == channel, ChatMessage.chat_id == chat_id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(messages))


def call_gemini(system_prompt: str, history, user_text: str):
    """Прямой вызов Gemini REST API. Возвращает текст ответа или None."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    contents = []
    for msg in history:
        role = "user" if msg.direction == "in" else "model"
        contents.append({"role": role, "parts": [{"text": msg.text or ""}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        # Запас токенов нужен моделям 2.5+: внутренние "размышления" тоже расходуют лимит
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 2048},
    }
    for model in GEMINI_MODELS:
        try:
            r = requests.post(
                GEMINI_URL.format(model=model),
                params={"key": api_key},
                json=payload,
                timeout=30,
            )
            if r.status_code != 200:
                # Квота/перегрузка/недоступность — пробуем следующую модель
                print(f"Gemini {model}: {r.status_code} {r.text[:200]} — пробуем следующую модель")
                continue
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"Gemini request failed ({model}):", e)
            continue
    return None


def generate_reply(db: Session, channel: str, chat_id: str, user_text: str):
    """Формирует ответ бота с учётом графика, базы знаний и истории диалога.

    Возвращает (text, is_bot_reply) или (None, False), если отвечать не нужно.
    """
    settings = get_bot_settings(db)
    if not settings.enabled or not channel_enabled(settings, channel):
        return None, False

    if not is_within_schedule(settings):
        # Автоответ вне графика — не спамим, если последнее исходящее уже было им
        last_out = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.channel == channel,
                ChatMessage.chat_id == chat_id,
                ChatMessage.direction == "out",
            )
            .order_by(ChatMessage.id.desc())
            .first()
        )
        off_msg = settings.off_hours_message or DEFAULT_OFF_HOURS
        if last_out and last_out.text == off_msg:
            return None, False
        return off_msg, True

    knowledge = _build_knowledge_text(db)
    persona = settings.persona or DEFAULT_PERSONA
    system_prompt = (
        f"{persona}\n\n"
        f"БАЗА ЗНАНИЙ КОМПАНИИ (отвечай строго на её основе):\n{knowledge}\n\n"
        "Правила ответа: пиши на языке клиента (обычно русский), кратко (1-4 предложения), "
        "тепло и естественно. Не используй markdown-разметку."
    )
    history = _recent_history(db, channel, chat_id)
    reply = call_gemini(system_prompt, history, user_text)
    if reply:
        return reply, True
    return None, False


def save_message(db: Session, channel: str, chat_id: str, direction: str,
                 text: str, sender_name: str = None, is_bot: bool = False) -> ChatMessage:
    msg = ChatMessage(
        channel=channel,
        chat_id=str(chat_id),
        direction=direction,
        text=text,
        sender_name=sender_name,
        is_bot=is_bot,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


# ---------- Отправка сообщений по каналам ----------

WAHA_URL = os.getenv("WAHA_URL", "http://127.0.0.1:3000")


def send_whatsapp(chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"{WAHA_URL}/api/sendText",
            json={"session": "default", "chatId": chat_id, "text": text},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        print("WA send error:", e)
        return False


def send_telegram(chat_id: str, text: str) -> bool:
    token = os.getenv("TG_BOT_TOKEN", "")
    if not token:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print("TG send error:", e)
        return False


def send_instagram(recipient_id: str, text: str) -> bool:
    """Отправка в Instagram Direct через Meta Graph API (нужен IG_PAGE_TOKEN)."""
    token = os.getenv("IG_PAGE_TOKEN", "")
    if not token:
        return False
    try:
        r = requests.post(
            "https://graph.facebook.com/v19.0/me/messages",
            params={"access_token": token},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print("IG send error:", e)
        return False


SENDERS = {
    "whatsapp": send_whatsapp,
    "telegram": send_telegram,
    "instagram": send_instagram,
}


def handle_incoming(db: Session, channel: str, chat_id: str, text: str, sender_name: str = None):
    """Полный цикл: сохранить входящее, сгенерировать и отправить ответ бота."""
    save_message(db, channel, chat_id, "in", text, sender_name=sender_name)
    reply, is_bot = generate_reply(db, channel, chat_id, text)
    if reply:
        sender = SENDERS.get(channel)
        sent = sender(chat_id, reply) if sender else False
        # Сохраняем ответ в ленту даже если канал недоступен (видно в Inbox)
        save_message(db, channel, chat_id, "out", reply, sender_name="AI Бот", is_bot=is_bot)
        return reply, sent
    return None, False
