import os
from docxtpl import DocxTemplate
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from num2words import num2words
from typing import Dict, Any


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


def generate_technichka_docx(context: Dict[str, Any], output_path: str) -> str:
    """Техничка для склада/персонала: позиции и количества без цен."""
    doc = Document()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"ТЕХНИЧКА № {context.get('number', '')} от {context.get('date', '')}")
    run.bold = True
    run.font.size = Pt(16)

    meta_lines = []
    if context.get("company_name"):
        meta_lines.append(f"Заказчик: {context['company_name']}")
    if context.get("event_name"):
        meta_lines.append(f"Мероприятие: {context['event_name']}")
    if context.get("event_address"):
        meta_lines.append(f"Адрес: {context['event_address']}")
    if context.get("rent_period"):
        meta_lines.append(f"Период: {context['rent_period']}")
    if context.get("assignee_name"):
        meta_lines.append(f"Ответственный на объекте: {context['assignee_name']}")
    for line in meta_lines:
        doc.add_paragraph(line)

    items = context.get("items", [])
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    headers = ["№", "Наименование", "Кол-во", "Дней / смен"]
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True

    for idx, item in enumerate(items, 1):
        row = table.add_row().cells
        row[0].text = str(idx)
        row[1].text = str(item.get("name", ""))
        row[2].text = str(item.get("quantity", 1))
        row[3].text = str(item.get("days", 1))

    note = doc.add_paragraph()
    note.add_run("Цены скрыты. Документ для склада и выездного персонала.").italic = True

    doc.save(output_path)
    return output_path


def generate_estimate_docx(context: Dict[str, Any], output_path: str) -> str:
    """Генерирует смету (.docx) с таблицей позиций и итогами."""
    doc = Document()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"СМЕТА № {context.get('number', '')} от {context.get('date', '')}")
    run.bold = True
    run.font.size = Pt(16)

    meta_lines = []
    if context.get("company_name"):
        meta_lines.append(f"Заказчик: {context['company_name']}")
    if context.get("event_name"):
        meta_lines.append(f"Мероприятие: {context['event_name']}")
    if context.get("event_address"):
        meta_lines.append(f"Адрес: {context['event_address']}")
    if context.get("rent_period"):
        meta_lines.append(f"Период аренды: {context['rent_period']}")
    for line in meta_lines:
        doc.add_paragraph(line)

    items = context.get("items", [])
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    headers = ["№", "Наименование", "Цена", "Кол-во", "Дней", "Сумма"]
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True

    for idx, item in enumerate(items, 1):
        row = table.add_row().cells
        row[0].text = str(idx)
        row[1].text = str(item.get("name", ""))
        row[2].text = _fmt_money(item.get("price", 0))
        row[3].text = str(item.get("quantity", 1))
        row[4].text = str(item.get("days", 1))
        row[5].text = _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0)))

    doc.add_paragraph("")
    totals = [
        ("Оборудование (со скидкой)", context.get("equipment_total", 0)),
        ("Логистика и персонал", context.get("fixed_total", 0)),
    ]
    if context.get("discount_percentage"):
        totals.insert(0, (f"Скидка на оборудование: {context['discount_percentage']:.0f}%", None))
    for label, value in totals:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.add_run(label + (f": {_fmt_money(value)}" if value is not None else ""))

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run(f"ИТОГО: {_fmt_money(context.get('grand_total', 0))}")
    run.bold = True
    run.font.size = Pt(14)

    p = doc.add_paragraph(get_rubles_text(context.get("grand_total", 0)))
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.save(output_path)
    return output_path
