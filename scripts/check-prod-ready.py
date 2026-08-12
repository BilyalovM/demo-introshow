#!/usr/bin/env python3
"""
Проверка готовности окружения Intro Show CRM к продакшену.
Запуск из корня репозитория (или с загруженным .env):

  python scripts/check-prod-ready.py
  # или
  cd /opt/introshow-crm && ./venv/bin/python scripts/check-prod-ready.py

Не трогает БД и не требует запущенного uvicorn — только env + эвристики.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def bad(msg: str) -> None:
    print(f"❌ {msg}")


def warn(msg: str) -> None:
    print(f"⚠️  {msg}")


def main() -> int:
    fails = 0
    print("Intro Show CRM — prod-ready check\n")

    db_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not db_url:
        bad("DATABASE_URL не задан (будет SQLite — не для боя сотрудников)")
        fails += 1
    elif db_url.startswith("sqlite"):
        bad(f"DATABASE_URL указывает на SQLite: {db_url[:60]}…")
        fails += 1
    elif db_url.startswith("postgres"):
        ok("DATABASE_URL задан (Postgres)")
        if "+psycopg2" not in db_url.split("://", 1)[0] and "postgresql://" in db_url:
            warn("Рекомендуется postgresql+psycopg2://… (приложение само поправит bare postgresql://)")
    else:
        warn(f"DATABASE_URL необычный префикс: {db_url[:40]}…")

    secret = (os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY") or "").strip()
    if not secret:
        bad("SESSION_SECRET не задан (≥64 символов)")
        fails += 1
    elif len(secret) < 64:
        bad(f"SESSION_SECRET слишком короткий ({len(secret)} < 64)")
        fails += 1
    else:
        ok(f"SESSION_SECRET задан ({len(secret)} символов)")

    if os.environ.get("FORCE_SECURE_COOKIE") == "1":
        ok("FORCE_SECURE_COOKIE=1")
    else:
        bad("FORCE_SECURE_COOKIE не =1 (нужно на HTTPS за nginx)")
        fails += 1

    if os.environ.get("SEED_DEMO_DEALS", "1").strip() == "0":
        ok("SEED_DEMO_DEALS=0 (демо-сид на старте выключен)")
    else:
        warn("SEED_DEMO_DEALS не =0 — на проде рекомендуется SEED_DEMO_DEALS=0")

    onec = (os.environ.get("ONEC_API_KEY") or "").strip()
    if not onec:
        warn("ONEC_API_KEY пуст (ок, если 1С не используется)")
    elif onec == "test-onec-key-123":
        bad("ONEC_API_KEY всё ещё тестовый (test-onec-key-123)")
        fails += 1
    else:
        ok("ONEC_API_KEY задан (не тестовый)")

    admin_pw = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    if admin_pw and admin_pw != "admin":
        ok("ADMIN_PASSWORD задан (не «admin») — применится только при пустой БД")
    elif admin_pw == "admin":
        bad("ADMIN_PASSWORD=admin — смените на сильный пароль")
        fails += 1
    else:
        warn("ADMIN_PASSWORD не задан — при пустой БД создастся admin/admin; смените пароль после первого входа")

    wa_url = (os.environ.get("WA_BRIDGE_URL") or "").strip()
    wa_key = (os.environ.get("WA_WEB_API_KEY") or "").strip()
    if wa_url or wa_key:
        if wa_url and wa_key:
            ok("WA_BRIDGE_URL и WA_WEB_API_KEY заданы")
        else:
            warn("WhatsApp: задайте оба WA_BRIDGE_URL и WA_WEB_API_KEY")
    else:
        warn("WhatsApp bridge не настроен (ок, если не используете)")

    if os.environ.get("VERCEL"):
        warn("VERCEL=1 — демо/эфемерный runtime, не боевой VPS")

    env_file = ROOT / ".env"
    if env_file.exists():
        ok(f".env найден: {env_file}")
    else:
        warn(".env не найден в корне проекта — скопируйте из .env.example на сервере")

    print()
    if fails:
        print(f"Итог: ❌ не готово ({fails} критических пунктов). См. docs/prod-readiness-2026-08-14.md")
        return 1
    print("Итог: ✅ критические env-пункты выглядят готовыми. Дальше — Postgres/nginx/systemd на VPS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
