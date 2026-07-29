import os
from docxtpl import DocxTemplate
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from num2words import num2words
from typing import Dict, Any, List, Optional


def resolve_photo_path(photo_url: str) -> str:
    if photo_url.startswith("/uploads/"):
        uploads_dir = os.environ.get("RENTAL_UPLOADS_DIR", "uploads")
        return os.path.join(uploads_dir, os.path.basename(photo_url))
    return photo_url.lstrip("/")

def get_rubles_text(amount: float) -> str:
    """
    Converts a number to Russian text.
    Handles the integer part and adds tiyn (decimals) for tenge.
    """
    try:
        integer_part = int(amount)
        decimal_part = int(round((amount - integer_part) * 100))
        
        text = num2words(integer_part, lang='ru')
        
        # format decimal part to always have 2 digits
        decimal_str = f"{decimal_part:02d}"
        
        return f"{text.capitalize()} тенге {decimal_str} тиын"
    except Exception as e:
        return f"{amount:,.2f} тенге"

def generate_contract(context: Dict[str, Any], template_path: str, output_path: str) -> str:
    """
    Generates a docx contract by injecting context into a template.
    Returns the path to the generated document.
    """
    doc = DocxTemplate(template_path)
    
    # Calculate the total in text
    total_cost = context.get('grand_total', 0.0)
    context['grand_total_text'] = get_rubles_text(total_cost)
    
    doc.render(context)
    doc.save(output_path)
    
    # Append appendix for photos and descriptions if they exist
    items = context.get('items', [])
    has_appendix = any(i.get('photo_url') or i.get('description') for i in items)
    
    if has_appendix:
        append_doc = Document(output_path)
        append_doc.add_page_break()
        append_doc.add_heading('Приложение: Спецификация оборудования', level=1)
        
        for item in items:
            if item.get('photo_url') or item.get('description'):
                append_doc.add_heading(item['name'], level=2)
                if item.get('description'):
                    append_doc.add_paragraph(item['description'])
                if item.get('photo_url'):
                    img_path = resolve_photo_path(item['photo_url'])
                    if os.path.exists(img_path):
                        append_doc.add_picture(img_path, width=Inches(3))
                        
        append_doc.save(output_path)
    
    return output_path


def _fmt_money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₸"


def _add_meta(doc: Document, context: Dict[str, Any], with_assignee: bool = False) -> None:
    """Шапка как в Excel: проект, контакт, менеджер, город, выезд/возврат, смены."""
    meta_lines = []
    project = context.get("project_name") or context.get("event_name")
    if project:
        meta_lines.append(f"Наименование проекта: {project}")
    if context.get("company_name"):
        meta_lines.append(f"Заказчик: {context['company_name']}")
    if context.get("contact_name"):
        meta_lines.append(f"Контактное лицо: {context['contact_name']}")
    if context.get("manager_name"):
        meta_lines.append(f"Менеджер: {context['manager_name']}")
    if context.get("city"):
        meta_lines.append(f"Город: {context['city']}")
    if context.get("event_address"):
        meta_lines.append(f"Адрес / площадка: {context['event_address']}")

    depart = context.get("departure_date") or ""
    ret = context.get("return_date") or ""
    if depart or ret:
        meta_lines.append(f"Выезд оборудования: {depart or '—'}")
        meta_lines.append(f"Возврат оборудования: {ret or '—'}")
    elif context.get("rent_period"):
        meta_lines.append(f"Период аренды: {context['rent_period']}")

    shifts_label = context.get("shifts_label")
    if shifts_label is None and context.get("shifts") is not None:
        s = context["shifts"]
        try:
            sf = float(s)
            shifts_label = str(int(sf)) if sf == int(sf) else str(sf)
        except (TypeError, ValueError):
            shifts_label = str(s)
    if shifts_label:
        meta_lines.append(f"Количество смен / дней: {shifts_label}")

    if with_assignee and context.get("assignee_name"):
        meta_lines.append(f"Ответственный на объекте: {context['assignee_name']}")
    for line in meta_lines:
        doc.add_paragraph(line)


def _add_items_table(
    doc: Document,
    items: List[Dict[str, Any]],
    with_prices: bool = True,
    name_suffix_fn=None,
) -> None:
    cols = 6 if with_prices else 4
    table = doc.add_table(rows=1, cols=cols)
    table.style = "Table Grid"
    headers = (
        ["№", "Наименование", "Цена", "Кол-во", "Дней", "Сумма"]
        if with_prices
        else ["№", "Наименование", "Кол-во", "Дней / смен"]
    )
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True

    for idx, item in enumerate(items, 1):
        row = table.add_row().cells
        name = str(item.get("name", ""))
        if name_suffix_fn:
            extra = name_suffix_fn(item)
            if extra:
                name = f"{name}{extra}"
        row[0].text = str(idx)
        row[1].text = name
        if with_prices:
            row[2].text = _fmt_money(item.get("price", 0))
            row[3].text = str(item.get("quantity", 1))
            row[4].text = str(item.get("days", 1))
            row[5].text = _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0)))
        else:
            row[2].text = str(item.get("quantity", 1))
            row[3].text = str(item.get("days", 1))


def _add_right_line(doc: Document, label: str, value: Optional[float] = None, bold: bool = False, size: int = 11) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    text = label if value is None else f"{label}: {_fmt_money(value)}"
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)


