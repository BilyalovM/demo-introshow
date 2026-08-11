"""
Каталог системных шаблонов документов + конструктор (MVP).

Настраиваются шапка / примечания / футер / блоки.
Структура таблицы позиций остаётся в document_generator.py.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

# Плейсхолдеры для UI-пикера (ключ → подпись)
PLACEHOLDERS: List[Dict[str, str]] = [
    {"key": "{{deal.title}}", "label": "Название проекта"},
    {"key": "{{company.name}}", "label": "Клиент (компания)"},
    {"key": "{{contact_name}}", "label": "Контакт клиента"},
    {"key": "{{deal.event_date}}", "label": "Дата мероприятия"},
    {"key": "{{deal.city}}", "label": "Город"},
    {"key": "{{deal.event_address}}", "label": "Адрес / локация"},
    {"key": "{{manager.name}}", "label": "Менеджер проекта"},
    {"key": "{{manager.phone}}", "label": "Телефон менеджера"},
    {"key": "{{sales_manager_name}}", "label": "Менеджер продаж"},
    {"key": "{{company_settings.name}}", "label": "Наша компания"},
    {"key": "{{company_settings.phone}}", "label": "Наш телефон"},
    {"key": "{{company_settings.email}}", "label": "Наш email"},
    {"key": "{{company_settings.address}}", "label": "Наш адрес"},
    {"key": "{{company_settings.bin}}", "label": "БИН"},
    {"key": "{{shifts}}", "label": "Смены"},
    {"key": "{{rent_period}}", "label": "Период аренды"},
    {"key": "{{departure_date}}", "label": "Выезд со склада"},
    {"key": "{{return_date}}", "label": "Возврат на склад"},
    {"key": "{{number}}", "label": "Номер документа"},
    {"key": "{{date}}", "label": "Дата документа"},
    {"key": "{{grand_total}}", "label": "Итого"},
    {"key": "{{grand_total_text}}", "label": "Итого прописью"},
]

# Алиасы плейсхолдеров → ключи контекста генерации
_PLACEHOLDER_MAP = {
    "deal.title": ("project_name", "event_name", "event_name"),
    "project_name": ("project_name", "event_name"),
    "event_name": ("event_name", "project_name"),
    "company.name": ("company_name",),
    "company_name": ("company_name",),
    "contact_name": ("contact_name",),
    "deal.event_date": ("event_date", "return_date"),
    "event_date": ("event_date", "return_date"),
    "deal.city": ("city",),
    "city": ("city",),
    "deal.event_address": ("event_address",),
    "event_address": ("event_address",),
    "manager.name": ("manager_name", "project_manager_name"),
    "manager_name": ("manager_name", "project_manager_name"),
    "manager.phone": ("manager_phone", "our_company_phone"),
    "manager_phone": ("manager_phone", "our_company_phone"),
    "sales_manager_name": ("sales_manager_name",),
    "company_settings.name": ("our_company_name",),
    "company_settings.phone": ("our_company_phone",),
    "company_settings.email": ("our_company_email",),
    "company_settings.address": ("our_company_address",),
    "company_settings.bin": ("our_company_bin",),
    "our_company_name": ("our_company_name",),
    "our_company_phone": ("our_company_phone",),
    "our_company_email": ("our_company_email",),
    "our_company_address": ("our_company_address",),
    "our_company_bin": ("our_company_bin",),
    "shifts": ("shifts_label", "shifts"),
    "shifts_label": ("shifts_label", "shifts"),
    "rent_period": ("rent_period",),
    "departure_date": ("departure_date",),
    "return_date": ("return_date",),
    "number": ("number", "contract_number"),
    "date": ("date", "contract_date"),
    "contract_number": ("contract_number", "number"),
    "contract_date": ("contract_date", "date"),
    "grand_total": ("grand_total",),
    "grand_total_text": ("grand_total_text",),
    "assignee_name": ("assignee_name",),
}

_COMMON_FIELDS = [
    "project", "client", "manager", "city", "dates", "shifts",
    "items_table", "company_letterhead", "logo",
]

DEFAULT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "doc_type": "estimate_internal",
        "name": "Смета внутренняя",
        "description": "Для нас: цены, субаренда с себестоимостью, маржа.",
        "formats": ["docx", "pdf"],
        "used_fields": _COMMON_FIELDS + ["totals", "margin", "subrental"],
        "show_logo": True,
        "show_company_block": True,
        "custom_title": "",
        "body_notes": "",
        "footer_notes": "",
        "include_sections": {
            "items_table": True,
            "totals": True,
            "signature": False,
            "company_contacts": True,
        },
    },
    {
        "doc_type": "estimate_client",
        "name": "Смета клиенту без цен",
        "description": "Клиенту: позиции без цены за ед., только суммы строк и итог.",
        "formats": ["docx", "pdf"],
        "used_fields": _COMMON_FIELDS + ["totals", "signature"],
        "show_logo": True,
        "show_company_block": True,
        "custom_title": "",
        "body_notes": "",
        "footer_notes": "",
        "include_sections": {
            "items_table": True,
            "totals": True,
            "signature": True,
            "company_contacts": True,
        },
    },
    {
        "doc_type": "estimate_client_priced",
        "name": "Смета клиенту с ценами",
        "description": "Клиенту: цены за единицу видны, без внутреннего блока маржи.",
        "formats": ["docx", "pdf"],
        "used_fields": _COMMON_FIELDS + ["totals", "signature", "unit_prices"],
        "show_logo": True,
        "show_company_block": True,
        "custom_title": "",
        "body_notes": "",
        "footer_notes": "",
        "include_sections": {
            "items_table": True,
            "totals": True,
            "signature": True,
            "company_contacts": True,
        },
    },
    {
        "doc_type": "contract",
        "name": "Договор",
        "description": "Юридический DOCX по Word-шаблону + PDF-спецификация.",
        "formats": ["docx", "pdf"],
        "used_fields": [
            "project", "client", "client_requisites", "manager", "city", "dates",
            "items_table", "totals", "company_letterhead", "logo",
        ],
        "show_logo": True,
        "show_company_block": True,
        "custom_title": "",
        "body_notes": "",
        "footer_notes": "",
        "include_sections": {
            "items_table": True,
            "totals": True,
            "signature": True,
            "company_contacts": True,
        },
    },
    {
        "doc_type": "technichka",
        "name": "Техничка",
        "description": "Склад/площадка: только оборудование, без персонала, логистики и цен.",
        "formats": ["docx", "pdf"],
        "used_fields": [
            "project", "manager", "city", "dates", "shifts",
            "items_table", "logo", "assignee",
        ],
        "show_logo": True,
        "show_company_block": True,
        "custom_title": "",
        "body_notes": "",
        "footer_notes": "",
        "include_sections": {
            "items_table": True,
            "totals": False,
            "signature": False,
            "company_contacts": False,
        },
    },
]

FIELD_LABELS = {
    "project": "Проект",
    "client": "Клиент",
    "client_requisites": "Реквизиты клиента",
    "manager": "Менеджер",
    "city": "Город",
    "dates": "Даты",
    "shifts": "Смены",
    "items_table": "Таблица позиций",
    "totals": "Итоги",
    "margin": "Маржа",
    "subrental": "Субаренда",
    "signature": "Подписи",
    "unit_prices": "Цены за ед.",
    "company_letterhead": "Реквизиты компании",
    "logo": "Логотип",
    "assignee": "Ответственный на объекте",
}


def render_placeholders(text: Optional[str], context: Dict[str, Any]) -> str:
    """Подставляет {{placeholders}} из контекста генерации."""
    if not text:
        return ""

    def _lookup(key: str) -> str:
        key = (key or "").strip()
        if not key:
            return ""
        candidates = _PLACEHOLDER_MAP.get(key)
        if candidates:
            for c in candidates:
                val = context.get(c)
                if val is not None and str(val).strip() != "":
                    return str(val)
        # прямой ключ
        if key in context and context[key] is not None:
            return str(context[key])
        # dotted → nested dict (letterhead.company_name)
        if "." in key:
            cur: Any = context
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            if cur is not None and str(cur).strip() != "":
                return str(cur)
        return ""

    return re.sub(
        r"\{\{\s*([^}]+?)\s*\}\}",
        lambda m: _lookup(m.group(1)),
        str(text),
    )


def _default_sections(doc_type: str) -> Dict[str, bool]:
    for row in DEFAULT_TEMPLATES:
        if row["doc_type"] == doc_type:
            return dict(row["include_sections"])
    return {
        "items_table": True,
        "totals": True,
        "signature": False,
        "company_contacts": True,
    }


def seed_document_templates(db: Session) -> int:
    """Создаёт 5 системных шаблонов, если их ещё нет. Возвращает число добавленных."""
    from database import DocumentTemplate

    added = 0
    for spec in DEFAULT_TEMPLATES:
        exists = (
            db.query(DocumentTemplate)
            .filter(DocumentTemplate.doc_type == spec["doc_type"])
            .first()
        )
        if exists:
            continue
        db.add(DocumentTemplate(
            doc_type=spec["doc_type"],
            name=spec["name"],
            description=spec["description"],
            is_active=True,
            show_logo=spec["show_logo"],
            show_company_block=spec["show_company_block"],
            custom_title=spec.get("custom_title") or "",
            body_notes=spec.get("body_notes") or "",
            footer_notes=spec.get("footer_notes") or "",
            include_sections=dict(spec["include_sections"]),
            formats=list(spec["formats"]),
            used_fields=list(spec["used_fields"]),
        ))
        added += 1
    if added:
        db.commit()
    return added


def template_to_dict(row) -> Dict[str, Any]:
    sections = row.include_sections if isinstance(row.include_sections, dict) else {}
    defaults = _default_sections(row.doc_type)
    merged_sections = {**defaults, **sections}
    formats = row.formats if isinstance(row.formats, list) else ["docx", "pdf"]
    used = row.used_fields if isinstance(row.used_fields, list) else []
    used_labeled = [
        {"key": f, "label": FIELD_LABELS.get(f, f)} for f in used
    ]
    return {
        "id": row.id,
        "doc_type": row.doc_type,
        "name": row.name,
        "description": row.description or "",
        "is_active": bool(row.is_active),
        "show_logo": bool(row.show_logo if row.show_logo is not None else True),
        "show_company_block": bool(
            row.show_company_block if row.show_company_block is not None else True
        ),
        "custom_title": row.custom_title or "",
        "body_notes": row.body_notes or "",
        "footer_notes": row.footer_notes or "",
        "include_sections": merged_sections,
        "formats": formats,
        "used_fields": used,
        "used_fields_labeled": used_labeled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "sample_docx_url": f"/api/document-templates/{row.doc_type}/sample?format=docx",
        "sample_pdf_url": f"/api/document-templates/{row.doc_type}/sample?format=pdf",
    }


def get_template_row(db: Session, doc_type: str):
    from database import DocumentTemplate

    base = (doc_type or "").replace("_pdf", "")
    return (
        db.query(DocumentTemplate)
        .filter(DocumentTemplate.doc_type == base)
        .first()
    )


def apply_template_to_context(
    context: Dict[str, Any],
    db: Optional[Session],
    doc_type: str,
) -> Dict[str, Any]:
    """
    Мержит настройки шаблона в context для document_generator.
    Ключи: tpl_show_logo, tpl_show_company_block, tpl_custom_title,
    tpl_body_notes, tpl_footer_notes, tpl_include_*.
    """
    ctx = dict(context or {})
    defaults = next(
        (d for d in DEFAULT_TEMPLATES if d["doc_type"] == (doc_type or "").replace("_pdf", "")),
        None,
    )
    show_logo = True
    show_company = True
    custom_title = ""
    body_notes = ""
    footer_notes = ""
    sections = _default_sections((doc_type or "").replace("_pdf", ""))

    row = None
    if db is not None:
        try:
            row = get_template_row(db, doc_type)
        except Exception:
            row = None

    if row:
        show_logo = bool(row.show_logo if row.show_logo is not None else True)
        show_company = bool(
            row.show_company_block if row.show_company_block is not None else True
        )
        custom_title = row.custom_title or ""
        body_notes = row.body_notes or ""
        footer_notes = row.footer_notes or ""
        if isinstance(row.include_sections, dict):
            sections = {**sections, **row.include_sections}
    elif defaults:
        show_logo = defaults["show_logo"]
        show_company = defaults["show_company_block"]
        custom_title = defaults.get("custom_title") or ""
        body_notes = defaults.get("body_notes") or ""
        footer_notes = defaults.get("footer_notes") or ""
        sections = dict(defaults["include_sections"])

    # grand_total_text для плейсхолдеров
    if "grand_total_text" not in ctx and ctx.get("grand_total") is not None:
        try:
            from document_generator import get_rubles_text
            ctx["grand_total_text"] = get_rubles_text(float(ctx.get("grand_total") or 0))
        except Exception:
            ctx["grand_total_text"] = str(ctx.get("grand_total") or "")

    ctx["tpl_show_logo"] = show_logo
    ctx["tpl_show_company_block"] = show_company
    ctx["tpl_custom_title"] = render_placeholders(custom_title, ctx).strip()
    ctx["tpl_body_notes"] = render_placeholders(body_notes, ctx).strip()
    ctx["tpl_footer_notes"] = render_placeholders(footer_notes, ctx).strip()
    ctx["tpl_include_items_table"] = bool(sections.get("items_table", True))
    ctx["tpl_include_totals"] = bool(sections.get("totals", True))
    ctx["tpl_include_signature"] = bool(sections.get("signature", False))
    ctx["tpl_include_company_contacts"] = bool(sections.get("company_contacts", True))

    if not show_logo:
        ctx["logo_path"] = None
        ctx["tpl_force_no_logo"] = True

    return ctx


def sample_context(doc_type: str) -> Dict[str, Any]:
    """Демо-контекст для превью / sample download."""
    base = {
        "number": "SAMPLE-1",
        "date": "01.08.2026",
        "contract_number": "SAMPLE-1",
        "contract_date": "01.08.2026",
        "company_name": "ТОО «Пример Клиент»",
        "director_name": "Иванов И.И.",
        "iin_bin": "123456789012",
        "iban": "KZ00 0000 0000 0000",
        "based_on": "Устава",
        "company_address": "г. Алматы, пр. Пример, 1",
        "bank_name": "Пример Банк",
        "kbe": "17",
        "bik": "EXAMPLE",
        "event_name": "Демо-мероприятие",
        "project_name": "Демо-мероприятие",
        "contact_name": "Айгуль С.",
        "manager_name": "Менеджер Проекта",
        "project_manager_name": "Менеджер Проекта",
        "sales_manager_name": "Менеджер Продаж",
        "city": "Алматы",
        "event_address": "Алматы Арена",
        "event_date": "15.08.2026",
        "departure_date": "14.08.2026",
        "return_date": "16.08.2026",
        "rent_period": "14.08.2026 — 16.08.2026",
        "shifts": 2,
        "shifts_label": "2",
        "assignee_name": "Техник Склада",
        "our_company_name": "Intro Show",
        "our_company_phone": "+7 (701) 554-13-80",
        "our_company_email": "show.intro@yandex.kz",
        "our_company_address": "Тюлькубасская улица, 4, Алматы",
        "our_company_bin": "",
        "manager_phone": "+7 (701) 554-13-80",
        "items": [
            {
                "name": "Светодиодный экран P3.9",
                "category": "Свет / LED",
                "quantity": 2,
                "days": 2,
                "price": 150000,
                "cost_price": 0,
                "line_total_base": 600000,
                "line_total_discounted": 600000,
                "warehouse_type": "own",
            },
            {
                "name": "Кабель силовой",
                "category": "Кабель",
                "quantity": 10,
                "days": 2,
                "price": 2000,
                "cost_price": 0,
                "line_total_base": 40000,
                "line_total_discounted": 40000,
                "warehouse_type": "own",
            },
            {
                "name": "Техник",
                "category": "Персонал",
                "quantity": 1,
                "days": 2,
                "price": 50000,
                "cost_price": 0,
                "line_total_base": 100000,
                "line_total_discounted": 100000,
                "warehouse_type": "own",
            },
        ],
        "equipment_base": 640000,
        "equipment_total": 640000,
        "fixed_total": 100000,
        "discount_amount": 0,
        "after_discount": 740000,
        "tax_percentage": 16,
        "tax_amount": 118400,
        "grand_total": 858400,
        "cost_total": 0,
        "margin": 858400,
        "discount_percentage": 0,
        "hide_subrental_section": doc_type.startswith("estimate_client"),
    }
    return base
