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


# ---------------------------------------------------------------------------
# PDF (fpdf2) — без системных зависимостей, работает на Vercel / Linux
# ---------------------------------------------------------------------------

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")


def _pdf_font_paths():
    regular = os.path.join(_FONTS_DIR, "DejaVuSans.ttf")
    bold = os.path.join(_FONTS_DIR, "DejaVuSans-Bold.ttf")
    return regular, bold


class _EstimatePDF:
    """Минимальная обёртка над FPDF с кириллицей (DejaVu)."""

    def __init__(self):
        from fpdf import FPDF

        self.pdf = FPDF(orientation="P", unit="mm", format="A4")
        self.pdf.set_auto_page_break(auto=True, margin=15)
        regular, bold = _pdf_font_paths()
        if not os.path.exists(regular):
            raise FileNotFoundError(f"PDF font missing: {regular}")
        self.pdf.add_font("DejaVu", "", regular)
        if os.path.exists(bold):
            self.pdf.add_font("DejaVu", "B", bold)
        else:
            self.pdf.add_font("DejaVu", "B", regular)
        self.pdf.add_page()
        self.pdf.set_font("DejaVu", size=10)

    def _reset_x(self):
        self.pdf.set_x(self.pdf.l_margin)

    def title(self, text: str):
        self._reset_x()
        self.pdf.set_font("DejaVu", "B", 14)
        self.pdf.multi_cell(0, 8, text, align="C")
        self._reset_x()
        self.pdf.ln(2)
        self.pdf.set_font("DejaVu", size=10)

    def line(self, text: str, bold: bool = False):
        self._reset_x()
        self.pdf.set_font("DejaVu", "B" if bold else "", 10)
        self.pdf.multi_cell(0, 5, text)
        self._reset_x()
        self.pdf.set_font("DejaVu", size=10)

    def right(self, text: str, bold: bool = False, size: int = 10):
        self._reset_x()
        self.pdf.set_font("DejaVu", "B" if bold else "", size)
        self.pdf.cell(0, 6, text, align="R", new_x="LMARGIN", new_y="NEXT")
        self.pdf.set_font("DejaVu", size=10)

    def table(self, headers: List[str], rows: List[List[str]], col_widths: List[float]):
        self._reset_x()
        usable = self.pdf.w - self.pdf.l_margin - self.pdf.r_margin
        total_w = sum(col_widths)
        if total_w > usable and total_w > 0:
            scale = usable / total_w
            col_widths = [w * scale for w in col_widths]
        self.pdf.set_font("DejaVu", "B", 8)
        for i, h in enumerate(headers):
            self.pdf.cell(col_widths[i], 6, h[:40], border=1)
        self.pdf.ln()
        self._reset_x()
        self.pdf.set_font("DejaVu", size=8)
        for row in rows:
            # Высота строки: одна строка текста (обрезаем длинные значения)
            cells = [str(c)[:80] for c in row]
            row_h = 6
            x0 = self.pdf.l_margin
            y0 = self.pdf.get_y()
            if y0 + row_h > self.pdf.h - 15:
                self.pdf.add_page()
                self._reset_x()
                y0 = self.pdf.get_y()
            for i, cell in enumerate(cells):
                x = x0 + sum(col_widths[:i])
                self.pdf.set_xy(x, y0)
                self.pdf.cell(col_widths[i], row_h, cell, border=1)
            self.pdf.set_xy(x0, y0 + row_h)
        self._reset_x()
        self.pdf.set_font("DejaVu", size=10)

    def save(self, path: str):
        self.pdf.output(path)


