# Деплой Intro Show CRM на VPS (боевой сервер)

**Без Postgres на Vercel данные могут пропадать.** Vercel + SQLite (`/tmp`) — только демо: cold start сбрасывает эфемерную БД. Для работы сотрудников нужен **VPS + постоянная БД (Postgres)** и каталог uploads.

## Что понадобится

- Ubuntu 22.04+ (или аналог)
- Домен с HTTPS (Nginx + Let’s Encrypt)
- Python 3.11+
- PostgreSQL 14+ (рекомендуется)
- systemd (или Docker)

## Переменные окружения

Скопируйте шаблон и заполните секреты на сервере (не коммитьте `.env`):

```bash
cp .env.example .env
nano .env
```

| Переменная | Обязательно | Назначение |
|---|---|---|
| `DATABASE_URL` | да (прод) | `postgresql+psycopg2://USER:PASS@localhost:5432/introshow` |
| `SESSION_SECRET` | да | Длинная случайная строка для cookie-сессий |
| `SECRET_KEY` | опционально | Алиас/запасной секрет; приложение читает `SESSION_SECRET` |
| `RENTAL_UPLOADS_DIR` | нет | Путь к файлам (по умолчанию `./uploads`) |
| `SESSION_MAX_AGE_HOURS` | нет | TTL сессии, по умолчанию `168` (7 дней) |
| `GEMINI_API_KEY` | для бота | Google Gemini |
| `TG_BOT_TOKEN` | опционально | Telegram |
| `IG_PAGE_TOKEN`, `IG_VERIFY_TOKEN` | опционально | Instagram |
| `ONEC_API_KEY` | опционально | API 1С |
| `LEAD_API_KEY` | опционально | Защита `POST /api/leads` |

Без `DATABASE_URL` используется SQLite (`rental_app.db`) — ок для локальной проверки, **не для боевых сотрудников**.

> **Важно:** **без Postgres на Vercel данные могут пропадать** (сделки, задачи, рабочие смены). Задайте `DATABASE_URL` на VPS до того, как пускать команду в CRM.

## Быстрый старт на VPS

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx postgresql postgresql-contrib

# Postgres
sudo -u postgres createuser introshow -P
sudo -u postgres createdb -O introshow introshow

cd /opt
sudo git clone <YOUR_REPO_URL> introshow-crm
cd introshow-crm
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# отредактируйте DATABASE_URL и SESSION_SECRET

mkdir -p uploads backups
# первый запуск создаст таблицы и admin
uvicorn app:app --host 127.0.0.1 --port 8000
```

Откройте `http://SERVER:8000/login` — логин по умолчанию после чистой БД: `admin` / `admin`. **Сразу смените пароль** и создайте сотрудников в Настройки → Сотрудники или `/users`.

## systemd (пример)

`/etc/systemd/system/introshow.service`:

```ini
[Unit]
Description=Intro Show CRM
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/introshow-crm
EnvironmentFile=/opt/introshow-crm/.env
ExecStart=/opt/introshow-crm/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now introshow
```

## HTTPS и cookie

Nginx проксирует на `127.0.0.1:8000`. Сессии: HttpOnly + SameSite=Lax; на HTTPS cookie ставится **Secure**.

Важно: за прокси передавайте `X-Forwarded-Proto: https`, иначе Secure-cookie может вести себя некорректно.

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 32m;
}
```

## Docker one-liner (приложение)

Готового `Dockerfile` в репозитории нет — минимальный вариант:

```bash
docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/introshow' \
  -e SESSION_SECRET='change-me' \
  -v /var/introshow/uploads:/app/uploads \
  -w /app python:3.11-slim \
  bash -lc 'pip install -r requirements.txt && uvicorn app:app --host 0.0.0.0 --port 8000'
```

(обычно удобнее systemd + venv; WAHA для WhatsApp — отдельный контейнер, см. Настройки).

## Бэкапы

```bash
chmod +x scripts/backup.sh
# cron ежедневно в 03:15
15 3 * * * cd /opt/introshow-crm && DATABASE_URL='postgresql://…' ./scripts/backup.sh >> /var/log/introshow-backup.log 2>&1
```

Скрипт кладёт dump Postgres (или копию SQLite) + `uploads.tar.gz` в `backups/`, ротация 14 дней.

## Миграция с демо (SQLite → Postgres)

1. Поднимите Postgres и задайте `DATABASE_URL`.
2. Запустите приложение один раз — `create_all` создаст схему.
3. Перенесите справочники/сделки отдельным скриптом или вручную (автомиграции mid-flight нет).
4. Переключите systemd на новый `.env`, проверьте логин и пару сделок.

## Чеклист «перед тем как пустить сотрудников»

1. **Сменить пароль admin**, создать личные логины (не общий admin).
2. **Воронки:** Лиды → Аренда / Продажа; стадии «Успешно» / «Отказ» помечены.
3. **Маршрутизация** источников (WhatsApp, сайт, карты…) в Настройки / CRM → Воронки.
4. **Права:** техникам — флаг `hide_prices` (и при необходимости `hide_margin` / `hide_payroll`).
5. Роли: `role_sales` / `role_project` по необходимости.
6. **Шапка сметы** — Настройки → реквизиты компании (название, телефон, email, адрес, БИН).
7. **Бэкап cron** работает; проверьте восстановление из одного дампа.
8. HTTPS включён; `SESSION_SECRET` уникальный.
9. (Опционально) `GEMINI_API_KEY` + тест WhatsApp на **вторичном** номере.
10. Пройти smoke: логин → создать лид → квалификация → конвертация → смета → задача → чат.

## Учётные записи (паттерн, без реальных паролей в git)

| Логин | Роль | Права (пример) |
|---|---|---|
| `admin` | admin | всё (только владелец/IT) |
| `dinara` | manager | CRM, сметы, клиенты; `role_sales` |
| `project1` | user | CRM, задачи, сегодня; `role_project` |
| `tech1` | user | сегодня, задачи, склад; `hide_prices` |

Пароли выдаются лично / через менеджера; в репозиторий не класть.

## Vercel demo

`vercel --prod` остаётся для демо-континуитета. **Без Postgres на Vercel данные могут пропадать** — SQLite в `/tmp` эфемерен. Боевая работа сотрудников — только на VPS с Postgres (`DATABASE_URL`).

В Настройках → «Проверка БД» видно backend, путь и счётчики; при SQLite на Vercel показывается красный баннер.

## Города (multi-city)

См. [multi-city.md](./multi-city.md). На VPS после деплоя таблицы/колонки создаются при старте (`cities`, `deals.city_id`, `users.city_id`, `work_sessions.start_place/end_place`). Алматы сидируется автоматически; Астану/Шымкент можно включить в **Настройки → Города CRM**.
