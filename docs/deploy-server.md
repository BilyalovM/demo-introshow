# Деплой Intro Show CRM на VPS (боевой сервер)

**Без Postgres на Vercel данные могут пропадать.** Vercel + SQLite (`/tmp`) — только демо: cold start сбрасывает эфемерную БД. Для работы сотрудников нужен **VPS + постоянная БД (Postgres)** и каталог uploads.

Чеклист запуска **14 Aug 2026**: [`prod-readiness-2026-08-14.md`](./prod-readiness-2026-08-14.md).  
Проверка env без запуска сервиса: `python scripts/check-prod-ready.py`.

## Что понадобится

- Ubuntu 22.04+ (или аналог)
- Домен с HTTPS (Nginx + Let’s Encrypt)
- Python 3.11+
- PostgreSQL 14+ (рекомендуется)
- systemd (шаблон: [`deploy/introshow.service`](../deploy/introshow.service))
- Nginx (шаблон: [`deploy/nginx.example.conf`](../deploy/nginx.example.conf))

## Переменные окружения

Скопируйте шаблон и заполните секреты на сервере (не коммитьте `.env`):

```bash
cp .env.example .env
nano .env
python scripts/check-prod-ready.py
```

| Переменная | Обязательно | Назначение |
|---|---|---|
| `DATABASE_URL` | да (прод) | `postgresql+psycopg2://USER:PASS@localhost:5432/introshow` |
| `SESSION_SECRET` | да | Случайная строка **≥ 64** символов для cookie-сессий |
| `FORCE_SECURE_COOKIE` | да (прод HTTPS) | `1` — Secure-флаг cookie за nginx |
| `SEED_DEMO_DEALS` | да (прод) | `0` — не сидить DEMO-сделки на старте |
| `ADMIN_PASSWORD` | рекомендуется | Пароль первого `admin` на **пустой** БД; иначе `admin`/`admin` |
| `ONEC_API_KEY` | если 1С | Боевой ключ (не `test-onec-key-123`) |
| `WA_BRIDGE_URL`, `WA_WEB_API_KEY` | если WhatsApp | Self-hosted bridge |
| `SECRET_KEY` | опционально | Алиас, если `SESSION_SECRET` пуст |
| `RENTAL_UPLOADS_DIR` | нет | Путь к файлам (по умолчанию `./uploads`) |
| `SESSION_MAX_AGE_HOURS` | нет | TTL сессии, по умолчанию `168` (7 дней) |
| `GEMINI_API_KEY` | для бота | Google Gemini |
| `TG_BOT_TOKEN` | опционально | Telegram |
| `IG_PAGE_TOKEN`, `IG_VERIFY_TOKEN` | опционально | Instagram |
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
# DATABASE_URL, SESSION_SECRET (≥64), FORCE_SECURE_COOKIE=1,
# SEED_DEMO_DEALS=0, ONEC_API_KEY, ADMIN_PASSWORD (сильный)

mkdir -p uploads backups
python scripts/check-prod-ready.py

# первый ручной запуск создаст таблицы и admin
uvicorn app:app --host 127.0.0.1 --port 8000
```

Откройте `https://YOUR_DOMAIN/login` (после nginx/SSL) — логин после чистой БД: `admin` / значение `ADMIN_PASSWORD` (или `admin`/`admin`). **Сразу смените пароль** в Настройки → «Сменить пароль» и создайте сотрудников.

## systemd

Шаблон в репозитории: [`deploy/introshow.service`](../deploy/introshow.service).

```bash
sudo cp deploy/introshow.service /etc/systemd/system/introshow.service
# при необходимости поправьте WorkingDirectory / User
sudo systemctl daemon-reload
sudo systemctl enable --now introshow
sudo systemctl status introshow
journalctl -u introshow -f
```

## HTTPS (nginx)

Шаблон: [`deploy/nginx.example.conf`](../deploy/nginx.example.conf).

```bash
sudo cp deploy/nginx.example.conf /etc/nginx/sites-available/introshow
sudo nano /etc/nginx/sites-available/introshow   # server_name
sudo ln -sf /etc/nginx/sites-available/introshow /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d crm.example.com
```

Сессии: HttpOnly + SameSite=Lax; на HTTPS cookie **Secure** (`FORCE_SECURE_COOKIE=1` и/или `X-Forwarded-Proto: https`).

Критично в `location /`:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
client_max_body_size 32m;
```

## Docker one-liner (приложение)

Готового `Dockerfile` в репозитории нет — минимальный вариант:

```bash
docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql+psycopg2://user:pass@host:5432/introshow' \
  -e SESSION_SECRET='change-me-to-64-chars-minimum-please-xxxxxxxxxxxx' \
  -e FORCE_SECURE_COOKIE=1 \
  -e SEED_DEMO_DEALS=0 \
  -v /var/introshow/uploads:/app/uploads \
  -w /app python:3.11-slim \
  bash -lc 'pip install -r requirements.txt && uvicorn app:app --host 0.0.0.0 --port 8000'
```

(обычно удобнее systemd + venv).

### WhatsApp Web bridge (обязательно отдельный процесс)

Сессия WhatsApp Web **не живёт на Vercel**. На этом же (или отдельном) VPS:

```bash
# в .env рядом с compose: CRM_URL=https://…  WA_WEB_API_KEY=…
docker compose up -d wa-bridge
```

В CRM задайте `WA_BRIDGE_URL` (доступный с хоста CRM URL моста) и тот же `WA_WEB_API_KEY`.  
Подробно: [`docs/whatsapp-web.md`](whatsapp-web.md). Альтернатива — контейнер WAHA (см. Настройки).

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

1. **Postgres** в `DATABASE_URL`; `python scripts/check-prod-ready.py` без ❌.
2. **SESSION_SECRET** ≥ 64; **FORCE_SECURE_COOKIE=1**; HTTPS (nginx + certbot).
3. **systemd** `introshow` активен; логи без ошибок.
4. **Сменить пароль admin**, создать личные логины (не общий admin).
5. **SEED_DEMO_DEALS=0**; **ONEC_API_KEY** боевой (если нужен 1С).
6. **Воронки:** Лиды → Аренда / Продажа; стадии «Успешно» / «Отказ» помечены.
7. **Маршрутизация** источников (WhatsApp, сайт, карты…) в Настройки / CRM → Воронки.
8. **Права:** техникам — флаг `hide_prices` (и при необходимости `hide_margin` / `hide_payroll`).
9. Роли: `role_sales` / `role_project` по необходимости.
10. **Шапка сметы** — Настройки → реквизиты компании (название, телефон, email, адрес, БИН).
11. **Бэкап cron** работает; проверьте восстановление из одного дампа.
12. (Опционально) WhatsApp bridge + тест на **вторичном** номере.
13. Пройти smoke: логин → создать лид → квалификация → конвертация → смета → задача → чат.

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

В Настройках → «Проверка БД» видно backend, путь, счётчики и предупреждения безопасности (SQLite, короткий SESSION_SECRET, пароль admin и т.д.).

## Города (multi-city)

См. [multi-city.md](./multi-city.md). На VPS после деплоя таблицы/колонки создаются при старте (`cities`, `deals.city_id`, `users.city_id`, `work_sessions.start_place/end_place`). Алматы сидируется автоматически; Астану/Шымкент можно включить в **Настройки → Города CRM**.