def generate_technichka_docx(context: Dict[str, Any], output_path: str) -> str:
    """Техничка для склада/персонала: позиции и количества без цен."""
    doc = Document()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"ТЕХНИЧКА № {context.get('number', '')} от {context.get('date', '')}")
    run.bold = True
    run.font.size = Pt(16)

    _add_meta(doc, context, with_assignee=True)

    items = context.get("items", [])
    _add_items_table(doc, items, with_prices=False)

    note = doc.add_paragraph()
    note.add_run("Цены скрыты. Документ для склада и выездного персонала.").italic = True

    doc.save(output_path)
    return output_path


def generate_estimate_docx(
    context: Dict[str, Any],
    output_path: str,
    mode: str = "internal",
) -> str:
    """Генерирует смету (.docx).

    mode:
      - internal («Для нас»): все позиции, блок субаренды, себестоимость и маржа
      - client («Клиентская»): без строк субаренды, без себестоимости/маржи
    """
    mode = (mode or "internal").strip().lower()
    if mode not in ("internal", "client"):
        mode = "internal"

    doc = Document()

    if mode == "client":
        title_text = f"СМЕТА № {context.get('number', '')} от {context.get('date', '')}"
    else:
        title_text = f"СМЕТА (ВНУТРЕННЯЯ) № {context.get('number', '')} от {context.get('date', '')}"

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(title_text)
    run.bold = True
    run.font.size = Pt(16)

    _add_meta(doc, context)

    all_items = list(context.get("items", []) or [])
    if mode == "client":
        main_items = [i for i in all_items if (i.get("warehouse_type") or "own") != "subrental"]
        sub_items = []
    else:
        main_items = [i for i in all_items if (i.get("warehouse_type") or "own") != "subrental"]
        sub_items = [i for i in all_items if (i.get("warehouse_type") or "own") == "subrental"]
        # Если в контексте уже отфильтровали — fallback: все как основные
        if not main_items and not sub_items and all_items:
            main_items = all_items

    if main_items:
        if mode == "internal" and sub_items:
            h = doc.add_paragraph()
            h.add_run("Оборудование и услуги").bold = True
        _add_items_table(
            doc,
            main_items,
            with_prices=True,
            name_suffix_fn=None,
        )
    elif mode == "client":
        doc.add_paragraph("Нет позиций для клиентской сметы.")

    if mode == "internal" and sub_items:
        doc.add_paragraph("")
        h = doc.add_paragraph()
        run = h.add_run("Субаренда (только для нас)")
        run.bold = True
        note = doc.add_paragraph()
        note.add_run("Раздел не попадает в клиентскую смету.").italic = True

        table = doc.add_table(rows=1, cols=7)
        table.style = "Table Grid"
        headers = ["№", "Наименование", "Поставщик", "Цена клиенту", "Себест.", "Кол-во × дней", "Сумма"]
        for i, htxt in enumerate(headers):
            cell = table.rows[0].cells[i]
            cell.text = htxt
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
        for idx, item in enumerate(sub_items, 1):
            row = table.add_row().cells
            qty = item.get("quantity", 1)
            days = item.get("days", 1)
            row[0].text = str(idx)
            row[1].text = str(item.get("name", ""))
            row[2].text = str(item.get("supplier") or "—")
            row[3].text = _fmt_money(item.get("price", 0))
            row[4].text = _fmt_money(item.get("cost_price", 0))
            row[5].text = f"{qty} × {days}"
            row[6].text = _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0)))

    doc.add_paragraph("")

    disc_pct = float(context.get("discount_percentage") or 0)
    tax_pct = float(context.get("tax_percentage") or 0)
    eq_base = context.get("equipment_base")
    if eq_base is None:
        eq_base = context.get("equipment_total", 0)
    eq_total = context.get("equipment_total", 0)
    fixed_total = context.get("fixed_total", 0)
    discount_amount = context.get("discount_amount")
    if discount_amount is None and disc_pct:
        discount_amount = float(eq_base) - float(eq_total)
    after_discount = context.get("after_discount")
    if after_discount is None:
        after_discount = float(eq_total) + float(fixed_total)
    tax_amount = context.get("tax_amount") or 0
    grand = context.get("grand_total", 0)

    _add_right_line(doc, "Оборудование (до скидки)", float(eq_base))
    if disc_pct:
        _add_right_line(doc, f"Скидка на оборудование {disc_pct:.0f}%", -float(discount_amount or 0))
    _add_right_line(doc, "Оборудование (со скидкой)", float(eq_total))
    _add_right_line(doc, "Логистика и персонал", float(fixed_total))
    _add_right_line(doc, "После скидки", float(after_discount))
    if tax_pct or tax_amount:
        _add_right_line(doc, f"Налог {tax_pct:.0f}%", float(tax_amount))
    _add_right_line(doc, "ИТОГО", float(grand), bold=True, size=14)

    p = doc.add_paragraph(get_rubles_text(float(grand or 0)))
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    if mode == "internal":
        cost_total = float(context.get("cost_total") or 0)
        margin = context.get("margin")
        if margin is None:
            margin = float(grand or 0) - cost_total
        doc.add_paragraph("")
        h = doc.add_paragraph()
        h.add_run("Маржа (внутренний блок)").bold = True
        _add_right_line(doc, "Себестоимость субаренды", cost_total)
        _add_right_line(doc, "Маржа", float(margin), bold=True)

    doc.save(output_path)
    return output_path
