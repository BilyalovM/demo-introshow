"""Простой in-memory sliding-window rate limit (одна инстанция / serverless-холодный старт).

Fail closed: при превышении лимита — 429.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from fastapi.responses import JSONResponse

_lock = threading.Lock()
_buckets: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def check_rate_limit(
    key: str,
    *,
    limit: int,
    window_sec: int,
    record: bool = True,
) -> Optional[JSONResponse]:
    """Вернуть JSONResponse 429 при превышении, иначе None.

    record=False — только проверка лимита без записи (для успешного логина).
    """
    now = time.time()
    cutoff = now - window_sec
    with _lock:
        q = _buckets[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            retry_after = max(1, int(window_sec - (now - q[0])) if q else window_sec)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Слишком много запросов. Подождите и попробуйте снова.",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        if record:
            q.append(now)
    return None


def limit_login(request) -> Optional[JSONResponse]:
    """Проверка лимита логина (без записи). Писать только неудачи — см. record_login_failure."""
    ip = _client_ip(request)
    return check_rate_limit(f"login:{ip}", limit=20, window_sec=15 * 60, record=False)


def record_login_failure(request) -> None:
    """Учесть неудачную попытку входа (успешные логины лимит не сжигают)."""
    ip = _client_ip(request)
    check_rate_limit(f"login:{ip}", limit=20, window_sec=15 * 60, record=True)


def limit_public_leads(request) -> Optional[JSONResponse]:
    # 20 заявок / час на IP (форма + API)
    ip = _client_ip(request)
    return check_rate_limit(f"leads:{ip}", limit=20, window_sec=60 * 60)


def limit_register(request) -> Optional[JSONResponse]:
    # 8 попыток регистрации / 15 минут на IP (защита от перебора токенов / спама)
    ip = _client_ip(request)
    return check_rate_limit(f"register:{ip}", limit=8, window_sec=15 * 60)


def peek_stats(prefix: str = "") -> Dict[str, int]:
    """Для отладки: размер активных окон."""
    now = time.time()
    with _lock:
        out = {}
        for k, q in _buckets.items():
            if prefix and not k.startswith(prefix):
                continue
            out[k] = sum(1 for t in q if t >= now - 3600)
        return out
