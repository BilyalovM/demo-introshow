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
        "preview_url": f"/api/document-templates/{row.doc_type}/preview.html",
        "preview_pdf_url": f"/api/document-templates/{row.doc_type}/preview.pdf?inline=1",
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
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Мержит настройки шаблона в context для document_generator.
    Ключи: tpl_show_logo, tpl_show_company_block, tpl_custom_title,
    tpl_body_notes, tpl_footer_notes, tpl_include_*.

    overrides — сессионные правки превью (custom_title / body_notes / footer_notes
    и опционально флаги show_logo / show_company_block / include_sections).
    """
    ctx = dict(context or {})
    overrides = overrides or {}
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

    if "custom_title" in overrides and overrides["custom_title"] is not None:
        custom_title = str(overrides["custom_title"])
    if "body_notes" in overrides and overrides["body_notes"] is not None:
        body_notes = str(overrides["body_notes"])
    if "footer_notes" in overrides and overrides["footer_notes"] is not None:
        footer_notes = str(overrides["footer_notes"])
    if "show_logo" in overrides and overrides["show_logo"] is not None:
        show_logo = bool(overrides["show_logo"])
    if "show_company_block" in overrides and overrides["show_company_block"] is not None:
        show_company = bool(overrides["show_company_block"])
    if isinstance(overrides.get("include_sections"), dict):
        sections = {**sections, **overrides["include_sections"]}

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
    # сырые значения для формы превью (с плейсхолдерами)
    ctx["tpl_custom_title_raw"] = custom_title or ""
    ctx["tpl_body_notes_raw"] = body_notes or ""
    ctx["tpl_footer_notes_raw"] = footer_notes or ""
    ctx["tpl_include_items_table"] = bool(sections.get("items_table", True))
    ctx["tpl_include_totals"] = bool(sections.get("totals", True))
    ctx["tpl_include_signature"] = bool(sections.get("signature", False))
    ctx["tpl_include_company_contacts"] = bool(sections.get("company_contacts", True))

    if not show_logo:
        ctx["logo_path"] = None
        ctx["tpl_force_no_logo"] = True

    return ctx


ALLOWED_CONTEXT_OVERRIDE_KEYS = frozenset({
    "event_name",
    "project_name",
    "company_name",
    "manager_name",
    "project_manager_name",
    "sales_manager_name",
    "contact_name",
    "city",
    "event_address",
    "event_date",
    "departure_date",
    "return_date",
    "rent_period",
    "shifts",
    "shifts_label",
    "number",
    "date",
    "contract_number",
    "contract_date",
})


def _parse_num(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    if not s or s in ("—", "-", "–"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def apply_preview_data_overrides(
    context: Dict[str, Any],
    context_overrides: Optional[Dict[str, Any]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Сессионные правки превью: поля шапки + позиции.
    Пересчитывает суммы строк и итог на сервере при изменении items.
    """
    ctx = dict(context or {})
    overrides = context_overrides or {}
    if overrides:
        for key, val in overrides.items():
            if key in ALLOWED_CONTEXT_OVERRIDE_KEYS and val is not None:
                ctx[key] = val
        if "event_name" in overrides and "project_name" not in overrides:
            ctx["project_name"] = overrides["event_name"]
        if "project_name" in overrides and "event_name" not in overrides:
            ctx["event_name"] = overrides["project_name"]
        if "manager_name" in overrides and "project_manager_name" not in overrides:
            ctx["project_manager_name"] = overrides["manager_name"]
        if "shifts" in overrides and "shifts_label" not in overrides:
            try:
                sh = float(overrides["shifts"])
                ctx["shifts"] = sh
                ctx["shifts_label"] = str(int(sh)) if sh == int(sh) else str(sh)
            except (TypeError, ValueError):
                ctx["shifts_label"] = str(overrides["shifts"])
        elif "shifts_label" in overrides and "shifts" not in overrides:
            try:
                ctx["shifts"] = float(str(overrides["shifts_label"]).replace(",", "."))
            except (TypeError, ValueError):
                pass
        dep = ctx.get("departure_date") or ""
        ret = ctx.get("return_date") or ctx.get("event_date") or ""
        if dep or ret:
            ctx["rent_period"] = f"{dep or '—'} — {ret or '—'}"

    if items is not None:
        base_items = list(ctx.get("items") or [])
        merged: List[Dict[str, Any]] = []
        for i, patch in enumerate(items):
            patch = patch or {}
            base = dict(base_items[i]) if i < len(base_items) else {}
            if "name" in patch and patch["name"] is not None:
                base["name"] = str(patch["name"])
            if "quantity" in patch or "qty" in patch:
                raw_q = patch["quantity"] if "quantity" in patch else patch.get("qty")
                base["quantity"] = max(1, int(_parse_num(raw_q, 1)))
            if "days" in patch and patch["days"] is not None:
                base["days"] = max(1, int(_parse_num(patch["days"], 1)))
            if "price" in patch and patch["price"] is not None:
                base["price"] = _parse_num(patch["price"], 0)
            if not base.get("category_type"):
                base["category_type"] = "equipment"
            if not base.get("warehouse_type"):
                base["warehouse_type"] = "own"
            merged.append(base)

        from calculator import calculate_estimate

        discount = float(ctx.get("discount_percentage") or 0)
        tax = ctx.get("tax_percentage")
        result = calculate_estimate(merged, discount, tax)
        ctx["items"] = result["items"]
        ctx["equipment_base"] = result.get("equipment_base", 0)
        ctx["equipment_total"] = result.get("equipment_total", 0)
        ctx["fixed_total"] = result.get("fixed_total", 0)
        ctx["discount_amount"] = result.get("discount_amount", 0)
        ctx["after_discount"] = result.get("after_discount", 0)
        ctx["tax_percentage"] = result.get("tax_percentage", tax)
        ctx["tax_amount"] = result.get("tax_amount", 0)
        ctx["grand_total"] = result.get("grand_total", 0)
        ctx["cost_total"] = result.get("cost_total", 0)
        ctx["margin"] = result.get("margin", 0)
        try:
            from document_generator import get_rubles_text

            ctx["grand_total_text"] = get_rubles_text(float(ctx.get("grand_total") or 0))
        except Exception:
            ctx["grand_total_text"] = str(ctx.get("grand_total") or "")

    return ctx


