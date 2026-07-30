# Версии Intro Show CRM

## v1.0.0 / ветка `v1-stable`

**Снимок рабочей системы до UX-оверхола v2.**

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

Vercel: в проекте → Deployments → найти деплой с ветки `main` / `v1-stable` на момент тега → **Promote to Production**.  
Либо переключить Production Branch на `v1-stable` и Redeploy.

---

## v2 — ветка `v2`

Новая версия с UX-оверхолом: хаб сделки, единая смета, экран «Сегодня», денежная картина, пайплайн отгрузки, in-app уведомления, шаблоны, клиентский пакет, готовность к Postgres.

Разработка только на `v2`. Пока v2 не принят в прод — **Production остаётся на v1** (`main` = freeze / `v1-stable`). Preview — из ветки `v2`.

### URL-стратегия

| Окружение | Ветка | Назначение |
|---|---|---|
| Production | `main` / `v1-stable` | Стабильный v1.0.0 |
| Preview | `v2` | Тест UX-оверхола |

Vercel CLI в среде агента недоступен — деплой preview: GitHub → Vercel (авто для `v2`) или `vercel` локально с этой ветки. **Не** делайте `vercel --prod` для v2, пока не решите cutover.

### Cutover на v2 (когда готовы)

```bash
git checkout main
git merge v2   # или PR v2 → main
git push origin main
# затем Promote preview → Production в Vercel
```

Откат после cutover: снова `git checkout v1.0.0` / деплой `v1-stable`.

---

## База данных и VPS

- **Демо (Vercel):** SQLite (`rental_app.db`), на serverless копируется в `/tmp` (эфемерно).
- **Боевой VPS:** задайте `DATABASE_URL`, например:

  ```bash
  DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/introshow
  ```

  Движок SQLAlchemy переключается по `DATABASE_URL` (см. `database.py`). SQLite без переменной продолжает работать локально и на Vercel.

Миграция SQLite → Postgres: выгрузка данных отдельным шагом (не делается автоматически mid-flight). Рекомендуется: поднять Postgres на VPS, прогнать `Base.metadata.create_all`, перенести справочники/сделки скриптом или вручную, переключить `DATABASE_URL`.
