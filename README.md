# Intro Show CRM

CRM-система для бизнеса по аренде звукового, светового и сценического оборудования.

## Возможности

- **CRM** — канбан-воронка сделок в стиле Битрикс24, карточки сделок, пользовательские поля, история.
- **Сметы и договоры** — конструктор смет с проверкой брони оборудования по датам, генерация DOCX (смета и договор).
- **Клиенты и контакты** — компании с реквизитами и контактными лицами.
- **Мессенджеры** — единый Inbox: WhatsApp Web (self-hosted `wa_bridge` / QR на VPS), Telegram, Instagram Direct (Meta); автосоздание сделок. См. `docs/whatsapp-web.md`.
- **AI-чат-бот** — отвечает клиентам по загруженной базе знаний (Google Gemini), настраиваемый график работы и персона.
- **Задачи** — канбан задач с ответственными, дедлайнами, приоритетами и привязкой к сделкам.
- **Сотрудники** — до 10 пользователей с гибкими правами доступа по разделам.
- **1С** — API обмена контрагентами и счетами (`/api/1c/*`, авторизация по `X-API-Key`).
- **PWA** — установка на телефон, мобильная адаптация всех страниц.

## Стек

FastAPI + SQLAlchemy (SQLite) + Jinja2, ванильный JS/CSS, PWA (service worker).

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Откройте http://127.0.0.1:8000 (логин по умолчанию: `admin`).

## Переменные окружения (`.env`)

| Переменная | Назначение |
|---|---|
| `SESSION_SECRET` | Секрет для подписи сессий (обязательно для продакшена) |
| `GEMINI_API_KEY` | Ключ Google Gemini для AI-бота |
| `TG_BOT_TOKEN` | Токен Telegram-бота |
| `IG_PAGE_TOKEN`, `IG_VERIFY_TOKEN` | Instagram Direct (Meta Graph API) |
| `WA_BRIDGE_URL`, `WA_WEB_API_KEY` | WhatsApp Web bridge на VPS (`docs/whatsapp-web.md`) |
| `ONEC_API_KEY` | Ключ API для обмена с 1С |

## Версии

См. [VERSIONS.md](VERSIONS.md): **v1.0.0 / `v1-stable`** — текущий прод-снимок; **ветка `v2`** — UX-оверхол. Откат: `git checkout v1.0.0`.

## Деплой на Vercel

Проект готов к деплою: `vercel.json` направляет все запросы в `app.py`,
**без Postgres на Vercel данные могут пропадать** — база копируется в `/tmp` (данные demo-режима сбрасываются при
перезапуске serverless-функции). Задайте `SESSION_SECRET` в настройках
проекта Vercel. Production держите на `main`/`v1-stable`; preview — ветка `v2`.

Для Postgres на VPS задайте `DATABASE_URL` (см. VERSIONS.md и [docs/deploy-server.md](docs/deploy-server.md)); без неё работает SQLite. Шаблон переменных: `.env.example`.