def build_html_preview(context: Dict[str, Any], doc_type: str) -> str:
    """Структурированный HTML-превью с contenteditable-зонами (data-field)."""
    import html as html_mod

    def esc(v: Any) -> str:
        return html_mod.escape(str(v if v is not None else ""))

    def ed(field: str, value: Any, extra_class: str = "") -> str:
        cls = f"pv-ed {extra_class}".strip()
        return (
            f'<span class="{cls}" contenteditable="true" data-field="{esc(field)}" '
            f'spellcheck="false">{esc(value)}</span>'
        )

    def ed_block(field: str, value: Any, extra_class: str = "") -> str:
        cls = f"pv-ed pv-ed-block {extra_class}".strip()
        text = esc(value).replace("\n", "<br>")
        return (
            f'<div class="{cls}" contenteditable="true" data-field="{esc(field)}" '
            f'spellcheck="false">{text}</div>'
        )

    title = (
        context.get("tpl_custom_title")
        or {
            "estimate_internal": "Смета внутренняя",
            "estimate_client": "Смета клиенту (без цен)",
            "estimate_client_priced": "Смета клиенту (с ценами)",
            "contract": "Договор / спецификация",
            "technichka": "Техничка",
        }.get(doc_type, "Документ")
    )
    company = context.get("our_company_name") or "Intro Show"
    client = context.get("company_name") or ""
    project = context.get("project_name") or context.get("event_name") or ""
    number = context.get("number") or context.get("contract_number") or ""
    date = context.get("date") or context.get("contract_date") or ""
    city = context.get("city") or ""
    address = context.get("event_address") or ""
    event_date = context.get("event_date") or ""
    departure = context.get("departure_date") or ""
    return_date = context.get("return_date") or ""
    manager = context.get("manager_name") or ""
    shifts = context.get("shifts_label")
    if shifts is None or shifts == "":
        shifts = context.get("shifts")
        shifts = "" if shifts is None else str(shifts)
    body_notes = context.get("tpl_body_notes") or ""
    footer_notes = context.get("tpl_footer_notes") or ""
    show_company = context.get("tpl_show_company_block", True)
    show_items = context.get("tpl_include_items_table", True)
    show_totals = context.get("tpl_include_totals", True)
    show_contacts = context.get("tpl_include_company_contacts", True)

    num_field = "contract_number" if doc_type == "contract" else "number"
    date_field = "contract_date" if doc_type == "contract" else "date"

    meta_rows = [
        f"<div><span>№</span> {ed(num_field, number)}</div>",
        f"<div><span>Дата</span> {ed(date_field, date)}</div>",
        f"<div><span>Проект</span> {ed('event_name', project)}</div>",
        f"<div><span>Клиент</span> {ed('company_name', client)}</div>",
        f"<div><span>Менеджер</span> {ed('manager_name', manager)}</div>",
        f"<div><span>Город</span> {ed('city', city)}</div>",
        f"<div><span>Адрес</span> {ed('event_address', address)}</div>",
        f"<div><span>Мероприятие</span> {ed('event_date', event_date)}</div>",
        f"<div><span>Выезд</span> {ed('departure_date', departure)}</div>",
        f"<div><span>Возврат</span> {ed('return_date', return_date)}</div>",
        f"<div><span>Смены</span> {ed('shifts_label', shifts)}</div>",
    ]

    def fmt_money(v: Any) -> str:
        if v is None:
            return ""
        try:
            return f"{float(v):,.0f}".replace(",", " ")
        except (TypeError, ValueError):
            return str(v)

    items_html = ""
    if show_items:
        rows = []
        for i, it in enumerate(context.get("items") or []):
            idx = i  # 0-based for data-field
            name = it.get("name") or ""
            qty = it.get("quantity") if it.get("quantity") is not None else ""
            days = it.get("days") if it.get("days") is not None else ""
            price = it.get("price")
            line = it.get("line_total_discounted")
            if line is None:
                line = it.get("line_total_base")
            name_ed = ed(f"items.{idx}.name", name)
            qty_ed = ed(f"items.{idx}.quantity", qty, "num")
            days_ed = ed(f"items.{idx}.days", days, "num")
            price_ed = ed(f"items.{idx}.price", fmt_money(price), "num")
            line_ed = ed(f"items.{idx}.line_sum", fmt_money(line), "num")
            if doc_type == "technichka":
                rows.append(
                    f"<tr data-item-index='{idx}'><td>{idx + 1}</td><td>{name_ed}</td>"
                    f"<td class='num'>{qty_ed}</td></tr>"
                )
            elif doc_type == "estimate_client":
                rows.append(
                    f"<tr data-item-index='{idx}'><td>{idx + 1}</td><td>{name_ed}</td>"
                    f"<td class='num'>{qty_ed}</td><td class='num'>{days_ed}</td>"
                    f"<td class='num'>{line_ed}</td></tr>"
                )
            else:
                rows.append(
                    f"<tr data-item-index='{idx}'><td>{idx + 1}</td><td>{name_ed}</td>"
                    f"<td class='num'>{qty_ed}</td><td class='num'>{days_ed}</td>"
                    f"<td class='num'>{price_ed}</td><td class='num'>{line_ed}</td></tr>"
                )
        if doc_type == "technichka":
            head = "<tr><th>№</th><th>Наименование</th><th>Кол-во</th></tr>"
        elif doc_type == "estimate_client":
            head = "<tr><th>№</th><th>Наименование</th><th>Кол-во</th><th>Смены</th><th>Сумма</th></tr>"
        else:
            head = (
                "<tr><th>№</th><th>Наименование</th><th>Кол-во</th>"
                "<th>Смены</th><th>Цена</th><th>Сумма</th></tr>"
            )
        items_html = (
            f"<table class='pv-items'><thead>{head}</thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    totals_html = ""
    if show_totals and doc_type != "technichka":
        gt = context.get("grand_total")
        gt_s = fmt_money(gt) if gt is not None else "—"
        totals_html = (
            f"<div class='pv-totals' data-field='grand_total'>"
            f"<strong>Итого:</strong> {esc(gt_s)} ₸</div>"
        )

    contacts = ""
    if show_company or show_contacts:
        phone = context.get("our_company_phone") or ""
        email = context.get("our_company_email") or ""
        addr = context.get("our_company_address") or ""
        contacts = (
            f"<div class='pv-contacts'><div class='pv-brand'>{esc(company)}</div>"
            f"<div>{esc(phone)}</div><div>{esc(email)}</div><div>{esc(addr)}</div></div>"
        )

    notes_body = (
        f"<div class='pv-notes-label'>Примечания</div>"
        f"{ed_block('body_notes', body_notes, 'pv-notes')}"
    )
    notes_footer = (
        f"<div class='pv-notes-label'>Футер</div>"
        f"{ed_block('footer_notes', footer_notes, 'pv-footer-notes')}"
    )

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(title)}</title>
<style>
body {{ margin:0; padding:24px; font-family: 'DM Sans', system-ui, sans-serif;
  color:#1a1a1a; background:#fff; font-size:14px; line-height:1.45; }}
.pv-hint-bar {{
  font-size:12px; color:#6b7280; margin:0 0 14px; padding:8px 10px;
  background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px;
}}
.pv-title {{ font-size:22px; font-weight:700; margin:0 0 12px; outline:none; }}
.pv-meta {{ display:grid; grid-template-columns:1fr 1fr; gap:8px 16px; margin-bottom:16px; }}
.pv-meta > div > span:first-child {{
  color:#6b7280; font-size:12px; font-weight:600; margin-right:6px; display:inline-block; min-width:72px;
}}
.pv-notes-label {{ font-size:11px; font-weight:700; color:#6b7280; text-transform:uppercase;
  letter-spacing:.04em; margin:12px 0 4px; }}
.pv-notes, .pv-footer-notes {{ margin:0 0 14px; white-space:pre-wrap; color:#374151; min-height:1.4em; }}
.pv-items {{ width:100%; border-collapse:collapse; margin:12px 0; }}
.pv-items th, .pv-items td {{ border-bottom:1px solid #e5e7eb; padding:8px 6px; text-align:left; vertical-align:top; }}
.pv-items th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:#6b7280; }}
.pv-items .num {{ text-align:right; white-space:nowrap; }}
.pv-totals {{ margin-top:14px; font-size:16px; text-align:right; }}
.pv-contacts {{ margin-top:24px; padding-top:14px; border-top:1px solid #e5e7eb; color:#4b5563; font-size:13px; }}
.pv-brand {{ font-weight:700; color:#111; margin-bottom:4px; }}
.pv-ed {{
  display:inline-block; min-width:1.5em; min-height:1.2em; outline:none;
  border:1px dashed transparent; border-radius:4px; padding:1px 4px; margin:-1px -4px;
  transition: border-color .15s, background .15s, box-shadow .15s;
  cursor:text;
}}
.pv-ed:hover {{ border-color:#cbd5e1; background:#f8fafc; }}
.pv-ed:focus {{
  border-color:#e25a3c; background:#fff7f5;
  box-shadow:0 0 0 2px rgba(226,90,60,.18);
}}
.pv-ed-block {{
  display:block; width:100%; box-sizing:border-box; padding:8px 10px; margin:0;
  border:1px dashed #d1d5db; border-radius:8px; min-height:48px;
}}
.pv-ed-block:hover {{ border-color:#94a3b8; }}
.pv-ed-block:focus {{
  border-color:#e25a3c; background:#fff7f5;
  box-shadow:0 0 0 2px rgba(226,90,60,.18);
}}
.pv-ed.num {{ min-width:3em; text-align:right; }}
@media (max-width:640px) {{ .pv-meta {{ grid-template-columns:1fr; }} body {{ padding:14px; }} }}
</style></head><body>
<p class="pv-hint-bar">Кликните, чтобы изменить · правки сессионные, затем «Применить» или уход с поля</p>
{contacts if show_company else ''}
<h1 class="pv-title pv-ed" contenteditable="true" data-field="custom_title" spellcheck="false">{esc(title)}</h1>
<div class="pv-meta">{''.join(meta_rows)}</div>
{notes_body}
{items_html}
{totals_html}
{notes_footer}
{contacts if (show_contacts and not show_company) else ''}
</body></html>"""


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
