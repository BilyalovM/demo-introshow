"""
Авторизация: хэширование паролей (PBKDF2 + соль) и подписанные сессионные токены.

Сессии (v2 security):
- Токен: "<username>:<session_version>:<exp_unix>:<hmac>"
- HMAC считается по "username:session_version:exp" с SESSION_SECRET.
- Cookie: HttpOnly, SameSite=Lax, Secure на HTTPS; max_age = SESSION_MAX_AGE (7 суток).
- Logout-all: инкремент User.session_version инвалидирует все выданные токены пользователя.
- Legacy-токены "username:sig" больше не принимаются (после деплоя нужна повторная авторизация).
"""
import hashlib
import hmac
import os
import secrets
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_FILE = os.path.join(BASE_DIR, ".session_secret")

# Сессия: 7 суток (было 30). При необходимости сократите через SESSION_MAX_AGE_HOURS.
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_HOURS", "168")) * 3600  # default 7d

# Разделы системы для настройки прав доступа сотрудников
SECTIONS = {
    "dashboard": "Дашборд",
    "today": "Сегодня",
    "inbox": "Чаты (Inbox)",
    "chats": "Внутренние чаты",
    "crm": "CRM",
    "quotes": "Сметы и договоры",
    "calendar": "Календарь",
    "equipment": "Склад",
    "companies": "Клиенты",
    "tasks": "Задачи",
    "analytics": "Аналитика",
    "assistant": "AI Чат-бот",
    "settings": "Настройки",
}

# Специальные флаги прав (хранятся в том же JSON permissions, не открывают раздел)
PERMISSION_FLAGS = {
    "crm_own_only": "CRM: видеть только свои сделки",
    "hide_prices": "Скрывать суммы и цены (техник / склад)",
    "hide_margin": "Скрывать маржу проекта",
    "hide_payroll": "Скрывать зарплатную ведомость",
    "hide_subrental_cost": "Скрывать себестоимость субаренды",
    "role_sales": "Роль: менеджер продаж",
    "role_project": "Роль: менеджер проекта",
}

# Соответствие URL-префиксов разделам (для проверки доступа)
PATH_SECTIONS = {
    "/today": "today",
    "/inbox": "inbox",
    "/chats": "chats",
    "/crm": "crm",
    "/quotes": "quotes",
    "/calendar": "calendar",
    "/equipment": "equipment",
    "/companies": "companies",
    "/tasks": "tasks",
    "/analytics": "analytics",
    "/assistant": "assistant",
    "/settings": "settings",
}


def _get_secret() -> bytes:
    env_secret = os.getenv("SESSION_SECRET")
    if env_secret:
        return env_secret.encode()
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r") as f:
            return f.read().strip().encode()
    secret = secrets.token_hex(32)
    try:
        with open(SECRET_FILE, "w") as f:
            f.write(secret)
    except OSError:
        pass
    return secret.encode()


_SECRET = _get_secret()

PBKDF2_ITERATIONS = 120_000


def get_password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not hashed_password:
        return False
    if hashed_password.startswith("pbkdf2$"):
        try:
            _, iterations, salt, digest = hashed_password.split("$")
            calc = hashlib.pbkdf2_hmac(
                "sha256", plain_password.encode(), salt.encode(), int(iterations)
            ).hex()
            return hmac.compare_digest(calc, digest)
        except (ValueError, TypeError):
            return False
    # Старый формат (несолёный SHA-256) — поддерживается для миграции
    legacy = hashlib.sha256(plain_password.encode()).hexdigest()
    return hmac.compare_digest(legacy, hashed_password)


def is_legacy_hash(hashed_password: str) -> bool:
    return bool(hashed_password) and not hashed_password.startswith("pbkdf2$")


def create_session_token(username: str, session_version: int = 0, max_age: int = None) -> str:
    """Подписанный токен с TTL и версией сессии (для logout-all)."""
    ttl = SESSION_MAX_AGE if max_age is None else int(max_age)
    exp = int(time.time()) + max(60, ttl)
    ver = int(session_version or 0)
    payload = f"{username}:{ver}:{exp}"
    sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def parse_session_token(token: str):
    """
    Вернуть (username, session_version) или None.
    Проверяет подпись и срок действия.
    """
    if not token or ":" not in token:
        return None
    parts = token.split(":")
    # Новый формат: user:ver:exp:sig
    if len(parts) >= 4:
        username = parts[0]
        try:
            ver = int(parts[1])
            exp = int(parts[2])
        except ValueError:
            return None
        sig = parts[-1]
        # username может содержать ":" — берём всё до ver:exp:sig
        # При username без ":" parts = [user, ver, exp, sig]
        if len(parts) > 4:
            username = ":".join(parts[:-3])
            try:
                ver = int(parts[-3])
                exp = int(parts[-2])
            except ValueError:
                return None
            sig = parts[-1]
        payload = f"{username}:{ver}:{exp}"
        expected = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if exp < int(time.time()):
            return None
        return username, ver
    return None


def get_username_from_token(token: str):
    """Совместимость: только username или None."""
    parsed = parse_session_token(token)
    if not parsed:
        return None
    return parsed[0]


def cookie_secure_flag(request=None) -> bool:
    """Secure=True на HTTPS / Vercel / за reverse-proxy с X-Forwarded-Proto."""
    if os.environ.get("VERCEL") or os.environ.get("FORCE_SECURE_COOKIE") == "1":
        return True
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        if proto == "https":
            return True
    return False


def session_cookie_kwargs(request=None, max_age: int = None) -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure_flag(request),
        "max_age": SESSION_MAX_AGE if max_age is None else int(max_age),
        "path": "/",
    }


def user_can_access(user, section: str) -> bool:
    """Проверка доступа пользователя к разделу."""
    if user is None:
        return False
    if user.role == "admin":
        return True
    perms = user.permissions
    if not perms:  # null/пусто = полный доступ (по умолчанию)
        return True
    # Флаги вроде crm_own_only не открывают разделы — учитываем только ключи SECTIONS
    section_perms = [p for p in perms if p in SECTIONS]
    if not section_perms:
        return True  # только флаги без разделов = полный доступ к разделам
    if section in section_perms:
        return True
    # Календарь доступен всем, у кого есть CRM или сметы (даже без отдельной галочки)
    if section == "calendar" and ("crm" in section_perms or "quotes" in section_perms):
        return True
    # «Сегодня» — операционный экран для CRM / задач / дашборда
    if section == "today" and any(s in section_perms for s in ("dashboard", "crm", "tasks", "calendar")):
        return True
    return False


def section_for_path(path: str):
    for prefix, section in PATH_SECTIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return section
    if path == "/":
        return "dashboard"
    return None


def user_has_flag(user, flag: str) -> bool:
    if not user or user.role == "admin":
        return False
    return flag in (user.permissions or [])
