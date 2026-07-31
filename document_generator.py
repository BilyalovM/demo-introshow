import os
from collections import OrderedDict
from docxtpl import DocxTemplate
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml, OxmlElement
from num2words import num2words
from typing import Dict, Any, List, Optional, Tuple


# Палитра Excel-шаблона «Новый шаблон сметы.xlsx»
COLOR_CORAL = "F78561"       # заголовки секций / итоги
COLOR_GRAY = "A6A6A6"        # строки ИТОГО, колонка кол-ва
COLOR_WHITE = "FFFFFF"
RGB_CORAL = (247, 133, 97)
RGB_GRAY = (166, 166, 166)


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


def _set_run_font(run, *, bold: bool = False, size: int = 11, color: Optional[Tuple[int, int, int]] = None):
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = "Arial"
    r = run._element
    rPr = r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), "Arial")
    rFonts.set(qn("w:hAnsi"), "Arial")
    rFonts.set(qn("w:cs"), "Arial")
    if color:
        run.font.color.rgb = RGBColor(*color)


def _shade_cell(cell, hex_color: str) -> None:
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}" w:val="clear"/>'))


def _set_cell_text(cell, text: str, *, bold: bool = False, size: int = 10, center: bool = False) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(str(text))
    _set_run_font(run, bold=bold, size=size)


def _item_section_name(item: Dict[str, Any]) -> str:
    cat = (item.get("category") or "").strip()
    return cat if cat else "Оборудование и услуги"


def _group_items(items: List[Dict[str, Any]]) -> "OrderedDict[str, List[Dict[str, Any]]]":
    grouped: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for item in items:
        key = _item_section_name(item)
        grouped.setdefault(key, []).append(item)
    return grouped


def _add_meta(doc: Document, context: Dict[str, Any], with_assignee: bool = False) -> None:
    """Шапка как в Excel: проект, контакт, менеджер, город, дни, выезд/возврат."""
    meta_pairs = []
    project = context.get("project_name") or context.get("event_name")
    if project:
        meta_pairs.append(("Наименование проекта", project))
    if context.get("company_name"):
        meta_pairs.append(("Заказчик", context["company_name"]))
    if context.get("contact_name"):
        meta_pairs.append(("Контактное лицо проекта", context["contact_name"]))
    if context.get("manager_name"):
        meta_pairs.append(("Менеджер проекта", context["manager_name"]))
    city = context.get("city") or ""
    address = context.get("event_address") or ""
    loc = " / ".join([p for p in (city, address) if p])
    if loc:
        meta_pairs.append(("Город / локация", loc))

    shifts_label = context.get("shifts_label")
    if shifts_label is None and context.get("shifts") is not None:
        s = context["shifts"]
        try:
            sf = float(s)
            shifts_label = str(int(sf)) if sf == int(sf) else str(sf)
        except (TypeError, ValueError):
            shifts_label = str(s)
    if shifts_label:
        meta_pairs.append(("Количество дней работы (смен)", shifts_label))

    depart = context.get("departure_date") or ""
    ret = context.get("return_date") or ""
    if depart or ret:
        meta_pairs.append(("Выезд оборудования со склада", depart or "—"))
        meta_pairs.append(("Возврат оборудования на склад", ret or "—"))
    elif context.get("rent_period"):
        meta_pairs.append(("Период аренды", context["rent_period"]))

    if with_assignee and context.get("assignee_name"):
        meta_pairs.append(("Ответственный на объекте", context["assignee_name"]))

    for label, value in meta_pairs:
        p = doc.add_paragraph()
        r1 = p.add_run(f"{label}: ")
        _set_run_font(r1, bold=False, size=11)
        r2 = p.add_run(str(value))
        _set_run_font(r2, bold=True, size=11)


