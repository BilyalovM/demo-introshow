from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, File, UploadFile, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict
import json
import os
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
from database import init_db, get_db, SessionLocal, Equipment, Company, Deal, DealItem, CustomField, DealFieldValue, DealHistory, Project2D, Folder, Pipeline, Stage, PushSubscription, User, BotSettings, KnowledgeItem, ChatMessage, Invoice, Task, Contact, Activity, DealAttachment, engine
from sqlalchemy import text, func

from calculator import calculate_estimate
from document_generator import generate_contract, get_rubles_text
import notifications
import auth
import chatbot

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
        "ALTER TABLE tasks ADD COLUMN priority VARCHAR DEFAULT 'normal'",
        "ALTER TABLE tasks ADD COLUMN completed_at DATETIME",
        # CRM → ближе к Битрикс24
        "ALTER TABLE deals ADD COLUMN assignee_id INTEGER",
        "ALTER TABLE deals ADD COLUMN source VARCHAR",
        "ALTER TABLE deals ADD COLUMN loss_reason VARCHAR",
        "ALTER TABLE deals ADD COLUMN is_qualified BOOLEAN DEFAULT 0",
        "ALTER TABLE deals ADD COLUMN is_archived BOOLEAN DEFAULT 0",
        "ALTER TABLE contacts ADD COLUMN is_primary BOOLEAN DEFAULT 0",
    ]:
        try:
            session.execute(text(ddl))
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
    discount_percentage: float = 0.0
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
    q = db.query(Deal).filter(Deal.is_archived == False)  # noqa: E712
    if _user_crm_own_only(user):
        q = q.filter(Deal.assignee_id == user.id)
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
            "company": d.company.name if d.company else None,
        })
    out.sort(key=lambda x: (x["date"], x["id"]))
    return out


@app.get("/api/calendar/deals/{deal_id}")
def api_calendar_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    if _user_crm_own_only(user) and d.assignee_id != user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    atts = db.query(DealAttachment).filter(DealAttachment.deal_id == deal_id).order_by(DealAttachment.id.desc()).all()
    return {
        "id": d.id,
        "title": d.title,
        "setup_date": d.setup_date,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "manager": (d.assignee.full_name or d.assignee.username) if d.assignee else None,
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
class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[str] = "normal"
    deal_id: Optional[int] = None

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    deal_id: Optional[int] = None

def _task_to_dict(t: Task) -> dict:
    today = datetime.today().strftime("%Y-%m-%d")
    overdue = bool(t.due_date and t.status not in ("done",) and t.due_date[:10] < today)
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "assignee": t.assignee, "created_by": t.created_by,
        "due_date": t.due_date, "priority": t.priority or "normal",
        "status": t.status, "deal_id": t.deal_id,
        "deal_title": t.deal.title if t.deal else None,
        "overdue": overdue,
        "created_at": t.created_at.strftime("%d.%m.%Y") if t.created_at else "",
        "completed_at": t.completed_at.strftime("%d.%m.%Y %H:%M") if t.completed_at else None,
    }

@app.get("/api/tasks")
def get_tasks(deal_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Task)
    if deal_id:
        query = query.filter(Task.deal_id == deal_id)
    tasks = query.order_by(Task.status, Task.due_date).all()
    return [_task_to_dict(t) for t in tasks]

@app.post("/api/tasks")
def create_task(t: TaskCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = Task(
        title=t.title,
        description=t.description,
        assignee=t.assignee or (user.full_name or user.username),
        created_by=user.full_name or user.username,
        due_date=t.due_date,
        priority=t.priority or "normal",
        deal_id=t.deal_id,
    )
    db.add(task)
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
    for field in ("title", "description", "assignee", "due_date", "priority", "deal_id"):
        value = getattr(t, field)
        if value is not None:
            setattr(task, field, value)
    if t.status is not None:
        task.status = t.status
        task.completed_at = datetime.utcnow() if t.status == "done" else None
    db.commit()
    return {"status": "success"}

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        db.delete(task)
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
def get_equipment(db: Session = Depends(get_db)):
    equip_list = db.query(Equipment).all()
    
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
        
        # Build dictionary
        eq_dict = {
            "id": eq.id,
            "name": eq.name,
            "category": eq.category,
            "price": eq.price,
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
    db_equip = Equipment(**item.dict())
    db.add(db_equip)
    db.commit()
    db.refresh(db_equip)
    return db_equip

@app.put("/api/equipment/{equip_id}")
def update_equipment(equip_id: int, item: EquipmentCreate, db: Session = Depends(get_db)):
    db_equip = db.query(Equipment).filter(Equipment.id == equip_id).first()
    if db_equip:
        for k, v in item.dict().items():
            setattr(db_equip, k, v)
        db.commit()
    return {"status": "success"}

class PriceUpdate(BaseModel):
    price: float

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
        } for c in db_comp.contacts],
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

