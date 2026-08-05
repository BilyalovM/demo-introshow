#!/usr/bin/env bash
# Бэкап SQLite/Postgres + uploads для Intro Show CRM.
# Usage:
#   ./scripts/backup.sh
#   BACKUP_DIR=/var/backups/introshow ./scripts/backup.sh
# Env:
#   DATABASE_URL — если задан postgres://…, делается pg_dump; иначе копируется rental_app.db
#   UPLOADS_DIR  — каталог uploads (по умолчанию ../uploads относительно скрипта)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/$STAMP"
UPLOADS_DIR="${UPLOADS_DIR:-$ROOT/uploads}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"

mkdir -p "$DEST"

echo "==> Backup → $DEST"

if [[ -n "${DATABASE_URL:-}" ]] && [[ "$DATABASE_URL" == postgres* ]]; then
  echo "… Postgres dump"
  # postgres:// → для pg_dump ок; убрать +psycopg2 если есть
  DUMP_URL="${DATABASE_URL/postgresql+psycopg2:/postgresql:}"
  DUMP_URL="${DUMP_URL/postgres:/postgresql:}"
  pg_dump --no-owner --format=custom -f "$DEST/db.dump" "$DUMP_URL"
  echo "    db.dump"
elif [[ -f "$ROOT/rental_app.db" ]]; then
  echo "… SQLite copy"
  cp -p "$ROOT/rental_app.db" "$DEST/rental_app.db"
  # Consistent snapshot if sqlite3 available
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$ROOT/rental_app.db" ".backup '$DEST/rental_app.consistent.db'" || true
  fi
else
  echo "!! Нет DATABASE_URL и нет $ROOT/rental_app.db — БД пропущена"
fi

if [[ -d "$UPLOADS_DIR" ]]; then
  echo "… uploads"
  tar -czf "$DEST/uploads.tar.gz" -C "$(dirname "$UPLOADS_DIR")" "$(basename "$UPLOADS_DIR")"
else
  echo "!! uploads не найдены ($UPLOADS_DIR)"
fi

# Мета
{
  echo "created_at=$STAMP"
  echo "host=$(hostname 2>/dev/null || echo unknown)"
  echo "database_url_set=$([ -n "${DATABASE_URL:-}" ] && echo yes || echo no)"
} > "$DEST/meta.txt"

# Ротация старых бэкапов
if [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] && [[ "$KEEP_DAYS" -gt 0 ]]; then
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true
fi

echo "OK: $DEST"
ls -lah "$DEST"
