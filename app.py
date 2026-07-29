from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, File, UploadFile, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict
import json
import os
import re
import tempfile
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_VERCEL = bool(os.environ.get("VERCEL"))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = (
    os.path.join(tempfile.gettempdir(), "rental_app_uploads")
    if IS_VERCEL
    else os.path.join(BASE_DIR, "uploads")
)
ENV_PATH = (
    os.path.join(tempfile.gettempdir(), "rental_app.env")
    if IS_VERCEL
    else os.path.join(BASE_DIR, ".env")
)
CONTRACT_TEMPLATE_PATH = os.path.join(TEMPLATES_DIR, "contract_template.docx")
os.environ.setdefault("RENTAL_UPLOADS_DIR", UPLOADS_DIR)

load_dotenv(os.path.join(BASE_DIR, ".env"))

from sqlalchemy.orm import Session
from database import init_db, get_db, SessionLocal, Equipment, Company, Deal, DealItem, CustomField, DealFieldValue, DealHistory, Project2D, Folder, Pipeline, Stage, PushSubscription, User, BotSettings, KnowledgeItem, ChatMessage, Invoice, Task, TaskComment, TaskAssignee, TaskObserver, TaskChecklistItem, Contact, Activity, DealAttachment, CrmNote, DealAdvance, DealExpense, DealPayrollLine, DealStaffAssignment, InternalChat, InternalChatMember, InternalMessage, engine
from sqlalchemy import text, func

from calculator import calculate_estimate, DEFAULT_TAX_PERCENTAGE
from document_generator import generate_contract, get_rubles_text
import notifications
import auth
import chatbot

# Налог в сметах всегда 16% (UI + DB + PDF/DOCX)
FIXED_TAX_PERCENTAGE = DEFAULT_TAX_PERCENTAGE

# Initialize the database
init_db()

# DB Migration & Seeding
with Session(engine) as session:
    # 1. Add pipeline_id to deals if it doesn't exist
    try:
        session.execute(text("ALTER TABLE deals ADD COLUMN pipeline_id INTEGER"))
        session.commit()
    except Exception:
        session.rollback() # column already exists
        
    # 2. Seed main pipeline if empty
    default_pipeline = session.query(Pipeline).first()
    if not default_pipeline:
        default_pipeline = Pipeline(name="Основная воронка")
        session.add(default_pipeline)
        session.commit()
        session.refresh(default_pipeline)
        
        # Create default stages
        default_stages = [
            "Первичный контакт", "Согласование сметы", "Договор и счет",
            "Предоплата внесена", "Монтаж / Мероприятие", "Успешно реализовано",
            "Сделка проиграна"
        ]
        for i, stage_name in enumerate(default_stages):
            st = Stage(pipeline_id=default_pipeline.id, name=stage_name, order_index=i+1)
            session.add(st)
        session.commit()
        
    # 3. Update existing deals to the default pipeline if they are null
    session.execute(text(f"UPDATE deals SET pipeline_id = {default_pipeline.id} WHERE pipeline_id IS NULL"))
    session.commit()

    # 4. Add AI & 3D properties and custom fields to equipment
    try:
        session.execute(text("ALTER TABLE equipment ADD COLUMN weight FLOAT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN dimensions TEXT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN power_w FLOAT"))
        session.execute(text("ALTER TABLE equipment ADD COLUMN dispersion TEXT"))
        session.commit()
    except Exception:
        session.rollback() # Columns already exist

    try:
        session.execute(text("ALTER TABLE equipment ADD COLUMN custom_fields JSON"))
        session.commit()
    except Exception:
        session.rollback()
        
    try:
        session.execute(text("ALTER TABLE companies ADD COLUMN telegram_chat_id VARCHAR"))
        session.commit()
    except Exception:
        session.rollback()

    # 4b. New columns: instagram for companies, permissions/full_name for users
    for ddl in [
        "ALTER TABLE companies ADD COLUMN instagram VARCHAR",
        "ALTER TABLE users ADD COLUMN full_name VARCHAR",
        "ALTER TABLE users ADD COLUMN permissions JSON",
        # Цена позиции в смете (может отличаться от цены склада)
        "ALTER TABLE deal_items ADD COLUMN price FLOAT",
        # Привязка сделки к контакту, чату мессенджера и прошлому обращению
        "ALTER TABLE deals ADD COLUMN contact_id INTEGER",
        "ALTER TABLE deals ADD COLUMN chat_channel VARCHAR",
        "ALTER TABLE deals ADD COLUMN chat_id VARCHAR",
        "ALTER TABLE deals ADD COLUMN prev_deal_id INTEGER",
        "ALTER TABLE deals ADD COLUMN created_at DATETIME",
        # Задачи в стиле Битрикс24
        "ALTER TABLE tasks ADD COLUMN description VARCHAR",
        "ALTER TABLE tasks ADD COLUMN created_by VARCHAR",
        "ALTER TABLE tasks ADD COLUMN creator_id INTEGER",
        "ALTER TABLE tasks ADD COLUMN tags VARCHAR",
        "ALTER TABLE tasks ADD COLUMN priority VARCHAR DEFAULT 'normal'",
        "ALTER TABLE tasks ADD COLUMN completed_at DATETIME",
        # CRM → ближе к Битрикс24
        "ALTER TABLE deals ADD COLUMN assignee_id INTEGER",
        "ALTER TABLE deals ADD COLUMN source VARCHAR",
        "ALTER TABLE deals ADD COLUMN loss_reason VARCHAR",
        "ALTER TABLE deals ADD COLUMN is_qualified BOOLEAN DEFAULT 0",
        "ALTER TABLE deals ADD COLUMN is_archived BOOLEAN DEFAULT 0",
        "ALTER TABLE contacts ADD COLUMN is_primary BOOLEAN DEFAULT 0",
        # Субаренда / внешний склад
        "ALTER TABLE equipment ADD COLUMN cost_price FLOAT DEFAULT 0",
        "ALTER TABLE equipment ADD COLUMN warehouse_type VARCHAR DEFAULT 'own'",
        "ALTER TABLE equipment ADD COLUMN supplier VARCHAR",
        # Налог в смете (%) — исторически DEFAULT 0; ниже принудительно 16
        "ALTER TABLE deals ADD COLUMN tax_percentage FLOAT DEFAULT 16",
        # Шапка сметы (как в Excel)
        "ALTER TABLE deals ADD COLUMN city VARCHAR",
        "ALTER TABLE deals ADD COLUMN shifts FLOAT DEFAULT 1",
    ]:
        try:
            session.execute(text(ddl))
            session.commit()
        except Exception:
            session.rollback()

    # 4b2. Налог всегда 16%: выравниваем существующие сделки
    try:
        session.execute(
            text("UPDATE deals SET tax_percentage = 16 WHERE tax_percentage IS NULL OR tax_percentage != 16")
        )
        session.commit()
    except Exception:
        session.rollback()

    # 4c. Стадии «в работе» для проверки брони оборудования:
    # если ни одна стадия не помечена, помечаем стандартные.
    try:
        active_cnt = session.query(Stage).filter(Stage.is_active_rent == True).count()  # noqa: E712
        if active_cnt == 0:
            for st in session.query(Stage).all():
                if any(k in (st.name or "") for k in ("Предоплата", "Монтаж", "Мероприятие")):
                    st.is_active_rent = True
            session.commit()
    except Exception:
        session.rollback()

    # 5. Create default admin user
    try:
        if session.query(User).count() == 0:
            admin_user = User(username="admin", hashed_password=auth.get_password_hash("admin"), role="admin", full_name="Администратор")
            session.add(admin_user)
            session.commit()
    except Exception:
        session.rollback()

app = FastAPI(title="Rental Business Automation")

def expand_company_type(name: str) -> str:
    from datetime import datetime
    if not name:
        return name
    name = name.strip()
    lower_name = name.lower()
    if lower_name.startswith("тоо "):
        return "Товарищество с ограниченной ответственностью " + name[4:]
    if lower_name.startswith("ип "):
        return "Индивидуальный предприниматель " + name[3:]
    if lower_name.startswith("ао "):
        return "Акционерное общество " + name[3:]
    return name

# Set up templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Models for the API
class EquipmentItem(BaseModel):
    name: str
    price: float
    quantity: int
    days: int
    category_type: str  # 'equipment' or 'fixed'

class LayoutOptimizationRequest(BaseModel):
    items: List[EquipmentItem]
    width: float
    length: float

# Additional Pydantic models for DB operations
class EquipmentCreate(BaseModel):
    name: str
    category: str
    price: float
    stock_quantity: int
    status: str
    folder_id: Optional[int] = None
    description: Optional[str] = None
    weight: Optional[float] = None
    dimensions: Optional[str] = None
    power_w: Optional[float] = None
    dispersion: Optional[str] = None
    custom_fields: Optional[Dict[str, str]] = {}
    cost_price: Optional[float] = 0.0
    warehouse_type: Optional[str] = "own"  # own | subrental
    supplier: Optional[str] = None

LEAD_SOURCES = {
    "manual": "Вручную",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "instagram": "Instagram",
    "site": "Сайт",
    "maps": "Карты (2GIS / Google / Яндекс)",
    "onec": "1С",
    "referral": "Рекомендация",
    "other": "Другое",
}
    
class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None

class DealCreate(BaseModel):
    title: str
    company_id: Optional[int] = None
    contact_id: Optional[int] = None
    assignee_id: Optional[int] = None
    pipeline_id: Optional[int] = 1
    setup_date: Optional[str] = None
    event_date: str
    event_address: Optional[str] = None
    city: Optional[str] = None
    shifts: Optional[float] = 1.0
    discount_percentage: float = 0.0
    tax_percentage: float = FIXED_TAX_PERCENTAGE
    items_json: Optional[str] = None
    source: Optional[str] = "manual"
    is_qualified: Optional[bool] = False

class DealStageUpdate(BaseModel):
    stage: int
    pipeline_id: Optional[int] = None
    loss_reason: Optional[str] = None

class CompanyCreate(BaseModel):
    name: str
    bin: str
    director_name: str
    phone: str
    email: str
    requisites: str
    based_on: str = "Устава"
    address: str = ""
    bank_name: str = ""
    kbe: str = ""
    bik: str = ""
    instagram: str = ""

class CustomFieldCreate(BaseModel):
    name: str
    field_type: str

class DealFieldValueUpdate(BaseModel):
    field_id: int
    value: str

class DealFieldUpdateList(BaseModel):
    fields: List[DealFieldValueUpdate]

class Project2DSave(BaseModel):
    layout_data_json: str

class PipelineCreate(BaseModel):
    name: str

class StageCreate(BaseModel):
    name: str
    order_index: int
    is_active_rent: Optional[bool] = False

verify_password = auth.verify_password
get_password_hash = auth.get_password_hash


def get_user_from_request(request: Request, db: Session):
    """Достаёт пользователя из подписанного cookie session_token."""
    token = request.cookies.get("session_token")
    username = auth.get_username_from_token(token)
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()


def get_current_user(request: Request, db: Session = Depends(get_db)):
    user = getattr(request.state, "user", None)
    if user is not None:
        # Объект из middleware привязан к другой сессии — перечитываем в текущей
        return db.query(User).filter(User.id == user.id).first()
    return get_user_from_request(request, db)


# Пути, доступные без авторизации
PUBLIC_PATH_PREFIXES = (
    "/static", "/uploads", "/login", "/api/login",
    "/api/tg/webhook", "/api/wa/webhook", "/api/ig/webhook",
    "/tracking/", "/api/push/", "/api/1c/", "/favicon", "/docs", "/openapi.json",
    "/roadmap",  # публичный статус/roadmap для клиента
    "/lead", "/api/leads",  # публичный захват лидов с сайта/карт
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    db = SessionLocal()
    try:
        user = get_user_from_request(request, db)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Не авторизован"})
            return RedirectResponse("/login")

        # Проверка доступа к разделу по правам сотрудника
        section = auth.section_for_path(path)
        if section and not auth.user_can_access(user, section):
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "Нет доступа к разделу"})
            return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>Доступ к разделу запрещён. Обратитесь к администратору.</h3>", status_code=403)

        request.state.user = user
        request.state.user_sections = [
            key for key in auth.SECTIONS if auth.user_can_access(user, key)
        ]
    finally:
        db.close()

    return await call_next(request)


# -----------------
# FRONTEND ROUTES
# -----------------

@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/roadmap", response_class=HTMLResponse)
async def read_roadmap_public(request: Request):
    """Публичная страница статуса CRM (без логина) — обновляется через static/roadmap.json."""
    return templates.TemplateResponse("roadmap_public.html", {"request": request})

@app.get("/lead", response_class=HTMLResponse)
async def read_lead_form(request: Request, source: str = "site"):
    """Публичная форма заявки (сайт / карты). Можно встраивать iframe или давать прямую ссылку."""
    src = source if source in LEAD_SOURCES else "site"
    return templates.TemplateResponse(
        "lead_public.html",
        {"request": request, "source": src, "sources": LEAD_SOURCES},
    )


class PublicLeadIn(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    message: Optional[str] = None
    event_date: Optional[str] = None
    event_address: Optional[str] = None
    source: Optional[str] = "site"  # site / maps / onec / other


def _ingest_lead(db: Session, payload: PublicLeadIn, force_source: Optional[str] = None) -> dict:
    """Создаёт компанию/контакт + сделку на первой стадии воронки."""
    source = (force_source or payload.source or "site").strip().lower()
    if source not in LEAD_SOURCES:
        source = "other"

    name = (payload.name or "").strip() or "Клиент"
    phone = (payload.phone or "").strip() or None
    email = (payload.email or "").strip() or None
    company_name = (payload.company or "").strip() or name
    message = (payload.message or "").strip()
    event_date = (payload.event_date or "").strip() or datetime.today().strftime("%Y-%m-%d")
    event_address = (payload.event_address or "").strip() or None

    company = None
    if phone:
        company = db.query(Company).filter(Company.phone == phone).first()
    if not company and email:
        company = db.query(Company).filter(Company.email == email).first()
    if not company:
        company = Company(
            name=company_name,
            bin="",
            director_name=name,
            phone=phone or "",
            email=email or "",
            requisites="",
        )
        db.add(company)
        db.commit()
        db.refresh(company)

    contact = db.query(Contact).filter(
        Contact.company_id == company.id,
        Contact.name == name,
    ).first()
    if not contact:
        contact = Contact(
            name=name,
            phone=phone,
            email=email,
            company_id=company.id,
            is_primary=True,
            comment=message or None,
        )
        db.add(contact)
        db.commit()
        db.refresh(contact)

    pipeline = db.query(Pipeline).order_by(Pipeline.id).first()
    stage = None
    if pipeline:
        stage = db.query(Stage).filter(Stage.pipeline_id == pipeline.id).order_by(Stage.order_index, Stage.id).first()

    title_bits = [f"Заявка: {name}"]
    if source in LEAD_SOURCES:
        title_bits.append(f"({LEAD_SOURCES[source]})")
    title = " ".join(title_bits)

    deal = Deal(
        title=title[:200],
        company_id=company.id,
        contact_id=contact.id,
        pipeline_id=pipeline.id if pipeline else None,
        stage=stage.id if stage else 1,
        setup_date=event_date,
        event_date=event_date,
        event_address=event_address,
        comment=message or None,
        source=source,
        assignee_id=_default_assignee_id(db),
        is_qualified=False,
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Лид создан из источника «{LEAD_SOURCES.get(source, source)}»",
    ))
    db.commit()
    return {
        "ok": True,
        "deal_id": deal.id,
        "company_id": company.id,
        "contact_id": contact.id,
        "source": source,
        "title": deal.title,
    }


@app.get("/api/leads/sources")
def api_lead_sources():
    return LEAD_SOURCES


@app.post("/api/leads")
def api_public_lead(payload: PublicLeadIn, request: Request, db: Session = Depends(get_db)):
    """Публичный захват лида (сайт / карты). Опционально LEAD_API_KEY в заголовке X-Lead-Key."""
    expected = (os.getenv("LEAD_API_KEY") or "").strip()
    if expected:
        got = (request.headers.get("X-Lead-Key") or request.query_params.get("key") or "").strip()
        if got != expected:
            raise HTTPException(status_code=401, detail="Invalid lead API key")
    if not (payload.name or "").strip() and not (payload.phone or "").strip():
        raise HTTPException(status_code=400, detail="Укажите имя или телефон")
    return _ingest_lead(db, payload)


@app.post("/api/1c/leads")
def api_1c_leads(payload: PublicLeadIn, request: Request, db: Session = Depends(get_db)):
    """Приём лидов из 1С (тот же ключ ONEC_API_KEY)."""
    expected = (os.getenv("ONEC_API_KEY") or "").strip()
    key = (request.headers.get("X-API-Key") or request.headers.get("X-1C-Key") or "").strip()
    if expected and key != expected:
        raise HTTPException(status_code=401, detail="Invalid 1C API key")
    payload.source = payload.source or "onec"
    return _ingest_lead(db, payload, force_source="onec")