def _add_estimate_table(
    doc: Document,
    items: List[Dict[str, Any]],
    *,
    with_prices: bool = True,
    name_suffix_fn=None,
) -> None:
    """Таблица с coral-заголовками секций и серыми ИТОГО — как в Excel."""
    if not items:
        return

    cols = 6 if with_prices else 3
    headers = (
        ["№", "Наименование", "Кол-во", "Цена за ед.", "Кол-во смен", "Сумма"]
        if with_prices
        else ["№", "Наименование", "Кол-во"]
    )

    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"
    table.autofit = True

    global_idx = 0
    for section, section_items in _group_items(items).items():
        # Section / column header (coral)
        hdr = table.add_row().cells
        _set_cell_text(hdr[0], section, bold=True, size=10)
        for i, h in enumerate(headers):
            if i == 0:
                continue
            _set_cell_text(hdr[i], h, bold=True, size=9, center=True)
        for c in hdr:
            _shade_cell(c, COLOR_CORAL)

        section_sum = 0.0
        for item in section_items:
            global_idx += 1
            row = table.add_row().cells
            name = str(item.get("name", ""))
            if name_suffix_fn:
                extra = name_suffix_fn(item)
                if extra:
                    name = f"{name}{extra}"
            qty = item.get("quantity", 1)
            days = item.get("days", 1)
            line_total = float(item.get("line_total_discounted", item.get("line_total_base", 0)) or 0)
            section_sum += line_total

            _set_cell_text(row[0], str(global_idx), size=9, center=True)
            _set_cell_text(row[1], name, size=9)
            if with_prices:
                _set_cell_text(row[2], str(qty), bold=True, size=9, center=True)
                _shade_cell(row[2], COLOR_GRAY)
                _set_cell_text(row[3], _fmt_money(item.get("price", 0)), bold=True, size=9, center=True)
                _set_cell_text(row[4], str(days), bold=True, size=9, center=True)
                _set_cell_text(row[5], _fmt_money(line_total), bold=True, size=9, center=True)
            else:
                _set_cell_text(row[2], str(qty), bold=True, size=9, center=True)
                _shade_cell(row[2], COLOR_GRAY)

        # Section total (gray)
        if with_prices:
            tot = table.add_row().cells
            _set_cell_text(tot[0], "", size=9)
            _set_cell_text(tot[1], "ИТОГО KZT", bold=True, size=10)
            _set_cell_text(tot[2], "", size=9)
            _set_cell_text(tot[3], "", size=9)
            _set_cell_text(tot[4], "", size=9)
            _set_cell_text(tot[5], _fmt_money(section_sum), bold=True, size=10, center=True)
            for c in tot:
                _shade_cell(c, COLOR_GRAY)


def _add_subrental_table(doc: Document, sub_items: List[Dict[str, Any]]) -> None:
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    headers = ["№", "Наименование", "Поставщик", "Цена клиенту", "Себест.", "Кол-во × смен", "Сумма"]
    for i, htxt in enumerate(headers):
        _set_cell_text(table.rows[0].cells[i], htxt, bold=True, size=9, center=True)
        _shade_cell(table.rows[0].cells[i], COLOR_CORAL)
    for idx, item in enumerate(sub_items, 1):
        row = table.add_row().cells
        qty = item.get("quantity", 1)
        days = item.get("days", 1)
        _set_cell_text(row[0], str(idx), size=9, center=True)
        _set_cell_text(row[1], str(item.get("name", "")), size=9)
        _set_cell_text(row[2], str(item.get("supplier") or "—"), size=9)
        _set_cell_text(row[3], _fmt_money(item.get("price", 0)), size=9, center=True)
        _set_cell_text(row[4], _fmt_money(item.get("cost_price", 0)), size=9, center=True)
        _set_cell_text(row[5], f"{qty} × {days}", bold=True, size=9, center=True)
        _shade_cell(row[5], COLOR_GRAY)
        _set_cell_text(
            row[6],
            _fmt_money(item.get("line_total_discounted", item.get("line_total_base", 0))),
            bold=True,
            size=9,
            center=True,
        )


