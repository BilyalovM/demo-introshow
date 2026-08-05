# Версии Intro Show CRM

## Production (cutover)

| | |
|---|---|
| Git `main` / `v2` | **v2** — security P0 (audit, sessions, rate limit) + Аренда/Продажа |
| Production URL | https://demo-introshow.vercel.app |
| Sessions | Cookie HttpOnly + SameSite=Lax + Secure(HTTPS); TTL 7d (`SESSION_MAX_AGE_HOURS`); logout-all via `User.session_version` |
| Rollback | Promote / redeploy `v1-stable` или tag `v1.0.0` (не удалять) |

---

## v1.0.0 / ветка `v1-stable`

**Снимок рабочей системы до UX-оверхола v2.** Сохранён для отката.

Включено: сметы (внутренняя / клиентская / техничка), CRM-канбан, внутренние чаты, задачи (Битрикс UX), авансы/расходы, зарплатная ведомость, команда→техничка, Inbox, склад/бронь, PWA-демо на Vercel (SQLite).

| | |
|---|---|
| Tag | `v1.0.0` |
| Branch | `v1-stable` |
| Freeze commit | `5b07a96d207f360289e6e5c4c4ba53d85ac087ef` |

### Откат на v1

Локально:

```bash
git fetch origin --tags
git checkout v1.0.0
# или долгоживущая ветка:
git checkout v1-stable
```

Vercel (production rollback):

1. Deployments → найти последний успешный деплой с `v1-stable` / commit `5b07a96` → **Promote to Production**, **или**
2. Project Settings → Git → Production Branch = `v1-stable` → Redeploy, **или**
3. `git checkout main && git reset --hard v1.0.0 && git push origin main` (только если осознанно откатываете main; предпочтительнее Promote без rewrite).

Ветку `v1-stable` и tag `v1.0.0` **не удалять**.

---

## v2 — ветка `v2` → production `main`

UX-оверхол: хаб сделки, единая смета, экран «Сегодня», денежная картина, пайплайн отгрузки, in-app уведомления, шаблоны, клиентский пакет, готовность к Postgres.

**Cutover выполнен:** `v2` смержен в `main`; production = v2.

Дальнейшая разработка: ветка `v2` (или feature-ветки → `v2` → `main`).

### URL-стратегия

| Окружение | Ветка | Назначение |
|---|---|---|
| Production | `main` (= v2) | Боевой демо https://demo-introshow.vercel.app |
| Rollback snapshot | `v1-stable` / `v1.0.0` | Стабильный v1 |
| Preview | feature / `v2` | Тест до merge в main |

---

## База данных и VPS

- **Демо (Vercel):** SQLite (`rental_app.db`), на serverless копируется в `/tmp` (эфемерно).
- **Боевой VPS:** задайте `DATABASE_URL`, например:

  ```bash
  DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/introshow
  ```

  Движок SQLAlchemy переключается по `DATABASE_URL` (см. `database.py`). SQLite без переменной продолжает работать локально и на Vercel.

Миграция SQLite → Postgres: выгрузка данных отдельным шагом (не делается автоматически mid-flight). Рекомендуется: поднять Postgres на VPS, прогнать `Base.metadata.create_all`, перенести справочники/сделки скриптом или вручную, переключить `DATABASE_URL`.