@app.post("/api/login")
async def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return JSONResponse(status_code=400, content={"error": "Неверный логин или пароль"})

    # Прозрачная миграция старых SHA-256 хэшей на PBKDF2
    if auth.is_legacy_hash(user.hashed_password):
        user.hashed_password = get_password_hash(password)
        db.commit()

    response = JSONResponse(content={"status": "success"})
    response.set_cookie(
        key="session_token",
        value=auth.create_session_token(user.username),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response

@app.post("/api/logout")
async def logout():
    response = JSONResponse(content={"status": "success"})
    response.delete_cookie("session_token")
    return response


@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deals = db.query(Deal).all()
    stages = {s.id: s for s in db.query(Stage).all()}

    def stage_name(d):
        st = stages.get(d.stage)
        return st.name if st else ""

    won_deals = [d for d in deals if "Успешно" in stage_name(d)]
    lost_deals = [d for d in deals if "проиграна" in stage_name(d).lower()]
    active_deals = [d for d in deals if d not in won_deals and d not in lost_deals]

    revenue = sum(d.final_sum or 0 for d in won_deals)
    in_work_sum = sum(d.final_sum or 0 for d in active_deals)

    # Воронка по стадиям (основная воронка)
    funnel = []
    pipeline = db.query(Pipeline).first()
    if pipeline:
        for st in sorted(pipeline.stages, key=lambda s: s.order_index):
            st_deals = [d for d in deals if d.stage == st.id]
            funnel.append({
                "name": st.name,
                "count": len(st_deals),
                "sum": sum(d.final_sum or 0 for d in st_deals),
            })
    max_count = max((f["count"] for f in funnel), default=0) or 1

    # Активные диалоги (уникальные чаты за последние 7 дней)
    week_ago = datetime.utcnow() - timedelta(days=7)
    active_chats = db.query(ChatMessage.channel, ChatMessage.chat_id)\
        .filter(ChatMessage.created_at >= week_ago).distinct().count()

    recent_deals = db.query(Deal).order_by(Deal.id.desc()).limit(5).all()
    recent_messages = db.query(ChatMessage).order_by(ChatMessage.id.desc()).limit(6).all()

    stats = {
        "revenue": revenue,
        "in_work_sum": in_work_sum,
        "deals_total": len(deals),
        "deals_active": len(active_deals),
        "deals_won": len(won_deals),
        "companies_count": db.query(Company).count(),
        "equipment_count": db.query(Equipment).count(),
        "active_chats": active_chats,
        "funnel": funnel,
        "max_count": max_count,
    }
    return templates.TemplateResponse("index.html", {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "recent_deals": recent_deals,
        "recent_messages": recent_messages,
        "stage_names": {sid: s.name for sid, s in stages.items()},
        "user": user,
    })

@app.get("/quotes/new", response_class=HTMLResponse)
def read_quotes_new(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("quotes_new.html", {"request": request})

@app.get("/quotes/{quote_id:int}", response_class=HTMLResponse)
def read_quote_detail(request: Request, quote_id: int, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("quotes_new.html", {"request": request, "quote_id": quote_id})

@app.get("/quotes", response_class=HTMLResponse)
def read_quotes(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deals = db.query(Deal).order_by(Deal.id.desc()).all()
    return templates.TemplateResponse("quotes.html", {"request": request, "active_page": "quotes", "deals": deals})

@app.get("/calendar", response_class=HTMLResponse)
def read_calendar(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("calendar.html", {"request": request, "active_page": "calendar"})

@app.get("/api/calendar/events")
def api_calendar_events(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """События календаря = сделки с датой мероприятия/монтажа в диапазоне."""
    date_from = (request.query_params.get("from") or "").strip()
    date_to = (request.query_params.get("to") or "").strip()
    mine = (request.query_params.get("mine") or "").strip() in ("1", "true", "yes")
    hide = _user_hide_prices(user)
    q = db.query(Deal).filter(Deal.is_archived == False)  # noqa: E712
    staff_ids = [
        r[0] for r in db.query(DealStaffAssignment.deal_id)
        .filter(DealStaffAssignment.user_id == user.id).distinct().all()
    ]
    # Личный календарь: свои сделки как менеджер ИЛИ назначения в команду
    if hide or mine or _user_crm_own_only(user):
        from sqlalchemy import or_
        filters = [Deal.assignee_id == user.id]
        if staff_ids:
            filters.append(Deal.id.in_(staff_ids))
        q = q.filter(or_(*filters))
    deals = q.all()
    out = []
    for d in deals:
        day = (d.event_date or d.setup_date or "")[:10]
        if not day:
            continue
        if date_from and day < date_from[:10]:
            continue
        if date_to and day > date_to[:10]:
            continue
        mgr = None
        if d.assignee:
            mgr = d.assignee.full_name or d.assignee.username
        att_cnt = db.query(DealAttachment).filter(DealAttachment.deal_id == d.id).count()
        out.append({
            "id": d.id,
            "title": d.title or f"Сделка #{d.id}",
            "date": day,
            "setup_date": d.setup_date,
            "event_date": d.event_date,
            "manager": mgr,
            "attachments_count": att_cnt,
            "company": None if hide else (d.company.name if d.company else None),
            "address": d.event_address or "",
            "hide_prices": hide,
            "my_assignment": d.id in staff_ids,
        })
    out.sort(key=lambda x: (x["date"], x["id"]))
    return out


@app.get("/api/calendar/deals/{deal_id}")
def api_calendar_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    if not _user_assigned_to_deal(db, user, d) and _user_crm_own_only(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _user_assigned_to_deal(db, user, d) and user.role != "admin" and _user_hide_prices(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    hide = _user_hide_prices(user)
    atts = db.query(DealAttachment).filter(DealAttachment.deal_id == deal_id).order_by(DealAttachment.id.desc()).all()
    # Техникам показываем вложения (техничку), без лишней коммерции
    return {
        "id": d.id,
        "title": d.title,
        "setup_date": d.setup_date,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "manager": (d.assignee.full_name or d.assignee.username) if d.assignee else None,
        "hide_prices": hide,
        "attachments": [{
            "id": a.id,
            "title": a.title,
            "kind": a.kind,
            "url": a.url,
            "file_name": a.file_name,
        } for a in atts],
    }


class CalendarAttachmentIn(BaseModel):
    kind: str = "link"
    url: Optional[str] = None
    title: Optional[str] = None


@app.post("/api/calendar/deals/{deal_id}/attachments")
def api_calendar_add_link(
    deal_id: int,
    payload: CalendarAttachmentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    if _user_crm_own_only(user) and d.assignee_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    att = DealAttachment(
        deal_id=deal_id,
        kind="link",
        url=url,
        title=(payload.title or "Ссылка").strip() or "Ссылка",
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return {"id": att.id, "title": att.title, "kind": att.kind, "url": att.url}


@app.post("/api/calendar/deals/{deal_id}/attachments/upload")
async def api_calendar_upload_file(
    deal_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    if _user_crm_own_only(user) and d.assignee_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    import uuid
    import re
    raw_name = file.filename or "file"
    safe = re.sub(r"[^\w.\-()+ ]+", "_", raw_name)[:120]
    filename = f"deal{deal_id}_{uuid.uuid4().hex[:10]}_{safe}"
    path = os.path.join(UPLOADS_DIR, filename)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    att = DealAttachment(
        deal_id=deal_id,
        kind="file",
        url=f"/uploads/{filename}",
        file_name=raw_name,
        title=(title or raw_name).strip() or raw_name,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return {"id": att.id, "title": att.title, "kind": att.kind, "url": att.url, "file_name": att.file_name}


@app.delete("/api/calendar/attachments/{att_id}")
def api_calendar_delete_attachment(
    att_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    att = db.query(DealAttachment).filter(DealAttachment.id == att_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Not found")
    d = db.query(Deal).filter(Deal.id == att.deal_id).first()
    if d and _user_crm_own_only(user) and d.assignee_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if att.kind == "file" and att.url and att.url.startswith("/uploads/"):
        fpath = os.path.join(UPLOADS_DIR, os.path.basename(att.url))
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except OSError:
            pass
    db.delete(att)
    db.commit()
    return {"ok": True}

@app.get("/inbox", response_class=HTMLResponse)
async def read_inbox(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("inbox.html", {"request": request, "active_page": "inbox"})

@app.get("/chats", response_class=HTMLResponse)
async def read_internal_chats(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("chats.html", {"request": request, "active_page": "chats"})

@app.get("/tasks", response_class=HTMLResponse)
async def read_tasks(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("tasks.html", {"request": request, "active_page": "tasks"})

@app.get("/analytics", response_class=HTMLResponse)
async def read_analytics(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deals = db.query(Deal).all()
    stages = {s.id: s for s in db.query(Stage).all()}

    def stage_name(d):
        st = stages.get(d.stage)
        return st.name if st else ""

    won = [d for d in deals if "Успешно" in stage_name(d)]
    lost = [d for d in deals if "проиграна" in stage_name(d).lower()]
    active = [d for d in deals if d not in won and d not in lost]
    closed_count = len(won) + len(lost)
    win_rate = round(len(won) / closed_count * 100) if closed_count else 0

    # Выручка по месяцам (по датам мероприятий выигранных сделок)
    monthly = {}
    for d in won:
        date_str = d.event_date or ""
        month = date_str[:7] if "-" in date_str else ""
        if month:
            monthly[month] = monthly.get(month, 0) + (d.final_sum or 0)
    monthly_rows = sorted(monthly.items())[-8:]
    max_month = max((v for _, v in monthly_rows), default=0) or 1

    funnel = []
    pipeline = db.query(Pipeline).first()
    if pipeline:
        for st in sorted(pipeline.stages, key=lambda s: s.order_index):
            st_deals = [d for d in deals if d.stage == st.id]
            funnel.append({
                "name": st.name,
                "count": len(st_deals),
                "sum": sum(d.final_sum or 0 for d in st_deals),
            })
    max_count = max((f["count"] for f in funnel), default=0) or 1

    # Статистика сообщений по каналам за 30 дней
    month_ago = datetime.utcnow() - timedelta(days=30)
    msg_stats = []
    for channel, label in [("whatsapp", "WhatsApp"), ("telegram", "Telegram"), ("instagram", "Instagram")]:
        incoming = db.query(ChatMessage).filter(ChatMessage.channel == channel, ChatMessage.direction == "in", ChatMessage.created_at >= month_ago).count()
        outgoing = db.query(ChatMessage).filter(ChatMessage.channel == channel, ChatMessage.direction == "out", ChatMessage.created_at >= month_ago).count()
        bot_replies = db.query(ChatMessage).filter(ChatMessage.channel == channel, ChatMessage.is_bot == True, ChatMessage.created_at >= month_ago).count()
        msg_stats.append({"label": label, "incoming": incoming, "outgoing": outgoing, "bot": bot_replies})

    # Разрезы по менеджерам / источникам / причинам отказа
    users_map = {u.id: (u.full_name or u.username) for u in db.query(User).all()}
    by_manager = {}
    for d in deals:
        if getattr(d, "is_archived", False):
            continue
        key = d.assignee_id or 0
        label = users_map.get(key, "Без ответственного") if key else "Без ответственного"
        row = by_manager.setdefault(label, {"name": label, "won_sum": 0, "won": 0, "lost": 0, "active": 0})
        sn = stage_name(d)
        if "Успешно" in sn:
            row["won"] += 1
            row["won_sum"] += d.final_sum or 0
        elif "проигра" in sn.lower():
            row["lost"] += 1
        else:
            row["active"] += 1
    by_manager_rows = sorted(by_manager.values(), key=lambda x: -x["won_sum"])

    by_source = {}
    SOURCE_LABELS = {
        "whatsapp": "WhatsApp", "telegram": "Telegram", "instagram": "Instagram",
        "manual": "Вручную", "referral": "Рекомендация", "site": "Сайт", "other": "Другое",
    }
    for d in deals:
        if getattr(d, "is_archived", False):
            continue
        src = d.source or ("manual" if not d.chat_channel else d.chat_channel)
        label = SOURCE_LABELS.get(src, src or "—")
        by_source[label] = by_source.get(label, 0) + 1
    by_source_rows = sorted(by_source.items(), key=lambda x: -x[1])

    loss_reasons = {}
    for d in lost:
        reason = (d.loss_reason or "Не указана").strip() or "Не указана"
        loss_reasons[reason] = loss_reasons.get(reason, 0) + 1
    loss_reason_rows = sorted(loss_reasons.items(), key=lambda x: -x[1])[:8]

    return templates.TemplateResponse("analytics.html", {
        "request": request,
        "active_page": "analytics",
        "revenue": sum(d.final_sum or 0 for d in won),
        "in_work_sum": sum(d.final_sum or 0 for d in active),
        "won_count": len(won),
        "lost_count": len(lost),
        "win_rate": win_rate,
        "monthly_rows": monthly_rows,
        "max_month": max_month,
        "funnel": funnel,
        "max_count": max_count,
        "msg_stats": msg_stats,
        "by_manager_rows": by_manager_rows,
        "by_source_rows": by_source_rows,
        "loss_reason_rows": loss_reason_rows,
    })


# -- Задачи --
TASK_STATUSES = {
    "open": "Новая",
    "in_progress": "Выполняется",
    "deferred": "Отложена",
    "done": "Завершена",
}

class TaskPersonIn(BaseModel):
    user_id: Optional[int] = None
    name: Optional[str] = None

class TaskChecklistItemIn(BaseModel):
    id: Optional[int] = None
    text: str
    is_done: Optional[bool] = False
    sort_order: Optional[int] = 0

class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    assignee: Optional[str] = None
    assignees: Optional[List[TaskPersonIn]] = None
    observers: Optional[List[TaskPersonIn]] = None
    checklist: Optional[List[TaskChecklistItemIn]] = None
    due_date: Optional[str] = None
    priority: Optional[str] = "normal"
    status: Optional[str] = "open"
    deal_id: Optional[int] = None
    tags: Optional[str] = None

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee: Optional[str] = None
    assignees: Optional[List[TaskPersonIn]] = None
    observers: Optional[List[TaskPersonIn]] = None
    checklist: Optional[List[TaskChecklistItemIn]] = None
    due_date: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    deal_id: Optional[int] = None
    tags: Optional[str] = None

class TaskCommentCreate(BaseModel):
    text: str

class TaskChecklistCreate(BaseModel):
    text: str
    sort_order: Optional[int] = None

class TaskChecklistUpdate(BaseModel):
    text: Optional[str] = None
    is_done: Optional[bool] = None
    sort_order: Optional[int] = None

def _task_tags_list(tags: Optional[str]) -> list:
    if not tags:
        return []
    return [x.strip() for x in str(tags).split(",") if x.strip()]

def _resolve_task_person(db: Session, person: TaskPersonIn) -> tuple:
    """Returns (user_id, display_name) or (None, None) if empty."""
    uid = person.user_id
    name = (person.name or "").strip()
    if uid:
        u = db.query(User).filter(User.id == uid).first()
        if u:
            return u.id, (u.full_name or u.username or name or str(u.id))
    if name:
        u = db.query(User).filter(
            (User.full_name == name) | (User.username == name)
        ).first()
        if u:
            return u.id, (u.full_name or u.username)
        return None, name
    return None, None

def _sync_task_people(db: Session, task: Task, assignees=None, observers=None):
    """Replace assignees/observers lists when provided (None = leave unchanged)."""
    if assignees is not None:
        db.query(TaskAssignee).filter(TaskAssignee.task_id == task.id).delete()
        seen = set()
        primary = None
        for p in assignees or []:
            uid, name = _resolve_task_person(db, p if isinstance(p, TaskPersonIn) else TaskPersonIn(**p))
            if not name:
                continue
            key = (uid, name.lower())
            if key in seen:
                continue
            seen.add(key)
            db.add(TaskAssignee(task_id=task.id, user_id=uid, name=name))
            if primary is None:
                primary = name
        task.assignee = primary
    if observers is not None:
        db.query(TaskObserver).filter(TaskObserver.task_id == task.id).delete()
        seen = set()
        for p in observers or []:
            uid, name = _resolve_task_person(db, p if isinstance(p, TaskPersonIn) else TaskPersonIn(**p))
            if not name:
                continue
            key = (uid, name.lower())
            if key in seen:
                continue
            seen.add(key)
            db.add(TaskObserver(task_id=task.id, user_id=uid, name=name))

def _mention_keys_for_user(u: User) -> list:
    """Keys that can appear after @ in chat (longest match preferred by caller)."""
    keys = []
    if u.username:
        keys.append(u.username)
    if u.full_name and u.full_name.strip():
        keys.append(u.full_name.strip())
    # unique, preserve order
    seen = set()
    out = []
    for k in keys:
        lk = k.lower()
        if lk in seen:
            continue
        seen.add(lk)
        out.append(k)
    return out

def _find_mentioned_users(db: Session, text: str) -> list:
    """Parse @username / @Имя from comment text; match staff users (longest key first)."""
    if not text or "@" not in text:
        return []
    users = db.query(User).all()
    candidates = []
    for u in users:
        for key in _mention_keys_for_user(u):
            candidates.append((len(key), key, u))
    candidates.sort(key=lambda x: (-x[0], x[1].lower()))
    found = []
    seen_ids = set()
    for _, key, u in candidates:
        # Boundary: not part of a larger word; allow spaces inside full_name keys
        pattern = r"(?<![\w])@" + re.escape(key) + r"(?![\w])"
        if re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE):
            if u.id not in seen_ids:
                seen_ids.add(u.id)
                found.append(u)
    return found

def _task_observers_payload(db: Session, task_id: int) -> list:
    obs = (
        db.query(TaskObserver)
        .filter(TaskObserver.task_id == task_id)
        .order_by(TaskObserver.id.asc())
        .all()
    )
    return [{"id": o.id, "user_id": o.user_id, "name": o.name} for o in obs]

def _ensure_observers_from_mentions(db: Session, task: Task, text: str) -> list:
    """Add mentioned users as TaskObserver if they are not creator/assignee/observer.
    Returns list of observer dicts for the task after update.
    """
    mentioned = _find_mentioned_users(db, text)
    if not mentioned:
        return _task_observers_payload(db, task.id)

    assignee_uids = {a.user_id for a in (task.assignees or []) if a.user_id}
    assignee_names = {(a.name or "").strip().lower() for a in (task.assignees or []) if a.name}
    if task.assignee:
        assignee_names.add(task.assignee.strip().lower())
    observer_uids = {o.user_id for o in (task.observers or []) if o.user_id}
    observer_names = {(o.name or "").strip().lower() for o in (task.observers or []) if o.name}
    creator_id = task.creator_id
    creator_name = (task.created_by or "").strip().lower()

    for u in mentioned:
        display = (u.full_name or u.username or "").strip()
        if not display:
            continue
        uname = (u.username or "").strip().lower()
        dname = display.lower()
        if creator_id and u.id == creator_id:
            continue
        if creator_name and (dname == creator_name or uname == creator_name):
            continue
        if u.id in assignee_uids or dname in assignee_names or uname in assignee_names:
            continue
        if u.id in observer_uids or dname in observer_names or uname in observer_names:
            continue
        db.add(TaskObserver(task_id=task.id, user_id=u.id, name=display))
        observer_uids.add(u.id)
        observer_names.add(dname)
        if uname:
            observer_names.add(uname)

    db.flush()
    return _task_observers_payload(db, task.id)

def _sync_task_checklist(db: Session, task: Task, items):
    """Replace checklist when list provided."""
    db.query(TaskChecklistItem).filter(TaskChecklistItem.task_id == task.id).delete()
    for i, it in enumerate(items or []):
        if isinstance(it, dict):
            it = TaskChecklistItemIn(**it)
        text = (it.text or "").strip()
        if not text:
            continue
        db.add(TaskChecklistItem(
            task_id=task.id,
            text=text,
            is_done=bool(it.is_done),
            sort_order=it.sort_order if it.sort_order is not None else i,
        ))

def _ensure_legacy_assignee_rows(db: Session, task: Task):
    """If task has assignee string but no TaskAssignee rows — seed one."""
    if not task.assignee:
        return
    existing = db.query(TaskAssignee).filter(TaskAssignee.task_id == task.id).count()
    if existing:
        return
    uid, name = _resolve_task_person(db, TaskPersonIn(name=task.assignee))
    db.add(TaskAssignee(task_id=task.id, user_id=uid, name=name or task.assignee))
    db.commit()

def _task_comment_to_dict(c: TaskComment) -> dict:
    author = ""
    if c.user:
        author = c.user.full_name or c.user.username or ""
    return {
        "id": c.id,
        "task_id": c.task_id,
        "user_id": c.user_id,
        "author": author,
        "text": c.text,
        "created_at": c.created_at.strftime("%d.%m.%Y %H:%M") if c.created_at else "",
        "created_at_iso": c.created_at.isoformat() if c.created_at else "",
    }

def _checklist_to_dict(it: TaskChecklistItem) -> dict:
    return {
        "id": it.id,
        "task_id": it.task_id,
        "text": it.text,
        "is_done": bool(it.is_done),
        "sort_order": it.sort_order or 0,
    }

def _task_to_dict(t: Task, comment_count: Optional[int] = None) -> dict:
    today = datetime.today().strftime("%Y-%m-%d")
    overdue = bool(t.due_date and t.status not in ("done",) and t.due_date[:10] < today)
    if comment_count is None:
        comment_count = len(t.comments) if t.comments is not None else 0
    assignees = [
        {"id": a.id, "user_id": a.user_id, "name": a.name}
        for a in (t.assignees or [])
    ]
    if not assignees and t.assignee:
        assignees = [{"id": None, "user_id": None, "name": t.assignee}]
    observers = [
        {"id": o.id, "user_id": o.user_id, "name": o.name}
        for o in (t.observers or [])
    ]
    checklist = [_checklist_to_dict(c) for c in (t.checklist_items or [])]
    done_n = sum(1 for c in checklist if c["is_done"])
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "assignee": t.assignee or (assignees[0]["name"] if assignees else None),
        "assignees": assignees,
        "observers": observers,
        "checklist": checklist,
        "checklist_done": done_n,
        "checklist_total": len(checklist),
        "created_by": t.created_by,
        "creator_id": t.creator_id,
        "due_date": t.due_date, "priority": t.priority or "normal",
        "status": t.status or "open",
        "status_label": TASK_STATUSES.get(t.status or "open", t.status or "open"),
        "deal_id": t.deal_id,
        "deal_title": t.deal.title if t.deal else None,
        "tags": t.tags or "",
        "tags_list": _task_tags_list(t.tags),
        "overdue": overdue,
        "comment_count": comment_count,
        "created_at": t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else "",
        "created_at_date": t.created_at.strftime("%d.%m.%Y") if t.created_at else "",
        "completed_at": t.completed_at.strftime("%d.%m.%Y %H:%M") if t.completed_at else None,
    }

@app.get("/api/tasks")
def get_tasks(deal_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Task)
    if deal_id:
        query = query.filter(Task.deal_id == deal_id)
    tasks = query.order_by(Task.status, Task.due_date).all()
    counts = dict(
        db.query(TaskComment.task_id, func.count(TaskComment.id))
        .group_by(TaskComment.task_id)
        .all()
    ) if tasks else {}
    return [_task_to_dict(t, comment_count=counts.get(t.id, 0)) for t in tasks]

@app.get("/api/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    count = db.query(TaskComment).filter(TaskComment.task_id == task_id).count()
    data = _task_to_dict(task, comment_count=count)
    data["comments"] = [
        _task_comment_to_dict(c)
        for c in db.query(TaskComment).filter(TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at.asc()).all()
    ]
    return data

@app.post("/api/tasks")
def create_task(t: TaskCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    status = t.status if t.status in TASK_STATUSES else "open"
    people = t.assignees
    if people is None and t.assignee:
        people = [TaskPersonIn(name=t.assignee)]
    if not people:
        people = [TaskPersonIn(user_id=user.id, name=user.full_name or user.username)]
    primary = None
    for p in people:
        _, name = _resolve_task_person(db, p)
        if name:
            primary = name
            break
    task = Task(
        title=t.title,
        description=t.description,
        assignee=primary or (user.full_name or user.username),
        created_by=user.full_name or user.username,
        creator_id=user.id,
        due_date=t.due_date,
        priority=t.priority or "normal",
        status=status,
        deal_id=t.deal_id,
        tags=t.tags,
    )
    db.add(task)
    db.flush()
    _sync_task_people(db, task, assignees=people, observers=t.observers if t.observers is not None else [])
    if t.checklist is not None:
        _sync_task_checklist(db, task, t.checklist)
    db.commit()
    db.refresh(task)
    if task.deal_id:
        db.add(DealHistory(deal_id=task.deal_id, action_text=f"Создана задача: {task.title}"))
        db.commit()
    return {"id": task.id, "status": "success"}

@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, t: TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    for field in ("title", "description", "due_date", "priority", "deal_id", "tags"):
        value = getattr(t, field)
        if value is not None:
            setattr(task, field, value)
    if t.assignees is not None:
        _sync_task_people(db, task, assignees=t.assignees)
    elif t.assignee is not None:
        _sync_task_people(db, task, assignees=[TaskPersonIn(name=t.assignee)])
    if t.observers is not None:
        _sync_task_people(db, task, observers=t.observers)
    if t.checklist is not None:
        _sync_task_checklist(db, task, t.checklist)
    if t.status is not None:
        if t.status not in TASK_STATUSES:
            return JSONResponse(status_code=400, content={"error": "Неизвестный статус"})
        task.status = t.status
        task.completed_at = datetime.utcnow() if t.status == "done" else None
    db.commit()
    return {"status": "success"}

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        db.query(TaskComment).filter(TaskComment.task_id == task_id).delete()
        db.query(TaskAssignee).filter(TaskAssignee.task_id == task_id).delete()
        db.query(TaskObserver).filter(TaskObserver.task_id == task_id).delete()
        db.query(TaskChecklistItem).filter(TaskChecklistItem.task_id == task_id).delete()
        db.delete(task)
        db.commit()
    return {"status": "success"}

@app.get("/api/tasks/{task_id}/comments")
def get_task_comments(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    comments = (
        db.query(TaskComment)
        .filter(TaskComment.task_id == task_id)
        .order_by(TaskComment.created_at.asc())
        .all()
    )
    return [_task_comment_to_dict(c) for c in comments]

@app.post("/api/tasks/{task_id}/comments")
def create_task_comment(
    task_id: int,
    body: TaskCommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Пустой комментарий"})
    comment = TaskComment(task_id=task_id, user_id=user.id, text=text)
    db.add(comment)
    observers = _ensure_observers_from_mentions(db, task, text)
    db.commit()
    db.refresh(comment)
    result = _task_comment_to_dict(comment)
    result["observers"] = observers
    return result

@app.get("/api/tasks/{task_id}/checklist")
def get_task_checklist(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    items = (
        db.query(TaskChecklistItem)
        .filter(TaskChecklistItem.task_id == task_id)
        .order_by(TaskChecklistItem.sort_order.asc(), TaskChecklistItem.id.asc())
        .all()
    )
    return [_checklist_to_dict(i) for i in items]

@app.post("/api/tasks/{task_id}/checklist")
def add_checklist_item(
    task_id: int,
    body: TaskChecklistCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Пустой пункт"})
    max_ord = db.query(func.max(TaskChecklistItem.sort_order)).filter(
        TaskChecklistItem.task_id == task_id
    ).scalar()
    item = TaskChecklistItem(
        task_id=task_id,
        text=text,
        is_done=False,
        sort_order=body.sort_order if body.sort_order is not None else ((max_ord or 0) + 1),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _checklist_to_dict(item)

@app.put("/api/tasks/{task_id}/checklist/{item_id}")
def update_checklist_item(
    task_id: int,
    item_id: int,
    body: TaskChecklistUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.query(TaskChecklistItem).filter(
        TaskChecklistItem.id == item_id,
        TaskChecklistItem.task_id == task_id,
    ).first()
    if not item:
        return JSONResponse(status_code=404, content={"error": "Пункт не найден"})
    if body.text is not None:
        text = body.text.strip()
        if not text:
            return JSONResponse(status_code=400, content={"error": "Пустой пункт"})
        item.text = text
    if body.is_done is not None:
        item.is_done = bool(body.is_done)
    if body.sort_order is not None:
        item.sort_order = body.sort_order
    db.commit()
    db.refresh(item)
    return _checklist_to_dict(item)

@app.delete("/api/tasks/{task_id}/checklist/{item_id}")
def delete_checklist_item(
    task_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.query(TaskChecklistItem).filter(
        TaskChecklistItem.id == item_id,
        TaskChecklistItem.task_id == task_id,
    ).first()
    if item:
        db.delete(item)
        db.commit()
    return {"status": "success"}

@app.get("/companies", response_class=HTMLResponse)
async def read_companies(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("companies.html", {"request": request, "active_page": "companies"})

@app.get("/users", response_class=HTMLResponse)
async def read_users(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>Доступ только для администратора.</h3>", status_code=403)
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "users"})

@app.get("/api/users")
def get_users(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    users = db.query(User).all()
    return [{
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "role": u.role,
        "permissions": u.permissions,
    } for u in users]

@app.get("/api/users/names")
def get_user_names(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Список сотрудников (для выбора ответственного) — доступен всем авторизованным."""
    return [{"id": u.id, "username": u.username, "full_name": u.full_name or u.username} for u in db.query(User).all()]

@app.get("/api/users/sections")
def get_user_sections(user: User = Depends(get_current_user)):
    """Справочник разделов и флагов прав для настройки доступа."""
    return {"sections": auth.SECTIONS, "flags": auth.PERMISSION_FLAGS}

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    full_name: Optional[str] = None
    permissions: Optional[List[str]] = None

class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    full_name: Optional[str] = None
    permissions: Optional[List[str]] = None

@app.post("/api/users")
def create_user(u: UserCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if db.query(User).count() >= 10:
        return JSONResponse(status_code=400, content={"error": "Максимальное количество пользователей (10) достигнуто"})
    
    existing = db.query(User).filter(User.username == u.username).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Пользователь уже существует"})
        
    new_user = User(
        username=u.username,
        hashed_password=get_password_hash(u.password),
        role=u.role,
        full_name=u.full_name,
        permissions=u.permissions,
    )
    db.add(new_user)
    db.commit()
    return {"status": "success"}

@app.put("/api/users/{user_id}")
def update_user(user_id: int, u: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return JSONResponse(status_code=404, content={"error": "Пользователь не найден"})
    if u.password:
        target.hashed_password = get_password_hash(u.password)
    if u.role is not None:
        # Нельзя снять роль админа с самого себя (чтобы не потерять доступ)
        if target.id == current_user.id and u.role != "admin":
            return JSONResponse(status_code=400, content={"error": "Нельзя снять роль администратора с самого себя"})
        target.role = u.role
    if u.full_name is not None:
        target.full_name = u.full_name
    if u.permissions is not None:
        target.permissions = u.permissions
    db.commit()
    return {"status": "success"}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if current_user.id == user_id:
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить самого себя"})
        
    u = db.query(User).filter(User.id == user_id).first()
    if u:
        db.delete(u)
        db.commit()
    return {"status": "success"}

@app.get("/settings", response_class=HTMLResponse)
async def read_settings(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})

@app.get("/assistant", response_class=HTMLResponse)
async def read_assistant(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("assistant.html", {"request": request, "active_page": "assistant"})

@app.get("/equipment", response_class=HTMLResponse)
async def read_equipment(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("equipment.html", {"request": request, "active_page": "equipment"})

@app.get("/crm", response_class=HTMLResponse)
async def read_crm(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("crm.html", {"request": request, "active_page": "crm"})

@app.get("/tracking/{deal_id}", response_class=HTMLResponse)
def read_tracking(request: Request, deal_id: int, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        return HTMLResponse("Заказ не найден", status_code=404)
    return templates.TemplateResponse("tracking.html", {"request": request, "deal": deal})

# -----------------
# API ROUTES
# -----------------

# -- Folders --
@app.get("/api/folders")
def get_folders(db: Session = Depends(get_db)):
    return db.query(Folder).all()

@app.post("/api/folders")
def create_folder(f: FolderCreate, db: Session = Depends(get_db)):
    new_f = Folder(name=f.name, parent_id=f.parent_id)
    db.add(new_f)
    db.commit()
    db.refresh(new_f)
    return new_f

# -- Equipment --
@app.get("/api/equipment")
def get_equipment(warehouse_type: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Equipment)
    if warehouse_type in ("own", "subrental"):
        q = q.filter(Equipment.warehouse_type == warehouse_type)
    equip_list = q.all()
    
    # Calculate rented quantities based on stages
    rented_counts = db.query(
        DealItem.equipment_id, 
        func.sum(DealItem.quantity).label('total_rented')
    ).join(Deal, Deal.id == DealItem.deal_id)\
     .join(Stage, Deal.stage == Stage.id)\
     .filter(Stage.is_active_rent == True)\
     .group_by(DealItem.equipment_id).all()
     
    rented_map = {row.equipment_id: row.total_rented for row in rented_counts}
    
    result = []
    for eq in equip_list:
        rented = rented_map.get(eq.id, 0)
        stock = eq.stock_quantity or 0
        wtype = getattr(eq, "warehouse_type", None) or "own"
        
        # Build dictionary
        eq_dict = {
            "id": eq.id,
            "name": eq.name,
            "category": eq.category,
            "price": eq.price,
            "cost_price": getattr(eq, "cost_price", None) or 0,
            "warehouse_type": wtype,
            "supplier": getattr(eq, "supplier", None),
            "stock_quantity": stock,
            "status": eq.status,
            "folder_id": eq.folder_id,
            "description": eq.description,
            "photo_url": eq.photo_url,
            "custom_fields": eq.custom_fields,
            "rented_quantity": rented,
            "available_quantity": stock - rented
        }
        result.append(eq_dict)
        
    return result

class EnrichRequest(BaseModel):
    name: str



class AISettingsUpdate(BaseModel):
    api_key: str

class TGTokenUpdate(BaseModel):
    token: str


def save_env_key(key: str, value: str):
    """Сохраняет пару KEY=VALUE в .env и в переменные окружения процесса."""
    env_path = ENV_PATH
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()

    with open(env_path, "w") as f:
        key_found = False
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                key_found = True
            else:
                f.write(line)
        if not key_found:
            f.write(f"{key}={value}\n")

    os.environ[key] = value


@app.post("/api/settings/telegram")
def update_tg_settings(settings: TGTokenUpdate, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    save_env_key("TG_BOT_TOKEN", settings.token)
    notifications.TG_TOKEN = settings.token
    return {"status": "success"}

@app.post("/api/settings/ai")
def update_ai_settings(settings: AISettingsUpdate, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    save_env_key("GEMINI_API_KEY", settings.api_key)
    return {"status": "success"}

class IGSettingsUpdate(BaseModel):
    page_token: str = ""
    verify_token: str = ""

@app.post("/api/settings/instagram")
def update_ig_settings(settings: IGSettingsUpdate, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if settings.page_token:
        save_env_key("IG_PAGE_TOKEN", settings.page_token)
    if settings.verify_token:
        save_env_key("IG_VERIFY_TOKEN", settings.verify_token)
    return {"status": "success"}

class OneCSettingsUpdate(BaseModel):
    api_key: str

@app.post("/api/settings/1c")
def update_1c_settings(settings: OneCSettingsUpdate, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    save_env_key("ONEC_API_KEY", settings.api_key)
    return {"status": "success"}

@app.get("/api/settings/status")
def get_settings_status(user: User = Depends(get_current_user)):
    """Статус подключённых интеграций (без раскрытия самих ключей)."""
    return {
        "telegram": bool(os.getenv("TG_BOT_TOKEN")),
        "gemini": bool(os.getenv("GEMINI_API_KEY")),
        "instagram": bool(os.getenv("IG_PAGE_TOKEN")),
        "onec": bool(os.getenv("ONEC_API_KEY")),
    }

@app.post("/api/equipment")
def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db)):
    data = item.dict()
    wtype = (data.get("warehouse_type") or "own").strip().lower()
    data["warehouse_type"] = "subrental" if wtype == "subrental" else "own"
    data["cost_price"] = float(data.get("cost_price") or 0)
    data["supplier"] = (data.get("supplier") or None) or None
    db_equip = Equipment(**data)
    db.add(db_equip)
    db.commit()
    db.refresh(db_equip)
    return db_equip

@app.post("/api/equipment/{equip_id}/duplicate-subrental")
def duplicate_equipment_to_subrental(
    equip_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Продублировать позицию своего склада в субаренду (новый SKU warehouse_type=subrental)."""
    src = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if not src:
        return JSONResponse(status_code=404, content={"error": "Оборудование не найдено"})
    name_key = (src.name or "").strip().lower()
    existing = db.query(Equipment).filter(Equipment.warehouse_type == "subrental").all()
    twin = next((e for e in existing if (e.name or "").strip().lower() == name_key), None)
    if twin:
        return {
            "id": twin.id,
            "status": "exists",
            "message": "Уже есть в субаренде",
            "equipment": {
                "id": twin.id, "name": twin.name, "category": twin.category,
                "price": twin.price, "cost_price": twin.cost_price or 0,
                "stock_quantity": twin.stock_quantity,
                "warehouse_type": "subrental", "supplier": twin.supplier,
                "folder_id": twin.folder_id, "status": twin.status,
            },
        }
    twin = Equipment(
        name=src.name,
        category=src.category,
        price=src.price,
        cost_price=float(src.cost_price or 0),
        stock_quantity=0,
        status=src.status or "Доступно",
        warehouse_type="subrental",
        supplier=src.supplier,
        folder_id=src.folder_id,
        description=src.description,
        photo_url=src.photo_url,
        weight=src.weight,
        dimensions=src.dimensions,
        power_w=src.power_w,
        dispersion=src.dispersion,
        custom_fields=dict(src.custom_fields or {}) if src.custom_fields else {},
    )
    db.add(twin)
    db.commit()
    db.refresh(twin)
    return {
        "id": twin.id,
        "status": "created",
        "message": "Создана копия в субаренде",
        "equipment": {
            "id": twin.id, "name": twin.name, "category": twin.category,
            "price": twin.price, "cost_price": twin.cost_price or 0,
            "stock_quantity": twin.stock_quantity,
            "warehouse_type": "subrental", "supplier": twin.supplier,
            "folder_id": twin.folder_id, "status": twin.status,
        },
    }

@app.put("/api/equipment/{equip_id}")
def update_equipment(equip_id: int, item: EquipmentCreate, db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if db_equip:
        data = item.dict()
        wtype = (data.get("warehouse_type") or "own").strip().lower()
        data["warehouse_type"] = "subrental" if wtype == "subrental" else "own"
        data["cost_price"] = float(data.get("cost_price") or 0)
        for k, v in data.items():
            setattr(db_equip, k, v)
        db.commit()
    return {"status": "success"}

class PriceUpdate(BaseModel):
    price: float

@app.post("/api/admin/import-catalog-xlsx")
async def import_catalog_xlsx(
    file: UploadFile = File(...),
    update_existing: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Импорт каталога из Excel-шаблона сметы (лист «Для нас»). Только admin."""
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор"})
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xlsm")):
        return JSONResponse(status_code=400, content={"error": "Нужен файл .xlsx"})
    try:
        from catalog_import import import_catalog_from_xlsx
    except ImportError:
        return JSONResponse(
            status_code=500,
            content={"error": "Не установлен openpyxl. Выполните: pip install openpyxl"},
        )
    fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        content = await file.read()
        with open(temp_path, "wb") as f:
            f.write(content)
        result = import_catalog_from_xlsx(db, temp_path, update_existing=update_existing)
        return result
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Ошибка импорта: {e}"})
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


@app.put("/api/equipment/{equip_id}/price")
def update_equipment_price(equip_id: int, upd: PriceUpdate, db: Session = Depends(get_db)):
    """Быстрое обновление цены на складе (из сметы)."""
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if not db_equip:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    db_equip.price = upd.price
    db.commit()
    return {"status": "success", "price": db_equip.price}

@app.delete("/api/equipment/{equip_id}")
def delete_equipment(equip_id: int, db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if db_equip:
        db.delete(db_equip)
        db.commit()
    return {"status": "success"}

@app.post("/api/equipment/{equip_id}/photo")
async def upload_equipment_photo(equip_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if not db_equip:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    ext = file.filename.split(".")[-1]
    filename = f"eq_{equip_id}.{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    with open(filepath, "wb") as buffer:
        buffer.write(await file.read())
        
    db_equip.photo_url = f"/uploads/{filename}"
    db.commit()
    return {"photo_url": db_equip.photo_url}

# -- CRM Companies --
@app.get("/api/companies")
def get_companies(db: Session = Depends(get_db)):
    return db.query(Company).all()

@app.post("/api/companies")
def create_company(comp: CompanyCreate, db: Session = Depends(get_db)):
    db_comp = Company(**comp.dict())
    db.add(db_comp)
    db.commit()
    db.refresh(db_comp)
    return db_comp


def _norm_phone_digits(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


@app.get("/api/companies/check-duplicates")
def check_company_duplicates(phone: Optional[str] = None, bin: Optional[str] = None, exclude_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Предупреждение о возможных дублях (не блокирует сохранение)."""
    matches = []
    phone_n = _norm_phone_digits(phone or "")
    if phone_n:
        for c in db.query(Company).filter(Company.phone.isnot(None)).all():
            if exclude_id and c.id == exclude_id:
                continue
            if _norm_phone_digits(c.phone) == phone_n:
                matches.append({"id": c.id, "name": c.name, "field": "phone", "value": c.phone})
    if bin:
        bin_s = bin.strip()
        for c in db.query(Company).filter(Company.bin == bin_s).all():
            if exclude_id and c.id == exclude_id:
                continue
            matches.append({"id": c.id, "name": c.name, "field": "bin", "value": c.bin})
    return {"duplicates": matches, "has_duplicates": len(matches) > 0}


@app.get("/api/companies/{company_id}")
def get_company_detail(company_id: int, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    deals = db.query(Deal).filter(Deal.company_id == company_id).order_by(Deal.id.desc()).all()
    deals_data = []
    for d in deals:
        deals_data.append({
            "id": d.id,
            "title": d.title,
            "stage_name": d.stage_obj.name if d.stage_obj else "Unknown",
            "pipeline_name": d.pipeline.name if d.pipeline else "Unknown",
            "event_date": d.event_date,
            "final_sum": d.final_sum
        })

    return {
        "company": {
            "id": db_comp.id, "name": db_comp.name, "bin": db_comp.bin, "director_name": db_comp.director_name,
            "phone": db_comp.phone, "email": db_comp.email, "requisites": db_comp.requisites,
            "based_on": db_comp.based_on, "address": db_comp.address, "bank_name": db_comp.bank_name,
            "kbe": db_comp.kbe, "bik": db_comp.bik,
            "instagram": db_comp.instagram, "telegram_chat_id": db_comp.telegram_chat_id
        },
        "deals": deals_data,
        "contacts": [{
            "id": c.id, "name": c.name, "position": c.position,
            "phone": c.phone, "email": c.email, "comment": c.comment,
            "is_primary": bool(c.is_primary),
        } for c in db_comp.contacts],
    }


@app.get("/api/contacts/{contact_id}")
def get_contact_detail(contact_id: int, db: Session = Depends(get_db)):
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if not c:
        return JSONResponse(status_code=404, content={"error": "Контакт не найден"})
    deals = db.query(Deal).filter(Deal.contact_id == contact_id).order_by(Deal.id.desc()).all()
    if not deals and c.company_id:
        deals = db.query(Deal).filter(Deal.company_id == c.company_id).order_by(Deal.id.desc()).limit(20).all()
    return {
        "contact": {
            "id": c.id, "name": c.name, "position": c.position,
            "phone": c.phone, "email": c.email, "comment": c.comment,
            "company_id": c.company_id, "is_primary": bool(c.is_primary),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "company_name": c.company.name if c.company else None,
            "company_phone": c.company.phone if c.company else None,
            "company_address": c.company.address if c.company else None,
        },
        "deals": [{
            "id": d.id, "title": d.title,
            "stage_name": d.stage_obj.name if d.stage_obj else "",
            "event_date": d.event_date, "final_sum": d.final_sum or 0,
            "source": d.source or d.chat_channel or "manual",
        } for d in deals],
    }


class CrmNoteIn(BaseModel):
    text: str


@app.get("/api/crm-notes")
def get_crm_notes(entity_type: str, entity_id: int, db: Session = Depends(get_db)):
    notes = db.query(CrmNote).filter(
        CrmNote.entity_type == entity_type,
        CrmNote.entity_id == entity_id,
    ).order_by(CrmNote.created_at.desc()).all()
    return [{
        "id": n.id, "text": n.text, "author": n.author,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n in notes]


@app.post("/api/crm-notes")
def create_crm_note(
    entity_type: str,
    entity_id: int,
    payload: CrmNoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if entity_type not in ("company", "contact"):
        raise HTTPException(status_code=400, detail="entity_type must be company|contact")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    note = CrmNote(
        entity_type=entity_type,
        entity_id=entity_id,
        text=text,
        author=(user.full_name or user.username),
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return {
        "id": note.id, "text": note.text, "author": note.author,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, comp: CompanyCreate, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    for key, value in comp.dict().items():
        setattr(db_comp, key, value)
        
    db.commit()
    db.refresh(db_comp)
    return db_comp

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int, db: Session = Depends(get_db)):
    db_comp = db.query(Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})
    
    if db.query(Deal).filter(Deal.company_id == company_id).first():
        return JSONResponse(status_code=400, content={"error": "Cannot delete company with active deals"})
        
    db.delete(db_comp)
    db.commit()
    return {"status": "success"}

# -- Контакты (контактные лица компаний, как в Битрикс24) --

class ContactCreate(BaseModel):
    name: str
    position: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    comment: Optional[str] = None
    company_id: Optional[int] = None
    is_primary: Optional[bool] = False

class ContactUpdate(BaseModel):
    name: Optional[str] = None
    position: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    comment: Optional[str] = None
    company_id: Optional[int] = None
    is_primary: Optional[bool] = None


def _set_primary_contact(db: Session, contact: Contact):
    if not contact.is_primary or not contact.company_id:
        return
    db.query(Contact).filter(
        Contact.company_id == contact.company_id,
        Contact.id != contact.id,
    ).update({Contact.is_primary: False})


@app.get("/api/contacts")
def get_contacts(company_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Contact)
    if company_id:
        query = query.filter(Contact.company_id == company_id)
    return [{
        "id": c.id, "name": c.name, "position": c.position,
        "phone": c.phone, "email": c.email, "comment": c.comment,
        "company_id": c.company_id, "is_primary": bool(c.is_primary),
        "company_name": c.company.name if c.company else None,
    } for c in query.order_by(Contact.is_primary.desc(), Contact.name).all()]


@app.post("/api/contacts")
def create_contact(c: ContactCreate, db: Session = Depends(get_db)):
    contact = Contact(**c.dict())
    db.add(contact)
    db.flush()
    _set_primary_contact(db, contact)
    db.commit()
    db.refresh(contact)
    return {"id": contact.id, "status": "success"}


@app.put("/api/contacts/{contact_id}")
def update_contact(contact_id: int, c: ContactUpdate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        return JSONResponse(status_code=404, content={"error": "Контакт не найден"})
    for key, value in c.dict(exclude_unset=True).items():
        setattr(contact, key, value)
    _set_primary_contact(db, contact)
    db.commit()
    return {"status": "success"}


@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if contact:
        # Отвязываем контакт от сделок
        db.query(Deal).filter(Deal.contact_id == contact_id).update({Deal.contact_id: None})
        db.delete(contact)
        db.commit()
    return {"status": "success"}


# -- WAHA (WhatsApp API) Proxies --
WAHA_URL = "http://127.0.0.1:3000"

@app.get("/api/wa/status")
def wa_status():
    try:
        r = requests.get(f"{WAHA_URL}/api/sessions/default", timeout=2)
        if r.status_code == 200:
            return r.json()
        return {"status": "NOT_FOUND"}
    except:
        return {"status": "OFFLINE", "error": "WAHA is not running"}

@app.post("/api/wa/start")
def wa_start():
    try:
        r = requests.post(f"{WAHA_URL}/api/sessions/start", json={"name": "default"}, timeout=5)
        return {"status": "started"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/wa/qr")
def wa_qr():
    try:
        # returns JSON {"mimetype": "image/png", "data": "base64..."} usually or we can request format
        r = requests.get(f"{WAHA_URL}/api/sessions/default/auth/qr?format=raw", timeout=2)
        if r.status_code == 200:
            from fastapi.responses import Response
            return Response(content=r.content, media_type="image/png")
        return JSONResponse(status_code=404, content={"error": "QR not ready or already scanned"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class SendMsg(BaseModel):
    phone: str
    text: str

@app.post("/api/wa/send")
def wa_send(msg: SendMsg):
    try:
        clean_phone = ''.join(filter(str.isdigit, msg.phone))
        if clean_phone.startswith('8'): clean_phone = '7' + clean_phone[1:]
        payload = {
            "session": "default",
            "chatId": f"{clean_phone}@c.us",
            "text": msg.text
        }
        r = requests.post(f"{WAHA_URL}/api/sendText", json=payload, timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# -- Custom Fields --
@app.get("/api/custom-fields")
def get_custom_fields(db: Session = Depends(get_db)):
    return db.query(CustomField).all()

@app.post("/api/custom-fields")
def create_custom_field(cf: CustomFieldCreate, db: Session = Depends(get_db)):
    db_cf = CustomField(name=cf.name, field_type=cf.field_type)
    db.add(db_cf)
    db.commit()
    db.refresh(db_cf)
    return db_cf

@app.delete("/api/custom-fields/{field_id}")
def delete_custom_field(field_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор может удалять поля"})
    cf = db.query(CustomField).filter(CustomField.id == field_id).first()
    if not cf:
        return JSONResponse(status_code=404, content={"error": "Поле не найдено"})
    db.query(DealFieldValue).filter(DealFieldValue.field_id == field_id).delete()
    db.delete(cf)
    db.commit()
    return {"status": "success"}

class PipelineRename(BaseModel):
    name: str


@app.get("/api/pipelines")
def get_pipelines(db: Session = Depends(get_db)):
    pipelines = db.query(Pipeline).all()
    return [{"id": p.id, "name": p.name, "stages_count": len(p.stages)} for p in pipelines]

@app.post("/api/pipelines")
def create_pipeline(pl: PipelineCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    db_pl = Pipeline(name=pl.name)
    db.add(db_pl)
    db.commit()
    db.refresh(db_pl)
    # Базовые стадии как у основной воронки
    defaults = [
        "Первичный контакт", "Согласование сметы", "Договор и счет",
        "Предоплата внесена", "Монтаж / Мероприятие", "Успешно реализовано",
        "Сделка проиграна",
    ]
    for i, name in enumerate(defaults):
        db.add(Stage(
            pipeline_id=db_pl.id, name=name, order_index=i + 1,
            is_active_rent=any(k in name for k in ("Предоплата", "Монтаж", "Мероприятие")),
        ))
    db.commit()
    return {"id": db_pl.id, "name": db_pl.name}


@app.put("/api/pipelines/{pipeline_id}")
def rename_pipeline(pipeline_id: int, pl: PipelineRename, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    pipe = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipe:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    pipe.name = pl.name
    db.commit()
    return {"id": pipe.id, "name": pipe.name}

@app.delete("/api/pipelines/{pipeline_id}")
def delete_pipeline(pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pl:
        return {"error": "Pipeline not found"}
    if db.query(Deal).filter(Deal.pipeline_id == pipeline_id).first():
        return {"error": "Cannot delete pipeline with active deals"}
    db.delete(pl)
    db.commit()
    return {"status": "ok"}

@app.get("/api/pipelines/{pipeline_id}/stages")
def get_stages(pipeline_id: int, db: Session = Depends(get_db)):
    stages = db.query(Stage).filter(Stage.pipeline_id == pipeline_id).order_by(Stage.order_index).all()
    return [{"id": s.id, "name": s.name, "order_index": s.order_index, "is_active_rent": s.is_active_rent} for s in stages]

@app.post("/api/pipelines/{pipeline_id}/stages")
def create_stage(pipeline_id: int, stage: StageCreate, db: Session = Depends(get_db)):
    st = Stage(pipeline_id=pipeline_id, name=stage.name, order_index=stage.order_index, is_active_rent=stage.is_active_rent)
    db.add(st)
    db.commit()
    db.refresh(st)
    return {"id": st.id, "name": st.name, "order_index": st.order_index, "is_active_rent": st.is_active_rent}

class StageUpdate(BaseModel):
    name: Optional[str] = None
    order_index: Optional[int] = None
    is_active_rent: Optional[bool] = None

@app.put("/api/stages/{stage_id}")
def update_stage(stage_id: int, stage_update: StageUpdate, db: Session = Depends(get_db)):
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return {"error": "Stage not found"}
    if stage_update.name is not None:
        st.name = stage_update.name
    if stage_update.order_index is not None:
        st.order_index = stage_update.order_index
    if stage_update.is_active_rent is not None:
        st.is_active_rent = stage_update.is_active_rent
    db.commit()
    return {"status": "ok"}

@app.delete("/api/stages/{stage_id}")
def delete_stage(stage_id: int, db: Session = Depends(get_db)):
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return {"error": "Stage not found"}
    if db.query(Deal).filter(Deal.stage == stage_id).first():
        return {"error": "Cannot delete stage with active deals"}
    db.delete(st)
    db.commit()
    return {"status": "ok"}

# -- CRM Deals --

from datetime import datetime

@app.get("/api/equipment/availability")
def get_equipment_availability(start_date: str = None, end_date: str = None, db: Session = Depends(get_db)):
    if not start_date or not end_date:
        return []
    
    # Simple string-based comparison for now, assuming format YYYY-MM-DD
    # Or dd.mm.yyyy, need to make sure frontend sends ISO YYYY-MM-DD format
    # In a real app we would parse dates, but for this simple SQLite setup:
    # Let's just find deals that OVERLAP with the requested dates.
    # We will fetch all deals and filter in Python for safety since dates might be stored in different formats.
    
    # Бронь считаем только по сделкам «в работе» (стадии с флагом is_active_rent,
    # например «Предоплата внесена», «Монтаж / Мероприятие»)
    active_stage_ids = [s.id for s in db.query(Stage).filter(Stage.is_active_rent == True).all()]  # noqa: E712
    if active_stage_ids:
        all_deals = db.query(Deal).filter(Deal.stage.in_(active_stage_ids)).all()
    else:
        all_deals = db.query(Deal).all()
    overlapping_deal_ids = []
    
    def parse_date(d_str):
        if not d_str: return None
        # Handle ISO strings or dates with time by taking the first 10 characters if it contains '-'
        try:
            if "T" in d_str: d_str = d_str.split("T")[0]
            elif " " in d_str: d_str = d_str.split(" ")[0]
            
            if "-" in d_str: return datetime.strptime(d_str, "%Y-%m-%d").date()
            if "." in d_str: return datetime.strptime(d_str, "%d.%m.%Y").date()
        except:
            pass
        return None

    s_dt = parse_date(start_date)
    e_dt = parse_date(end_date)
    
    if s_dt and e_dt:
        for deal in all_deals:
            ds_dt = parse_date(deal.setup_date) or parse_date(deal.event_date)
            de_dt = parse_date(deal.event_date) or parse_date(deal.setup_date)
            if not ds_dt or not de_dt:
                continue
                
            # Check overlap: Overlaps if (StartA <= EndB) and (EndA >= StartB)
            if s_dt <= de_dt and e_dt >= ds_dt:
                overlapping_deal_ids.append(deal.id)
                
    booked_items = {}
    if overlapping_deal_ids:
        # Fetch items belonging to overlapping deals
        items = db.query(DealItem).filter(DealItem.deal_id.in_(overlapping_deal_ids)).all()
        for item in items:
            deal_title = item.deal.title or f"Договор #{item.deal_id}"
            deal_dates = f"{item.deal.setup_date} - {item.deal.event_date}"
            until = item.deal.event_date or item.deal.setup_date or ""
            # DD.MM.YYYY для сообщений пользователю
            try:
                if until and "-" in until:
                    p = until[:10].split("-")
                    until = f"{p[2]}.{p[1]}.{p[0]}"
            except Exception:
                pass
            if item.equipment_id not in booked_items:
                booked_items[item.equipment_id] = {"booked": 0, "conflicts": []}
            booked_items[item.equipment_id]["booked"] += item.quantity
            booked_items[item.equipment_id]["conflicts"].append({
                "deal_id": item.deal_id,
                "deal_title": deal_title,
                "contract_no": f"CRM-{item.deal_id}",
                "until": until,
                "dates": deal_dates,
                "qty": item.quantity
            })
            
    return [{"equipment_id": k, "booked": v["booked"], "conflicts": v["conflicts"]} for k, v in booked_items.items()]

FIXED_CATEGORIES = [
    "Логистика",
    "Персонал",
    "Расходники",
    "Логистика, Тех персонал",
    "Логистика/Тех персонал/Расходники",
]


def _is_fixed_category(category: Optional[str]) -> bool:
    if not category:
        return False
    if category in FIXED_CATEGORIES:
        return True
    low = category.lower()
    return any(k in low for k in ("логистика", "персонал", "расходник"))


def _item_price(di: DealItem) -> float:
    """Цена позиции: сохранённая в смете, иначе текущая цена склада."""
    if di.price is not None:
        return float(di.price)
    return float(di.equipment.price) if di.equipment else 0.0


def _is_personnel_item(deal_item: DealItem) -> bool:
    if not deal_item.equipment:
        return False
    cat = (deal_item.equipment.category or "").strip()
    return cat == "Персонал" or "персонал" in cat.lower()


def _serialize_payroll_line(line: DealPayrollLine) -> dict:
    return {
        "id": line.id,
        "deal_id": line.deal_id,
        "equipment_id": line.equipment_id,
        "role_name": line.role_name or "",
        "user_id": line.user_id,
        "user_name": (line.user.full_name or line.user.username) if line.user else None,
        "quantity": line.quantity or 1,
        "days": line.days or 1,
        "rate": line.rate or 0,
        "gross": line.gross or 0,
        "attendance": line.attendance or "pending",
        "fine_amount": line.fine_amount or 0,
        "comment": line.comment or "",
    }


def _payroll_summary(deal: Deal) -> dict:
    lines = list(deal.payroll_lines or [])
    advances = list(deal.advances or [])
    adv_by_user = {}
    for a in advances:
        adv_by_user[a.user_id] = adv_by_user.get(a.user_id, 0) + float(a.amount or 0)

    by_user = {}
    unassigned_gross = 0.0
    total_gross = 0.0
    total_fines = 0.0
    for line in lines:
        gross = float(line.gross or 0)
        att = line.attendance or "pending"
        fine = float(line.fine_amount or 0) if att == "fine" else 0.0
        if att == "absent":
            continue
        total_gross += gross
        total_fines += fine
        if not line.user_id:
            unassigned_gross += max(0.0, gross - fine)
            continue
        row = by_user.setdefault(line.user_id, {
            "user_id": line.user_id,
            "user_name": (line.user.full_name or line.user.username) if line.user else "—",
            "gross": 0.0,
            "fines": 0.0,
            "advances": float(adv_by_user.get(line.user_id, 0.0)),
            "net": 0.0,
        })
        row["gross"] += gross
        row["fines"] += fine

    for row in by_user.values():
        row["net"] = max(0.0, row["gross"] - row["fines"] - row["advances"])

    total_advances = sum(float(a.amount or 0) for a in advances)
    total_net = sum(r["net"] for r in by_user.values())

    return {
        "lines_count": len(lines),
        "by_user": list(by_user.values()),
        "unassigned_gross": unassigned_gross,
        "total_gross": total_gross,
        "total_fines": total_fines,
        "total_advances": total_advances,
        "total_net": total_net,
    }


def generate_payroll_for_deal(db: Session, deal: Deal, replace: bool = True) -> int:
    """Создаёт строки ведомости из позиций сметы категории «Персонал»."""
    if replace and deal.payroll_lines:
        for line in list(deal.payroll_lines):
            db.delete(line)
        db.flush()

    created = 0
    for it in deal.items:
        if not _is_personnel_item(it):
            continue
        qty = int(it.quantity or 1)
        days = int(it.days or 1)
        rate = _item_price(it)
        gross = rate * qty * days
        db.add(DealPayrollLine(
            deal_id=deal.id,
            equipment_id=it.equipment_id,
            role_name=it.equipment.name if it.equipment else "Персонал",
            user_id=None,
            quantity=qty,
            days=days,
            rate=rate,
            gross=gross,
            attendance="pending",
            fine_amount=0.0,
        ))
        created += 1
    return created


def _deal_calc_items(deal: Deal, exclude_subrental: bool = False) -> list:
    items = []
    for di in deal.items:
        eq = di.equipment
        if not eq:
            continue
        wtype = getattr(eq, "warehouse_type", None) or "own"
        if exclude_subrental and wtype == "subrental":
            continue
        cat_type = "fixed" if _is_fixed_category(eq.category) else "equipment"
        items.append({
            "name": eq.name,
            "price": _item_price(di),
            "quantity": di.quantity,
            "days": di.days,
            "category_type": cat_type,
            "photo_url": eq.photo_url,
            "description": eq.description,
            "warehouse_type": wtype,
            "cost_price": float(getattr(eq, "cost_price", 0) or 0),
            "equipment_id": eq.id,
            "category": eq.category,
            "supplier": getattr(eq, "supplier", None),
        })
    return items


def _deal_tax(deal: Deal) -> float:
    """Налог всегда 16%. При чтении подтягиваем значение в БД, если устарело."""
    current = float(getattr(deal, "tax_percentage", 0) or 0)
    if current != FIXED_TAX_PERCENTAGE:
        try:
            deal.tax_percentage = FIXED_TAX_PERCENTAGE
        except Exception:
            pass
    return FIXED_TAX_PERCENTAGE


def _calc_deal(deal: Deal, exclude_subrental: bool = False) -> dict:
    return calculate_estimate(
        _deal_calc_items(deal, exclude_subrental=exclude_subrental),
        deal.discount_percentage or 0,
        _deal_tax(deal),
    )


def _recalc_deal_sum(db: Session, deal: Deal) -> None:
    result = _calc_deal(deal)
    deal.final_sum = result["grand_total"]
    db.commit()


def _estimate_totals_payload(result: dict) -> dict:
    return {
        "equipment_base": result.get("equipment_base", 0),
        "equipment_total": result.get("equipment_total", 0),
        "fixed_total": result.get("fixed_total", 0),
        "discount_amount": result.get("discount_amount", 0),
        "after_discount": result.get("after_discount", 0),
        "tax_percentage": result.get("tax_percentage", 0),
        "tax_amount": result.get("tax_amount", 0),
        "grand_total": result.get("grand_total", 0),
        "cost_total": result.get("cost_total", 0),
        "margin": result.get("margin", 0),
    }


def _default_assignee_id(db: Session) -> Optional[int]:
    u = db.query(User).filter(User.role.in_(["admin", "manager"])).order_by(User.id).first()
    return u.id if u else None


def _user_crm_own_only(user: User) -> bool:
    if not user or user.role == "admin":
        return False
    perms = user.permissions or []
    return "crm_own_only" in perms


def _user_hide_prices(user: User) -> bool:
    if not user or user.role == "admin":
        return False
    return "hide_prices" in (user.permissions or [])


def _user_assigned_to_deal(db: Session, user: User, deal: Deal) -> bool:
    if not user or not deal:
        return False
    if user.role == "admin":
        return True
    if deal.assignee_id == user.id:
        return True
    return db.query(DealStaffAssignment).filter(
        DealStaffAssignment.deal_id == deal.id,
        DealStaffAssignment.user_id == user.id,
    ).first() is not None


def _deal_shifts(deal: Deal) -> float:
    try:
        return float(getattr(deal, "shifts", None) or 1)
    except (TypeError, ValueError):
        return 1.0


def _estimate_header_fields(deal: Deal) -> dict:
    """Шапка сметы как в Excel: проект, контакт, менеджер, город, выезд/возврат, смены."""
    depart = deal.setup_date or ""
    ret = deal.event_date or ""
    rent_period = ""
    if depart or ret:
        rent_period = f"{depart or '—'} — {ret or '—'}"

    contact_name = ""
    try:
        if deal.contact:
            contact_name = deal.contact.name or ""
    except Exception:
        contact_name = ""

    manager_name = ""
    try:
        if deal.assignee:
            manager_name = deal.assignee.full_name or deal.assignee.username or ""
    except Exception:
        manager_name = ""

    shifts = _deal_shifts(deal)
    shifts_label = str(int(shifts)) if shifts == int(shifts) else str(shifts)

    return {
        "company_name": deal.company.name if deal.company else "",
        "event_name": deal.title or "",
        "project_name": deal.title or "",
        "contact_name": contact_name,
        "manager_name": manager_name,
        "city": (getattr(deal, "city", None) or "") or "",
        "event_address": deal.event_address or "",
        "departure_date": depart,
        "return_date": ret,
        "rent_period": rent_period,
        "shifts": shifts,
        "shifts_label": shifts_label,
    }


def _build_technichka_context(deal: Deal, assignee_name: str = "") -> dict:
    result = _calc_deal(deal)
    header = _estimate_header_fields(deal)
    return {
        "number": f"TECH-{deal.id}",
        "date": datetime.today().strftime("%d.%m.%Y"),
        **header,
        "assignee_name": assignee_name or "",
        "items": result["items"],
    }


def _save_technichka_file(deal: Deal, assignee_name: str = "") -> tuple:
    """Генерирует DOCX технички, сохраняет в uploads. Returns (url, filename, abs_path)."""
    from document_generator import generate_technichka_docx
    import uuid
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    fname = f"technichka_deal{deal.id}_{uuid.uuid4().hex[:8]}.docx"
    abs_path = os.path.join(UPLOADS_DIR, fname)
    generate_technichka_docx(_build_technichka_context(deal, assignee_name), abs_path)
    return f"/uploads/{fname}", fname, abs_path


def assign_staff_to_deal(
    db: Session,
    deal: Deal,
    emp: User,
    created_by: str,
    role_name: Optional[str] = None,
    note: Optional[str] = None,
) -> DealStaffAssignment:
    """Назначить сотрудника: техничка → вложение → задача (высокий приоритет) → напоминание за 1 день."""
    existing = db.query(DealStaffAssignment).filter(
        DealStaffAssignment.deal_id == deal.id,
        DealStaffAssignment.user_id == emp.id,
    ).first()
    if existing:
        return existing

    emp_name = emp.full_name or emp.username
    url, fname, _ = _save_technichka_file(deal, emp_name)
    att = DealAttachment(
        deal_id=deal.id,
        kind="file",
        url=url,
        title=f"Техничка — {emp_name}",
        file_name=fname,
    )
    db.add(att)
    db.flush()

    event_day = (deal.event_date or deal.setup_date or "")[:10]
    setup_day = (deal.setup_date or deal.event_date or "")[:10]
    city = (getattr(deal, "city", None) or "").strip()
    address = (deal.event_address or "").strip()
    place = ", ".join(x for x in [city, address] if x) or "—"
    dates_line = f"Выезд / монтаж: {setup_day or '—'} · Возврат / мероприятие: {event_day or '—'}"
    desc_parts = [
        f"Вы назначены на проект «{deal.title}».",
        f"Адрес: {place}",
        dates_line,
        f"Роль: {role_name or '—'}",
        f"Техничка (вложена к сделке): {url}",
        "Скачивать техничку из сметы не нужно — файл уже прикреплён к задаче и сделке.",
    ]
    if note:
        desc_parts.append(f"Комментарий: {note}")
    task = Task(
        title=f"Выезд: {deal.title}",
        description="\n".join(desc_parts),
        assignee=emp_name,
        created_by=created_by,
        due_date=event_day or setup_day or None,
        priority="high",
        deal_id=deal.id,
        status="open",
        tags="выезд,техничка",
    )
    db.add(task)
    db.flush()
    db.add(TaskAssignee(task_id=task.id, user_id=emp.id, name=emp_name))
    db.add(TaskChecklistItem(task_id=task.id, text="Изучить техничку", is_done=False, sort_order=0))
    db.add(TaskChecklistItem(task_id=task.id, text="Подготовить оборудование к выезду", is_done=False, sort_order=1))
    db.add(TaskChecklistItem(task_id=task.id, text="Выезд / монтаж на площадке", is_done=False, sort_order=2))

    # Напоминание за 1 день до эвента/сборки
    remind_day = None
    base_day = event_day or setup_day
    if base_day:
        try:
            remind_day = (datetime.strptime(base_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            remind_day = None
    if remind_day:
        db.add(Activity(
            deal_id=deal.id,
            type="reminder",
            title=f"Напоминание: завтра выезд «{deal.title}» ({emp_name})",
            due_at=remind_day,
            status="planned",
            assignee_id=emp.id,
            created_by=created_by,
        ))

    row = DealStaffAssignment(
        deal_id=deal.id,
        user_id=emp.id,
        role_name=(role_name or "").strip() or None,
        note=(note or "").strip() or None,
        task_id=task.id,
        attachment_id=att.id,
        notified_at=datetime.utcnow(),
        created_by=created_by,
    )
    db.add(row)
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Назначен сотрудник {emp_name}: техничка, задача (приоритет высокий) и напоминание за 1 день",
    ))
    return row


def _deal_has_overdue_activity(db: Session, deal_id: int) -> bool:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    acts = db.query(Activity).filter(
        Activity.deal_id == deal_id,
        Activity.status == "planned",
        Activity.due_at.isnot(None),
    ).all()
    for a in acts:
        due = (a.due_at or "")[:10]
        if due and due < today:
            return True
    return False


def _serialize_deal_card(d: Deal, db: Session) -> dict:
    assignee_name = None
    if d.assignee_id and d.assignee:
        assignee_name = d.assignee.full_name or d.assignee.username
    return {
        "id": d.id,
        "title": d.title,
        "company_id": d.company_id,
        "company_name": d.company.name if d.company else "Unknown",
        "company_phone": d.company.phone if d.company else "",
        "pipeline_id": d.pipeline_id,
        "stage": d.stage,
        "event_date": d.event_date,
        "setup_date": d.setup_date,
        "final_sum": d.final_sum or 0,
        "chat_channel": d.chat_channel,
        "assignee_id": d.assignee_id,
        "assignee_name": assignee_name,
        "contact_id": d.contact_id,
        "source": d.source or d.chat_channel or "manual",
        "loss_reason": d.loss_reason,
        "is_qualified": bool(d.is_qualified),
        "is_archived": bool(d.is_archived),
        "has_overdue_activity": _deal_has_overdue_activity(db, d.id),
    }


@app.get("/api/deals")
def get_deals(
    request: Request,
    pipeline_id: Optional[int] = None,
    assignee: Optional[str] = None,
    source: Optional[str] = None,
    q: Optional[str] = None,
    rent_from: Optional[str] = None,
    rent_to: Optional[str] = None,
    overdue_only: Optional[bool] = False,
    include_archived: Optional[bool] = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Deal)
    if not include_archived:
        query = query.filter((Deal.is_archived == False) | (Deal.is_archived.is_(None)))  # noqa: E712
    if pipeline_id:
        query = query.filter(Deal.pipeline_id == pipeline_id)
    if _user_crm_own_only(user):
        query = query.filter(Deal.assignee_id == user.id)
    elif assignee == "me":
        query = query.filter(Deal.assignee_id == user.id)
    elif assignee and assignee.isdigit():
        query = query.filter(Deal.assignee_id == int(assignee))
    if source:
        query = query.filter((Deal.source == source) | ((Deal.source.is_(None)) & (Deal.chat_channel == source)))
    if rent_from:
        query = query.filter(Deal.setup_date >= rent_from)
    if rent_to:
        query = query.filter(Deal.event_date <= rent_to)

    deals = query.order_by(Deal.id.desc()).all()
    result = []
    q_norm = (q or "").strip().lower()
    for d in deals:
        card = _serialize_deal_card(d, db)
        if overdue_only and not card["has_overdue_activity"]:
            continue
        if q_norm:
            blob = " ".join([
                card["title"] or "",
                card["company_name"] or "",
                card["company_phone"] or "",
                str(card["id"]),
            ]).lower()
            if q_norm not in blob:
                continue
        result.append(card)
    return result

@app.post("/api/deals")
def create_deal(deal: DealCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Find first stage for the pipeline
    first_stage = db.query(Stage).filter(Stage.pipeline_id == deal.pipeline_id).order_by(Stage.order_index).first()
    stage_id = first_stage.id if first_stage else 1

    contact_id = deal.contact_id
    if not contact_id and deal.company_id:
        primary = db.query(Contact).filter(
            Contact.company_id == deal.company_id, Contact.is_primary == True  # noqa: E712
        ).first()
        if primary:
            contact_id = primary.id

    assignee_id = deal.assignee_id or user.id or _default_assignee_id(db)

    db_deal = Deal(
        title=deal.title,
        company_id=deal.company_id,
        contact_id=contact_id,
        assignee_id=assignee_id,
        pipeline_id=deal.pipeline_id,
        setup_date=deal.setup_date,
        event_date=deal.event_date,
        event_address=deal.event_address,
        city=(deal.city or "").strip() or None,
        shifts=float(deal.shifts) if deal.shifts is not None else 1.0,
        discount_percentage=deal.discount_percentage,
        tax_percentage=FIXED_TAX_PERCENTAGE,
        stage=stage_id,
        source=deal.source or "manual",
        is_qualified=bool(deal.is_qualified),
    )
    db.add(db_deal)
    db.commit()
    db.refresh(db_deal)
    
    history_entry = DealHistory(deal_id=db_deal.id, action_text="Сделка создана")
    db.add(history_entry)
    db.commit()

    if deal.items_json:
        try:
            items_list = json.loads(deal.items_json)
            for it in items_list:
                db_item = DealItem(
                    deal_id=db_deal.id, equipment_id=it['id'],
                    quantity=it['qty'], days=it['days'],
                    price=it.get('price'),
                )
                db.add(db_item)
            db.commit()
            _recalc_deal_sum(db, db_deal)
        except Exception:
            pass

    return {"id": db_deal.id}

@app.put("/api/deals/{deal_id}/stage")
def update_deal_stage(deal_id: int, stage_update: DealStageUpdate, db: Session = Depends(get_db)):
    db_deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not db_deal:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    new_stage_obj = db.query(Stage).filter(Stage.id == stage_update.stage).first()
    if new_stage_obj and "проигра" in (new_stage_obj.name or "").lower():
        reason = (stage_update.loss_reason or "").strip()
        # При каждом переносе на проигранную стадию причина обязательна (текстом)
        if not reason:
            return JSONResponse(status_code=400, content={"error": "Укажите причину отказа"})
        db_deal.loss_reason = reason

    old_stage = db_deal.stage
    old_name = ""
    old_st = db.query(Stage).filter(Stage.id == old_stage).first()
    if old_st:
        old_name = old_st.name
    db_deal.stage = stage_update.stage
    if stage_update.pipeline_id is not None:
        db_deal.pipeline_id = stage_update.pipeline_id

    new_name = new_stage_obj.name if new_stage_obj else str(stage_update.stage)
    hist = f"Стадия изменена: {old_name or old_stage} → {new_name}"
    if db_deal.loss_reason and new_stage_obj and "проигра" in (new_stage_obj.name or "").lower():
        hist += f" (причина: {db_deal.loss_reason})"
    db.add(DealHistory(deal_id=deal_id, action_text=hist))

    # При «Успешно» — сформировать зарплатную ведомость из строк персонала сметы (если ещё нет)
    if new_stage_obj and "успешн" in (new_stage_obj.name or "").lower():
        if not db_deal.payroll_lines:
            n = generate_payroll_for_deal(db, db_deal, replace=True)
            if n:
                db.add(DealHistory(
                    deal_id=deal_id,
                    action_text=f"Сформирована зарплатная ведомость: {n} строк(и) из сметы",
                ))

    db.commit()

    if new_stage_obj and ("Монтаж" in new_stage_obj.name or "доставлен" in new_stage_obj.name.lower()):
        company = db_deal.company
        if company:
            msg = f"Здравствуйте, {company.director_name or company.name}! Ваш заказ '{db_deal.title}' перешел в статус: {new_stage_obj.name}. Оборудование доставлено/монтируется."
            if company.phone:
                notifications.send_wa_message(company.phone, msg)
            if company.telegram_chat_id:
                notifications.send_tg_message(company.telegram_chat_id, msg)
            for sub in db_deal.push_subscriptions:
                notifications.send_web_push({
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                }, msg)

    return {"status": "success"}

@app.get("/api/deals/{deal_id}")
def get_deal_detail(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if not _user_assigned_to_deal(db, user, d):
        if _user_crm_own_only(user) or _user_hide_prices(user):
            return JSONResponse(status_code=403, content={"error": "Нет доступа к этой сделке"})
    hide = _user_hide_prices(user)
    
    items = []
    for i in d.items:
        price = 0 if hide else _item_price(i)
        eq = i.equipment
        items.append({
            "id": i.id,
            "equipment_id": i.equipment_id,
            "quantity": i.quantity,
            "days": i.days,
            "name": eq.name if eq else "Unknown",
            "price": price,
            "stock_price": 0 if hide else (eq.price if eq else 0),
            "category_type": "fixed" if eq and _is_fixed_category(eq.category) else "equipment",
            "warehouse_type": (getattr(eq, "warehouse_type", None) or "own") if eq else "own",
            "cost_price": 0 if hide else float(getattr(eq, "cost_price", 0) or 0) if eq else 0,
            "supplier": getattr(eq, "supplier", None) if eq else None,
            "category": eq.category if eq else "",
        })

    totals = None if hide else _estimate_totals_payload(_calc_deal(d))

    history = [{"action_text": h.action_text, "created_at": h.created_at.strftime("%Y-%m-%d %H:%M:%S"), "kind": "history"} for h in sorted(d.history, key=lambda x: x.created_at, reverse=True)]
    custom_values = {cv.field_id: cv.value for cv in d.custom_values}

    prev_deal = None
    if d.prev_deal_id:
        pd = db.query(Deal).filter(Deal.id == d.prev_deal_id).first()
        if pd:
            prev_deal = {"id": pd.id, "title": pd.title}

    contact = None
    if d.contact_id:
        c = db.query(Contact).filter(Contact.id == d.contact_id).first()
        if c:
            contact = {"id": c.id, "name": c.name, "phone": c.phone, "position": c.position, "email": c.email}

    assignee_name = None
    if d.assignee_id and d.assignee:
        assignee_name = d.assignee.full_name or d.assignee.username

    activities = [{
        "id": a.id, "type": a.type, "title": a.title, "due_at": a.due_at,
        "status": a.status, "assignee_id": a.assignee_id,
        "assignee_name": (a.assignee.full_name or a.assignee.username) if a.assignee else None,
        "result": a.result, "created_by": a.created_by,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
        "kind": "activity",
    } for a in sorted(d.activities, key=lambda x: x.created_at or datetime.utcnow(), reverse=True)]

    invoices = [{
        "id": i.id, "number": i.number, "date": i.date, "amount": i.amount,
        "status": i.status, "company_bin": i.company_bin, "company_name": i.company_name,
    } for i in sorted(d.invoices, key=lambda x: x.id, reverse=True)]

    advances = [{
        "id": a.id, "user_id": a.user_id,
        "user_name": (a.user.full_name or a.user.username) if a.user else "—",
        "amount": a.amount or 0, "date": a.date or "",
        "comment": a.comment or "", "created_by": a.created_by or "",
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M") if a.created_at else "",
    } for a in sorted(d.advances, key=lambda x: x.id, reverse=True)]

    expenses = [{
        "id": e.id, "category": e.category or "other",
        "amount": e.amount or 0, "date": e.date or "",
        "description": e.description or "", "created_by": e.created_by or "",
        "created_at": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
    } for e in sorted(d.expenses, key=lambda x: x.id, reverse=True)]

    return {
        "id": d.id,
        "title": d.title,
        "company_id": d.company_id,
        "company_name": d.company.name if d.company else "Unknown",
        "company_phone": d.company.phone if d.company else "",
        "company_email": d.company.email if d.company else "",
        "company_instagram": d.company.instagram if d.company else "",
        "company_telegram": d.company.telegram_chat_id if d.company else "",
        "pipeline_id": d.pipeline_id,
        "stage": d.stage,
        "setup_date": d.setup_date,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "city": getattr(d, "city", None) or "",
        "shifts": float(getattr(d, "shifts", None) or 1),
        "discount_percentage": 0 if hide else d.discount_percentage,
        "tax_percentage": 0 if hide else _deal_tax(d),
        "final_sum": 0 if hide else d.final_sum,
        "totals": totals,
        "hide_prices": hide,
        "comment": d.comment,
        "created_at": d.created_at.strftime("%d.%m.%Y") if d.created_at else "",
        "chat_channel": d.chat_channel,
        "chat_id": d.chat_id,
        "contact": contact,
        "contact_id": d.contact_id,
        "assignee_id": d.assignee_id,
        "assignee_name": assignee_name,
        "source": d.source or d.chat_channel or "manual",
        "loss_reason": d.loss_reason,
        "is_qualified": bool(d.is_qualified),
        "is_archived": bool(d.is_archived),
        "prev_deal": prev_deal,
        "items": items,
        "history": history,
        "activities": activities,
        "invoices": [] if hide else invoices,
        "advances": [] if hide else advances,
        "expenses": [] if hide else expenses,
        "advances_total": 0 if hide else sum(a["amount"] for a in advances),
        "expenses_total": 0 if hide else sum(e["amount"] for e in expenses),
        "payroll_lines": [] if hide else [_serialize_payroll_line(p) for p in sorted(d.payroll_lines, key=lambda x: x.id)],
        "payroll_summary": None if hide else _payroll_summary(d),
        "staff": [{
            "id": s.id,
            "user_id": s.user_id,
            "user_name": (s.user.full_name or s.user.username) if s.user else "—",
            "role_name": s.role_name or "",
            "note": s.note or "",
            "task_id": s.task_id,
            "attachment_id": s.attachment_id,
            "attachment_url": next((a.url for a in d.attachments if a.id == s.attachment_id), None) if s.attachment_id else None,
            "notified_at": s.notified_at.strftime("%Y-%m-%d %H:%M") if s.notified_at else "",
            "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
        } for s in sorted(d.staff_assignments, key=lambda x: x.id, reverse=True)],
        "custom_values": custom_values
    }

class DealItemsUpdate(BaseModel):
    discount_percentage: float
    tax_percentage: Optional[float] = None
    items: List[dict] # {equipment_id, quantity, days}

@app.put("/api/deals/{deal_id}/items")
def update_deal_items(deal_id: int, update: DealItemsUpdate, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    # Remove old items
    db.query(DealItem).filter(DealItem.deal_id == deal_id).delete()
    
    # Add new items
    for i in update.items:
        di = DealItem(
            deal_id=deal_id,
            equipment_id=i["equipment_id"],
            quantity=i["quantity"],
            days=i["days"],
            price=i.get("price"),
        )
        db.add(di)
        
    d.discount_percentage = update.discount_percentage
    d.tax_percentage = FIXED_TAX_PERCENTAGE
    db.commit()
    db.refresh(d)
    _recalc_deal_sum(db, d)
    
    return {
        "status": "success",
        "final_sum": d.final_sum,
        "totals": _estimate_totals_payload(_calc_deal(d)),
    }

class DealUpdate(BaseModel):
    title: Optional[str] = None
    event_date: Optional[str] = None
    setup_date: Optional[str] = None
    event_address: Optional[str] = None
    city: Optional[str] = None
    shifts: Optional[float] = None
    comment: Optional[str] = None
    company_id: Optional[int] = None
    contact_id: Optional[int] = None
    assignee_id: Optional[int] = None
    source: Optional[str] = None
    loss_reason: Optional[str] = None
    is_qualified: Optional[bool] = None
    is_archived: Optional[bool] = None
    pipeline_id: Optional[int] = None

@app.put("/api/deals/{deal_id}")
def update_deal(deal_id: int, update: DealUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if _user_crm_own_only(user) and d.assignee_id != user.id:
        return JSONResponse(status_code=403, content={"error": "Нет доступа"})

    data = update.dict(exclude_unset=True)
    if "assignee_id" in data and user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Нет права менять ответственного"})

    # Автоподстановка основного контакта при смене компании
    if "company_id" in data and data["company_id"] and "contact_id" not in data:
        primary = db.query(Contact).filter(
            Contact.company_id == data["company_id"], Contact.is_primary == True  # noqa: E712
        ).first()
        if primary:
            data["contact_id"] = primary.id

    for field, value in data.items():
        setattr(d, field, value)

    db.commit()
    return {"status": "success"}


@app.post("/api/deals/{deal_id}/archive")
def archive_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    d.is_archived = True
    db.add(DealHistory(deal_id=deal_id, action_text="Сделка архивирована"))
    db.commit()
    return {"status": "success"}


@app.post("/api/deals/{deal_id}/unarchive")
def unarchive_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    d.is_archived = False
    db.add(DealHistory(deal_id=deal_id, action_text="Сделка восстановлена из архива"))
    db.commit()
    return {"status": "success"}


# -- Дела (Activities) --
class ActivityCreate(BaseModel):
    deal_id: int
    type: str = "call"
    title: str
    due_at: Optional[str] = None
    assignee_id: Optional[int] = None
    status: Optional[str] = "planned"
    result: Optional[str] = None


class ActivityUpdate(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    due_at: Optional[str] = None
    assignee_id: Optional[int] = None
    status: Optional[str] = None
    result: Optional[str] = None


@app.get("/api/activities")
def get_activities(deal_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Activity)
    if deal_id:
        q = q.filter(Activity.deal_id == deal_id)
    rows = q.order_by(Activity.id.desc()).all()
    return [{
        "id": a.id, "deal_id": a.deal_id, "type": a.type, "title": a.title,
        "due_at": a.due_at, "status": a.status, "assignee_id": a.assignee_id,
        "assignee_name": (a.assignee.full_name or a.assignee.username) if a.assignee else None,
        "result": a.result, "created_by": a.created_by,
        "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
    } for a in rows]


@app.post("/api/activities")
def create_activity(a: ActivityCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == a.deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    act = Activity(
        deal_id=a.deal_id, type=a.type or "call", title=a.title,
        due_at=a.due_at, assignee_id=a.assignee_id or user.id,
        status=a.status or "planned", result=a.result,
        created_by=user.username,
    )
    db.add(act)
    db.add(DealHistory(deal_id=a.deal_id, action_text=f"Запланировано дело ({a.type}): {a.title}"))
    db.commit()
    db.refresh(act)
    return {"id": act.id, "status": "success"}


@app.put("/api/activities/{activity_id}")
def update_activity(activity_id: int, a: ActivityUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    act = db.query(Activity).filter(Activity.id == activity_id).first()
    if not act:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    for key, value in a.dict(exclude_unset=True).items():
        setattr(act, key, value)
    if a.status == "done":
        db.add(DealHistory(deal_id=act.deal_id, action_text=f"Дело выполнено: {act.title}"))
    db.commit()
    return {"status": "success"}


@app.delete("/api/activities/{activity_id}")
def delete_activity(activity_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    act = db.query(Activity).filter(Activity.id == activity_id).first()
    if act:
        db.delete(act)
        db.commit()
    return {"status": "success"}


# -- Счета в UI CRM --
class InvoiceCreate(BaseModel):
    deal_id: int
    number: Optional[str] = None
    date: Optional[str] = None
    amount: float = 0.0
    status: str = "draft"
    company_bin: Optional[str] = None
    company_name: Optional[str] = None


class InvoiceUpdate(BaseModel):
    number: Optional[str] = None
    date: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None


def _maybe_move_deal_on_invoice_paid(db: Session, deal: Deal):
    """Простой робот: оплаченный счёт → стадия «Предоплата внесена»."""
    if not deal or not deal.pipeline_id:
        return
    target = None
    for st in db.query(Stage).filter(Stage.pipeline_id == deal.pipeline_id).order_by(Stage.order_index):
        if "Предоплата" in (st.name or ""):
            target = st
            break
    if not target or deal.stage == target.id:
        return
    old = db.query(Stage).filter(Stage.id == deal.stage).first()
    deal.stage = target.id
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Автопереход по оплате счёта: {(old.name if old else deal.stage)} → {target.name}",
    ))


@app.get("/api/invoices")
def get_invoices(deal_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Invoice)
    if deal_id:
        q = q.filter(Invoice.deal_id == deal_id)
    return [{
        "id": i.id, "number": i.number, "date": i.date, "amount": i.amount,
        "status": i.status, "deal_id": i.deal_id,
        "company_bin": i.company_bin, "company_name": i.company_name,
    } for i in q.order_by(Invoice.id.desc()).all()]


@app.post("/api/invoices")
def create_invoice(inv: InvoiceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == inv.deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    number = inv.number or f"INV-{deal.id}-{int(datetime.utcnow().timestamp()) % 100000}"
    if db.query(Invoice).filter(Invoice.number == number).first():
        return JSONResponse(status_code=400, content={"error": "Счёт с таким номером уже есть"})
    company = deal.company
    row = Invoice(
        number=number,
        date=inv.date or datetime.utcnow().strftime("%Y-%m-%d"),
        amount=inv.amount if inv.amount else (deal.final_sum or 0),
        status=inv.status or "draft",
        deal_id=deal.id,
        company_bin=inv.company_bin or (company.bin if company else ""),
        company_name=inv.company_name or (company.name if company else ""),
    )
    db.add(row)
    db.add(DealHistory(deal_id=deal.id, action_text=f"Создан счёт {number} на {row.amount} ₸"))
    if row.status == "paid":
        _maybe_move_deal_on_invoice_paid(db, deal)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "number": row.number, "status": "success"}


@app.put("/api/invoices/{invoice_id}")
def update_invoice(invoice_id: int, inv: InvoiceUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    prev_status = row.status
    for key, value in inv.dict(exclude_unset=True).items():
        setattr(row, key, value)
    if row.status == "paid" and prev_status != "paid" and row.deal_id:
        deal = db.query(Deal).filter(Deal.id == row.deal_id).first()
        if deal:
            db.add(DealHistory(deal_id=deal.id, action_text=f"Счёт {row.number} оплачен"))
            _maybe_move_deal_on_invoice_paid(db, deal)
    db.commit()
    return {"status": "success"}


@app.delete("/api/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if row:
        db.delete(row)
        db.commit()
    return {"status": "success"}


# -- Авансы и расходы проекта (P0) --

class AdvanceCreate(BaseModel):
    deal_id: int
    user_id: int
    amount: float
    date: Optional[str] = None
    comment: Optional[str] = None


class ExpenseCreate(BaseModel):
    deal_id: int
    amount: float
    category: Optional[str] = "other"  # taxi / purchase / delivery / other
    date: Optional[str] = None
    description: Optional[str] = None


EXPENSE_CATEGORIES = {
    "taxi": "Такси",
    "purchase": "Закупка",
    "delivery": "Довоз / логистика",
    "other": "Прочее",
}


@app.get("/api/advances")
def list_advances(deal_id: Optional[int] = None, user_id: Optional[int] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(DealAdvance)
    if deal_id:
        q = q.filter(DealAdvance.deal_id == deal_id)
    if user_id:
        q = q.filter(DealAdvance.user_id == user_id)
    return [{
        "id": a.id, "deal_id": a.deal_id, "user_id": a.user_id,
        "user_name": (a.user.full_name or a.user.username) if a.user else "—",
        "amount": a.amount or 0, "date": a.date or "",
        "comment": a.comment or "", "created_by": a.created_by or "",
        "deal_title": a.deal.title if a.deal else "",
    } for a in q.order_by(DealAdvance.id.desc()).all()]


@app.post("/api/advances")
def create_advance(body: AdvanceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == body.deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    emp = db.query(User).filter(User.id == body.user_id).first()
    if not emp:
        return JSONResponse(status_code=404, content={"error": "Сотрудник не найден"})
    if body.amount is None or body.amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Укажите сумму аванса"})
    row = DealAdvance(
        deal_id=deal.id,
        user_id=emp.id,
        amount=float(body.amount),
        date=body.date or datetime.utcnow().strftime("%Y-%m-%d"),
        comment=(body.comment or "").strip() or None,
        created_by=user.full_name or user.username,
    )
    db.add(row)
    emp_name = emp.full_name or emp.username
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Аванс {row.amount:,.0f} ₸ → {emp_name}".replace(",", " "),
    ))
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": "success"}


@app.delete("/api/advances/{advance_id}")
def delete_advance(advance_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(DealAdvance).filter(DealAdvance.id == advance_id).first()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Не найдено"})
    deal_id = row.deal_id
    emp_name = (row.user.full_name or row.user.username) if row.user else "—"
    amount = row.amount or 0
    db.delete(row)
    db.add(DealHistory(
        deal_id=deal_id,
        action_text=f"Аванс удалён: {amount:,.0f} ₸ ({emp_name})".replace(",", " "),
    ))
    db.commit()
    return {"status": "success"}


@app.get("/api/expenses")
def list_expenses(deal_id: Optional[int] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(DealExpense)
    if deal_id:
        q = q.filter(DealExpense.deal_id == deal_id)
    return [{
        "id": e.id, "deal_id": e.deal_id, "category": e.category or "other",
        "category_label": EXPENSE_CATEGORIES.get(e.category or "other", e.category),
        "amount": e.amount or 0, "date": e.date or "",
        "description": e.description or "", "created_by": e.created_by or "",
        "deal_title": e.deal.title if e.deal else "",
    } for e in q.order_by(DealExpense.id.desc()).all()]


@app.post("/api/expenses")
def create_expense(body: ExpenseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == body.deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    if body.amount is None or body.amount <= 0:
        return JSONResponse(status_code=400, content={"error": "Укажите сумму расхода"})
    cat = body.category if body.category in EXPENSE_CATEGORIES else "other"
    row = DealExpense(
        deal_id=deal.id,
        category=cat,
        amount=float(body.amount),
        date=body.date or datetime.utcnow().strftime("%Y-%m-%d"),
        description=(body.description or "").strip() or None,
        created_by=user.full_name or user.username,
    )
    db.add(row)
    label = EXPENSE_CATEGORIES.get(cat, cat)
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Расход {label}: {row.amount:,.0f} ₸".replace(",", " "),
    ))
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": "success"}


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(DealExpense).filter(DealExpense.id == expense_id).first()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Не найдено"})
    deal_id = row.deal_id
    label = EXPENSE_CATEGORIES.get(row.category or "other", row.category)
    amount = row.amount or 0
    db.delete(row)
    db.add(DealHistory(
        deal_id=deal_id,
        action_text=f"Расход удалён ({label}): {amount:,.0f} ₸".replace(",", " "),
    ))
    db.commit()
    return {"status": "success"}


class PayrollLineUpdate(BaseModel):
    user_id: Optional[int] = None
    attendance: Optional[str] = None  # pending / present / absent / fine
    fine_amount: Optional[float] = None
    comment: Optional[str] = None
    quantity: Optional[int] = None
    days: Optional[int] = None
    rate: Optional[float] = None


@app.post("/api/deals/{deal_id}/payroll/generate")
def api_generate_payroll(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    n = generate_payroll_for_deal(db, deal, replace=True)
    db.add(DealHistory(
        deal_id=deal.id,
        action_text=f"Зарплатная ведомость пересобрана: {n} строк(и)",
    ))
    db.commit()
    db.refresh(deal)
    return {
        "status": "success",
        "created": n,
        "payroll_lines": [_serialize_payroll_line(p) for p in deal.payroll_lines],
        "payroll_summary": _payroll_summary(deal),
    }


class StaffAssignIn(BaseModel):
    user_id: int
    role_name: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/deals/{deal_id}/staff")
def api_assign_staff(deal_id: int, body: StaffAssignIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    emp = db.query(User).filter(User.id == body.user_id).first()
    if not emp:
        return JSONResponse(status_code=404, content={"error": "Сотрудник не найден"})
    existed = db.query(DealStaffAssignment).filter(
        DealStaffAssignment.deal_id == deal.id,
        DealStaffAssignment.user_id == emp.id,
    ).first()
    if existed:
        return JSONResponse(status_code=400, content={"error": "Сотрудник уже назначен на этот проект"})
    row = assign_staff_to_deal(
        db, deal, emp,
        created_by=user.full_name or user.username,
        role_name=body.role_name,
        note=body.note,
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "status": "success",
        "attachment_url": next((a.url for a in deal.attachments if a.id == row.attachment_id), None),
        "task_id": row.task_id,
    }


@app.delete("/api/deals/{deal_id}/staff/{assignment_id}")
def api_unassign_staff(deal_id: int, assignment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(DealStaffAssignment).filter(
        DealStaffAssignment.id == assignment_id,
        DealStaffAssignment.deal_id == deal_id,
    ).first()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Назначение не найдено"})
    name = (row.user.full_name or row.user.username) if row.user else "—"
    db.delete(row)
    db.add(DealHistory(deal_id=deal_id, action_text=f"Снят с проекта: {name}"))
    db.commit()
    return {"status": "success"}


@app.get("/api/deals/{deal_id}/technichka")
def download_technichka(deal_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Скачать техничку (без цен) для склада/персонала."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if not _user_assigned_to_deal(db, user, d) and user.role != "admin" and _user_crm_own_only(user):
        return JSONResponse(status_code=403, content={"error": "Нет доступа"})
    from document_generator import generate_technichka_docx
    who = user.full_name or user.username
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    generate_technichka_docx(_build_technichka_context(d, who), temp_path)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    return FileResponse(
        temp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Technichka_{d.id}.docx",
    )


@app.put("/api/payroll-lines/{line_id}")
def update_payroll_line(line_id: int, body: PayrollLineUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    line = db.query(DealPayrollLine).filter(DealPayrollLine.id == line_id).first()
    if not line:
        return JSONResponse(status_code=404, content={"error": "Строка не найдена"})
    data = body.dict(exclude_unset=True)
    if "user_id" in data:
        uid = data["user_id"]
        if uid:
            emp = db.query(User).filter(User.id == uid).first()
            if not emp:
                return JSONResponse(status_code=404, content={"error": "Сотрудник не найден"})
        line.user_id = uid or None
    if "attendance" in data and data["attendance"]:
        att = data["attendance"]
        if att not in ("pending", "present", "absent", "fine"):
            return JSONResponse(status_code=400, content={"error": "Некорректная явка"})
        line.attendance = att
        if att != "fine":
            line.fine_amount = 0.0
    if "fine_amount" in data and data["fine_amount"] is not None:
        line.fine_amount = max(0.0, float(data["fine_amount"]))
        if line.fine_amount > 0:
            line.attendance = "fine"
    if "comment" in data:
        line.comment = (data["comment"] or "").strip() or None
    if "quantity" in data and data["quantity"] is not None:
        line.quantity = max(1, int(data["quantity"]))
    if "days" in data and data["days"] is not None:
        line.days = max(1, int(data["days"]))
    if "rate" in data and data["rate"] is not None:
        line.rate = float(data["rate"])
    line.gross = float(line.rate or 0) * int(line.quantity or 1) * int(line.days or 1)
    db.commit()
    db.refresh(line)
    deal = db.query(Deal).filter(Deal.id == line.deal_id).first()
    return {
        "status": "success",
        "line": _serialize_payroll_line(line),
        "payroll_summary": _payroll_summary(deal) if deal else None,
    }


class DealCommentRequest(BaseModel):
    comment: str

@app.post("/api/deals/{deal_id}/comments")
def add_deal_comment(deal_id: int, request: DealCommentRequest, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    history_entry = DealHistory(
        deal_id=deal_id,
        action_text=f"Комментарий: {request.comment}"
    )
    db.add(history_entry)
    db.commit()
    return {"status": "success"}

@app.put("/api/deals/{deal_id}/fields")
def update_deal_fields(deal_id: int, update: DealFieldUpdateList, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    for field_update in update.fields:
        cv = db.query(DealFieldValue).filter(DealFieldValue.deal_id == deal_id, DealFieldValue.field_id == field_update.field_id).first()
        if cv:
            cv.value = field_update.value
        else:
            new_cv = DealFieldValue(deal_id=deal_id, field_id=field_update.field_id, value=field_update.value)
            db.add(new_cv)
            
    history_entry = DealHistory(deal_id=deal_id, action_text="Обновлены дополнительные поля")
    db.add(history_entry)
    db.commit()
    return {"status": "success"}

class PushSubModel(BaseModel):
    deal_id: int
    endpoint: str
    keys: dict

@app.post("/api/push/subscribe")
def subscribe_push(sub: PushSubModel, db: Session = Depends(get_db)):
    existing = db.query(PushSubscription).filter_by(endpoint=sub.endpoint, deal_id=sub.deal_id).first()
    if not existing:
        new_sub = PushSubscription(
            deal_id=sub.deal_id,
            endpoint=sub.endpoint,
            p256dh=sub.keys.get("p256dh", ""),
            auth=sub.keys.get("auth", "")
        )
        db.add(new_sub)
        db.commit()
    return {"status": "success"}

@app.get("/api/push/vapid_public_key")
def get_vapid_key():
    import os
    pub = os.getenv("VAPID_PUBLIC_KEY", "")
    return {"public_key": pub}

CHANNEL_LABELS = {"whatsapp": "WhatsApp", "telegram": "Telegram", "instagram": "Instagram"}


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _find_company_for_chat(db: Session, channel: str, chat_id: str):
    if channel == "telegram":
        comp = db.query(Company).filter(Company.telegram_chat_id == str(chat_id)).first()
        if comp:
            return comp
    if channel == "whatsapp":
        phone = _normalize_phone(chat_id.split("@")[0])
        if phone:
            for comp in db.query(Company).filter(Company.phone.isnot(None)).all():
                if _normalize_phone(comp.phone) == phone:
                    return comp
    return None


def ensure_deal_for_chat(db: Session, channel: str, chat_id: str, sender_name: str = None):
    """Автосоздание сделки по входящему сообщению (логика Битрикс24).

    - Есть открытая сделка, привязанная к этому чату — диалог продолжается в ней.
    - Все сделки закрыты — создаётся новая со ссылкой на прошлое обращение.
    - Сделок не было — создаётся новая на первой стадии воронки.
    """
    chat_id = str(chat_id)
    closed_stage_ids = {
        s.id for s in db.query(Stage).all()
        if any(k in (s.name or "") for k in ("Успешно", "проигра", "Проигра"))
    }

    linked = (
        db.query(Deal)
        .filter(Deal.chat_channel == channel, Deal.chat_id == chat_id)
        .order_by(Deal.id.desc())
        .all()
    )
    for d in linked:
        if getattr(d, "is_archived", False):
            continue
        if d.stage not in closed_stage_ids:
            return d  # открытая сделка уже есть — продолжаем диалог в ней

    # Компания: ищем по номеру/чату, иначе создаём карточку клиента
    company = _find_company_for_chat(db, channel, chat_id)
    if not company:
        label = CHANNEL_LABELS.get(channel, channel)
        phone = ""
        if channel == "whatsapp":
            raw = chat_id.split("@")[0]
            phone = f"+{raw}" if raw.isdigit() else ""
        company = Company(
            name=sender_name or f"Клиент {label}",
            phone=phone,
            bin="", director_name=sender_name or "", email="", requisites="",
        )
        if channel == "telegram":
            company.telegram_chat_id = chat_id
        db.add(company)
        db.commit()
        db.refresh(company)

    pipeline = db.query(Pipeline).first()
    first_stage = (
        db.query(Stage)
        .filter(Stage.pipeline_id == pipeline.id)
        .order_by(Stage.order_index)
        .first()
    ) if pipeline else None

    prev_deal = linked[0] if linked else None
    label = CHANNEL_LABELS.get(channel, channel)
    deal = Deal(
        title=f"Заявка из {label} — {sender_name or chat_id}",
        company_id=company.id,
        pipeline_id=pipeline.id if pipeline else 1,
        stage=first_stage.id if first_stage else 1,
        event_date="",
        chat_channel=channel,
        chat_id=chat_id,
        prev_deal_id=prev_deal.id if prev_deal else None,
        source=channel,
        assignee_id=_default_assignee_id(db),
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)

    db.add(DealHistory(deal_id=deal.id, action_text=f"Сделка создана автоматически: входящее сообщение в {label}"))
    if prev_deal:
        db.add(DealHistory(
            deal_id=deal.id,
            action_text=f"Клиент ранее обращался к нам: сделка №{prev_deal.id} «{prev_deal.title}»",
        ))
    db.commit()
    return deal


@app.post("/api/tg/webhook")
def tg_webhook(update: dict, db: Session = Depends(get_db)):
    if "message" in update and "text" in update["message"]:
        msg = update["message"]
        text_msg = msg["text"]
        chat_id = msg["chat"]["id"]
        sender = (msg.get("from") or {}).get("first_name") or str(chat_id)

        if text_msg.startswith("/start company_"):
            try:
                company_id = int(text_msg.split("_")[1])
                comp = db.query(Company).filter(Company.id == company_id).first()
                if comp:
                    comp.telegram_chat_id = str(chat_id)
                    db.commit()
                    notifications.send_tg_message(str(chat_id), "Успешно! Теперь вы будете получать уведомления о заказах сюда.")
            except Exception:
                pass
        elif text_msg.startswith("/start"):
            chatbot.save_message(db, "telegram", str(chat_id), "in", text_msg, sender_name=sender)
        else:
            # Обычное сообщение — сделка в CRM + AI чат-бот
            try:
                ensure_deal_for_chat(db, "telegram", str(chat_id), sender_name=sender)
            except Exception as e:
                print("ensure_deal_for_chat (tg) error:", e)
            chatbot.handle_incoming(db, "telegram", str(chat_id), text_msg, sender_name=sender)
    return {"status": "ok"}


class TGWebhookSetup(BaseModel):
    url: str  # публичный https-адрес сервера, например https://crm.introshow.kz

@app.post("/api/tg/set-webhook")
def tg_set_webhook(setup: TGWebhookSetup, user: User = Depends(get_current_user)):
    """Регистрирует вебхук Telegram-бота на этот сервер."""
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    token = os.getenv("TG_BOT_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"error": "Сначала сохраните токен Telegram-бота"})
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": setup.url.rstrip("/") + "/api/tg/webhook"},
            timeout=10,
        )
        return r.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# -- WhatsApp входящие (webhook от WAHA) --
@app.post("/api/wa/webhook")
def wa_webhook(event: dict, db: Session = Depends(get_db)):
    """Приём входящих сообщений WhatsApp от WAHA.

    В WAHA нужно указать webhook: http://<этот сервер>/api/wa/webhook (событие message).
    """
    if event.get("event") not in ("message", "message.any"):
        return {"status": "ignored"}
    payload = event.get("payload") or {}
    if payload.get("fromMe"):
        return {"status": "ignored"}
    chat_id = payload.get("from") or ""
    text_msg = payload.get("body") or ""
    if not chat_id or not text_msg:
        return {"status": "ignored"}
    sender = ((payload.get("_data") or {}).get("notifyName")) or chat_id.split("@")[0]
    try:
        ensure_deal_for_chat(db, "whatsapp", chat_id, sender_name=sender)
    except Exception as e:
        print("ensure_deal_for_chat (wa) error:", e)
    chatbot.handle_incoming(db, "whatsapp", chat_id, text_msg, sender_name=sender)
    return {"status": "ok"}


# -- Instagram Direct (Meta Graph API webhook) --
@app.get("/api/ig/webhook")
def ig_webhook_verify(request: Request):
    """Верификация вебхука Meta (hub.challenge)."""
    params = request.query_params
    verify_token = os.getenv("IG_VERIFY_TOKEN", "")
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return JSONResponse(status_code=403, content={"error": "Verification failed"})

@app.post("/api/ig/webhook")
def ig_webhook(event: dict, db: Session = Depends(get_db)):
    """Приём входящих сообщений Instagram Direct."""
    try:
        for entry in event.get("entry", []):
            for messaging in entry.get("messaging", []):
                message = messaging.get("message") or {}
                if message.get("is_echo"):
                    continue
                sender_id = (messaging.get("sender") or {}).get("id")
                text_msg = message.get("text")
                if sender_id and text_msg:
                    try:
                        ensure_deal_for_chat(db, "instagram", str(sender_id), sender_name=f"IG {sender_id}")
                    except Exception as e:
                        print("ensure_deal_for_chat (ig) error:", e)
                    chatbot.handle_incoming(db, "instagram", str(sender_id), text_msg, sender_name=f"IG {sender_id}")
    except Exception as e:
        print("IG webhook error:", e)
    return {"status": "ok"}


# -----------------
# ВНУТРЕННИЕ ЧАТЫ СОТРУДНИКОВ (отдельно от клиентского Inbox)
# -----------------

def _user_display(u: User) -> str:
    return (u.full_name or u.username) if u else "Сотрудник"


def _ensure_chat_member(db: Session, chat_id: int, user_id: int) -> InternalChatMember:
    member = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == chat_id, InternalChatMember.user_id == user_id)
        .first()
    )
    if not member:
        member = InternalChatMember(chat_id=chat_id, user_id=user_id, last_read_message_id=0)
        db.add(member)
        db.flush()
    return member


def _find_dm_chat(db: Session, user_a: int, user_b: int) -> Optional[InternalChat]:
    """Найти существующий DM между двумя пользователями."""
    chats_a = [
        r[0]
        for r in db.query(InternalChatMember.chat_id)
        .filter(InternalChatMember.user_id == user_a)
        .all()
    ]
    if not chats_a:
        return None
    shared = (
        db.query(InternalChatMember.chat_id)
        .filter(
            InternalChatMember.user_id == user_b,
            InternalChatMember.chat_id.in_(chats_a),
        )
        .all()
    )
    for (chat_id,) in shared:
        chat = db.query(InternalChat).filter(InternalChat.id == chat_id, InternalChat.chat_type == "dm").first()
        if not chat:
            continue
        member_count = db.query(InternalChatMember).filter(InternalChatMember.chat_id == chat_id).count()
        if member_count == 2:
            return chat
    return None


def _chat_to_dict(db: Session, chat: InternalChat, current_user_id: int) -> dict:
    member = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == chat.id, InternalChatMember.user_id == current_user_id)
        .first()
    )
    last_read = (member.last_read_message_id or 0) if member else 0
    last_msg = (
        db.query(InternalMessage)
        .filter(InternalMessage.chat_id == chat.id)
        .order_by(InternalMessage.id.desc())
        .first()
    )
    unread = (
        db.query(InternalMessage)
        .filter(
            InternalMessage.chat_id == chat.id,
            InternalMessage.id > last_read,
            InternalMessage.sender_id != current_user_id,
        )
        .count()
    )
    members = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == chat.id)
        .all()
    )
    member_users = []
    peer_name = None
    for m in members:
        u = db.query(User).filter(User.id == m.user_id).first()
        if not u:
            continue
        member_users.append({"id": u.id, "name": _user_display(u)})
        if chat.chat_type == "dm" and u.id != current_user_id:
            peer_name = _user_display(u)

    title = chat.title
    if chat.chat_type == "dm":
        title = peer_name or title or "Личный чат"
    elif chat.chat_type == "deal":
        if chat.deal:
            title = chat.title or f"Проект: {chat.deal.title}"
        else:
            title = chat.title or f"Сделка #{chat.deal_id}"

    return {
        "id": chat.id,
        "chat_type": chat.chat_type,
        "title": title,
        "deal_id": chat.deal_id,
        "deal_title": chat.deal.title if chat.deal else None,
        "members": member_users,
        "last_message": last_msg.text if last_msg else "",
        "last_at": last_msg.created_at.strftime("%Y-%m-%d %H:%M:%S") if last_msg and last_msg.created_at else (
            chat.updated_at.strftime("%Y-%m-%d %H:%M:%S") if chat.updated_at else ""
        ),
        "unread": unread,
        "updated_at": chat.updated_at.strftime("%Y-%m-%d %H:%M:%S") if chat.updated_at else "",
    }


@app.get("/api/internal-chats")
def list_internal_chats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    chat_ids = [
        r[0]
        for r in db.query(InternalChatMember.chat_id)
        .filter(InternalChatMember.user_id == user.id)
        .all()
    ]
    if not chat_ids:
        return []
    chats = (
        db.query(InternalChat)
        .filter(InternalChat.id.in_(chat_ids))
        .order_by(InternalChat.updated_at.desc())
        .all()
    )
    return [_chat_to_dict(db, c, user.id) for c in chats]


@app.get("/api/internal-chats/unread-count")
def internal_chats_unread_count(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    memberships = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.user_id == user.id)
        .all()
    )
    total = 0
    for m in memberships:
        total += (
            db.query(InternalMessage)
            .filter(
                InternalMessage.chat_id == m.chat_id,
                InternalMessage.id > (m.last_read_message_id or 0),
                InternalMessage.sender_id != user.id,
            )
            .count()
        )
    return {"unread": total}


class InternalDmCreate(BaseModel):
    user_id: int


@app.post("/api/internal-chats/dm")
def create_or_get_dm(body: InternalDmCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if body.user_id == user.id:
        return JSONResponse(status_code=400, content={"error": "Нельзя создать чат с собой"})
    other = db.query(User).filter(User.id == body.user_id).first()
    if not other:
        return JSONResponse(status_code=404, content={"error": "Сотрудник не найден"})
    chat = _find_dm_chat(db, user.id, other.id)
    if not chat:
        chat = InternalChat(
            chat_type="dm",
            title=None,
            created_by_id=user.id,
            updated_at=datetime.utcnow(),
        )
        db.add(chat)
        db.flush()
        db.add(InternalChatMember(chat_id=chat.id, user_id=user.id, last_read_message_id=0))
        db.add(InternalChatMember(chat_id=chat.id, user_id=other.id, last_read_message_id=0))
        db.commit()
        db.refresh(chat)
    return _chat_to_dict(db, chat, user.id)


class InternalDealChatCreate(BaseModel):
    deal_id: int


@app.post("/api/internal-chats/deal")
def create_or_get_deal_chat(body: InternalDealChatCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    deal = db.query(Deal).filter(Deal.id == body.deal_id).first()
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    chat = (
        db.query(InternalChat)
        .filter(InternalChat.chat_type == "deal", InternalChat.deal_id == deal.id)
        .first()
    )
    if not chat:
        chat = InternalChat(
            chat_type="deal",
            title=f"Проект: {deal.title}",
            deal_id=deal.id,
            created_by_id=user.id,
            updated_at=datetime.utcnow(),
        )
        db.add(chat)
        db.flush()
        _ensure_chat_member(db, chat.id, user.id)
        if deal.assignee_id and deal.assignee_id != user.id:
            _ensure_chat_member(db, chat.id, deal.assignee_id)
        db.commit()
        db.refresh(chat)
    else:
        _ensure_chat_member(db, chat.id, user.id)
        db.commit()
        db.refresh(chat)
    return _chat_to_dict(db, chat, user.id)


@app.get("/api/internal-chats/{chat_id}/messages")
def get_internal_messages(chat_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    member = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == chat_id, InternalChatMember.user_id == user.id)
        .first()
    )
    if not member:
        return JSONResponse(status_code=403, content={"error": "Нет доступа к чату"})
    messages = (
        db.query(InternalMessage)
        .filter(InternalMessage.chat_id == chat_id)
        .order_by(InternalMessage.id)
        .limit(500)
        .all()
    )
    if messages:
        member.last_read_message_id = messages[-1].id
        db.commit()
    return [{
        "id": m.id,
        "sender_id": m.sender_id,
        "sender_name": _user_display(m.sender) if m.sender else "",
        "text": m.text,
        "task_id": m.task_id,
        "mine": m.sender_id == user.id,
        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "",
    } for m in messages]


class InternalMessageSend(BaseModel):
    text: str


@app.post("/api/internal-chats/{chat_id}/messages")
def send_internal_message(
    chat_id: int,
    body: InternalMessageSend,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    text = (body.text or "").strip()
    if not text:
        return JSONResponse(status_code=400, content={"error": "Пустое сообщение"})
    chat = db.query(InternalChat).filter(InternalChat.id == chat_id).first()
    if not chat:
        return JSONResponse(status_code=404, content={"error": "Чат не найден"})
    member = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == chat_id, InternalChatMember.user_id == user.id)
        .first()
    )
    if not member:
        return JSONResponse(status_code=403, content={"error": "Нет доступа к чату"})
    msg = InternalMessage(chat_id=chat_id, sender_id=user.id, text=text)
    db.add(msg)
    chat.updated_at = datetime.utcnow()
    db.flush()
    member.last_read_message_id = msg.id
    db.commit()
    db.refresh(msg)
    return {
        "id": msg.id,
        "sender_id": msg.sender_id,
        "sender_name": _user_display(user),
        "text": msg.text,
        "task_id": None,
        "mine": True,
        "created_at": msg.created_at.strftime("%Y-%m-%d %H:%M:%S") if msg.created_at else "",
    }


class InternalTaskFromMessage(BaseModel):
    title: Optional[str] = None
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[str] = "normal"


@app.post("/api/internal-chats/messages/{message_id}/create-task")
def create_task_from_internal_message(
    message_id: int,
    body: InternalTaskFromMessage,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    msg = db.query(InternalMessage).filter(InternalMessage.id == message_id).first()
    if not msg:
        return JSONResponse(status_code=404, content={"error": "Сообщение не найдено"})
    member = (
        db.query(InternalChatMember)
        .filter(InternalChatMember.chat_id == msg.chat_id, InternalChatMember.user_id == user.id)
        .first()
    )
    if not member:
        return JSONResponse(status_code=403, content={"error": "Нет доступа к чату"})
    chat = db.query(InternalChat).filter(InternalChat.id == msg.chat_id).first()
    if msg.task_id:
        return {"id": msg.task_id, "status": "exists", "deal_id": chat.deal_id if chat else None}

    title = (body.title or "").strip()
    if not title:
        title = (msg.text or "").strip()
        if len(title) > 120:
            title = title[:117] + "…"
    if not title:
        title = "Задача из чата"

    assignee_name = body.assignee or (user.full_name or user.username)
    task = Task(
        title=title,
        description=f"Из внутреннего чата:\n{msg.text}",
        assignee=assignee_name,
        created_by=user.full_name or user.username,
        creator_id=user.id,
        due_date=body.due_date,
        priority=body.priority or "normal",
        deal_id=chat.deal_id if chat else None,
    )
    db.add(task)
    db.flush()
    uid, name = _resolve_task_person(db, TaskPersonIn(name=assignee_name))
    db.add(TaskAssignee(task_id=task.id, user_id=uid, name=name or assignee_name))
    msg.task_id = task.id
    if task.deal_id:
        db.add(DealHistory(deal_id=task.deal_id, action_text=f"Создана задача из чата: {task.title}"))
    db.commit()
    return {"id": task.id, "status": "success", "deal_id": task.deal_id}


# -----------------
# INBOX (единая лента чатов)
# -----------------

@app.get("/api/inbox/chats")
def get_inbox_chats(channel: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(ChatMessage)
    if channel:
        query = query.filter(ChatMessage.channel == channel)
    messages = query.order_by(ChatMessage.id.desc()).limit(1000).all()

    chats = {}
    for m in messages:
        key = (m.channel, m.chat_id)
        if key not in chats:
            chats[key] = {
                "channel": m.channel,
                "chat_id": m.chat_id,
                "name": m.sender_name if m.direction == "in" else m.chat_id,
                "last_message": m.text,
                "last_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        elif m.direction == "in" and m.sender_name and chats[key]["name"] == chats[key]["chat_id"]:
            chats[key]["name"] = m.sender_name

    # Привязка чатов к сделкам CRM (последняя сделка по чату)
    for (ch, cid), chat in chats.items():
        deal = (
            db.query(Deal)
            .filter(Deal.chat_channel == ch, Deal.chat_id == cid)
            .order_by(Deal.id.desc())
            .first()
        )
        if deal:
            chat["deal_id"] = deal.id
            chat["deal_title"] = deal.title
    return list(chats.values())

@app.get("/api/inbox/messages")
def get_inbox_messages(channel: str, chat_id: str, db: Session = Depends(get_db)):
    messages = db.query(ChatMessage)\
        .filter(ChatMessage.channel == channel, ChatMessage.chat_id == chat_id)\
        .order_by(ChatMessage.id).limit(500).all()
    return [{
        "id": m.id,
        "direction": m.direction,
        "text": m.text,
        "sender_name": m.sender_name,
        "is_bot": m.is_bot,
        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    } for m in messages]

class InboxSend(BaseModel):
    channel: str
    chat_id: str
    text: str

@app.post("/api/inbox/send")
def inbox_send(msg: InboxSend, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sender_fn = chatbot.SENDERS.get(msg.channel)
    if not sender_fn:
        return JSONResponse(status_code=400, content={"error": "Неизвестный канал"})
    sent = sender_fn(msg.chat_id, msg.text)
    chatbot.save_message(db, msg.channel, msg.chat_id, "out", msg.text,
                         sender_name=(user.full_name or user.username), is_bot=False)
    return {"status": "success", "delivered": sent}


# -----------------
# AI ЧАТ-БОТ: настройки, база знаний, тест
# -----------------

class BotSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    schedule: Optional[dict] = None
    channels: Optional[dict] = None
    persona: Optional[str] = None
    off_hours_message: Optional[str] = None
    timezone: Optional[str] = None

@app.get("/api/bot/settings")
def get_bot_settings_api(db: Session = Depends(get_db)):
    s = chatbot.get_bot_settings(db)
    return {
        "enabled": s.enabled,
        "schedule": s.schedule or chatbot.DEFAULT_SCHEDULE,
        "channels": s.channels or chatbot.DEFAULT_CHANNELS,
        "persona": s.persona or chatbot.DEFAULT_PERSONA,
        "off_hours_message": s.off_hours_message or chatbot.DEFAULT_OFF_HOURS,
        "timezone": s.timezone or "Asia/Almaty",
        "within_schedule_now": chatbot.is_within_schedule(s),
        "ai_key_configured": bool(os.getenv("GEMINI_API_KEY")),
    }

@app.post("/api/bot/settings")
def update_bot_settings_api(update: BotSettingsUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Недостаточно прав"})
    s = chatbot.get_bot_settings(db)
    if update.enabled is not None:
        s.enabled = update.enabled
    if update.schedule is not None:
        s.schedule = update.schedule
    if update.channels is not None:
        s.channels = update.channels
    if update.persona is not None:
        s.persona = update.persona
    if update.off_hours_message is not None:
        s.off_hours_message = update.off_hours_message
    if update.timezone is not None:
        s.timezone = update.timezone
    db.commit()
    return {"status": "success"}

class KnowledgeCreate(BaseModel):
    title: str
    content: str

@app.get("/api/bot/knowledge")
def get_knowledge(db: Session = Depends(get_db)):
    items = db.query(KnowledgeItem).order_by(KnowledgeItem.id.desc()).all()
    return [{"id": i.id, "title": i.title, "content": i.content,
             "created_at": i.created_at.strftime("%Y-%m-%d %H:%M")} for i in items]

@app.post("/api/bot/knowledge")
def create_knowledge(item: KnowledgeCreate, db: Session = Depends(get_db)):
    ki = KnowledgeItem(title=item.title, content=item.content)
    db.add(ki)
    db.commit()
    db.refresh(ki)
    return {"id": ki.id, "status": "success"}

@app.post("/api/bot/knowledge/upload")
async def upload_knowledge(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Загрузка текстового файла (.txt, .md, .csv) в базу знаний бота."""
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("cp1251", errors="replace")
    ki = KnowledgeItem(title=file.filename or "Файл", content=content[:50000])
    db.add(ki)
    db.commit()
    return {"id": ki.id, "status": "success"}

@app.delete("/api/bot/knowledge/{item_id}")
def delete_knowledge(item_id: int, db: Session = Depends(get_db)):
    ki = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if ki:
        db.delete(ki)
        db.commit()
    return {"status": "success"}

class BotTestRequest(BaseModel):
    text: str

@app.post("/api/bot/test")
def test_bot(req: BotTestRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Тестовый диалог с ботом прямо из панели (не отправляется клиентам)."""
    s = chatbot.get_bot_settings(db)
    knowledge = chatbot._build_knowledge_text(db)
    persona = s.persona or chatbot.DEFAULT_PERSONA
    system_prompt = (
        f"{persona}\n\nБАЗА ЗНАНИЙ КОМПАНИИ (отвечай строго на её основе):\n{knowledge}\n\n"
        "Правила ответа: пиши на языке клиента, кратко (1-4 предложения), тепло и естественно. "
        "Не используй markdown-разметку."
    )
    reply = chatbot.call_gemini(system_prompt, [], req.text)
    if not reply:
        return JSONResponse(status_code=502, content={
            "error": "Не удалось получить ответ ИИ. Проверьте, что ключ Gemini API сохранён в Настройках."
        })
    return {"reply": reply}


# -----------------
# ИНТЕГРАЦИЯ С 1С (обмен данными по API-ключу)
# -----------------

def check_1c_key(request: Request) -> bool:
    configured = os.getenv("ONEC_API_KEY", "")
    provided = request.headers.get("X-API-Key", "")
    return bool(configured) and provided == configured

@app.get("/api/1c/counterparties")
def onec_get_counterparties(request: Request, db: Session = Depends(get_db)):
    """Выгрузка контрагентов для 1С."""
    if not check_1c_key(request):
        return JSONResponse(status_code=401, content={"error": "Неверный или отсутствующий X-API-Key"})
    companies = db.query(Company).all()
    return [{
        "id": c.id, "name": c.name, "bin": c.bin, "director_name": c.director_name,
        "phone": c.phone, "email": c.email, "address": c.address,
        "bank_name": c.bank_name, "bik": c.bik, "kbe": c.kbe, "iban": c.requisites,
    } for c in companies]

class OneCCounterparty(BaseModel):
    name: str
    bin: str
    director_name: Optional[str] = ""
    phone: Optional[str] = ""
    email: Optional[str] = ""
    address: Optional[str] = ""
    bank_name: Optional[str] = ""
    bik: Optional[str] = ""
    kbe: Optional[str] = ""
    iban: Optional[str] = ""

@app.post("/api/1c/counterparties")
def onec_upsert_counterparties(request: Request, items: List[OneCCounterparty], db: Session = Depends(get_db)):
    """Загрузка/обновление контрагентов из 1С (сопоставление по БИН)."""
    if not check_1c_key(request):
        return JSONResponse(status_code=401, content={"error": "Неверный или отсутствующий X-API-Key"})
    created, updated = 0, 0
    for item in items:
        comp = db.query(Company).filter(Company.bin == item.bin).first() if item.bin else None
        if comp:
            comp.name = item.name
            comp.director_name = item.director_name or comp.director_name
            comp.phone = item.phone or comp.phone
            comp.email = item.email or comp.email
            comp.address = item.address or comp.address
            comp.bank_name = item.bank_name or comp.bank_name
            comp.bik = item.bik or comp.bik
            comp.kbe = item.kbe or comp.kbe
            comp.requisites = item.iban or comp.requisites
            updated += 1
        else:
            db.add(Company(
                name=item.name, bin=item.bin, director_name=item.director_name or "",
                phone=item.phone or "", email=item.email or "", address=item.address or "",
                bank_name=item.bank_name or "", bik=item.bik or "", kbe=item.kbe or "",
                requisites=item.iban or "", based_on="Устава",
            ))
            created += 1
    db.commit()
    return {"status": "success", "created": created, "updated": updated}

@app.get("/api/1c/invoices")
def onec_get_invoices(request: Request, db: Session = Depends(get_db)):
    """Выгрузка счетов для 1С."""
    if not check_1c_key(request):
        return JSONResponse(status_code=401, content={"error": "Неверный или отсутствующий X-API-Key"})
    invoices = db.query(Invoice).all()
    return [{
        "number": i.number, "date": i.date, "company_bin": i.company_bin,
        "company_name": i.company_name, "amount": i.amount, "status": i.status,
        "deal_id": i.deal_id, "external_id": i.external_id,
    } for i in invoices]

class OneCInvoice(BaseModel):
    number: str
    date: str
    company_bin: Optional[str] = ""
    company_name: Optional[str] = ""
    amount: float = 0.0
    status: str = "new"
    deal_id: Optional[int] = None
    external_id: Optional[str] = None

@app.post("/api/1c/invoices")
def onec_upsert_invoices(request: Request, items: List[OneCInvoice], db: Session = Depends(get_db)):
    """Загрузка/обновление счетов из 1С (сопоставление по номеру)."""
    if not check_1c_key(request):
        return JSONResponse(status_code=401, content={"error": "Неверный или отсутствующий X-API-Key"})
    created, updated = 0, 0
    for item in items:
        inv = db.query(Invoice).filter(Invoice.number == item.number).first()
        if inv:
            inv.date = item.date
            inv.company_bin = item.company_bin
            inv.company_name = item.company_name
            inv.amount = item.amount
            inv.status = item.status
            inv.external_id = item.external_id or inv.external_id
            updated += 1
        else:
            db.add(Invoice(
                number=item.number, date=item.date, company_bin=item.company_bin,
                company_name=item.company_name, amount=item.amount, status=item.status,
                deal_id=item.deal_id, external_id=item.external_id,
            ))
            created += 1
    db.commit()
    return {"status": "success", "created": created, "updated": updated}

@app.get("/api/deals/{deal_id}/contract")
def download_deal_contract(deal_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    comp = d.company
    if not comp:
        return JSONResponse(status_code=400, content={"error": "No company linked"})

    # Договор — как клиентская смета: без субаренды
    result = _calc_deal(d, exclude_subrental=True)
    
    context = {
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        "company_name": comp.name,
        "director_name": comp.director_name,
        "iin_bin": comp.bin,
        "iban": comp.requisites,
        "event_name": d.title,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "discount_percentage": d.discount_percentage
    }
    
    template_path = CONTRACT_TEMPLATE_PATH
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    def cleanup_file(path: str):
        try: os.remove(path)
        except: pass
            
    background_tasks.add_task(cleanup_file, temp_path)
    
    return FileResponse(
        temp_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Contract_{d.id}_{comp.name}.docx"
    )

@app.get("/api/deals/{deal_id}/estimate")
def download_deal_estimate(
    deal_id: int,
    background_tasks: BackgroundTasks,
    mode: str = "internal",
    db: Session = Depends(get_db),
):
    """Скачивание сметы .docx: mode=internal|client (по умолчанию internal)."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mode_norm = (mode or "internal").strip().lower()
    if mode_norm not in ("internal", "client"):
        mode_norm = "internal"

    # Клиентская смета — без позиций субаренды; внутренние итоги считаем по полному составу
    context = _build_estimate_context(d, mode_norm)

    from document_generator import generate_estimate_docx
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    generate_estimate_docx(context, temp_path, mode=mode_norm)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    fname = (
        f"Smeta_client_CRM-{d.id}.docx"
        if mode_norm == "client"
        else f"Smeta_vnutr_CRM-{d.id}.docx"
    )
    return FileResponse(
        temp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=fname,
    )


def _build_estimate_context(d: Deal, mode_norm: str) -> dict:
    result = _calc_deal(d, exclude_subrental=(mode_norm == "client"))
    header = _estimate_header_fields(d)
    return {
        "number": f"CRM-{d.id}",
        "date": datetime.today().strftime("%d.%m.%Y"),
        **header,
        "items": result["items"],
        "equipment_base": result.get("equipment_base", 0),
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "discount_amount": result.get("discount_amount", 0),
        "after_discount": result.get("after_discount", result["grand_total"]),
        "tax_percentage": result.get("tax_percentage", _deal_tax(d)),
        "tax_amount": result.get("tax_amount", 0),
        "grand_total": result["grand_total"],
        "cost_total": result.get("cost_total", 0),
        "margin": result.get("margin", 0),
        "discount_percentage": d.discount_percentage or 0,
    }


@app.get("/api/deals/{deal_id}/estimate.pdf")
def download_deal_estimate_pdf(
    deal_id: int,
    background_tasks: BackgroundTasks,
    mode: str = "internal",
    db: Session = Depends(get_db),
):
    """Скачивание сметы .pdf: mode=internal|client."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mode_norm = (mode or "internal").strip().lower()
    if mode_norm not in ("internal", "client"):
        mode_norm = "internal"

    from document_generator import generate_estimate_pdf
    context = _build_estimate_context(d, mode_norm)
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    generate_estimate_pdf(context, temp_path, mode=mode_norm)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    fname = (
        f"Smeta_client_CRM-{d.id}.pdf"
        if mode_norm == "client"
        else f"Smeta_vnutr_CRM-{d.id}.pdf"
    )
    return FileResponse(temp_path, media_type="application/pdf", filename=fname)


@app.get("/api/deals/{deal_id}/contract.pdf")
def download_deal_contract_pdf(
    deal_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """PDF договора: реквизиты + спецификация (без полного юр. текста Word-шаблона)."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    comp = d.company
    if not comp:
        return JSONResponse(status_code=400, content={"error": "No company linked"})

    result = _calc_deal(d, exclude_subrental=True)
    context = {
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        "company_name": comp.name,
        "director_name": comp.director_name,
        "iin_bin": comp.bin,
        "iban": comp.requisites,
        "event_name": d.title,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "discount_percentage": d.discount_percentage or 0,
        "tax_percentage": result.get("tax_percentage", FIXED_TAX_PERCENTAGE),
        "tax_amount": result.get("tax_amount", 0),
    }

    from document_generator import generate_contract_pdf
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    generate_contract_pdf(context, temp_path)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    return FileResponse(
        temp_path,
        media_type="application/pdf",
        filename=f"Contract_{d.id}_{comp.name}.pdf",
    )


@app.get("/api/deals/{deal_id}/contract-preview")
async def get_deal_contract_preview(deal_id: int, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Deal not found")
        
    comp = db.query(Company).filter(Company.id == d.company_id).first()
    if not comp:
        raise HTTPException(status_code=400, detail="Deal has no associated company")
        
    result = _calc_deal(d, exclude_subrental=True)
    
    # Check equipment photos
    for item in result["items"]:
        eq = db.query(Equipment).filter(Equipment.name == item["name"]).first()
        if eq:
            item["photo_url"] = eq.photo_url
            item["description"] = eq.description
            
        item["price_text"] = f"₸{item['price']:,.2f}".replace(",", " ")
        item["subtotal_text"] = f"₸{item['line_total_discounted']:,.2f}".replace(",", " ")
            
    expanded_company = expand_company_type(comp.name)
    
    context = {
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        "company_name": expanded_company,
        "director_name": comp.director_name or "",
        "iin_bin": comp.bin or "",
        "iban": comp.requisites or "",
        "based_on": comp.based_on or "Устава",
        "company_address": comp.address or "",
        "bank_name": comp.bank_name or "",
        "kbe": comp.kbe or "",
        "bik": comp.bik or "",
        "event_name": d.title or "",
        "event_date": d.event_date or "",
        "event_address": d.event_address or "",
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "equipment_total_text": f"₸{result['equipment_total']:,.2f}".replace(",", " "),
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "grand_total_text": get_rubles_text(result["grand_total"]),
        "discount_percentage": d.discount_percentage
    }
    
    return JSONResponse(content=context)

# -- Calculator integration --
@app.post("/api/v1/calculate")
async def api_calculate(request: Request):
    data = await request.json()
    items = data.get("items", [])
    discount = float(data.get("discount", 0.0))
    result = calculate_estimate(items, discount, FIXED_TAX_PERCENTAGE)
    return JSONResponse(content=result)

@app.post("/api/preview-contract")
async def api_preview_contract(
    contract_number: str = Form(...),
    contract_date: str = Form(...),
    company_name: str = Form(...),
    director_name: str = Form(...),
    iin_bin: str = Form(...),
    iban: str = Form(...),
    based_on: str = Form("Устава"),
    company_address: str = Form(""),
    bank_name: str = Form(""),
    kbe: str = Form(""),
    bik: str = Form(""),
    event_name: str = Form(...),
    event_date: str = Form(...),
    event_address: str = Form(...),
    items_json: str = Form("[]"),
    discount: float = Form(0.0),
    db: Session = Depends(get_db)
):
    try:
        items = json.loads(items_json)
    except Exception:
        items = []

    calc_result = calculate_estimate(items, discount)
    for item in calc_result["items"]:
        eq = db.query(Equipment).filter(Equipment.name == item["name"]).first()
        if eq:
            item["photo_url"] = eq.photo_url
            item["description"] = eq.description
            
        item["price_text"] = f"₸{item['price']:,.2f}".replace(",", " ")
        item["subtotal_text"] = f"₸{item['line_total_discounted']:,.2f}".replace(",", " ")
            
    expanded_company = expand_company_type(company_name)
    
    context = {
        "contract_number": contract_number,
        "contract_date": contract_date,
        "company_name": expanded_company,
        "director_name": director_name,
        "iin_bin": iin_bin,
        "iban": iban,
        "based_on": based_on,
        "company_address": company_address,
        "bank_name": bank_name,
        "kbe": kbe,
        "bik": bik,
        "event_name": event_name,
        "event_date": event_date,
        "event_address": event_address,
        "items": calc_result["items"],
        "equipment_total": calc_result["equipment_total"],
        "equipment_total_text": f"₸{calc_result['equipment_total']:,.2f}".replace(",", " "),
        "fixed_total": calc_result["fixed_total"],
        "grand_total": calc_result["grand_total"],
        "grand_total_text": get_rubles_text(calc_result["grand_total"]),
        "discount_percentage": discount
    }
    
    return JSONResponse(content=context)

@app.post("/api/generate-html-preview")
async def api_generate_html_preview(request: Request):
    import io
    import mammoth
    from document_generator import generate_contract
    context = await request.json()
    
    template_path = CONTRACT_TEMPLATE_PATH
    
    # We can generate to a temp file, but doing it in memory is tricky with docxtpl unless we use temp file
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    with open(temp_path, "rb") as docx_file:
        result = mammoth.convert_to_html(docx_file)
        html_content = result.value
        
    try:
        os.remove(temp_path)
    except:
        pass
        
    return JSONResponse(content={"html": html_content})

@app.post("/api/download-contract")
async def api_download_contract(
    request: Request,
    background_tasks: BackgroundTasks
):
    context = await request.json()
    
    template_path = CONTRACT_TEMPLATE_PATH
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    
    generate_contract(context, template_path, temp_path)
    
    def cleanup_file(path: str):
        try:
            os.remove(path)
        except Exception:
            pass
            
    background_tasks.add_task(cleanup_file, temp_path)
    
    filename = f"Contract_{context.get('contract_number', 'Doc')}.docx"
    return FileResponse(
        temp_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename
    )

@app.post("/api/v1/optimize-layout")
async def optimize_layout(data: LayoutOptimizationRequest):
    # Mock AI response
    response_data = {
        "status": "success",
        "message": "AI Layout Optimization calculated successfully.",
        "dimensions": {
            "width": data.width,
            "length": data.length
        },
        "placement_suggestions": [
            {
                "item_type": "sound_portal",
                "position": {"x": 0.5, "y": 0.5},
                "angle": 15,
                "note": "Оптимальный угол раскрытия для покрытия зала."
            },
            {
                "item_type": "truss",
                "position": {"x": data.width / 2, "y": data.length - 2},
                "angle": 0,
                "note": "Фермовая конструкция по центру."
            }
        ]
    }
    return JSONResponse(content=response_data)

@app.get("/api/deals/{deal_id}/2d-project")
def get_2d_project(deal_id: int, db: Session = Depends(get_db)):
    proj = db.query(Project2D).filter(Project2D.deal_id == deal_id).first()
    if proj:
        return {"layout_data_json": proj.layout_data_json}
    return {"layout_data_json": "[]"}

@app.post("/api/deals/{deal_id}/2d-project")
def save_2d_project(deal_id: int, proj: Project2DSave, db: Session = Depends(get_db)):
    p = db.query(Project2D).filter(Project2D.deal_id == deal_id).first()
    if p:
        p.layout_data_json = proj.layout_data_json
    else:
        new_p = Project2D(deal_id=deal_id, layout_data_json=proj.layout_data_json)
        db.add(new_p)
    db.commit()
    return {"status": "success"}

@app.post("/api/deals/{deal_id}/3d-scene")
async def analyze_3d_scene(
    deal_id: int, 
    file: UploadFile = File(...), 
    room_type: str = Form(...), 
    acoustics_level: str = Form(...),
    db: Session = Depends(get_db)
):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
        
    equipment_summary = []
    for di in d.items:
        if di.equipment:
            equipment_summary.append(f"{di.quantity}x {di.equipment.name} ({di.equipment.category})")
            
    eq_text = ", ".join(equipment_summary) if equipment_summary else "оборудование не добавлено в смету"
            
    mock_response = {
        "status": "success",
        "file_analyzed": file.filename,
        "room_type": room_type,
        "acoustics_level": acoustics_level,
        "ai_analysis": f"Анализ помещения завершен. Обнаружен {room_type} тип площадки. Уровень акустики: {acoustics_level}. Для текущей сметы ({eq_text}) рекомендуется следующее размещение:",
        "recommendations": [
            {
                "item": "Звук",
                "action": "Разместите портальные системы под углом 15 градусов в сторону центра зала для минимизации переотражений от стен."
            },
            {
                "item": "Свет",
                "action": "Фермовые конструкции для света лучше установить в 5 метрах от края сцены для равномерного освещения."
            },
            {
                "item": "Экраны",
                "action": "Разместите LED экраны на высоте не менее 1.5м от уровня сцены для хорошего обзора с задних рядов."
            }
        ]
    }
    
    return JSONResponse(content=mock_response)