def _add_totals_block(doc: Document, context: Dict[str, Any], mode: str) -> None:
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

    rows = [
        ("Итоговая сумма за оборудование", float(eq_base)),
    ]
    if disc_pct:
        rows.append((f"Скидка на оборудование {disc_pct:.0f}%", -float(discount_amount or 0)))
        rows.append(("Оборудование со скидкой", float(eq_total)))
    rows.append(("Работа персонала + расходники", float(fixed_total)))
    rows.append(("Сумма итого", float(after_discount)))
    if tax_pct or tax_amount:
        rows.append((f"Итоговая сумма за проект с учетом НДС {tax_pct:.0f}%", float(grand)))
    else:
        rows.append(("ИТОГО", float(grand)))

    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in rows:
        cells = table.add_row().cells
        _set_cell_text(cells[0], label, bold=True, size=10)
        _set_cell_text(cells[1], _fmt_money(value), bold=True, size=10, center=True)
        _shade_cell(cells[0], COLOR_CORAL)
        _shade_cell(cells[1], COLOR_CORAL)

    p = doc.add_paragraph(get_rubles_text(float(grand or 0)))
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if p.runs:
        _set_run_font(p.runs[0], bold=True, size=11)

    if mode == "internal":
        cost_total = float(context.get("cost_total") or 0)
        margin = context.get("margin")
        if margin is None:
            margin = float(grand or 0) - cost_total
        if cost_total or margin:
            doc.add_paragraph("")
            h = doc.add_paragraph()
            run = h.add_run("Маржа (внутренний блок)")
            _set_run_font(run, bold=True, size=11)
            mt = doc.add_table(rows=0, cols=2)
            mt.style = "Table Grid"
            for label, value in (
                ("Себестоимость субаренды", cost_total),
                ("Маржа", float(margin)),
            ):
                cells = mt.add_row().cells
                _set_cell_text(cells[0], label, bold=True, size=10)
                _set_cell_text(cells[1], _fmt_money(value), bold=True, size=10, center=True)
                _shade_cell(cells[0], COLOR_GRAY)
                _shade_cell(cells[1], COLOR_GRAY)