FIXED_CATEGORIES = ["Логистика", "Персонал", "Расходники"]


def _item_price(di: DealItem) -> float:
    """Цена позиции: сохранённая в смете, иначе текущая цена склада."""
    if di.price is not None:
        return di.price
    return di.equipment.price if di.equipment else 0


def _deal_calc_items(deal: Deal) -> list:
    items = []
    for di in deal.items:
        eq = di.equipment
        if not eq:
            continue
        cat_type = "fixed" if eq.category in FIXED_CATEGORIES else "equipment"
        items.append({
            "name": eq.name,
            "price": _item_price(di),
            "quantity": di.quantity,
            "days": di.days,
            "category_type": cat_type,
            "photo_url": eq.photo_url,
            "description": eq.description,
        })
    return items


def _recalc_deal_sum(db: Session, deal: Deal) -> None:
    result = calculate_estimate(_deal_calc_items(deal), deal.discount_percentage or 0)
    deal.final_sum = result["grand_total"]
    db.commit()


def _default_assignee_id(db: Session) -> Optional[int]:
    u = db.query(User).filter(User.role.in_(["admin", "manager"])).order_by(User.id).first()
    return u.id if u else None


def _user_crm_own_only(user: User) -> bool:
    if not user or user.role == "admin":
        return False
    perms = user.permissions or []
    return "crm_own_only" in perms


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
        discount_percentage=deal.discount_percentage,
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
        if not (stage_update.loss_reason or db_deal.loss_reason):
            return JSONResponse(status_code=400, content={"error": "Укажите причину отказа"})
        if stage_update.loss_reason:
            db_deal.loss_reason = stage_update.loss_reason

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
    if _user_crm_own_only(user) and d.assignee_id != user.id:
        return JSONResponse(status_code=403, content={"error": "Нет доступа к этой сделке"})
    
    items = []
    for i in d.items:
        items.append({
            "id": i.id,
            "equipment_id": i.equipment_id,
            "quantity": i.quantity,
            "days": i.days,
            "name": i.equipment.name if i.equipment else "Unknown",
            "price": _item_price(i),
            "stock_price": i.equipment.price if i.equipment else 0,
            "category_type": "fixed" if i.equipment and i.equipment.category in FIXED_CATEGORIES else "equipment"
        })

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
        "discount_percentage": d.discount_percentage,
        "final_sum": d.final_sum,
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
        "invoices": invoices,
        "custom_values": custom_values
    }

class DealItemsUpdate(BaseModel):
    discount_percentage: float
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
    db.commit()
    db.refresh(d)
    _recalc_deal_sum(db, d)
    
    return {"status": "success", "final_sum": d.final_sum}

class DealUpdate(BaseModel):
    title: Optional[str] = None
    event_date: Optional[str] = None
    setup_date: Optional[str] = None
    event_address: Optional[str] = None
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

    result = calculate_estimate(_deal_calc_items(d), d.discount_percentage)
    
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
def download_deal_estimate(deal_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Скачивание сметы сделки в формате .docx."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    result = calculate_estimate(_deal_calc_items(d), d.discount_percentage or 0)

    rent_period = ""
    if d.setup_date or d.event_date:
        rent_period = f"{d.setup_date or '—'} — {d.event_date or '—'}"

    context = {
        "number": f"CRM-{d.id}",
        "date": datetime.today().strftime("%d.%m.%Y"),
        "company_name": d.company.name if d.company else "",
        "event_name": d.title or "",
        "event_address": d.event_address or "",
        "rent_period": rent_period,
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "discount_percentage": d.discount_percentage or 0,
    }

    from document_generator import generate_estimate_docx
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)
    generate_estimate_docx(context, temp_path)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    return FileResponse(
        temp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Smeta_CRM-{d.id}.docx",
    )


@app.get("/api/deals/{deal_id}/contract-preview")
async def get_deal_contract_preview(deal_id: int, db: Session = Depends(get_db)):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Deal not found")
        
    comp = db.query(Company).filter(Company.id == d.company_id).first()
    if not comp:
        raise HTTPException(status_code=400, detail="Deal has no associated company")
        
    result = calculate_estimate(_deal_calc_items(d), d.discount_percentage)
    
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
    result = calculate_estimate(items, discount)
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