def _pdf_meta_lines(context: Dict[str, Any], with_assignee: bool = False) -> List[str]:
    lines = []
    project = context.get("project_name") or context.get("event_name")
    if project:
        lines.append(f"Наименование проекта: {project}")
    if context.get("company_name"):
        lines.append(f"Заказчик: {context['company_name']}")
    if context.get("contact_name"):
        lines.append(f"Контактное лицо: {context['contact_name']}")
    if context.get("manager_name"):
        lines.append(f"Менеджер: {context['manager_name']}")
    if context.get("city"):
        lines.append(f"Город: {context['city']}")
    if context.get("event_address"):
        lines.append(f"Адрес / площадка: {context['event_address']}")
    depart = context.get("departure_date") or ""
    ret = context.get("return_date") or ""
    if depart or ret:
        lines.append(f"Выезд оборудования: {depart or '—'}")
        lines.append(f"Возврат оборудования: {ret or '—'}")
    elif context.get("rent_period"):
        lines.append(f"Период аренды: {context['rent_period']}")
    shifts_label = context.get("shifts_label")
    if shifts_label is None and context.get("shifts") is not None:
        s = context["shifts"]
        try:
            sf = float(s)
            shifts_label = str(int(sf)) if sf == int(sf) else str(sf)
        except (TypeError, ValueError):
            shifts_label = str(s)
    if shifts_label:
        lines.append(f"Количество смен / дней: {shifts_label}")
    if with_assignee and context.get("assignee_name"):
        lines.append(f"Ответственный на объекте: {context['assignee_name']}")
    return lines


def generate_estimate_pdf(
    context: Dict[str, Any],
    output_path: str,
    mode: str = "internal",
) -> str:
    """PDF-смета: те же mode=internal|client и данные, что у DOCX."""
    mode = (mode or "internal").strip().lower()
    if mode not in ("internal", "client"):
        mode = "internal"

    pdf = _EstimatePDF()
    if mode == "client":
        title_text = f"СМЕТА № {context.get('number', '')} от {context.get('date', '')}"
    else:
        title_text = f"СМЕТА (ВНУТРЕННЯЯ) № {context.get('number', '')} от {context.get('date', '')}"
    pdf.title(title_text)

    for line in _pdf_meta_lines(context):
        pdf.line(line)
    pdf.pdf.ln(2)

    all_items = list(context.get("items", []) or [])
    if mode == "client":
        main_items = [i for i in all_items if (i.get("warehouse_type") or "own") != "subrental"]
        sub_items = []
    else:
        main_items = [i for i in all_items if (i.get("warehouse_type") or "own") != "subrental"]
        sub_items = [i for i in all_items if (i.get("warehouse_type") or "own") == "subrental"]
        if not main_items and not sub_items and all_items:
            main_items = all_items

    if main_items:
        if mode == "internal" and sub_items:
            pdf.line("Оборудование и услуги", bold=True)
        rows = []
        for idx, item in enumerate(main_items, 1):
            rows.append([
                str(idx),
                str(item.get("name", ""))[:60],
                _fmt_money(item.get("price", 0)),
                str(item.get("quantity", 1)),
                str(item.get("days", 1)),
                _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0))),
            ])
        pdf.table(
            ["№", "Наименование", "Цена", "Кол-во", "Дней", "Сумма"],
            rows,
            [10, 70, 30, 18, 18, 34],
        )
    elif mode == "client":
        pdf.line("Нет позиций для клиентской сметы.")

    if mode == "internal" and sub_items:
        pdf.pdf.ln(3)
        pdf.line("Субаренда (только для нас)", bold=True)
        pdf.line("Раздел не попадает в клиентскую смету.")
        rows = []
        for idx, item in enumerate(sub_items, 1):
            qty = item.get("quantity", 1)
            days = item.get("days", 1)
            rows.append([
                str(idx),
                str(item.get("name", ""))[:40],
                str(item.get("supplier") or "—")[:20],
                _fmt_money(item.get("price", 0)),
                _fmt_money(item.get("cost_price", 0)),
                f"{qty}×{days}",
                _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0))),
            ])
        pdf.table(
            ["№", "Наименование", "Поставщик", "Цена", "Себест.", "К×Д", "Сумма"],
            rows,
            [8, 48, 28, 24, 24, 16, 32],
        )

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

    pdf.pdf.ln(3)
    pdf.right(f"Оборудование (до скидки): {_fmt_money(float(eq_base))}")
    if disc_pct:
        pdf.right(f"Скидка на оборудование {disc_pct:.0f}%: −{_fmt_money(float(discount_amount or 0))}")
    pdf.right(f"Оборудование (со скидкой): {_fmt_money(float(eq_total))}")
    pdf.right(f"Логистика и персонал: {_fmt_money(float(fixed_total))}")
    pdf.right(f"После скидки: {_fmt_money(float(after_discount))}")
    if tax_pct or tax_amount:
        pdf.right(f"Налог {tax_pct:.0f}%: {_fmt_money(float(tax_amount))}")
    pdf.right(f"ИТОГО: {_fmt_money(float(grand))}", bold=True, size=12)
    pdf.right(get_rubles_text(float(grand or 0)))

    if mode == "internal":
        cost_total = float(context.get("cost_total") or 0)
        margin = context.get("margin")
        if margin is None:
            margin = float(grand or 0) - cost_total
        pdf.pdf.ln(2)
        pdf.line("Маржа (внутренний блок)", bold=True)
        pdf.right(f"Себестоимость субаренды: {_fmt_money(cost_total)}")
        pdf.right(f"Маржа: {_fmt_money(float(margin))}", bold=True)

    pdf.save(output_path)
    return output_path


