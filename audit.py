"""Журнал аудита: кто что менял (сделки, сметы, права, ведомость, маршрутизация)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from database import AuditLog


def _safe_json(data: Any) -> Optional[dict]:
    if data is None:
        return None
    if isinstance(data, dict):
        try:
            json.dumps(data, ensure_ascii=False, default=str)
            return data
        except (TypeError, ValueError):
            return {"_repr": str(data)[:2000]}
    return {"value": str(data)[:2000]}


def write_audit(
    db: Session,
    *,
    user_id: Optional[int],
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    diff: Any = None,
    ip: Optional[str] = None,
    commit: bool = False,
) -> AuditLog:
    row = AuditLog(
        user_id=user_id,
        entity_type=(entity_type or "unknown")[:64],
        entity_id=entity_id,
        action=(action or "update")[:128],
        diff=_safe_json(diff),
        ip=(ip or None),
        created_at=datetime.utcnow(),
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def request_ip(request) -> Optional[str]:
    if request is None:
        return None
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


ACTION_LABELS = {
    "create": "создание",
    "update": "изменение",
    "stage_change": "смена стадии",
    "items_save": "сохранение сметы",
    "permissions": "права доступа",
    "user_create": "создание пользователя",
    "user_update": "изменение пользователя",
    "user_delete": "удаление пользователя",
    "payroll_update": "ведомость",
    "payroll_generate": "формирование ведомости",
    "routing_change": "маршрутизация",
    "pipeline_change": "воронка",
    "logout_all": "выход со всех устройств",
    "login": "вход",
}

ENTITY_LABELS = {
    "deal": "Сделка",
    "user": "Пользователь",
    "payroll": "Ведомость",
    "pipeline_routing": "Маршрутизация",
    "pipeline": "Воронка",
    "session": "Сессия",
}