def generate_technichka_docx(context: Dict[str, Any], output_path: str) -> str:
    """Техничка для склада/персонала: позиции и количества без цен."""
    doc = Document()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"ТЕХНИЧКА № {context.get('number', '')} от {context.get('date', '')}")
    _set_run_font(run, bold=True, size=16)

    _add_meta(doc, context, with_assignee=True)

    items = context.get("items", [])
    _add_estimate_table(doc, items, with_prices=False)

    note = doc.add_paragraph()
    nr = note.add_run("Цены скрыты. Документ для склада и выездного персонала.")
    nr.italic = True
    _set_run_font(nr, size=9)

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
    _set_run_font(run, bold=True, size=16)

    _add_meta(doc, context)

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
        _add_estimate_table(doc, main_items, with_prices=True)
    elif mode == "client":
        doc.add_paragraph("Нет позиций для клиентской сметы.")

    if mode == "internal" and sub_items:
        doc.add_paragraph("")
        h = doc.add_paragraph()
        run = h.add_run("Субаренда (только для нас)")
        _set_run_font(run, bold=True, size=12)
        note = doc.add_paragraph()
        nr = note.add_run("Раздел не попадает в клиентскую смету.")
        nr.italic = True
        _add_subrental_table(doc, sub_items)

    doc.add_paragraph("")
    _add_totals_block(doc, context, mode)

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
    """Минимальная обёртка над FPDF с кириллицей (DejaVu) и палитрой Excel."""

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

    def table(
        self,
        headers: List[str],
        rows: List[List[str]],
        col_widths: List[float],
        *,
        header_rgb: Tuple[int, int, int] = RGB_CORAL,
        fill_header: bool = True,
    ):
        self._reset_x()
        usable = self.pdf.w - self.pdf.l_margin - self.pdf.r_margin
        total_w = sum(col_widths)
        if total_w > usable and total_w > 0:
            scale = usable / total_w
            col_widths = [w * scale for w in col_widths]

        if fill_header:
            self.pdf.set_fill_color(*header_rgb)
            self.pdf.set_font("DejaVu", "B", 8)
            for i, h in enumerate(headers):
                self.pdf.cell(col_widths[i], 6, h[:40], border=1, fill=True)
            self.pdf.ln()
            self._reset_x()

        self.pdf.set_font("DejaVu", size=8)
        for row in rows:
            cells = [str(c)[:80] for c in row]
            row_h = 6
            x0 = self.pdf.l_margin
            y0 = self.pdf.get_y()
            if y0 + row_h > self.pdf.h - 15:
                self.pdf.add_page()
                self._reset_x()
                y0 = self.pdf.get_y()
            is_total = cells and ("ИТОГО" in cells[0].upper() or "ИТОГО" in (cells[1].upper() if len(cells) > 1 else ""))
            if is_total:
                self.pdf.set_fill_color(*RGB_GRAY)
                self.pdf.set_font("DejaVu", "B", 8)
            for i, cell in enumerate(cells):
                x = x0 + sum(col_widths[:i])
                self.pdf.set_xy(x, y0)
                self.pdf.cell(col_widths[i], row_h, cell, border=1, fill=is_total)
            if is_total:
                self.pdf.set_font("DejaVu", size=8)
            self.pdf.set_xy(x0, y0 + row_h)
        self._reset_x()
        self.pdf.set_font("DejaVu", size=10)

    def totals_table(self, rows: List[Tuple[str, str]]):
        """Coral totals block like Excel footer."""
        self._reset_x()
        usable = self.pdf.w - self.pdf.l_margin - self.pdf.r_margin
        w_label, w_val = usable * 0.62, usable * 0.38
        self.pdf.set_fill_color(*RGB_CORAL)
        self.pdf.set_font("DejaVu", "B", 9)
        for label, value in rows:
            y0 = self.pdf.get_y()
            if y0 + 7 > self.pdf.h - 15:
                self.pdf.add_page()
                self._reset_x()
            self.pdf.cell(w_label, 7, label[:70], border=1, fill=True)
            self.pdf.cell(w_val, 7, value[:40], border=1, fill=True, align="R")
            self.pdf.ln()
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
        lines.append(f"Контактное лицо проекта: {context['contact_name']}")
    if context.get("manager_name"):
        lines.append(f"Менеджер проекта: {context['manager_name']}")
    city = context.get("city") or ""
    address = context.get("event_address") or ""
    loc = " / ".join([p for p in (city, address) if p])
    if loc:
        lines.append(f"Город / локация: {loc}")
    shifts_label = context.get("shifts_label")
    if shifts_label is None and context.get("shifts") is not None:
        s = context["shifts"]
        try:
            sf = float(s)
            shifts_label = str(int(sf)) if sf == int(sf) else str(sf)
        except (TypeError, ValueError):
            shifts_label = str(s)
    if shifts_label:
        lines.append(f"Количество дней работы (смен): {shifts_label}")
    depart = context.get("departure_date") or ""
    ret = context.get("return_date") or ""
    if depart or ret:
        lines.append(f"Выезд оборудования со склада: {depart or '—'}")
        lines.append(f"Возврат оборудования на склад: {ret or '—'}")
    elif context.get("rent_period"):
        lines.append(f"Период аренды: {context['rent_period']}")
    if with_assignee and context.get("assignee_name"):
        lines.append(f"Ответственный на объекте: {context['assignee_name']}")
    return lines


