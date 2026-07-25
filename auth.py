"""
Авторизация: хэширование паролей (PBKDF2 + соль) и подписанные сессионные токены.

Токен: "<username>:<hmac_sha256(secret, username)>". Токен действителен, пока
пользователь существует в БД, поэтому удаление сотрудника сразу отзывает доступ.
"""
import hashlib
import hmac
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_FILE = os.path.join(BASE_DIR, ".session_secret")

# Разделы системы для настройки прав доступа сотрудников
SECTIONS = {
    "dashboard": "Дашборд",
    "inbox": "Чаты (Inbox)",
    "crm": "CRM",
    "quotes": "Сметы и договоры",
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
}

# Соответствие URL-префиксов разделам (для проверки доступа)
PATH_SECTIONS = {
    "/inbox": "inbox",
    "/crm": "crm",
    "/quotes": "quotes",
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


def create_session_token(username: str) -> str:
    sig = hmac.new(_SECRET, username.encode(), hashlib.sha256).hexdigest()
    return f"{username}:{sig}"


def get_username_from_token(token: str):
    if not token or ":" not in token:
        return None
    username, sig = token.rsplit(":", 1)
    expected = hmac.new(_SECRET, username.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig, expected):
        return username
    return None


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
    return section in section_perms


def section_for_path(path: str):
    for prefix, section in PATH_SECTIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return section
    if path == "/":
        return "dashboard"
    return None
