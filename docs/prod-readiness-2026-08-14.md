# Готовность к продакшену — Intro Show CRM (запуск 14 Aug 2026)

Сервер (VPS) будет позже. Этот документ фиксирует: **что уже сделано в репозитории** и **что Максим должен сделать на VPS**, когда появится машина.

Источник аудита: desktop `implementation_plan.md` (полная проверка модулей ✅).

---

## ✅ Сделано в коде / репозитории

| Пункт | Статус | Где |
|---|---|---|
| Дубликат `_user_display_name` | ✅ одна функция, пустой user → `""` | `app.py` |
| SQL миграции с параметрами | ✅ `:pid` bound params (SQLite + Postgres) | `app.py` |
| `.env.example` для прода | ✅ `DATABASE_URL`, `SESSION_SECRET`, `FORCE_SECURE_COOKIE`, `ONEC_API_KEY`, `ADMIN_PASSWORD`, `SEED_DEMO_DEALS`, WA | `.env.example` |
| `SESSION_SECRET` из env | ✅ сначала env, затем `SECRET_KEY`, затем `.session_secret` | `auth.py` |
| Демо-сид безопасен | ✅ `only_if_empty=True` по умолчанию; `SEED_DEMO_DEALS=0` отключает | `demo_seed.py`, `app.py` |
| Предупреждения при старте | ✅ лог SQLite / admin:admin / нет SESSION_SECRET | `app.py` |
| Баннер в Настройках | ✅ `/api/admin/db-health` + security_warnings | Settings → Проверка БД |
| Смена своего пароля | ✅ UI + `POST /api/me/change-password` (+ `/api/admin/change-password`) | Settings |
| Nginx пример | ✅ | `deploy/nginx.example.conf` |
| systemd пример | ✅ | `deploy/introshow.service` |
| Скрипт проверки env | ✅ | `scripts/check-prod-ready.py` |
| Документация деплоя | ✅ расширенный чеклист | `docs/deploy-server.md` |
| Локальный SQLite / демо | ✅ не сломан (без `DATABASE_URL` работает как раньше) | — |

**Не делается из репо (нужен сервер):** создание Postgres, выпуск SSL, установка systemd на реальном VPS, смена боевых секретов на машине.

---

## 📋 Чеклист Максима на VPS (когда появится сервер)

### Инфраструктура
- [ ] Ubuntu 22.04+ (или аналог), Python 3.11+, Nginx, PostgreSQL 14+
- [ ] `sudo -u postgres createuser introshow -P && createdb -O introshow introshow`
- [ ] Клон репо в `/opt/introshow-crm`, `python3 -m venv venv`, `pip install -r requirements.txt`
- [ ] `cp .env.example .env` и заполнить секреты (см. ниже)
- [ ] `python scripts/check-prod-ready.py` → все критические ✅
- [ ] `deploy/introshow.service` → `/etc/systemd/system/`, `systemctl enable --now introshow`
- [ ] `deploy/nginx.example.conf` + certbot HTTPS
- [ ] Cron бэкапа: `scripts/backup.sh` (pg_dump + uploads)

### Переменные в `.env` на сервере
- [ ] `DATABASE_URL=postgresql+psycopg2://introshow:<ПАРОЛЬ>@localhost:5432/introshow`
- [ ] `SESSION_SECRET=` случайная строка **≥ 64** символов  
  `python -c "import secrets; print(secrets.token_hex(32))"`
- [ ] `FORCE_SECURE_COOKIE=1`
- [ ] `SEED_DEMO_DEALS=0`
- [ ] `ONEC_API_KEY=` боевой (не `test-onec-key-123`)
- [ ] `ADMIN_PASSWORD=` сильный (только для **первого** создания admin на пустой БД) **или** сразу сменить пароль в UI
- [ ] При WhatsApp: `WA_BRIDGE_URL`, `WA_WEB_API_KEY`

### Безопасность / функционал после первого входа
- [ ] Сменить пароль admin (Настройки → Сменить пароль)
- [ ] Создать менеджеров с правами (не общий admin)
- [ ] Реквизиты компании в Настройках + логотип `/static/img/introshow_logo.png`
- [ ] Воронки / маршрутизация источников
- [ ] Каталог оборудования (если нужно)
- [ ] Smoke: логин → лид → смета → документ → задача
- [ ] (Опц.) WhatsApp bridge: `docker compose up -d wa-bridge` — см. `docs/whatsapp-web.md`

### Мониторинг
- [ ] `journalctl -u introshow -f`
- [ ] Уведомления о падении сервиса (по желанию)

---

## Демо (Vercel)

`vercel --prod` остаётся для **демо**. Без Postgres на Vercel данные в `/tmp` SQLite могут пропадать. Бой сотрудников — только VPS + Postgres.

---

## Быстрые команды (на будущее)

```bash
cp .env.example .env
# отредактировать .env
python scripts/check-prod-ready.py

sudo cp deploy/introshow.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now introshow

sudo cp deploy/nginx.example.conf /etc/nginx/sites-available/introshow
# правки server_name → certbot --nginx -d …
```

Подробности: [`deploy-server.md`](./deploy-server.md).