def _pdf_build_sectioned_rows(
    items: List[Dict[str, Any]],
    *,
    with_prices: bool = True,
) -> Tuple[List[str], List[List[str]], List[float]]:
    if with_prices:
        headers = ["№", "Наименование", "Кол-во", "Цена", "Смен", "Сумма"]
        widths = [10, 70, 18, 28, 18, 34]
    else:
        headers = ["№", "Наименование", "Кол-во"]
        widths = [12, 140, 28]

    rows: List[List[str]] = []
    idx = 0
    for section, section_items in _group_items(items).items():
        if with_prices:
            rows.append([section, "", "Кол-во", "Цена", "Смен", "Сумма"])
        else:
            rows.append([section, "", "Кол-во"])
        # Mark section header by prefix for fill detection — use coral via special marker
        rows[-1][0] = f"§ {section}"
        section_sum = 0.0
        for item in section_items:
            idx += 1
            qty = item.get("quantity", 1)
            days = item.get("days", 1)
            line_total = float(item.get("line_total_discounted", item.get("line_total_base", 0)) or 0)
            section_sum += line_total
            if with_prices:
                rows.append([
                    str(idx),
                    str(item.get("name", ""))[:60],
                    str(qty),
                    _fmt_money(item.get("price", 0)),
                    str(days),
                    _fmt_money(line_total),
                ])
            else:
                rows.append([str(idx), str(item.get("name", ""))[:70], str(qty)])
        if with_prices:
            rows.append(["", "ИТОГО KZT", "", "", "", _fmt_money(section_sum)])
    return headers, rows, widths


def _pdf_draw_sectioned_table(pdf: _EstimatePDF, items: List[Dict[str, Any]], *, with_prices: bool = True):
    """Draw grouped table with coral section headers and gray totals."""
    if not items:
        return
    headers, rows, col_widths = _pdf_build_sectioned_rows(items, with_prices=with_prices)
    usable = pdf.pdf.w - pdf.pdf.l_margin - pdf.pdf.r_margin
    total_w = sum(col_widths)
    if total_w > usable and total_w > 0:
        scale = usable / total_w
        col_widths = [w * scale for w in col_widths]

    # Skip generic header — each section has its own coral header row
    pdf.pdf.set_font("DejaVu", size=8)
    for row in rows:
        cells = [str(c)[:80] for c in row]
        row_h = 6
        x0 = pdf.pdf.l_margin
        y0 = pdf.pdf.get_y()
        if y0 + row_h > pdf.pdf.h - 15:
            pdf.pdf.add_page()
            pdf._reset_x()
            y0 = pdf.pdf.get_y()

        is_section = cells[0].startswith("§ ")
        is_total = (not is_section) and any("ИТОГО" in c.upper() for c in cells)

        if is_section:
            pdf.pdf.set_fill_color(*RGB_CORAL)
            pdf.pdf.set_font("DejaVu", "B", 8)
            # Section title in col0, column labels in rest
            label = cells[0][2:]  # strip §
            display = [label] + cells[1:]
            for i, cell in enumerate(display):
                x = x0 + sum(col_widths[:i])
                pdf.pdf.set_xy(x, y0)
                pdf.pdf.cell(col_widths[i], row_h, cell[:40], border=1, fill=True)
            pdf.pdf.set_font("DejaVu", size=8)
        elif is_total:
            pdf.pdf.set_fill_color(*RGB_GRAY)
            pdf.pdf.set_font("DejaVu", "B", 8)
            for i, cell in enumerate(cells):
                x = x0 + sum(col_widths[:i])
                pdf.pdf.set_xy(x, y0)
                pdf.pdf.cell(col_widths[i], row_h, cell, border=1, fill=True)
            pdf.pdf.set_font("DejaVu", size=8)
        else:
            for i, cell in enumerate(cells):
                x = x0 + sum(col_widths[:i])
                pdf.pdf.set_xy(x, y0)
                # Gray qty column like Excel
                fill_qty = with_prices and i == 2
                if fill_qty:
                    pdf.pdf.set_fill_color(*RGB_GRAY)
                pdf.pdf.cell(col_widths[i], row_h, cell, border=1, fill=fill_qty)
        pdf.pdf.set_xy(x0, y0 + row_h)
    pdf._reset_x()
    pdf.pdf.set_font("DejaVu", size=10)


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
        _pdf_draw_sectioned_table(pdf, main_items, with_prices=True)
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
            ["№", "Наименование", "Поставщик", "Цена", "Себест.", "К×С", "Сумма"],
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

    totals_rows = [("Итоговая сумма за оборудование", _fmt_money(float(eq_base)))]
    if disc_pct:
        totals_rows.append((f"Скидка на оборудование {disc_pct:.0f}%", f"−{_fmt_money(float(discount_amount or 0))}"))
        totals_rows.append(("Оборудование со скидкой", _fmt_money(float(eq_total))))
    totals_rows.append(("Работа персонала + расходники", _fmt_money(float(fixed_total))))
    totals_rows.append(("Сумма итого", _fmt_money(float(after_discount))))
    if tax_pct or tax_amount:
        totals_rows.append(
            (f"Итоговая сумма за проект с учетом НДС {tax_pct:.0f}%", _fmt_money(float(grand)))
        )
    else:
        totals_rows.append(("ИТОГО", _fmt_money(float(grand))))

    pdf.pdf.ln(3)
    pdf.totals_table(totals_rows)
    pdf.right(get_rubles_text(float(grand or 0)), bold=True)

    if mode == "internal":
        cost_total = float(context.get("cost_total") or 0)
        margin = context.get("margin")
        if margin is None:
            margin = float(grand or 0) - cost_total
        if cost_total or margin:
            pdf.pdf.ln(2)
            pdf.line("Маржа (внутренний блок)", bold=True)
            pdf.pdf.set_fill_color(*RGB_GRAY)
            usable = pdf.pdf.w - pdf.pdf.l_margin - pdf.pdf.r_margin
            w_label, w_val = usable * 0.62, usable * 0.38
            pdf.pdf.set_font("DejaVu", "B", 9)
            for label, value in (
                ("Себестоимость субаренды", _fmt_money(cost_total)),
                ("Маржа", _fmt_money(float(margin))),
            ):
                pdf.pdf.cell(w_label, 7, label, border=1, fill=True)
                pdf.pdf.cell(w_val, 7, value, border=1, fill=True, align="R")
                pdf.pdf.ln()
                pdf._reset_x()
            pdf.pdf.set_font("DejaVu", size=10)

    pdf.save(output_path)
    return output_path


