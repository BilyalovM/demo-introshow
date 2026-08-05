# Обзор Twenty CRM vs Intro Show CRM

**Дата:** 5 августа 2026  
**Источники:** [Elest.io — Twenty](https://elest.io/open-source/twenty), [GitHub twentyhq/twenty](https://github.com/twentyhq/twenty) (README, LICENSE), [docs.twenty.com](https://docs.twenty.com), код Intro Show (`rental_app`, `PROJECT_CONTEXT.md`, `docs/plan-vstrecha-2026-08-05.md`).

---

## Что такое Twenty (кратко)

**Twenty** — open-source CRM («открытая альтернатива Salesforce»), ориентированная на гибкую модель данных, воронки, автоматизации и AI. Позиционируется как CRM, которую можно **собирать и версионировать как код** (SDK/CLI apps: объекты, views, agents, logic functions).

- **Репозиторий:** https://github.com/twentyhq/twenty (~54k★ на момент обзора)
- **Лицензия:** в основном **AGPLv3** + исключение для приложений через Application Interfaces; часть файлов — коммерческая Enterprise (`/* @license Enterprise */`); SDK/UI-пакеты — **MIT**
- **Деплой:** Twenty Cloud, self-host (Docker Compose), managed-хостинг (в т.ч. Elest.io: email sync, kanban, custom data model)
- **Не отраслевой прокат:** стандартные объекты — Companies, People, Opportunities, Tasks, Notes; домен «смета / техничка / субаренда / ведомость» нужно строить самим

---

## Архитектура и стек

| Слой | Twenty (официально) | Intro Show |
|---|---|---|
| Backend | NestJS, GraphQL Yoga, TypeORM, BullMQ | FastAPI (Python) |
| Frontend | React (Jotai, Linaria, Lingui) | Jinja2 + HTML/CSS/JS, PWA |
| БД | PostgreSQL (+ Redis; ClickHouse для audit/analytics) | SQLite по умолчанию, Postgres-ready (`DATABASE_URL`) |
| Монорепо | Nx / Yarn (`twenty-front`, `twenty-server`, `twenty-sdk`, …) | Один сервис `app.py` + модули |
| Очереди | BullMQ workers | Синхронные запросы + webhooks мессенджеров |
| Файлы | Local FS или S3 / S3-совместимое | Локальные `uploads/` (+ DOCX-шаблоны) |
| Multi-tenant | Schema-per-tenant; self-host: single-workspace по умолчанию, опция `IS_MULTIWORKSPACE_ENABLED` | Один инстанс / одна компания; мультигород — в плане P2 |

**Инфра Twenty (self-host):** Postgres, Redis, server + worker; шифрование секретов (`ENCRYPTION_KEY`); SSRF-защита исходящих запросов; code/logic functions в prod по умолчанию **выключены** (нужен Lambda/E2B для песочницы).

**Инфра Intro Show:** лёгкий монолит; для WhatsApp — Docker WAHA; деплой demo на Vercel (`demo-introshow.vercel.app`).

---

## Модули / фичи

Легенда: **Да** / **Частично** / **Нет**. «Частично» — есть упрощённый аналог или другой канал.

| Область | Twenty | Intro Show | Комментарий |
|---|---|---|---|
| Auth (логин/пароль) | Да | Да | Intro: HMAC cookie + PBKDF2-SHA256 (`auth.py`) |
| OAuth Google/Microsoft | Да | Нет | Twenty: login + Gmail/Calendar/Outlook sync |
| SSO (SAML / OIDC) | Да (Organization plan) | Нет | SAML 2.0, Google Workspace, Entra ID |
| RBAC по объектам/полям | Да | Частично | Intro: права **по разделам** + флаги `crm_own_only`, `hide_prices` |
| Row-level permissions | Да (Premium / Organization) | Частично | Intro: «только свои сделки» — флаг, не универсальные фильтры |
| Стандартные CRM-объекты | Companies, People, Opportunities, Tasks, Notes | Компании, контакты, сделки/лиды, задачи | У Intro плюс склад, сметы, персонал |
| Кастомная модель данных | Да (UI + code apps) | Частично | Intro: кастомные поля сделок; объекты жёстко в коде |
| Pipelines / Kanban | Да (views: table / kanban / calendar) | Да | Воронки лидов/продаж, стадии, `creates_deal` |
| Задачи | Да (стандартный объект) | Да | Канбан, дедлайны, привязка к сделкам |
| Inbox / messaging | Email (Gmail/M365/IMAP) | WhatsApp / Telegram / Instagram | Разные каналы: Twenty — почта; Intro — мессенджеры |
| Внутренние чаты | Нет (как отдельный продукт) | Да | Модуль chats |
| Workflows / automation | Да (visual builder + code) | Частично | Intro: бот, статус→мессенджер, напоминания в плане P1 |
| AI | Agents, chatbot по CRM-данным | Gemini-бот по базе знаний в мессенджерах | Разный фокус |
| API | REST + GraphQL (schema-per-workspace), webhooks, OAuth | REST (лиды, 1С, webhooks WA/TG/IG) | Twenty: автогенерация под схему |
| Audit log | Да (в key features; UI/ClickHouse, enterprise-контур) | Нет | Intro: нет журнала «кто что менял» |
| Multi-tenant / workspaces | Да | Нет | Intro — одна орг; города — будущее |
| Mobile | Нет официального native app в docs | PWA | Intro: manifest + service worker |
| File storage | Local / S3 | Local uploads | Twenty документирует S3 для prod |
| Dashboards | Да (виджеты) | Да | Intro: analytics + dashboard |
| CSV import/export | Да | Нет / слабо | Twenty — first-class |
| Сметы / КП / договоры аренды | Нет из коробки (tutorial PDF/quote через apps/workflows) | Да | Ядро Intro Show |
| Техничка | Нет | Да | Экспорт `/api/deals/{id}/technichka` |
| Субаренда / склад проката | Нет | Да | Каталог own/subrental, конфликты дат |
| Зарплатная ведомость / ФОТ по сделке | Нет | Да | Payroll из сметы «Персонал» |
| Интеграция 1С | Нет из коробки | Заготовка API | `X-API-Key` `/api/1c/*` |

---

## Безопасность: что у Twenty и чего не хватает Intro Show

### У Twenty (по docs / Legal FAQ / self-host)

- RBAC: объекты, поля, settings, actions; роли на API keys и AI agents  
- SSO + JIT provisioning (Organization)  
- Encryption in transit/at rest (cloud); schema-per-tenant isolation  
- Soft-delete + retention; audit logs (заявлены в key features)  
- API rate limit (документировано: 100 req/min)  
- Self-host: `ENCRYPTION_KEY`, SSRF safe mode, sandbox для code execution  
- Cloud: SOC 2, GDPR (Trust Center); support access можно отключить  
- Лицензионный риск: AGPL при **модификации и предоставлении третьим лицам** — иначе Organization license

### У Intro Show сейчас

**Есть:** закрытый middleware, PBKDF2, httponly cookie `samesite=lax`, права по разделам, флаги скрытия цен / «свои сделки», API-ключ для 1С.

**Пробелы относительно практики Twenty / enterprise-гигиены:**

| Пробел | Зачем важно для Intro Show |
|---|---|
| Нет audit log | Сметы, маржа, ведомость, права — спорные изменения без следа |
| Cookie без `Secure`, сессия 30 дней, токен без TTL в подписи | Долгоживущий доступ при утечке cookie |
| Нет SSO / MFA | Для малого штата некритично; при росте и подрядчиках — нужно |
| RBAC только секции, не поля объектов | Техник видит раздел, но нет тонкой матрицы «видеть маржу / не видеть» кроме `hide_prices` |
| Нет rate limit на login / публичный `/api/leads` | Брутфорс и спам-лиды |
| SQLite + бэкапы «на совести» деплоя | Нет встроенной политики retention/soft-delete как у Twenty Cloud |
| Секреты мессенджеров/Gemini в `.env` | Нормально; нет ротации encryption-at-rest для токенов как у Twenty |

SSO уровня SAML для текущей команды Intro Show — **не must-have**. Audit, ужесточение сессий и точечные права — да.

---

## Что реально стоит добавить/улучшить у Intro Show

Практично: **не переписывать на Twenty**, а перенять паттерны. Согласовано с планом встречи 05.08.2026.

### P0 (1–2 недели) — безопасность и контроль без смены платформы

1. **Журнал аудита (минимум)** — кто менял сделку/смету/права/ведомость (user, entity, action, timestamp, diff или JSON snapshot).  
2. **Сессии:** `Secure` cookie на HTTPS, сократить `max_age`, опционально expire в токене / logout-all.  
3. **Rate limit** на `/api/login` и публичный захват лидов.  
4. Дожать продуктовый P0 встречи: воронки аренда/продажа, бот на тестовом WhatsApp, два режима сметы — это важнее «фич Twenty».

### P1 (2–4 недели) — автоматизация «как workflows», но узко

5. **Правила-роботы без visual builder:** сделка без движения >24ч → напоминание; эскалация просроченных задач (уже в плане встречи).  
6. **Роли sales vs project** в карточке + проверка прав (не только секции).  
7. **Полевая видимость:** расширить `hide_prices` / отдельные флаги (маржа, ведомость, себестоимость субаренды).  
8. Backup-скрипт Postgres/SQLite + uploads в cron (если ещё не на проде).

### P2 (горизонт) — только при масштабе

9. Подрядчики с ограниченным доступом (отдельная воронка) — ближе к row-level Twenty.  
10. MFA (TOTP) или SSO, если появится оргструктура/мультигород.  
11. Расширение публичного API под интеграции (не полный GraphQL-метаслой).  
12. S3-совместимое хранилище вложений при выходе с одного диска VPS.

---

## Чего не копировать (overkill для прокатного бизнеса)

| Twenty-фича | Почему не брать сейчас |
|---|---|
| Миграция всего продукта на Twenty | Потеря смет/технички/субаренды/ведомости/WAHA; месяцы кастомных objects + apps |
| AGPL-форк ядра Twenty | Лицензионная и инженерная нагрузка; стек NestJS ≠ текущий Python-монолит |
| Visual workflow builder «на всё» | Достаточно 3–5 жёстких роботов под прокат |
| Полный email mailbox sync | Канал продаж — WhatsApp/IG/TG, не Gmail threads |
| Multi-workspace SaaS | Одна компания; города — копирование регламента, не schema-per-tenant |
| Row-level Premium + ClickHouse audit enterprise | Дорого по сложности; хватит таблицы `audit_events` в Postgres/SQLite |
| Marketplace apps / create-twenty-app | Имеет смысл только если *стать* на Twenty |
| AI agents с code interpreter / Lambda | Уже есть целевой Gemini-бот в мессенджерах |

---

## Вывод: развивать свой продукт vs мигрировать на Twenty

**Рекомендация: развивать Intro Show**, используя Twenty как **референс паттернов** (RBAC-глубина, audit, сессии, точечные автоматизации), а не как целевую платформу.

| Критерий | Вердикт |
|---|---|
| Fit домена проката | Intro Show сильно впереди из коробки |
| Скорость до сентябрьского сезона | Миграция сорвёт сроки; доработки P0 — нет |
| Стоимость владения | Twenty self-host = Postgres+Redis+workers+обновления AGPL; Intro уже в проде/демо |
| Безопасность | Подтянуть у Intro точечно; не требует смены CRM |
| Когда revisited Twenty | Если продукт станет мультиарендным SaaS для чужих компаний *без* глубокой прокатной логики — маловероятный сценарий Intro Show |

**Итог одной фразой:** Twenty — сильный general-purpose open CRM; Intro Show — vertical rental OS. Перенос на Twenty уничтожит преимущество; заимствовать стоит дисциплину доступа и аудита.

---

### Источники (для проверки фактов)

1. https://elest.io/open-source/twenty  
2. https://github.com/twentyhq/twenty — README, LICENSE  
3. https://docs.twenty.com/getting-started/key-features.md  
4. https://docs.twenty.com/user-guide/permissions-access/…  
5. https://docs.twenty.com/developers/self-host/capabilities/setup.md  
6. https://docs.twenty.com/developers/extend/api.md  
7. https://docs.twenty.com/user-guide/legal/how-tos/legal-faq.md  
8. Intro Show: `PROJECT_CONTEXT.md`, `README.md`, `auth.py`, `docs/plan-vstrecha-2026-08-05.md`