def generate_contract_pdf(context: Dict[str, Any], output_path: str) -> str:
    """Упрощённый PDF договора: реквизиты + спецификация (как приложение к Word-шаблону)."""
    pdf = _EstimatePDF()
    pdf.title(
        f"ДОГОВОР № {context.get('contract_number', '')} от {context.get('contract_date', '')}"
    )
    pdf.line(f"Заказчик: {context.get('company_name') or '—'}", bold=True)
    if context.get("director_name"):
        pdf.line(f"Директор / представитель: {context['director_name']}")
    if context.get("iin_bin"):
        pdf.line(f"ИИН/БИН: {context['iin_bin']}")
    if context.get("iban"):
        pdf.line(f"ИБан / реквизиты: {context['iban']}")
    if context.get("event_name"):
        pdf.line(f"Мероприятие: {context['event_name']}")
    if context.get("event_date"):
        pdf.line(f"Дата: {context['event_date']}")
    if context.get("event_address"):
        pdf.line(f"Адрес: {context['event_address']}")
    pdf.pdf.ln(2)
    pdf.line("Спецификация оборудования и услуг", bold=True)

    items = list(context.get("items", []) or [])
    rows = []
    for idx, item in enumerate(items, 1):
        rows.append([
            str(idx),
            str(item.get("name", ""))[:60],
            _fmt_money(item.get("price", 0)),
            str(item.get("quantity", 1)),
            str(item.get("days", 1)),
            _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0))),
        ])
    if rows:
        pdf.table(
            ["№", "Наименование", "Цена", "Кол-во", "Дней", "Сумма"],
            rows,
            [10, 70, 30, 18, 18, 34],
        )
    else:
        pdf.line("Нет позиций.")

    disc_pct = float(context.get("discount_percentage") or 0)
    tax_pct = float(context.get("tax_percentage") or 0)
    pdf.pdf.ln(3)
    pdf.right(f"Оборудование: {_fmt_money(float(context.get('equipment_total') or 0))}")
    pdf.right(f"Логистика и персонал: {_fmt_money(float(context.get('fixed_total') or 0))}")
    if disc_pct:
        pdf.right(f"Скидка: {disc_pct:.0f}%")
    if tax_pct or context.get("tax_amount"):
        pdf.right(f"Налог {tax_pct:.0f}%: {_fmt_money(float(context.get('tax_amount') or 0))}")
    grand = float(context.get("grand_total") or 0)
    pdf.right(f"ИТОГО: {_fmt_money(grand)}", bold=True, size=12)
    pdf.right(get_rubles_text(grand))
    pdf.pdf.ln(4)
    pdf.line(
        "Полный юридический текст договора см. в версии Word. "
        "Этот PDF — спецификация и итоговая сумма для клиента."
    )
    pdf.save(output_path)
    return output_path
