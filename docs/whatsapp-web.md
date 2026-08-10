# WhatsApp Web (бесплатный self-hosted) и Instagram

Подключение WhatsApp к Intro Show CRM **без платных агрегаторов** (Wazzup и т.п.) через QR WhatsApp Web.

**Официальный платный путь Meta Cloud API** — отдельная история (Business API, шаблоны, верификация). Ниже — **неофициальный** мост на WhatsApp Web.

---

## Архитектура (словами)

```
Телефон WhatsApp
      ↕  (протокол WhatsApp Web)
wa_bridge (Node + whatsapp-web.js + Chromium)  ← только на VPS, постоянный процесс
      │
      │  inbound:  POST {CRM_URL}/api/webhooks/whatsapp-web   (X-API-Key)
      │  outbound: CRM → POST {WA_BRIDGE_URL}/send            (X-API-Key)
      ▼
Intro Show CRM (FastAPI) — Vercel или тот же VPS
      │
      ├─ Inbox (channel = whatsapp)
      └─ ensure_deal_for_chat → автосделка в CRM
```

| Компонент | Где живёт | Зачем |
|-----------|-----------|--------|
| CRM UI + webhook | Vercel / VPS | Приём сообщений, ответы менеджера, сделки |
| `wa_bridge/` | **VPS обязательно** | QR, сессия WhatsApp Web на диске, отправка |
| Сессия | `uploads/wa-web-session/` на VPS | LocalAuth — **не коммитить** |

На **Vercel serverless** сессию WhatsApp Web держать нельзя: нет постоянного процесса и постоянного диска для Chromium/auth.

Альтернатива тому же неофициальному стеку: **WAHA** Docker (`/api/wa/webhook`) — уже был в проекте; основной рекомендованный путь теперь — `wa_bridge`.

---

## Как подключить (чек-лист)

### 1. Секрет и URL

Придумайте длинный `WA_WEB_API_KEY` (общий для CRM и bridge).

**CRM** (Vercel Environment Variables или `.env` на VPS):

```bash
WA_BRIDGE_URL=https://wa.ваш-домен.kz   # или http://IP:3001 за VPN/firewall
WA_WEB_API_KEY=длинный-секрет
```

**Bridge** (на VPS, рядом с `docker-compose.yml`):

```bash
CRM_URL=https://demo-introshow.vercel.app   # публичный HTTPS CRM
WA_WEB_API_KEY=длинный-секрет               # тот же
```

### 2. Запуск моста на VPS

```bash
cd /opt/introshow-crm   # или путь к клону репо
mkdir -p uploads/wa-web-session
# в .env: CRM_URL=...  WA_WEB_API_KEY=...
docker compose up -d wa-bridge
docker compose logs -f wa-bridge
```

Без Docker:

```bash
cd wa_bridge
npm install
export CRM_URL=https://ваш-crm
export CRM_API_KEY=длинный-секрет
export BRIDGE_API_KEY=длинный-секрет
export SESSION_PATH=../uploads/wa-web-session
npm start
```

Нужны Node ≥ 18 и Chromium/зависимости Puppeteer. Порт по умолчанию **3001**.

CRM должен **достучаться** до `WA_BRIDGE_URL` (исходящий HTTPS/HTTP с Vercel → VPS).  
Bridge должен **достучаться** до публичного CRM webhook.

### 3. QR в CRM

1. Войти админом → **Настройки** → блок **«WhatsApp Web (бесплатно, QR)»**.
2. Нажать **«Подключить»**.
3. Отсканировать QR: WhatsApp → **Привязанные устройства** → Привязка устройства.
4. Статус станет **«Подключено»**.

### 4. Проверка end-to-end

1. Написать на подключённый номер с другого телефона.
2. Сообщение появляется в **Inbox** (канал WhatsApp).
3. Создаётся сделка (`ensure_deal_for_chat`).
4. Ответ из Inbox уходит через bridge → WhatsApp.

---

## API

| Метод | Путь | Кто вызывает | Назначение |
|-------|------|--------------|------------|
| POST | `/api/webhooks/whatsapp-web` | bridge → CRM | Входящее сообщение / status |
| GET | `/api/wa-web/status` | UI (сессия админа) | Статус моста |
| POST | `/api/wa-web/connect` | UI admin | Старт QR / reconnect |
| GET | `/api/wa-web/qr` | UI | PNG QR |
| POST | `/api/wa-web/logout` | UI admin | Logout + wipe session |
| POST | `{WA_BRIDGE_URL}/send` | CRM → bridge | Исходящее |

Webhook payload (message):

```json
{
  "event": "message",
  "chat_id": "77001234567@c.us",
  "text": "Здравствуйте",
  "sender_name": "Иван"
}
```

Заголовок: `X-API-Key: <WA_WEB_API_KEY>`.  
Без ключа webhook отклоняется (кроме локальной отладки `WA_WEB_ALLOW_OPEN=1`).

---

## Риски и ограничения (обязательно прочитать)

1. **Неофициальный API** — эмуляция WhatsApp Web (Baileys / whatsapp-web.js / WAHA). Meta может ограничить или забанить номер.
2. **ToS** — использование на свой страх и риск; для продакшена с большим объёмом лучше **Meta Cloud API** или легальный BSP.
3. **Только VPS** — постоянный процесс + диск; не рассчитывайте на «только Vercel».
4. **Память** — Chromium ~0.5–1 ГБ RAM; на слабом VPS следите за OOM.
5. **Медиа / группы** — MVP заточен под текстовые личные чаты; файлы и группы можно расширить позже.
6. **Один номер ≈ одна сессия** на одном bridge.

Рекомендация: 5–7 дней на **тестовом** номере, потом основной.

---

## Контраст с Meta Cloud API / Wazzup

| | WhatsApp Web bridge | Meta Cloud API / BSP (Wazzup…) |
|--|---------------------|--------------------------------|
| Стоимость | инфра VPS | подписка / диалоги |
| QR | да | нет (официальный onboarding) |
| Бан / ToS | риск неофициального клиента | в рамках правил Meta |
| Шаблоны 24h | нет официальных ограничений Cloud | да, политика шаблонов |
| Деплой CRM | Vercel ок | Vercel ок |
| Сессия WA | только VPS | у провайдера |

---

## Instagram — честная оценка «бесплатных» вариантов

| Вариант | Вердикт |
|---------|---------|
| Неофициальные DM-скрейперы / puppeteer IG | **Не внедряем.** Ломаются часто, высокий риск бана аккаунта. |
| Mentions / комментарии через неофиц. парсеры | Хрупко, не замена Inbox DM. |
| **Meta Messenger API for Instagram** (официально) | Рекомендуемый путь: уже есть заготовки `/api/ig/webhook`, `IG_PAGE_TOKEN`, `IG_VERIFY_TOKEN` в Настройках. |

Практический план по IG:

1. Бизнес-аккаунт Instagram + Facebook Page.
2. Приложение Meta → продукт Instagram Messaging.
3. Webhook на `https://ваш-crm/api/ig/webhook`, verify token в `.env`.
4. Ответы из Inbox через Graph API (`chatbot.send_instagram`).

Платный агрегатор для IG имеет смысл только если не хотите сами проходить Meta App Review.

---

## Связанные файлы

- `wa_bridge/server.js` — мост
- `docker-compose.yml` — сервис `wa-bridge`
- `app.py` — `/api/wa-web/*`, `/api/webhooks/whatsapp-web`
- `chatbot.py` — `send_whatsapp` (bridge → WAHA fallback)
- `templates/settings.html` — UI «WhatsApp Web»
- `docs/deploy-server.md` — общий деплой VPS