def generate_technichka_pdf(context: Dict[str, Any], output_path: str) -> str:
    """PDF-техничка в той же палитре (если понадобится)."""
    pdf = _EstimatePDF()
    pdf.title(f"ТЕХНИЧКА № {context.get('number', '')} от {context.get('date', '')}")
    for line in _pdf_meta_lines(context, with_assignee=True):
        pdf.line(line)
    pdf.pdf.ln(2)
    items = list(context.get("items", []) or [])
    _pdf_draw_sectioned_table(pdf, items, with_prices=False)
    pdf.pdf.ln(2)
    pdf.line("Цены скрыты. Документ для склада и выездного персонала.")
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
    if items:
        _pdf_draw_sectioned_table(pdf, items, with_prices=True)
    else:
        pdf.line("Нет позиций.")

    disc_pct = float(context.get("discount_percentage") or 0)
    tax_pct = float(context.get("tax_percentage") or 0)
    grand = float(context.get("grand_total") or 0)
    totals = [
        ("Оборудование", _fmt_money(float(context.get("equipment_total") or 0))),
        ("Логистика и персонал", _fmt_money(float(context.get("fixed_total") or 0))),
    ]
    if disc_pct:
        totals.append((f"Скидка {disc_pct:.0f}%", "—"))
    if tax_pct or context.get("tax_amount"):
        totals.append((f"Налог {tax_pct:.0f}%", _fmt_money(float(context.get("tax_amount") or 0))))
    totals.append(("ИТОГО", _fmt_money(grand)))
    pdf.pdf.ln(3)
    pdf.totals_table(totals)
    pdf.right(get_rubles_text(grand), bold=True)
    pdf.pdf.ln(4)
    pdf.line(
        "Полный юридический текст договора см. в версии Word. "
        "Этот PDF — спецификация и итоговая сумма для клиента."
    )
    pdf.save(output_path)
    return output_path
