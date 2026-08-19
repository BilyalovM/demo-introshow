from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, File, UploadFile, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Tuple
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
from database import init_db, get_db, SessionLocal, Equipment, Company, Deal, DealItem, CustomField, DealFieldValue, DealHistory, Project2D, Folder, Pipeline, Stage, PipelineRoutingRule, PushSubscription, User, UserInvite, Role, BotSettings, KnowledgeItem, ChatMessage, Invoice, Task, TaskComment, TaskAssignee, TaskObserver, TaskChecklistItem, Contact, Activity, DealAttachment, DealDocument, DocumentTemplate, CrmNote, DealAdvance, DealExpense, DealPayrollLine, DealStaffAssignment, InternalChat, InternalChatMember, InternalMessage, AppNotification, EstimateTemplate, ChecklistTemplate, AuditLog, AppSetting, WorkSession, City, engine, database_backend_info, DATABASE_URL
import secrets
from sqlalchemy import text, func, or_

from calculator import calculate_estimate, DEFAULT_TAX_PERCENTAGE
from document_generator import generate_contract, get_rubles_text
import document_templates as doc_templates
import notifications
import auth
import chatbot
import rate_limit
import audit
import demo_seed

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

    # 1b. Pipeline kinds / stage flags — ДО любых ORM-запросов к Pipeline/Stage
    for ddl in [
        "ALTER TABLE pipelines ADD COLUMN kind VARCHAR DEFAULT 'deal'",
        "ALTER TABLE pipelines ADD COLUMN target_pipeline_id INTEGER",
        "ALTER TABLE stages ADD COLUMN is_won BOOLEAN DEFAULT 0",
        "ALTER TABLE stages ADD COLUMN is_lost BOOLEAN DEFAULT 0",
        "ALTER TABLE stages ADD COLUMN creates_deal BOOLEAN DEFAULT 0",
    ]:
        try:
            session.execute(text(ddl))
            session.commit()
        except Exception:
            session.rollback()
        
    # 2. Seed main pipeline if empty
    default_pipeline = session.query(Pipeline).first()
    if not default_pipeline:
        default_pipeline = Pipeline(name="Основная воронка", kind="deal")
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
            st = Stage(
                pipeline_id=default_pipeline.id,
                name=stage_name,
                order_index=i+1,
                is_won="успешн" in stage_name.lower(),
                is_lost="проигра" in stage_name.lower(),
                is_active_rent=any(k in stage_name for k in ("Предоплата", "Монтаж", "Мероприятие")),
            )
            session.add(st)
        session.commit()
        
    # 3. Update existing deals to the default pipeline if they are null
    session.execute(
        text("UPDATE deals SET pipeline_id = :pid WHERE pipeline_id IS NULL"),
        {"pid": default_pipeline.id},
    )
    session.commit()

    try:
        for p in session.query(Pipeline).all():
            if not getattr(p, "kind", None):
                p.kind = "deal"
        # Пометить существующие стадии успех/проигрыш
        for st in session.query(Stage).all():
            name_l = (st.name or "").lower()
            if "успешн" in name_l and not st.is_won:
                st.is_won = True
            if "проигра" in name_l and not st.is_lost:
                st.is_lost = True
        session.commit()
    except Exception:
        session.rollback()

    # Seed воронки «Лиды» + привязка к продажам
    try:
        deal_pipeline = (
            session.query(Pipeline)
            .filter(Pipeline.kind == "deal")
            .order_by(Pipeline.id)
            .first()
        ) or session.query(Pipeline).order_by(Pipeline.id).first()
        if deal_pipeline and not getattr(deal_pipeline, "kind", None):
            deal_pipeline.kind = "deal"
            session.commit()

        leads_pipeline = (
            session.query(Pipeline)
            .filter(Pipeline.kind == "lead")
            .order_by(Pipeline.id)
            .first()
        )
        if not leads_pipeline:
            by_name = session.query(Pipeline).filter(Pipeline.name == "Лиды").first()
            if by_name:
                leads_pipeline = by_name
                leads_pipeline.kind = "lead"
            else:
                leads_pipeline = Pipeline(
                    name="Лиды",
                    kind="lead",
                    target_pipeline_id=deal_pipeline.id if deal_pipeline else None,
                )
                session.add(leads_pipeline)
                session.commit()
                session.refresh(leads_pipeline)
                lead_stages = [
                    ("Новый лид", False, False, False),
                    ("В работе", False, False, False),
                    ("Квалифицирован", False, False, False),
                    ("Успешно", True, False, True),
                    ("Отказ", False, True, False),
                ]
                for i, (nm, won, lost, creates) in enumerate(lead_stages):
                    session.add(Stage(
                        pipeline_id=leads_pipeline.id,
                        name=nm,
                        order_index=i + 1,
                        is_won=won,
                        is_lost=lost,
                        creates_deal=creates,
                    ))
                session.commit()
        if leads_pipeline and deal_pipeline and not leads_pipeline.target_pipeline_id:
            leads_pipeline.target_pipeline_id = deal_pipeline.id
            session.commit()

        # Две deal-воронки: Аренда + Продажа (лид → выбор целевой при конвертации)
        try:
            deal_pipes = (
                session.query(Pipeline)
                .filter(Pipeline.kind == "deal")
                .order_by(Pipeline.id)
                .all()
            )
            rental = next((p for p in deal_pipes if (p.name or "").strip().lower() in ("аренда", "прокат")), None)
            sales = next((p for p in deal_pipes if (p.name or "").strip().lower() in ("продажа", "продажи")), None)
            if not rental and deal_pipes:
                # Переименовать первую deal-воронку («Основная…») → Аренда
                first = deal_pipes[0]
                if (first.name or "").strip().lower() in ("основная воронка", "продажи", "основная"):
                    first.name = "Аренда"
                    rental = first
                    session.commit()
                else:
                    rental = first
            if not rental:
                rental = Pipeline(name="Аренда", kind="deal")
                session.add(rental)
                session.commit()
                session.refresh(rental)
                for i, (nm, won, lost, rent) in enumerate([
                    ("Первичный контакт", False, False, False),
                    ("Согласование сметы", False, False, False),
                    ("Договор и счет", False, False, False),
                    ("Предоплата внесена", False, False, True),
                    ("Монтаж / Мероприятие", False, False, True),
                    ("Успешно реализовано", True, False, False),
                    ("Сделка проиграна", False, True, False),
                ]):
                    session.add(Stage(
                        pipeline_id=rental.id, name=nm, order_index=i + 1,
                        is_won=won, is_lost=lost, is_active_rent=rent,
                    ))
                session.commit()
            if not sales:
                sales = Pipeline(name="Продажа", kind="deal")
                session.add(sales)
                session.commit()
                session.refresh(sales)
                for i, (nm, won, lost) in enumerate([
                    ("Первичный контакт", False, False),
                    ("КП / согласование", False, False),
                    ("Договор и счет", False, False),
                    ("Оплата", False, False),
                    ("Отгружено / закрыто", True, False),
                    ("Отказ", False, True),
                ]):
                    session.add(Stage(
                        pipeline_id=sales.id, name=nm, order_index=i + 1,
                        is_won=won, is_lost=lost, is_active_rent=False,
                    ))
                session.commit()
            if leads_pipeline and rental and not leads_pipeline.target_pipeline_id:
                leads_pipeline.target_pipeline_id = rental.id
                session.commit()
        except Exception:
            session.rollback()

        # Дефолтные правила маршрутизации источников → Лиды
        if leads_pipeline:
            for src in ("whatsapp", "telegram", "instagram", "site", "maps", "onec", "manual", "referral", "other"):
                exists = session.query(PipelineRoutingRule).filter(PipelineRoutingRule.source == src).first()
                if not exists:
                    session.add(PipelineRoutingRule(
                        source=src,
                        pipeline_id=leads_pipeline.id,
                        assignee_id=None,
                        is_active=True,
                    ))
            session.commit()
    except Exception:
        session.rollback()

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
        "ALTER TABLE equipment ADD COLUMN condition VARCHAR DEFAULT 'good'",
        # Налог в смете (%) — исторически DEFAULT 0; ниже принудительно 16
        "ALTER TABLE deals ADD COLUMN tax_percentage FLOAT DEFAULT 16",
        # Шапка сметы (как в Excel)
        "ALTER TABLE deals ADD COLUMN city VARCHAR",
        "ALTER TABLE deals ADD COLUMN shifts FLOAT DEFAULT 1",
        # v2: операционный пайплайн отгрузки
        "ALTER TABLE deals ADD COLUMN ops_status VARCHAR DEFAULT 'none'",
        # v2: статусы выдачи субаренды на позициях сметы
        "ALTER TABLE deal_items ADD COLUMN subrental_status VARCHAR",
        "ALTER TABLE deal_items ADD COLUMN issued_at DATETIME",
        "ALTER TABLE deal_items ADD COLUMN issued_by_id INTEGER",
        "ALTER TABLE deal_items ADD COLUMN returned_at DATETIME",
        "ALTER TABLE deal_items ADD COLUMN returned_by_id INTEGER",
        "ALTER TABLE deal_items ADD COLUMN subrental_note VARCHAR",
        # Сотрудник на проекте ↔ задача «Выезд»
        "ALTER TABLE deal_staff_assignments ADD COLUMN task_id INTEGER",
        "ALTER TABLE deal_staff_assignments ADD COLUMN attachment_id INTEGER",
        "ALTER TABLE deal_staff_assignments ADD COLUMN notified_at DATETIME",
        "ALTER TABLE deal_staff_assignments ADD COLUMN role_name VARCHAR",
        "ALTER TABLE deal_staff_assignments ADD COLUMN note VARCHAR",
        "ALTER TABLE deal_staff_assignments ADD COLUMN created_by VARCHAR",
        # v2 security: logout-all через инкремент версии сессии
        "ALTER TABLE users ADD COLUMN session_version INTEGER DEFAULT 0",
        # Квалификация лида + менеджеры + фиксы
        "ALTER TABLE deals ADD COLUMN qualification VARCHAR",
        "ALTER TABLE deals ADD COLUMN sales_manager_id INTEGER",
        "ALTER TABLE deals ADD COLUMN project_manager_id INTEGER",
        "ALTER TABLE deals ADD COLUMN sales_fix_kzt FLOAT DEFAULT 0",
        "ALTER TABLE deals ADD COLUMN project_fix_kzt FLOAT DEFAULT 0",
        "ALTER TABLE deals ADD COLUMN margin_target_pct FLOAT DEFAULT 10",
        # Multi-city foundation
        "ALTER TABLE users ADD COLUMN city_id INTEGER",
        "ALTER TABLE users ADD COLUMN phone VARCHAR",
        "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1",
        "ALTER TABLE users ADD COLUMN dismissed_at DATETIME",
        "ALTER TABLE users ADD COLUMN role_id INTEGER",
        "ALTER TABLE user_invites ADD COLUMN role_id INTEGER",
        "ALTER TABLE deals ADD COLUMN city_id INTEGER",
        "ALTER TABLE work_sessions ADD COLUMN city_id INTEGER",
        "ALTER TABLE work_sessions ADD COLUMN start_place VARCHAR",
        "ALTER TABLE work_sessions ADD COLUMN end_place VARCHAR",
        # Soft-delete / корзина (additive only — never drops data)
        "ALTER TABLE deals ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE deals ADD COLUMN deleted_by_id INTEGER",
        "ALTER TABLE tasks ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE tasks ADD COLUMN deleted_by_id INTEGER",
        "ALTER TABLE companies ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE companies ADD COLUMN deleted_by_id INTEGER",
        "ALTER TABLE deal_documents ADD COLUMN deleted_at DATETIME",
        "ALTER TABLE deal_documents ADD COLUMN deleted_by_id INTEGER",
    ]:
        try:
            session.execute(text(ddl))
            session.commit()
        except Exception:
            session.rollback()

    # Города: Алматы (default), Астана/Шымкент наготове
    try:
        seed_cities = [
            ("Алматы", "almaty", True, "Asia/Almaty", 10),
            ("Астана", "astana", False, "Asia/Almaty", 20),
            ("Шымкент", "shymkent", False, "Asia/Almaty", 30),
        ]
        for name, slug, active, tz, order in seed_cities:
            row = session.query(City).filter(City.slug == slug).first()
            if not row:
                session.add(City(
                    name=name, slug=slug, is_active=active,
                    timezone=tz, sort_order=order,
                ))
        session.commit()
        almaty = session.query(City).filter(City.slug == "almaty").first()
        if almaty:
            session.execute(
                text("UPDATE deals SET city_id = :cid WHERE city_id IS NULL"),
                {"cid": almaty.id},
            )
            # Подтянуть текстовый city из справочника, если пусто
            session.execute(
                text(
                    "UPDATE deals SET city = :cname "
                    "WHERE (city IS NULL OR TRIM(city) = '') AND city_id = :cid"
                ),
                {"cname": almaty.name, "cid": almaty.id},
            )
            # Активный город организации по умолчанию
            active_setting = session.query(AppSetting).filter(AppSetting.key == "active_city_id").first()
            if not active_setting:
                session.add(AppSetting(key="active_city_id", value=str(almaty.id)))
            session.commit()
    except Exception as e:
        session.rollback()
        print("cities seed error:", e)

    # Системные роли доступа (права сотрудников)
    try:
        default_roles = [
            (
                "Менеджер продаж",
                "CRM, сметы, клиенты и коммуникации. Свои сделки и флаг менеджера продаж.",
                [
                    "dashboard", "today", "inbox", "chats", "crm", "quotes",
                    "calendar", "companies", "tasks", "analytics",
                    "crm_own_only", "role_sales",
                ],
            ),
            (
                "Менеджер проекта",
                "Ведение проектов: CRM, документы, склад, календарь и задачи.",
                [
                    "dashboard", "today", "crm", "quotes", "documents",
                    "calendar", "equipment", "companies", "tasks",
                    "role_project", "hide_margin",
                ],
            ),
            (
                "Техник / Склад",
                "Склад, календарь и задачи. Цены, маржа и ФОТ скрыты.",
                [
                    "today", "equipment", "calendar", "tasks",
                    "hide_prices", "hide_margin", "hide_payroll", "hide_subrental_cost",
                ],
            ),
            (
                "Админ-помощник",
                "Почти полный доступ к разделам без роли администратора системы.",
                list(auth.SECTIONS.keys()),
            ),
        ]
        for name, desc, perms in default_roles:
            existing = session.query(Role).filter(Role.name == name).first()
            if not existing:
                session.add(Role(
                    name=name,
                    description=desc,
                    permissions=perms,
                    is_system=True,
                    created_at=datetime.utcnow(),
                ))
        session.commit()
    except Exception as e:
        session.rollback()
        print("roles seed error:", e)

    # Seed реквизитов компании для шапки сметы (если ещё пусто)
    try:
        defaults = {
            "company_name": "Intro Show",
            "company_phone": "+7 (701) 554-13-80",
            "company_email": "show.intro@yandex.kz",
            "company_address": "Тюлькубасская улица, 4, Алматы",
            "company_bin": "",
        }
        for key, val in defaults.items():
            existing = session.query(AppSetting).filter(AppSetting.key == key).first()
            if not existing:
                session.add(AppSetting(key=key, value=val))
        session.commit()
    except Exception:
        session.rollback()

    # Seed системных шаблонов документов (смета / договор / техничка)
    try:
        doc_templates.seed_document_templates(session)
    except Exception as e:
        session.rollback()
        print("document_templates seed error:", e)

    # 4b2. Налог всегда 16%: выравниваем существующие сделки
    try:
        session.execute(
            text("UPDATE deals SET tax_percentage = 16 WHERE tax_percentage IS NULL OR tax_percentage != 16")
        )
        session.commit()
    except Exception:
        session.rollback()

    # 4b3. Пустые/NULL типы склада → own
    try:
        session.execute(
            text("UPDATE equipment SET warehouse_type = 'own' WHERE warehouse_type IS NULL OR TRIM(warehouse_type) = ''")
        )
        session.commit()
    except Exception:
        session.rollback()

    # 4b3b. Состояние товара на складе (внутреннее)
    try:
        session.execute(
            text("UPDATE equipment SET condition = 'good' WHERE condition IS NULL OR TRIM(condition) = ''")
        )
        session.commit()
    except Exception:
        session.rollback()

    # 4b4. Демо-каталог субаренды (Vercel SQLite эфемерен — без seed вкладка пустая)
    try:
        sub_cnt = session.query(Equipment).filter(Equipment.warehouse_type == "subrental").count()
        if sub_cnt == 0:
            for name, cat, price, cost, supplier, qty in [
                ("LED экран P3.9 (субаренда)", "Экраны", 45000, 32000, "EventPro Almaty", 10),
                ("Линейный массив 2×top (субаренда)", "Звук", 80000, 55000, "SoundRent", 4),
                ("Дым-машина hazer (субаренда)", "Свет", 12000, 7000, "LightHub", 6),
                ("Генератор 30 кВт (субаренда)", "Логистика", 35000, 22000, "PowerGo", 2),
            ]:
                session.add(Equipment(
                    name=name,
                    category=cat,
                    price=float(price),
                    cost_price=float(cost),
                    stock_quantity=int(qty),
                    status="Доступно",
                    warehouse_type="subrental",
                    supplier=supplier,
                    description="Демо-позиция внешнего склада (субаренда)",
                ))
            session.commit()
    except Exception:
        session.rollback()

    # 4b5. Демо-сделки: только если CRM пустая (cold start /tmp на Vercel).
    # На проде отключить: SEED_DEMO_DEALS=0
    if os.environ.get("SEED_DEMO_DEALS", "1").strip() != "0":
        try:
            demo_seed.seed_demo_deals(session, only_if_empty=True)
        except Exception as e:
            session.rollback()
            print("demo_seed error:", e)
    else:
        print("demo_seed: skipped (SEED_DEMO_DEALS=0)")

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

    # 5. Create default admin user (пароль из ADMIN_PASSWORD или "admin")
    try:
        if session.query(User).count() == 0:
            admin_pw = (os.environ.get("ADMIN_PASSWORD") or "admin").strip() or "admin"
            admin_user = User(
                username="admin",
                hashed_password=auth.get_password_hash(admin_pw),
                role="admin",
                full_name="Администратор",
            )
            session.add(admin_user)
            session.commit()
            if admin_pw == "admin":
                print(
                    "⚠️  SECURITY: создан admin с паролем по умолчанию «admin». "
                    "Смените пароль сразу после входа или задайте ADMIN_PASSWORD."
                )
    except Exception:
        session.rollback()

    # 6. Громкие предупреждения готовности к проду (лог при старте)
    try:
        _prod_warns = []
        _info = database_backend_info()
        if _info.get("is_sqlite"):
            _prod_warns.append(
                "SQLite вместо Postgres — для боя сотрудников обязателен DATABASE_URL=postgresql+psycopg2://…"
            )
        if not os.environ.get("SESSION_SECRET"):
            _prod_warns.append(
                "SESSION_SECRET не задан в env — секрет берётся из файла/.session_secret; для прода задайте ≥64 символов"
            )
        _admin = session.query(User).filter(User.username == "admin").first()
        if _admin and auth.verify_password("admin", _admin.hashed_password):
            _prod_warns.append(
                "Пароль пользователя admin всё ещё «admin» — смените в Настройках или через ADMIN_PASSWORD на чистой БД"
            )
        if os.environ.get("FORCE_SECURE_COOKIE") != "1" and not os.environ.get("VERCEL"):
            if os.environ.get("ENV", "").lower() in ("production", "prod") or _info.get("is_postgres"):
                _prod_warns.append(
                    "FORCE_SECURE_COOKIE не =1 — на HTTPS за nginx рекомендуется FORCE_SECURE_COOKIE=1"
                )
        for _w in _prod_warns:
            print(f"⚠️  PROD-READY: {_w}")
        if _prod_warns and (
            os.environ.get("ENV", "").lower() in ("production", "prod")
            or os.environ.get("FORCE_SECURE_COOKIE") == "1"
            or _info.get("is_postgres")
        ):
            print("⚠️  PROD-READY: исправьте пункты выше до запуска сотрудников (см. docs/prod-readiness-2026-08-14.md)")
    except Exception as e:
        print("prod-ready check error:", e)

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
EQUIPMENT_CONDITIONS = ("good", "medium", "needs_repair")


def _normalize_equipment_condition(value: Optional[str]) -> str:
    v = (value or "good").strip().lower()
    return v if v in EQUIPMENT_CONDITIONS else "good"


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
    condition: Optional[str] = "good"  # good | medium | needs_repair (склад only)

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

QUALIFICATION_VALUES = ("rental", "sale", "spam")
QUALIFICATION_LABELS = {
    "rental": "Аренда",
    "sale": "Продажа",
    "spam": "Спам-отказ",
}


class DealCreate(BaseModel):
    title: str
    company_id: Optional[int] = None
    contact_id: Optional[int] = None
    assignee_id: Optional[int] = None
    sales_manager_id: Optional[int] = None
    project_manager_id: Optional[int] = None
    pipeline_id: Optional[int] = 1
    setup_date: Optional[str] = None
    event_date: str
    event_address: Optional[str] = None
    city: Optional[str] = None
    city_id: Optional[int] = None
    shifts: Optional[float] = 1.0
    discount_percentage: float = 0.0
    tax_percentage: float = FIXED_TAX_PERCENTAGE
    items_json: Optional[str] = None
    source: Optional[str] = "manual"
    qualification: Optional[str] = None
    is_qualified: Optional[bool] = False
    sales_fix_kzt: Optional[float] = 0.0
    project_fix_kzt: Optional[float] = 0.0
    margin_target_pct: Optional[float] = 10.0

class DealStageUpdate(BaseModel):
    stage: int
    pipeline_id: Optional[int] = None
    loss_reason: Optional[str] = None
    # При конвертации лида: целевая deal-воронка (Аренда / Продажа)
    convert_to_pipeline_id: Optional[int] = None
    # Можно передать квалификацию вместе со сменой стадии
    qualification: Optional[str] = None

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
    kind: Optional[str] = "deal"  # lead | deal
    target_pipeline_id: Optional[int] = None

class StageCreate(BaseModel):
    name: str
    order_index: Optional[int] = None
    is_active_rent: Optional[bool] = False
    is_won: Optional[bool] = False
    is_lost: Optional[bool] = False
    creates_deal: Optional[bool] = False

verify_password = auth.verify_password
get_password_hash = auth.get_password_hash


def get_user_from_request(request: Request, db: Session):
    """Достаёт пользователя из подписанного cookie session_token (TTL + session_version)."""
    token = request.cookies.get("session_token")
    parsed = auth.parse_session_token(token)
    if not parsed:
        return None
    username, token_ver = parsed
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return None
    current_ver = int(getattr(user, "session_version", 0) or 0)
    if token_ver != current_ver:
        return None
    return user


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
    "/api/webhooks/whatsapp-web",  # WhatsApp Web bridge (VPS) → CRM
    "/tracking/", "/api/push/", "/api/1c/", "/favicon",
    "/openapi.json",
    "/roadmap",  # публичный статус/roadmap для клиента
    "/lead", "/api/leads",  # публичный захват лидов с сайта/карт
    "/register", "/api/register",  # регистрация сотрудника по invite-ссылке
)

# Точные публичные пути (не префиксы) — чтобы /documents не совпал с /docs
PUBLIC_PATH_EXACT = frozenset({"/docs", "/openapi.json", "/redoc"})


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATH_EXACT:
        return True
    # Swagger UI assets: /docs/oauth2-redirect и т.п.
    if path.startswith("/docs/"):
        return True
    return any(path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public_path(path):
        return await call_next(request)

    db = SessionLocal()
    try:
        user = get_user_from_request(request, db)
        if not user:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "Не авторизован"})
            return RedirectResponse("/login")
        if getattr(user, "is_active", True) is False:
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "Доступ отозван. Обратитесь к администратору."})
            return HTMLResponse(
                "<h3 style='font-family:sans-serif;padding:40px'>Доступ отозван. Обратитесь к администратору.</h3>",
                status_code=403,
            )

        # Проверка доступа к разделу по правам сотрудника
        section = auth.section_for_path(path)
        # Превью/генерация шаблонов: CRM и сметы могут вызывать API без раздела «Документы».
        # Сам UI /documents и вкладка в карточке сделки — только при явном праве documents.
        if section == "documents" and path.startswith("/api/document-templates"):
            can_preview_api = (
                auth.user_can_access(user, "documents")
                or auth.user_can_access(user, "crm")
                or auth.user_can_access(user, "quotes")
            )
            if not can_preview_api:
                return JSONResponse(status_code=403, content={"error": "Нет доступа к разделу"})
        elif section and not auth.user_can_access(user, section):
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "Нет доступа к разделу"})
            # После логина браузер всегда идёт на «/». Если дашборда нет в правах —
            # мягко ведём в первый доступный раздел вместо «запрещено» / ощущения кика.
            if path == "/" or path == "":
                home = auth.first_accessible_path(user)
                if home and home != "/":
                    return RedirectResponse(home, status_code=302)
            return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>Доступ к разделу запрещён. Обратитесь к администратору.</h3>", status_code=403)

        request.state.user = user
        request.state.user_sections = [
            key for key in auth.SECTIONS if auth.user_can_access(user, key)
        ]
        try:
            _attach_city_context(request, db, user)
        except Exception:
            request.state.cities = []
            request.state.current_city = None
            request.state.current_city_name = "Алматы"
            request.state.can_switch_city = False
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
    """Создаёт компанию/контакт + сделку на первой стадии воронки (по правилам маршрутизации)."""
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

    pipeline, route_assignee = _resolve_pipeline_for_source(db, source)
    stage = None
    if pipeline:
        stage = db.query(Stage).filter(Stage.pipeline_id == pipeline.id).order_by(Stage.order_index, Stage.id).first()

    title_bits = [f"Заявка: {name}"]
    if source in LEAD_SOURCES:
        title_bits.append(f"({LEAD_SOURCES[source]})")
    title = " ".join(title_bits)

    assignee_id = route_assignee or _default_assignee_id(db)
    default_city = _default_city(db)
    deal = Deal(
        title=title[:200],
        company_id=company.id,
        contact_id=contact.id,
        pipeline_id=pipeline.id if pipeline else None,
        stage=stage.id if stage else 1,
        setup_date=event_date,
        event_date=event_date,
        event_address=event_address,
        city=default_city.name if default_city else "Алматы",
        city_id=default_city.id if default_city else None,
        comment=message or None,
        source=source,
        assignee_id=assignee_id,
        sales_manager_id=assignee_id,
        qualification=None,
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
    limited = rate_limit.limit_public_leads(request)
    if limited:
        return limited
    expected = (os.getenv("LEAD_API_KEY") or "").strip()
    if expected:
        got = (request.headers.get("X-Lead-Key") or request.query_params.get("key") or "").strip()
        if got != expected:
            raise HTTPException(status_code=401, detail="Invalid lead API key")
    if not (payload.name or "").strip() and not (payload.phone or "").strip():
        raise HTTPException(status_code=400, detail="Укажите имя или телефон")
    result = _ingest_lead(db, payload)
    try:
        audit.write_audit(
            db, user_id=None, entity_type="deal", entity_id=result.get("deal_id"),
            action="create", diff={"source": "public_lead", **{k: result.get(k) for k in ("source", "title")}},
            ip=audit.request_ip(request), commit=True,
        )
    except Exception:
        pass
    return result


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
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    limited = rate_limit.limit_login(request)
    if limited:
        return limited
    login_name = (username or "").strip().lower()
    user = db.query(User).filter(User.username == login_name).first()
    if not user:
        # совместимость со старыми логинами без нормализации регистра
        user = db.query(User).filter(User.username == (username or "").strip()).first()
    if not user or not verify_password(password, user.hashed_password):
        rate_limit.record_login_failure(request)
        return JSONResponse(status_code=400, content={"error": "Неверный логин или пароль"})
    if getattr(user, "is_active", True) is False:
        rate_limit.record_login_failure(request)
        return JSONResponse(status_code=403, content={"error": "Доступ отозван. Обратитесь к администратору."})

    # Прозрачная миграция старых SHA-256 хэшей на PBKDF2
    if auth.is_legacy_hash(user.hashed_password):
        user.hashed_password = get_password_hash(password)
        db.commit()

    ver = int(getattr(user, "session_version", 0) or 0)
    token = auth.create_session_token(user.username, session_version=ver)
    redirect = auth.first_accessible_path(user)
    response = JSONResponse(content={
        "status": "success",
        "session_days": round(auth.SESSION_MAX_AGE / 86400, 1),
        "redirect": redirect,
    })
    response.set_cookie(
        key="session_token",
        value=token,
        **auth.session_cookie_kwargs(request),
    )
    return response

@app.post("/api/logout")
async def logout(request: Request):
    response = JSONResponse(content={"status": "success"})
    # delete_cookie должен совпадать по path/secure с set_cookie
    response.delete_cookie(
        "session_token",
        path="/",
        samesite="lax",
        secure=auth.cookie_secure_flag(request),
    )
    return response


@app.post("/api/logout-all")
async def logout_all(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Инвалидировать все сессии пользователя (инкремент session_version). Текущий cookie тоже сбрасывается."""
    user.session_version = int(getattr(user, "session_version", 0) or 0) + 1
    audit.write_audit(
        db, user_id=user.id, entity_type="session", entity_id=user.id,
        action="logout_all", diff={"session_version": user.session_version},
        ip=audit.request_ip(request),
    )
    db.commit()
    response = JSONResponse(content={
        "status": "success",
        "message": "Все сессии завершены. Войдите снова на этом устройстве.",
    })
    response.delete_cookie(
        "session_token",
        path="/",
        samesite="lax",
        secure=auth.cookie_secure_flag(request),
    )
    return response


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _change_own_password(
    body: ChangePasswordIn,
    request: Request,
    db: Session,
    user: User,
):
    """Смена собственного пароля (текущий + новый). Инвалидирует прочие сессии."""
    cur = (body.current_password or "").strip()
    new = (body.new_password or "").strip()
    if not cur or not new:
        return JSONResponse(status_code=400, content={"error": "Укажите текущий и новый пароль"})
    if len(new) < 8:
        return JSONResponse(status_code=400, content={"error": "Новый пароль не короче 8 символов"})
    if new == cur:
        return JSONResponse(status_code=400, content={"error": "Новый пароль должен отличаться от текущего"})
    if not verify_password(cur, user.hashed_password):
        return JSONResponse(status_code=400, content={"error": "Неверный текущий пароль"})
    user.hashed_password = get_password_hash(new)
    user.session_version = int(getattr(user, "session_version", 0) or 0) + 1
    audit.write_audit(
        db, user_id=user.id, entity_type="user", entity_id=user.id,
        action="password_change", diff={"password": "changed"},
        ip=audit.request_ip(request),
    )
    db.commit()
    # Обновляем cookie текущей сессии под новую session_version
    token = auth.create_session_token(user.username, session_version=user.session_version)
    response = JSONResponse(content={
        "status": "success",
        "message": "Пароль изменён. Другие устройства нужно заново авторизовать.",
    })
    response.set_cookie("session_token", token, **auth.session_cookie_kwargs(request))
    return response


@app.post("/api/me/change-password")
async def api_me_change_password(
    body: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _change_own_password(body, request, db, user)


@app.post("/api/admin/change-password")
async def api_admin_change_password(
    body: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Алиас: смена собственного пароля (не чужого)."""
    return _change_own_password(body, request, db, user)


# -- Рабочий день (учёт смен + геолокация) --

def _almaty_now() -> datetime:
    """Локальное время Алматы (UTC+5) без внешних зависимостей."""
    return datetime.utcnow() + timedelta(hours=5)


def _almaty_day_start_utc(days_ago: int = 0) -> datetime:
    """Начало календарного дня Алматы в UTC (для фильтра started_at)."""
    local = _almaty_now() - timedelta(days=days_ago)
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight - timedelta(hours=5)


# --- Multi-city helpers -------------------------------------------------
ACTIVE_CITY_COOKIE = "active_city_id"
_geocode_cache: Dict[str, Optional[str]] = {}


def _city_to_dict(c: City) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "is_active": bool(c.is_active),
        "timezone": c.timezone,
        "sort_order": int(c.sort_order or 0),
    }


def _default_city(db: Session) -> Optional[City]:
    return (
        db.query(City)
        .filter(City.slug == "almaty")
        .first()
        or db.query(City).filter(City.is_active == True).order_by(City.sort_order, City.id).first()  # noqa: E712
        or db.query(City).order_by(City.sort_order, City.id).first()
    )


def _cities_for_user(db: Session, user: Optional[User], include_inactive: bool = False) -> List[City]:
    q = db.query(City).order_by(City.sort_order, City.id)
    if user and user.role == "admin" and include_inactive:
        return q.all()
    rows = q.filter(City.is_active == True).all()  # noqa: E712
    if not user:
        return rows
    if user.role in ("admin", "manager"):
        return rows
    home_id = getattr(user, "city_id", None)
    if home_id:
        home = next((c for c in rows if c.id == home_id), None)
        if home:
            return [home]
        # домашний город неактивен — всё равно отдадим его
        home_row = db.query(City).filter(City.id == home_id).first()
        return [home_row] if home_row else rows
    return rows


def _resolve_active_city(db: Session, user: Optional[User], request: Optional[Request] = None) -> Optional[City]:
    allowed = _cities_for_user(db, user, include_inactive=False)
    allowed_ids = {c.id for c in allowed}
    cookie_id = None
    if request is not None:
        raw = request.cookies.get(ACTIVE_CITY_COOKIE)
        if raw and str(raw).isdigit():
            cookie_id = int(raw)
    if cookie_id and cookie_id in allowed_ids:
        return next(c for c in allowed if c.id == cookie_id)
    if user and getattr(user, "city_id", None) and user.city_id in allowed_ids:
        return next(c for c in allowed if c.id == user.city_id)
    setting = db.query(AppSetting).filter(AppSetting.key == "active_city_id").first()
    if setting and setting.value and str(setting.value).isdigit():
        sid = int(setting.value)
        if sid in allowed_ids:
            return next(c for c in allowed if c.id == sid)
    if allowed:
        return allowed[0]
    return _default_city(db)


def _attach_city_context(request: Request, db: Session, user: Optional[User]):
    cities = _cities_for_user(db, user, include_inactive=False)
    current = _resolve_active_city(db, user, request)
    request.state.cities = cities
    request.state.current_city = current
    request.state.current_city_name = current.name if current else "Алматы"
    request.state.can_switch_city = len(cities) > 1


def _apply_deal_city_filter(query, city_id: Optional[int]):
    if not city_id:
        return query
    # null city_id — видимы во всех городах (legacy / до backfill)
    return query.filter((Deal.city_id == city_id) | (Deal.city_id.is_(None)))


def _not_deleted(query, model):
    """Default lists: hide soft-deleted rows (Корзина)."""
    col = getattr(model, "deleted_at", None)
    if col is None:
        return query
    return query.filter(col.is_(None))


def _require_trash_access(user: User):
    if not user or user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Только администратор или менеджер"})
    return None


def _soft_delete_entity(entity, user: Optional[User]):
    entity.deleted_at = datetime.utcnow()
    if hasattr(entity, "deleted_by_id"):
        entity.deleted_by_id = user.id if user else None


def _restore_entity(entity):
    entity.deleted_at = None
    if hasattr(entity, "deleted_by_id"):
        entity.deleted_by_id = None


def _user_display_name(u: Optional[User]) -> str:
    """Единое безопасное отображаемое имя; пустой user → \"\"."""
    if not u:
        return ""
    return (u.full_name or u.username or "").strip()


def _sync_deal_city_text(db: Session, deal: Deal):
    """Держим текстовое Deal.city синхронным с City.name для документов."""
    if not getattr(deal, "city_id", None):
        return
    city = db.query(City).filter(City.id == deal.city_id).first()
    if city:
        deal.city = city.name


def _reverse_geocode(lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    """Nominatim reverse geocode → короткая метка. Fail soft → None."""
    if lat is None or lng is None:
        return None
    key = f"{float(lat):.4f},{float(lng):.4f}"
    if key in _geocode_cache:
        return _geocode_cache[key]
    label = None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "accept-language": "ru",
                "zoom": 16,
            },
            headers={
                "User-Agent": "IntroShowCRM/1.0 (rental; contact: show.intro@yandex.kz)",
            },
            timeout=3.5,
        )
        if resp.ok:
            data = resp.json() or {}
            addr = data.get("address") or {}
            road = addr.get("road") or addr.get("pedestrian") or addr.get("residential")
            suburb = addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter")
            city = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("state")
            )
            parts = [p for p in (city, suburb or road) if p]
            if not parts and data.get("display_name"):
                parts = [str(data["display_name"]).split(",")[0].strip()]
            label = ", ".join(parts) if parts else None
            if label and len(label) > 80:
                label = label[:77] + "…"
    except Exception:
        label = None
    _geocode_cache[key] = label
    return label


def _session_duration_seconds(sess: WorkSession, now: Optional[datetime] = None) -> int:
    start = sess.started_at
    if not start:
        return 0
    end = sess.ended_at or now or datetime.utcnow()
    return max(0, int((end - start).total_seconds()))


def _work_session_to_dict(sess: WorkSession, user_name: Optional[str] = None) -> dict:
    now = datetime.utcnow()
    open_sess = sess.ended_at is None
    return {
        "id": sess.id,
        "user_id": sess.user_id,
        "user_name": user_name,
        "city_id": getattr(sess, "city_id", None),
        "started_at": sess.started_at.isoformat() + "Z" if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() + "Z" if sess.ended_at else None,
        "is_open": open_sess,
        "elapsed_seconds": _session_duration_seconds(sess, now),
        "start_lat": sess.start_lat,
        "start_lng": sess.start_lng,
        "start_accuracy": sess.start_accuracy,
        "start_geo_denied": bool(sess.start_geo_denied),
        "start_place": getattr(sess, "start_place", None) or sess.start_label,
        "end_lat": sess.end_lat,
        "end_lng": sess.end_lng,
        "end_accuracy": sess.end_accuracy,
        "end_geo_denied": bool(sess.end_geo_denied),
        "end_place": getattr(sess, "end_place", None),
        "start_label": sess.start_label,
        "note": sess.note,
    }


def _parse_geo_payload(data: Optional[dict]) -> dict:
    data = data or {}
    def _f(key):
        v = data.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    geo_denied = bool(data.get("geo_denied"))
    lat, lng, acc = _f("lat"), _f("lng"), _f("accuracy")
    if lat is None or lng is None:
        if data.get("lat") is not None or data.get("lng") is not None:
            geo_denied = True
        lat = lng = acc = None
    return {"lat": lat, "lng": lng, "accuracy": acc, "geo_denied": geo_denied}


async def _read_json_body(request: Request) -> dict:
    try:
        raw = await request.json()
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


@app.get("/api/workday/current")
def workday_current(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    sess = (
        db.query(WorkSession)
        .filter(WorkSession.user_id == user.id, WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.desc())
        .first()
    )
    if not sess:
        return {
            "is_working": False,
            "session": None,
            "elapsed_seconds": 0,
        }
    payload = _work_session_to_dict(sess, user.full_name or user.username)
    return {
        "is_working": True,
        "session": payload,
        "elapsed_seconds": payload["elapsed_seconds"],
    }


@app.post("/api/workday/start")
async def workday_start(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    open_sess = (
        db.query(WorkSession)
        .filter(WorkSession.user_id == user.id, WorkSession.ended_at.is_(None))
        .first()
    )
    if open_sess:
        payload = _work_session_to_dict(open_sess, user.full_name or user.username)
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Рабочий день уже начат",
                "is_working": True,
                "session": payload,
                "elapsed_seconds": payload["elapsed_seconds"],
            },
        )

    geo = _parse_geo_payload(await _read_json_body(request))
    place = _reverse_geocode(geo["lat"], geo["lng"]) if geo["lat"] is not None else None
    active_city = _resolve_active_city(db, user, request)
    sess = WorkSession(
        user_id=user.id,
        city_id=active_city.id if active_city else getattr(user, "city_id", None),
        started_at=datetime.utcnow(),
        start_lat=geo["lat"],
        start_lng=geo["lng"],
        start_accuracy=geo["accuracy"],
        start_geo_denied=geo["geo_denied"] or (geo["lat"] is None),
        start_place=place,
    )
    db.add(sess)
    db.flush()
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="work_session",
        entity_id=sess.id,
        action="workday_start",
        diff={
            "lat": geo["lat"],
            "lng": geo["lng"],
            "accuracy": geo["accuracy"],
            "geo_denied": sess.start_geo_denied,
            "start_place": place,
            "city_id": sess.city_id,
        },
        ip=audit.request_ip(request),
    )
    db.commit()
    db.refresh(sess)
    payload = _work_session_to_dict(sess, user.full_name or user.username)
    return {
        "status": "ok",
        "is_working": True,
        "session": payload,
        "elapsed_seconds": payload["elapsed_seconds"],
    }


@app.post("/api/workday/end")
async def workday_end(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sess = (
        db.query(WorkSession)
        .filter(WorkSession.user_id == user.id, WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.desc())
        .first()
    )
    if not sess:
        return JSONResponse(
            status_code=409,
            content={"detail": "Нет открытого рабочего дня", "is_working": False, "session": None},
        )

    geo = _parse_geo_payload(await _read_json_body(request))
    place = _reverse_geocode(geo["lat"], geo["lng"]) if geo["lat"] is not None else None
    sess.ended_at = datetime.utcnow()
    sess.end_lat = geo["lat"]
    sess.end_lng = geo["lng"]
    sess.end_accuracy = geo["accuracy"]
    sess.end_geo_denied = geo["geo_denied"] or (geo["lat"] is None)
    sess.end_place = place
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="work_session",
        entity_id=sess.id,
        action="workday_end",
        diff={
            "lat": geo["lat"],
            "lng": geo["lng"],
            "accuracy": geo["accuracy"],
            "geo_denied": sess.end_geo_denied,
            "end_place": place,
            "duration_seconds": _session_duration_seconds(sess),
        },
        ip=audit.request_ip(request),
    )
    db.commit()
    db.refresh(sess)
    payload = _work_session_to_dict(sess, user.full_name or user.username)
    return {
        "status": "ok",
        "is_working": False,
        "session": payload,
        "elapsed_seconds": payload["elapsed_seconds"],
    }


def _workday_history_impl(db: Session, user: User, from_date=None, to_date=None, filter_user_id=None, city_id=None):
    is_admin = user.role == "admin"
    q = db.query(WorkSession)
    if is_admin:
        if filter_user_id:
            q = q.filter(WorkSession.user_id == filter_user_id)
    else:
        q = q.filter(WorkSession.user_id == user.id)
    if city_id:
        q = q.filter((WorkSession.city_id == city_id) | (WorkSession.city_id.is_(None)))

    def _parse_day(s: Optional[str], end: bool = False) -> Optional[datetime]:
        if not s:
            return None
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            utc = d - timedelta(hours=5)  # день Алматы → UTC
            if end:
                utc = utc + timedelta(days=1)
            return utc
        except ValueError:
            return None

    start_utc = _parse_day(from_date)
    end_utc = _parse_day(to_date, end=True)
    if start_utc:
        q = q.filter(WorkSession.started_at >= start_utc)
    if end_utc:
        q = q.filter(WorkSession.started_at < end_utc)

    sessions = q.order_by(WorkSession.started_at.desc()).limit(500).all()
    users_map = {u.id: (u.full_name or u.username) for u in db.query(User).all()}
    now = datetime.utcnow()
    today_start = _almaty_day_start_utc(0)
    week_start = _almaty_day_start_utc(_almaty_now().weekday())  # Monday=0

    summary_q = db.query(WorkSession)
    if is_admin:
        if filter_user_id:
            summary_q = summary_q.filter(WorkSession.user_id == filter_user_id)
    else:
        summary_q = summary_q.filter(WorkSession.user_id == user.id)
    if city_id:
        summary_q = summary_q.filter(
            (WorkSession.city_id == city_id) | (WorkSession.city_id.is_(None))
        )
    week_sessions = summary_q.filter(
        or_(
            WorkSession.started_at >= week_start,
            WorkSession.ended_at.is_(None),
        )
    ).all()

    by_user = {}
    for s in week_sessions:
        row = by_user.setdefault(s.user_id, {
            "user_id": s.user_id,
            "name": users_map.get(s.user_id, f"#{s.user_id}"),
            "today_seconds": 0,
            "week_seconds": 0,
            "is_working": False,
        })
        dur = _session_duration_seconds(s, now)
        if s.started_at and s.started_at >= week_start:
            row["week_seconds"] += dur
        elif s.ended_at is None and s.started_at and s.started_at < week_start:
            row["week_seconds"] += max(0, int((now - week_start).total_seconds()))
        if s.started_at and s.started_at >= today_start:
            row["today_seconds"] += dur
        elif s.ended_at is None and s.started_at and s.started_at < today_start:
            row["today_seconds"] += max(0, int((now - today_start).total_seconds()))
        if s.ended_at is None:
            row["is_working"] = True

    return {
        "sessions": [_work_session_to_dict(s, users_map.get(s.user_id)) for s in sessions],
        "summary": sorted(by_user.values(), key=lambda x: (-x["today_seconds"], x["name"])),
        "is_admin": is_admin,
    }


@app.get("/api/workday/history")
def workday_history(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """История смен: ?from=&to=&user_id=&city_id=. Админ — все; обычный — только свои."""
    uid_raw = request.query_params.get("user_id")
    try:
        filter_uid = int(uid_raw) if uid_raw else None
    except ValueError:
        filter_uid = None
    city_raw = request.query_params.get("city_id")
    try:
        filter_city = int(city_raw) if city_raw not in (None, "") else None
    except ValueError:
        filter_city = None
    if filter_city is None:
        active = _resolve_active_city(db, user, request)
        filter_city = active.id if active else None
    if filter_city == 0:
        filter_city = None
    return _workday_history_impl(
        db, user,
        from_date=request.query_params.get("from"),
        to_date=request.query_params.get("to"),
        filter_user_id=filter_uid,
        city_id=filter_city,
    )


@app.get("/", response_class=HTMLResponse)
async def read_dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    active = _resolve_active_city(db, user, request)
    deals_q = _not_deleted(db.query(Deal), Deal)
    deals_q = _apply_deal_city_filter(deals_q, active.id if active else None)
    deals = deals_q.all()
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

    recent_deals = _not_deleted(db.query(Deal), Deal).order_by(Deal.id.desc()).limit(5).all()
    recent_messages = db.query(ChatMessage).order_by(ChatMessage.id.desc()).limit(6).all()

    stats = {
        "revenue": revenue,
        "in_work_sum": in_work_sum,
        "deals_total": len(deals),
        "deals_active": len(active_deals),
        "deals_won": len(won_deals),
        "companies_count": _not_deleted(db.query(Company), Company).count(),
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
    deals = _not_deleted(db.query(Deal), Deal).order_by(Deal.id.desc()).all()
    return templates.TemplateResponse("quotes.html", {"request": request, "active_page": "quotes", "deals": deals})

@app.get("/calendar", response_class=HTMLResponse)
def read_calendar(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("calendar.html", {"request": request, "active_page": "calendar"})


@app.get("/today", response_class=HTMLResponse)
def read_today(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("today.html", {"request": request, "active_page": "today"})


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
    q = _not_deleted(db.query(Deal), Deal).filter(Deal.is_archived == False)  # noqa: E712
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
    active_city = _resolve_active_city(db, user, request)
    deals_q = _not_deleted(db.query(Deal), Deal)
    deals_q = _apply_deal_city_filter(deals_q, active_city.id if active_city else None)
    deals = deals_q.all()
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

    week_start_local = (_almaty_now() - timedelta(days=_almaty_now().weekday())).strftime("%Y-%m-%d")
    today_local = _almaty_now().strftime("%Y-%m-%d")
    workday_data = _workday_history_impl(
        db, user,
        from_date=week_start_local,
        to_date=today_local,
        city_id=active_city.id if active_city else None,
    )

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
        "workday_summary": workday_data.get("summary") or [],
        "workday_sessions": (workday_data.get("sessions") or [])[:40],
        "workday_is_admin": workday_data.get("is_admin"),
        "workday_from": week_start_local,
        "workday_to": today_local,
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
    query = _not_deleted(db.query(Task), Task)
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
    task = _not_deleted(db.query(Task), Task).filter(Task.id == task_id).first()
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
    for a in (task.assignees or []):
        if a.user_id and a.user_id != user.id:
            _notify_user(
                db, a.user_id,
                kind="task_assign",
                title=f"Новая задача: {task.title}",
                body=f"Постановщик: {user.full_name or user.username}",
                link=f"/tasks?open={task.id}",
                deal_id=task.deal_id,
                task_id=task.id,
                skip_user_id=user.id,
            )
    db.commit()
    return {"id": task.id, "status": "success"}

@app.put("/api/tasks/{task_id}")
def update_task(task_id: int, t: TaskUpdate, db: Session = Depends(get_db)):
    task = _not_deleted(db.query(Task), Task).filter(Task.id == task_id).first()
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

def _soft_delete_task(db: Session, task_id: Optional[int], user: Optional[User] = None) -> bool:
    """Переместить задачу в корзину (soft-delete)."""
    if not task_id:
        return False
    task = _not_deleted(db.query(Task), Task).filter(Task.id == task_id).first()
    if not task:
        return False
    _soft_delete_entity(task, user)
    return True


def _hard_purge_task(db: Session, task_id: Optional[int]) -> bool:
    """Окончательно удалить задачу и связанные строки (только из корзины)."""
    if not task_id:
        return False
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return False
    db.query(AppNotification).filter(AppNotification.task_id == task_id).update(
        {AppNotification.task_id: None}, synchronize_session=False
    )
    db.query(InternalMessage).filter(InternalMessage.task_id == task_id).update(
        {InternalMessage.task_id: None}, synchronize_session=False
    )
    db.query(DealStaffAssignment).filter(DealStaffAssignment.task_id == task_id).update(
        {DealStaffAssignment.task_id: None}, synchronize_session=False
    )
    db.query(TaskComment).filter(TaskComment.task_id == task_id).delete(synchronize_session=False)
    db.query(TaskAssignee).filter(TaskAssignee.task_id == task_id).delete(synchronize_session=False)
    db.query(TaskObserver).filter(TaskObserver.task_id == task_id).delete(synchronize_session=False)
    db.query(TaskChecklistItem).filter(TaskChecklistItem.task_id == task_id).delete(synchronize_session=False)
    db.delete(task)
    return True


def _purge_task(db: Session, task_id: Optional[int]) -> bool:
    """Совместимость: soft-delete (корзина), не hard-delete."""
    return _soft_delete_task(db, task_id, None)


@app.delete("/api/tasks/{task_id}")
def delete_task(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not _soft_delete_task(db, task_id, user):
        return JSONResponse(status_code=404, content={"error": "Задача не найдена"})
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="task",
        entity_id=task_id,
        action="soft_delete",
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success", "trashed": True}

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
    mentioned = _find_mentioned_users(db, text)
    for u in mentioned:
        if u.id == user.id:
            continue
        _notify_user(
            db, u.id,
            kind="mention",
            title=f"Вас упомянули в задаче «{task.title}»",
            body=text[:200],
            link=f"/tasks?open={task.id}",
            deal_id=task.deal_id,
            task_id=task.id,
            skip_user_id=user.id,
        )
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

def _active_users_count(db: Session) -> int:
    q = db.query(User)
    # is_active может отсутствовать на очень старых строках — считаем None как True
    return q.filter(or_(User.is_active == True, User.is_active.is_(None))).count()  # noqa: E712


def _normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def _invite_status(inv: UserInvite) -> str:
    if inv.revoked_at:
        return "revoked"
    if inv.used_at:
        return "used"
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        return "expired"
    return "pending"


def _invite_public_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/register/{token}"


def _add_user_to_company_chat(db: Session, user_id: int) -> None:
    try:
        company_chat = (
            db.query(InternalChat)
            .filter(InternalChat.chat_type == "company")
            .order_by(InternalChat.id.asc())
            .first()
        )
        if company_chat:
            _ensure_chat_member(db, company_chat.id, user_id)
            db.commit()
    except Exception:
        db.rollback()


def _role_to_dict(r: Role) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description or "",
        "permissions": r.permissions or [],
        "is_system": bool(r.is_system),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _resolve_access_role(
    db: Session,
    *,
    role_id: Optional[int],
    role_str: Optional[str],
    permissions: Optional[List[str]],
    allow_admin: bool = True,
) -> tuple:
    """Вернуть (user.role string, role_id, permissions) с копией прав из Role."""
    if role_str == "admin":
        if not allow_admin:
            return None
        return "admin", None, []

    access_role = None
    if role_id is not None:
        access_role = db.query(Role).filter(Role.id == role_id).first()
        if not access_role:
            return None

    # Кастомная роль → user.role = user (manager оставляем только как legacy без role_id)
    system_role = "user"
    if role_str in ("user", "manager") and access_role is None:
        system_role = role_str

    if permissions is not None:
        perms = list(permissions)
    elif access_role is not None:
        perms = list(access_role.permissions or [])
    else:
        perms = []

    return system_role, (access_role.id if access_role else None), perms


@app.get("/api/users")
def get_users(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    users = db.query(User).order_by(User.id.asc()).all()
    return [{
        "id": u.id,
        "username": u.username,
        "full_name": u.full_name,
        "phone": getattr(u, "phone", None),
        "role": u.role,
        "role_id": getattr(u, "role_id", None),
        "role_name": (u.access_role.name if getattr(u, "access_role", None) else None),
        "permissions": u.permissions,
        "city_id": getattr(u, "city_id", None),
        "city_name": (u.home_city.name if getattr(u, "home_city", None) else None),
        "is_active": bool(getattr(u, "is_active", True) is not False),
        "dismissed_at": u.dismissed_at.isoformat() if getattr(u, "dismissed_at", None) else None,
    } for u in users]

@app.get("/api/users/names")
def get_user_names(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Список сотрудников (для выбора ответственного) — доступен всем авторизованным."""
    rows = (
        db.query(User)
        .filter(or_(User.is_active == True, User.is_active.is_(None)))  # noqa: E712
        .all()
    )
    return [{"id": u.id, "username": u.username, "full_name": u.full_name or u.username} for u in rows]

@app.get("/api/users/sections")
def get_user_sections(user: User = Depends(get_current_user)):
    """Справочник разделов и флагов прав для настройки доступа."""
    return {"sections": auth.SECTIONS, "flags": auth.PERMISSION_FLAGS}

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    role_id: Optional[int] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    permissions: Optional[List[str]] = None
    city_id: Optional[int] = None

class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    role_id: Optional[int] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    permissions: Optional[List[str]] = None
    city_id: Optional[int] = None


class InviteCreate(BaseModel):
    permissions: Optional[List[str]] = None
    role: str = "user"
    role_id: Optional[int] = None
    city_id: Optional[int] = None
    expires_days: Optional[int] = 7


class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: Optional[List[str]] = None


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[str]] = None


class RegisterIn(BaseModel):
    token: str
    first_name: str
    last_name: str
    phone: Optional[str] = None
    email: str
    password: str
    password_confirm: str


@app.post("/api/users")
def create_user(
    u: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if _active_users_count(db) >= 10:
        return JSONResponse(status_code=400, content={"error": "Максимальное количество пользователей (10) достигнуто"})

    username = _normalize_username(u.username)
    if not username:
        return JSONResponse(status_code=400, content={"error": "Укажите логин"})
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return JSONResponse(status_code=400, content={"error": "Пользователь уже существует"})

    resolved = _resolve_access_role(
        db, role_id=u.role_id, role_str=u.role, permissions=u.permissions, allow_admin=True,
    )
    if resolved is None:
        return JSONResponse(status_code=400, content={"error": "Роль не найдена или недопустима"})
    system_role, access_role_id, perms = resolved

    new_user = User(
        username=username,
        hashed_password=get_password_hash(u.password),
        role=system_role,
        role_id=access_role_id,
        full_name=u.full_name,
        phone=(u.phone or "").strip() or None,
        permissions=perms,
        city_id=u.city_id,
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    audit.write_audit(
        db, user_id=current_user.id, entity_type="user", entity_id=new_user.id,
        action="user_create",
        diff={
            "username": new_user.username,
            "role": new_user.role,
            "role_id": new_user.role_id,
            "permissions": new_user.permissions,
        },
        ip=audit.request_ip(request), commit=True,
    )
    _add_user_to_company_chat(db, new_user.id)
    return {"status": "success"}

@app.put("/api/users/{user_id}")
def update_user(
    user_id: int,
    u: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return JSONResponse(status_code=404, content={"error": "Пользователь не найден"})
    diff = {}
    if u.password:
        target.hashed_password = get_password_hash(u.password)
        diff["password"] = "changed"
        # Смена пароля — инвалидируем чужие сессии
        target.session_version = int(getattr(target, "session_version", 0) or 0) + 1
    unset = u.dict(exclude_unset=True)
    if "role" in unset or "role_id" in unset or "permissions" in unset:
        next_role_str = u.role if "role" in unset else target.role
        next_role_id = u.role_id if "role_id" in unset else getattr(target, "role_id", None)
        # Явный сброс role_id при выборе администратора
        if next_role_str == "admin":
            next_role_id = None
        next_perms = u.permissions if "permissions" in unset else target.permissions
        resolved = _resolve_access_role(
            db,
            role_id=next_role_id,
            role_str=next_role_str,
            permissions=next_perms if "permissions" in unset else (
                list(next_perms) if next_perms is not None else None
            ),
            allow_admin=True,
        )
        if resolved is None:
            return JSONResponse(status_code=400, content={"error": "Роль не найдена или недопустима"})
        system_role, access_role_id, perms = resolved
        if target.id == current_user.id and system_role != "admin":
            return JSONResponse(status_code=400, content={"error": "Нельзя снять роль администратора с самого себя"})
        if target.role != system_role:
            diff["role"] = {"from": target.role, "to": system_role}
        if getattr(target, "role_id", None) != access_role_id:
            diff["role_id"] = {"from": getattr(target, "role_id", None), "to": access_role_id}
        if target.permissions != perms:
            diff["permissions"] = {"from": target.permissions, "to": perms}
        target.role = system_role
        target.role_id = access_role_id
        target.permissions = perms
    if u.full_name is not None:
        if target.full_name != u.full_name:
            diff["full_name"] = {"from": target.full_name, "to": u.full_name}
        target.full_name = u.full_name
    if u.phone is not None:
        phone = (u.phone or "").strip() or None
        if target.phone != phone:
            diff["phone"] = {"from": target.phone, "to": phone}
        target.phone = phone
    if "city_id" in unset:
        new_cid = u.city_id
        if new_cid is not None and not db.query(City).filter(City.id == new_cid).first():
            return JSONResponse(status_code=400, content={"error": "Город не найден"})
        if target.city_id != new_cid:
            diff["city_id"] = {"from": target.city_id, "to": new_cid}
        target.city_id = new_cid
    if diff:
        audit.write_audit(
            db, user_id=current_user.id, entity_type="user", entity_id=target.id,
            action="permissions" if "permissions" in diff else "user_update",
            diff=diff, ip=audit.request_ip(request),
        )
    db.commit()
    return {"status": "success"}


@app.post("/api/users/{user_id}/dismiss")
def dismiss_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Мягкое увольнение: отозвать доступ в CRM, историю сохранить."""
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if current_user.id == user_id:
        return JSONResponse(status_code=400, content={"error": "Нельзя уволить самого себя"})
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return JSONResponse(status_code=404, content={"error": "Пользователь не найден"})
    if getattr(target, "is_active", True) is False:
        return {"status": "success", "already": True}
    target.is_active = False
    target.dismissed_at = datetime.utcnow()
    target.session_version = int(getattr(target, "session_version", 0) or 0) + 1
    audit.write_audit(
        db, user_id=current_user.id, entity_type="user", entity_id=target.id,
        action="user_dismiss", diff={"username": target.username},
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success"}


@app.post("/api/users/{user_id}/restore")
def restore_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Восстановить доступ уволенному сотруднику."""
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        return JSONResponse(status_code=404, content={"error": "Пользователь не найден"})
    if _active_users_count(db) >= 10 and getattr(target, "is_active", True) is False:
        return JSONResponse(status_code=400, content={"error": "Максимальное количество активных пользователей (10) достигнуто"})
    target.is_active = True
    target.dismissed_at = None
    audit.write_audit(
        db, user_id=current_user.id, entity_type="user", entity_id=target.id,
        action="user_restore", diff={"username": target.username},
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success"}


def _purge_user_dependencies(db: Session, user_id: int) -> None:
    """Снять FK-ссылки перед жёстким удалением сотрудника (Postgres + SQLite)."""
    # Приглашения
    db.query(UserInvite).filter(UserInvite.used_by_user_id == user_id).update(
        {UserInvite.used_by_user_id: None}, synchronize_session=False
    )
    db.query(UserInvite).filter(UserInvite.created_by_id == user_id).update(
        {UserInvite.created_by_id: None}, synchronize_session=False
    )
    # Обязательные связи — удаляем строки
    for model, col in (
        (InternalChatMember, InternalChatMember.user_id),
        (InternalMessage, InternalMessage.sender_id),
        (AppNotification, AppNotification.user_id),
        (WorkSession, WorkSession.user_id),
        (DealStaffAssignment, DealStaffAssignment.user_id),
        (DealAdvance, DealAdvance.user_id),
    ):
        db.query(model).filter(col == user_id).delete(synchronize_session=False)
    # Nullable — обнуляем
    db.query(TaskAssignee).filter(TaskAssignee.user_id == user_id).update(
        {TaskAssignee.user_id: None}, synchronize_session=False
    )
    db.query(TaskObserver).filter(TaskObserver.user_id == user_id).update(
        {TaskObserver.user_id: None}, synchronize_session=False
    )
    db.query(TaskComment).filter(TaskComment.user_id == user_id).update(
        {TaskComment.user_id: None}, synchronize_session=False
    )
    db.query(Deal).filter(Deal.assignee_id == user_id).update({Deal.assignee_id: None}, synchronize_session=False)
    db.query(Deal).filter(Deal.sales_manager_id == user_id).update({Deal.sales_manager_id: None}, synchronize_session=False)
    db.query(Deal).filter(Deal.project_manager_id == user_id).update({Deal.project_manager_id: None}, synchronize_session=False)
    db.query(Deal).filter(Deal.deleted_by_id == user_id).update({Deal.deleted_by_id: None}, synchronize_session=False)
    db.query(Task).filter(Task.creator_id == user_id).update({Task.creator_id: None}, synchronize_session=False)
    db.query(Task).filter(Task.deleted_by_id == user_id).update({Task.deleted_by_id: None}, synchronize_session=False)
    db.query(Activity).filter(Activity.assignee_id == user_id).update({Activity.assignee_id: None}, synchronize_session=False)
    db.query(DealPayrollLine).filter(DealPayrollLine.user_id == user_id).update(
        {DealPayrollLine.user_id: None}, synchronize_session=False
    )
    db.query(InternalChat).filter(InternalChat.created_by_id == user_id).update(
        {InternalChat.created_by_id: None}, synchronize_session=False
    )
    db.query(AuditLog).filter(AuditLog.user_id == user_id).update({AuditLog.user_id: None}, synchronize_session=False)
    db.query(Company).filter(Company.deleted_by_id == user_id).update(
        {Company.deleted_by_id: None}, synchronize_session=False
    )
    db.query(DealDocument).filter(DealDocument.deleted_by_id == user_id).update(
        {DealDocument.deleted_by_id: None}, synchronize_session=False
    )
    db.query(DealItem).filter(DealItem.issued_by_id == user_id).update(
        {DealItem.issued_by_id: None}, synchronize_session=False
    )
    db.query(DealItem).filter(DealItem.returned_by_id == user_id).update(
        {DealItem.returned_by_id: None}, synchronize_session=False
    )


@app.delete("/api/users/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if current_user.id == user_id:
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить самого себя"})

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return {"status": "success"}
    if u.username == "admin" or u.role == "admin":
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить администратора"})

    try:
        audit.write_audit(
            db, user_id=current_user.id, entity_type="user", entity_id=u.id,
            action="user_delete", diff={"username": u.username},
            ip=audit.request_ip(request),
        )
        _purge_user_dependencies(db, u.id)
        db.delete(u)
        db.commit()
    except Exception as e:
        db.rollback()
        return JSONResponse(
            status_code=500,
            content={"error": f"Не удалось удалить сотрудника: {type(e).__name__}"},
        )
    return {"status": "success"}


@app.get("/api/roles")
def list_roles(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    rows = db.query(Role).order_by(Role.is_system.desc(), Role.name.asc()).all()
    return [_role_to_dict(r) for r in rows]


@app.post("/api/roles")
def create_role(
    payload: RoleCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    name = (payload.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название роли"})
    if db.query(Role).filter(Role.name == name).first():
        return JSONResponse(status_code=400, content={"error": "Роль с таким названием уже есть"})
    role = Role(
        name=name,
        description=(payload.description or "").strip() or None,
        permissions=list(payload.permissions or []),
        is_system=False,
        created_at=datetime.utcnow(),
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    audit.write_audit(
        db, user_id=current_user.id, entity_type="role", entity_id=role.id,
        action="role_create",
        diff={"name": role.name, "permissions": role.permissions},
        ip=audit.request_ip(request), commit=True,
    )
    return {"status": "success", "role": _role_to_dict(role)}


@app.put("/api/roles/{role_id}")
def update_role(
    role_id: int,
    payload: RoleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        return JSONResponse(status_code=404, content={"error": "Роль не найдена"})
    unset = payload.dict(exclude_unset=True)
    diff = {}
    if "name" in unset:
        name = (payload.name or "").strip()
        if not name:
            return JSONResponse(status_code=400, content={"error": "Укажите название роли"})
        clash = db.query(Role).filter(Role.name == name, Role.id != role.id).first()
        if clash:
            return JSONResponse(status_code=400, content={"error": "Роль с таким названием уже есть"})
        if role.name != name:
            diff["name"] = {"from": role.name, "to": name}
        role.name = name
    if "description" in unset:
        desc = (payload.description or "").strip() or None
        if role.description != desc:
            diff["description"] = {"from": role.description, "to": desc}
        role.description = desc
    if "permissions" in unset:
        perms = list(payload.permissions or [])
        if role.permissions != perms:
            diff["permissions"] = {"from": role.permissions, "to": perms}
        role.permissions = perms
    if diff:
        audit.write_audit(
            db, user_id=current_user.id, entity_type="role", entity_id=role.id,
            action="role_update", diff=diff, ip=audit.request_ip(request),
        )
    db.commit()
    db.refresh(role)
    return {"status": "success", "role": _role_to_dict(role)}


@app.delete("/api/roles/{role_id}")
def delete_role(
    role_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        return JSONResponse(status_code=404, content={"error": "Роль не найдена"})
    if role.is_system:
        return JSONResponse(status_code=400, content={"error": "Системную роль нельзя удалить"})
    users_cnt = db.query(User).filter(User.role_id == role.id).count()
    invites_cnt = db.query(UserInvite).filter(UserInvite.role_id == role.id).count()
    if users_cnt or invites_cnt:
        return JSONResponse(
            status_code=400,
            content={"error": "Роль используется сотрудниками или приглашениями. Сначала смените роль у них."},
        )
    audit.write_audit(
        db, user_id=current_user.id, entity_type="role", entity_id=role.id,
        action="role_delete", diff={"name": role.name},
        ip=audit.request_ip(request),
    )
    db.delete(role)
    db.commit()
    return {"status": "success"}


@app.get("/api/invites")
def list_invites(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    rows = db.query(UserInvite).order_by(UserInvite.id.desc()).limit(50).all()
    out = []
    for inv in rows:
        status = _invite_status(inv)
        out.append({
            "id": inv.id,
            "token": inv.token,
            "permissions": inv.permissions or [],
            "role": inv.role or "user",
            "role_id": getattr(inv, "role_id", None),
            "role_name": (inv.access_role.name if getattr(inv, "access_role", None) else None),
            "city_id": inv.city_id,
            "city_name": (inv.city.name if inv.city else None),
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "used_at": inv.used_at.isoformat() if inv.used_at else None,
            "revoked_at": inv.revoked_at.isoformat() if inv.revoked_at else None,
            "used_by_user_id": inv.used_by_user_id,
            "status": status,
            "url": None,  # URL собираем на клиенте; для pending отдаём ниже
        })
    return out


@app.post("/api/invites")
def create_invite(
    payload: InviteCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    role = (payload.role or "user").strip() or "user"
    if role == "admin":
        return JSONResponse(status_code=400, content={"error": "Нельзя создать ссылку с ролью администратора"})
    if payload.city_id is not None and not db.query(City).filter(City.id == payload.city_id).first():
        return JSONResponse(status_code=400, content={"error": "Город не найден"})
    resolved = _resolve_access_role(
        db,
        role_id=payload.role_id,
        role_str=role,
        # None = взять права роли; явный [] с фронта = полный доступ (см. user_can_access).
        # Для invite с role_id предпочитаем права роли, если чекбоксы пустые —
        # иначе сотрудник регистрируется без разделов и «вылетает» с дашборда.
        permissions=(
            None
            if payload.role_id and not (payload.permissions or [])
            else payload.permissions
        ),
        allow_admin=False,
    )
    if resolved is None:
        return JSONResponse(status_code=400, content={"error": "Роль не найдена или недопустима"})
    system_role, access_role_id, perms = resolved
    days = payload.expires_days if payload.expires_days and payload.expires_days > 0 else 7
    days = min(int(days), 30)
    token = secrets.token_urlsafe(32)
    inv = UserInvite(
        token=token,
        permissions=perms,
        role=system_role,
        role_id=access_role_id,
        city_id=payload.city_id,
        created_by_id=current_user.id,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=days),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    audit.write_audit(
        db, user_id=current_user.id, entity_type="invite", entity_id=inv.id,
        action="invite_create",
        diff={
            "role": inv.role,
            "role_id": inv.role_id,
            "permissions": inv.permissions,
            "expires_at": inv.expires_at.isoformat(),
        },
        ip=audit.request_ip(request), commit=True,
    )
    url = _invite_public_url(request, inv.token)
    return {
        "status": "success",
        "id": inv.id,
        "token": inv.token,
        "url": url,
        "expires_at": inv.expires_at.isoformat(),
        "permissions": inv.permissions or [],
        "role": inv.role,
        "role_id": inv.role_id,
    }


@app.post("/api/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    inv = db.query(UserInvite).filter(UserInvite.id == invite_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"error": "Ссылка не найдена"})
    if inv.used_at:
        return JSONResponse(status_code=400, content={"error": "Ссылка уже использована"})
    if inv.revoked_at:
        return {"status": "success", "already": True}
    inv.revoked_at = datetime.utcnow()
    audit.write_audit(
        db, user_id=current_user.id, entity_type="invite", entity_id=inv.id,
        action="invite_revoke", diff={"token_prefix": inv.token[:8]},
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success"}


@app.get("/register/{token}", response_class=HTMLResponse)
async def read_register(request: Request, token: str, db: Session = Depends(get_db)):
    inv = db.query(UserInvite).filter(UserInvite.token == token).first()
    status = _invite_status(inv) if inv else "invalid"
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "token": token,
            "invite_ok": status == "pending",
            "invite_status": status,
        },
    )


@app.get("/api/register/{token}")
def api_register_info(token: str, request: Request, db: Session = Depends(get_db)):
    limited = rate_limit.limit_register(request)
    if limited:
        return limited
    inv = db.query(UserInvite).filter(UserInvite.token == token).first()
    if not inv:
        return JSONResponse(status_code=404, content={"error": "Ссылка недействительна", "status": "invalid"})
    status = _invite_status(inv)
    if status != "pending":
        messages = {
            "used": "Ссылка уже использована",
            "expired": "Срок действия ссылки истёк",
            "revoked": "Ссылка отозвана администратором",
        }
        return JSONResponse(
            status_code=400,
            content={"error": messages.get(status, "Ссылка недоступна"), "status": status},
        )
    return {"status": "ok", "role": inv.role or "user", "expires_at": inv.expires_at.isoformat() if inv.expires_at else None}


@app.post("/api/register")
def api_register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    limited = rate_limit.limit_register(request)
    if limited:
        return limited
    inv = db.query(UserInvite).filter(UserInvite.token == (payload.token or "").strip()).first()
    if not inv or _invite_status(inv) != "pending":
        return JSONResponse(status_code=400, content={"error": "Ссылка недействительна или уже использована"})

    first = (payload.first_name or "").strip()
    last = (payload.last_name or "").strip()
    email = _normalize_username(payload.email)
    phone = (payload.phone or "").strip() or None
    password = payload.password or ""
    if not first or not last:
        return JSONResponse(status_code=400, content={"error": "Укажите имя и фамилию"})
    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"error": "Укажите корректную почту — она будет логином"})
    if len(password) < 6:
        return JSONResponse(status_code=400, content={"error": "Пароль должен быть не короче 6 символов"})
    if password != (payload.password_confirm or ""):
        return JSONResponse(status_code=400, content={"error": "Пароли не совпадают"})
    if _active_users_count(db) >= 10:
        return JSONResponse(status_code=400, content={"error": "Достигнут лимит сотрудников. Обратитесь к администратору."})
    if db.query(User).filter(User.username == email).first():
        return JSONResponse(status_code=400, content={"error": "Пользователь с такой почтой уже есть"})

    full_name = f"{first} {last}".strip()
    # Права: снимок с invite; если пусто — копируем из роли. role_id сохраняем.
    access_role_id = getattr(inv, "role_id", None)
    access_role = None
    if access_role_id:
        access_role = db.query(Role).filter(Role.id == access_role_id).first()
        if not access_role:
            access_role_id = None
    system_role = inv.role or "user"
    if system_role == "admin":
        system_role = "user"
    invite_perms = list(inv.permissions or [])
    if not invite_perms and access_role is not None:
        invite_perms = list(access_role.permissions or [])
    new_user = User(
        username=email,
        hashed_password=get_password_hash(password),
        role=system_role,
        role_id=access_role_id,
        full_name=full_name,
        phone=phone,
        permissions=invite_perms,
        city_id=inv.city_id,
        is_active=True,
        session_version=0,
    )
    db.add(new_user)
    db.flush()
    inv.used_at = datetime.utcnow()
    inv.used_by_user_id = new_user.id
    db.commit()
    db.refresh(new_user)
    audit.write_audit(
        db, user_id=new_user.id, entity_type="user", entity_id=new_user.id,
        action="user_register_invite",
        diff={
            "username": new_user.username,
            "invite_id": inv.id,
            "role_id": new_user.role_id,
            "permissions": new_user.permissions,
        },
        ip=audit.request_ip(request), commit=True,
    )
    _add_user_to_company_chat(db, new_user.id)
    # Сразу выдаём сессию — без лишнего круга /login (особенно важно на мобильном PWA).
    ver = int(getattr(new_user, "session_version", 0) or 0)
    token = auth.create_session_token(new_user.username, session_version=ver)
    redirect = auth.first_accessible_path(new_user)
    response = JSONResponse(content={"status": "success", "redirect": redirect})
    response.set_cookie(
        key="session_token",
        value=token,
        **auth.session_cookie_kwargs(request),
    )
    return response

@app.get("/settings", response_class=HTMLResponse)
async def read_settings(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})


@app.get("/trash", response_class=HTMLResponse)
async def read_trash(request: Request, user: User = Depends(get_current_user)):
    if user.role not in ("admin", "manager"):
        return HTMLResponse(
            "<h3 style='font-family:sans-serif;padding:40px'>Корзина доступна администратору и менеджеру.</h3>",
            status_code=403,
        )
    return templates.TemplateResponse("trash.html", {"request": request, "active_page": "trash"})


@app.get("/api/admin/db-health")
def api_db_health(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Проверка БД: счётчики + путь + предупреждение SQLite на Vercel / безопасность прода."""
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор"})
    info = database_backend_info()
    deals_alive = _not_deleted(db.query(Deal), Deal).count()
    tasks_alive = _not_deleted(db.query(Task), Task).count()
    deals_trash = db.query(Deal).filter(Deal.deleted_at.isnot(None)).count()
    tasks_trash = db.query(Task).filter(Task.deleted_at.isnot(None)).count()
    work_sessions = db.query(WorkSession).count()
    companies_alive = _not_deleted(db.query(Company), Company).count()
    invites_total = db.query(UserInvite).count()
    invites_pending = db.query(UserInvite).filter(
        UserInvite.used_at.is_(None),
        UserInvite.revoked_at.is_(None),
        UserInvite.expires_at >= datetime.utcnow(),
    ).count()

    security_warnings = []
    if info.get("is_sqlite"):
        security_warnings.append(
            "SQLite вместо Postgres — для боя сотрудников обязателен DATABASE_URL (postgresql+psycopg2://…)."
        )
    session_secret = os.environ.get("SESSION_SECRET") or ""
    if not session_secret:
        security_warnings.append(
            "SESSION_SECRET не задан в окружении — задайте случайную строку ≥64 символов."
        )
    elif len(session_secret) < 64:
        security_warnings.append(
            f"SESSION_SECRET короткий ({len(session_secret)} символов) — рекомендуется ≥64."
        )
    admin_row = db.query(User).filter(User.username == "admin").first()
    if admin_row and verify_password("admin", admin_row.hashed_password):
        security_warnings.append(
            "Пароль admin всё ещё «admin» — смените в блоке «Сменить пароль» ниже."
        )
    if os.environ.get("FORCE_SECURE_COOKIE") != "1" and not info.get("on_vercel"):
        security_warnings.append(
            "FORCE_SECURE_COOKIE≠1 — на HTTPS за nginx включите FORCE_SECURE_COOKIE=1."
        )
    if (os.environ.get("ONEC_API_KEY") or "").strip() in ("", "test-onec-key-123"):
        security_warnings.append(
            "ONEC_API_KEY не задан или тестовый — смените перед интеграцией с 1С."
        )

    warning_ru = None
    if info.get("ephemeral_warning"):
        warning_ru = (
            "На Vercel используется эфемерный SQLite (/tmp). Данные могут пропасть при cold start. "
            "Для постоянной работы нужен VPS + Postgres (DATABASE_URL)."
        )
    elif security_warnings:
        warning_ru = " · ".join(security_warnings)
    elif info.get("is_sqlite"):
        warning_ru = "SQLite без Postgres — ок для локальной проверки, не для боевых сотрудников."

    return {
        **info,
        "counts": {
            "deals": deals_alive,
            "deals_in_trash": deals_trash,
            "tasks": tasks_alive,
            "tasks_in_trash": tasks_trash,
            "work_sessions": work_sessions,
            "companies": companies_alive,
            "invites": invites_total,
            "invites_pending": invites_pending,
        },
        "db_path": info.get("sqlite_path") or ("DATABASE_URL (Postgres)" if info.get("is_postgres") else str(DATABASE_URL)[:80]),
        "security_warnings": security_warnings,
        "warning_ru": warning_ru,
    }


@app.get("/api/trash")
def api_trash_list(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_trash_access(user)
    if denied:
        return denied
    users_map = {u.id: _user_display_name(u) for u in db.query(User).all()}
    deals = (
        db.query(Deal)
        .filter(Deal.deleted_at.isnot(None))
        .order_by(Deal.deleted_at.desc())
        .limit(200)
        .all()
    )
    tasks = (
        db.query(Task)
        .filter(Task.deleted_at.isnot(None))
        .order_by(Task.deleted_at.desc())
        .limit(200)
        .all()
    )
    companies = (
        db.query(Company)
        .filter(Company.deleted_at.isnot(None))
        .order_by(Company.deleted_at.desc())
        .limit(100)
        .all()
    )

    def _fmt(dt):
        return dt.strftime("%d.%m.%Y %H:%M") if dt else ""

    return {
        "deals": [
            {
                "id": d.id,
                "title": d.title,
                "company_name": d.company.name if d.company else "",
                "deleted_at": _fmt(d.deleted_at),
                "deleted_by": users_map.get(d.deleted_by_id),
                "entity_type": "deal",
            }
            for d in deals
        ],
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "deal_id": t.deal_id,
                "deleted_at": _fmt(t.deleted_at),
                "deleted_by": users_map.get(t.deleted_by_id),
                "entity_type": "task",
            }
            for t in tasks
        ],
        "companies": [
            {
                "id": c.id,
                "title": c.name,
                "deleted_at": _fmt(c.deleted_at),
                "deleted_by": users_map.get(c.deleted_by_id),
                "entity_type": "company",
            }
            for c in companies
        ],
    }


@app.post("/api/trash/{entity_type}/{entity_id}/restore")
def api_trash_restore(
    request: Request,
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    denied = _require_trash_access(user)
    if denied:
        return denied
    entity_type = (entity_type or "").lower().strip()
    if entity_type == "deal":
        row = db.query(Deal).filter(Deal.id == entity_id, Deal.deleted_at.isnot(None)).first()
    elif entity_type == "task":
        row = db.query(Task).filter(Task.id == entity_id, Task.deleted_at.isnot(None)).first()
    elif entity_type == "company":
        row = db.query(Company).filter(Company.id == entity_id, Company.deleted_at.isnot(None)).first()
    else:
        return JSONResponse(status_code=400, content={"error": "Неизвестный тип"})
    if not row:
        return JSONResponse(status_code=404, content={"error": "Не найдено в корзине"})
    _restore_entity(row)
    if entity_type == "deal":
        db.add(DealHistory(deal_id=entity_id, action_text="Сделка восстановлена из корзины"))
        # Вернуть задачи сделки, удалённые вместе с ней (по времени ≈ deleted_at сделки)
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        action="restore",
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success", "restored": True}


@app.delete("/api/trash/{entity_type}/{entity_id}")
def api_trash_purge(
    request: Request,
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Удалить навсегда — только admin."""
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Удаление навсегда — только администратор"})
    entity_type = (entity_type or "").lower().strip()
    if entity_type == "task":
        row = db.query(Task).filter(Task.id == entity_id, Task.deleted_at.isnot(None)).first()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Не найдено в корзине"})
        _hard_purge_task(db, entity_id)
    elif entity_type == "deal":
        row = db.query(Deal).filter(Deal.id == entity_id, Deal.deleted_at.isnot(None)).first()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Не найдено в корзине"})
        # Смета = сделка: позиции и история остаются для аудита? Hard purge deal items + deal
        db.query(DealItem).filter(DealItem.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealFieldValue).filter(DealFieldValue.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealHistory).filter(DealHistory.deal_id == entity_id).delete(synchronize_session=False)
        db.query(Activity).filter(Activity.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealAttachment).filter(DealAttachment.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealDocument).filter(DealDocument.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealAdvance).filter(DealAdvance.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealExpense).filter(DealExpense.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealPayrollLine).filter(DealPayrollLine.deal_id == entity_id).delete(synchronize_session=False)
        db.query(DealStaffAssignment).filter(DealStaffAssignment.deal_id == entity_id).delete(synchronize_session=False)
        for t in db.query(Task).filter(Task.deal_id == entity_id).all():
            _hard_purge_task(db, t.id)
        db.query(Project2D).filter(Project2D.deal_id == entity_id).delete(synchronize_session=False)
        db.query(PushSubscription).filter(PushSubscription.deal_id == entity_id).delete(synchronize_session=False)
        db.query(Invoice).filter(Invoice.deal_id == entity_id).update(
            {Invoice.deal_id: None}, synchronize_session=False
        )
        db.query(Deal).filter(Deal.prev_deal_id == entity_id).update(
            {Deal.prev_deal_id: None}, synchronize_session=False
        )
        db.delete(row)
    elif entity_type == "company":
        row = db.query(Company).filter(Company.id == entity_id, Company.deleted_at.isnot(None)).first()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Не найдено в корзине"})
        if _not_deleted(db.query(Deal), Deal).filter(Deal.company_id == entity_id).first():
            return JSONResponse(
                status_code=400,
                content={"error": "Сначала удалите или переместите связанные сделки"},
            )
        db.delete(row)
    else:
        return JSONResponse(status_code=400, content={"error": "Неизвестный тип"})
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        action="purge",
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success", "purged": True}


@app.get("/assistant", response_class=HTMLResponse)
async def read_assistant(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("assistant.html", {"request": request, "active_page": "assistant"})

@app.get("/equipment", response_class=HTMLResponse)
async def read_equipment(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("equipment.html", {"request": request, "active_page": "equipment"})

@app.get("/crm", response_class=HTMLResponse)
async def read_crm(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        "crm.html",
        {
            "request": request,
            "active_page": "crm",
            "can_access_documents": auth.user_can_access(user, "documents"),
            "hide_prices": _user_hide_prices(user),
        },
    )


@app.get("/documents", response_class=HTMLResponse)
async def read_documents(request: Request, user: User = Depends(get_current_user)):
    if not auth.user_can_access(user, "documents"):
        return HTMLResponse("Нет доступа к разделу «Документы»", status_code=403)
    can_edit = bool(
        user
        and (
            user.role == "admin"
            or auth.user_can_access(user, "settings")
            or auth.user_can_access(user, "documents")
        )
    )
    return templates.TemplateResponse(
        "documents.html",
        {
            "request": request,
            "active_page": "documents",
            "can_edit_templates": can_edit,
        },
    )


@app.post("/api/admin/restore-demo-deals")
def api_restore_demo_deals(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Восстановить недостающие DEMO-сделки без удаления пользовательских данных."""
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор"})
    try:
        result = demo_seed.seed_demo_deals(db, only_if_empty=False)
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": str(e)})
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="deal",
        entity_id=0,
        action="restore_demo_deals",
        diff=result,
        ip=audit.request_ip(request),
    )
    db.commit()
    return result


@app.get("/api/documents")
def api_list_documents(
    deal_id: Optional[int] = None,
    doc_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_available: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Реестр документов: сохранённые DealDocument + файлы uploads +
    (опционально) доступные к генерации типы по каждой сделке.
    """
    q = db.query(DealDocument).order_by(DealDocument.created_at.desc(), DealDocument.id.desc())
    if deal_id:
        q = q.filter(DealDocument.deal_id == deal_id)
    if doc_type:
        q = q.filter(DealDocument.doc_type == doc_type)
    if date_from:
        try:
            q = q.filter(DealDocument.created_at >= datetime.strptime(date_from[:10], "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.filter(
                DealDocument.created_at
                < datetime.strptime(date_to[:10], "%Y-%m-%d") + timedelta(days=1)
            )
        except ValueError:
            pass

    deals_cache = {}
    items = []
    seen_keys = set()

    for row in q.limit(500).all():
        deal = deals_cache.get(row.deal_id)
        if deal is None:
            deal = db.query(Deal).filter(Deal.id == row.deal_id).first()
            deals_cache[row.deal_id] = deal
        key = (row.deal_id, row.doc_type, row.filename or "")
        seen_keys.add((row.deal_id, row.doc_type))
        items.append({
            "id": row.id,
            "deal_id": row.deal_id,
            "deal_title": deal.title if deal else f"#{row.deal_id}",
            "company_name": deal.company.name if deal and deal.company else None,
            "doc_type": row.doc_type,
            "doc_label": DOC_TYPE_LABELS.get(row.doc_type, row.doc_type),
            "filename": row.filename,
            "path": row.path,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "created_by": row.created_by,
            "download_url": row.path if (row.path and str(row.path).startswith("/uploads/"))
            else _download_url_for_doc(row.deal_id, row.doc_type),
            "source": "registry",
            "status": "generated",
        })

    # Файлы технички из uploads (если ещё не в реестре)
    for up in _scan_uploads_technichka(db):
        if deal_id and up["deal_id"] != deal_id:
            continue
        if doc_type and up["doc_type"] != doc_type:
            continue
        key = (up["deal_id"], up["doc_type"])
        if key in seen_keys:
            continue
        if date_from and up.get("created_at") and up["created_at"][:10] < date_from[:10]:
            continue
        if date_to and up.get("created_at") and up["created_at"][:10] > date_to[:10]:
            continue
        seen_keys.add(key)
        items.append(up)

    # Доступные к генерации (виртуальные) — по сделкам с позициями / всем активным
    if include_available:
        deals_q = _not_deleted(db.query(Deal), Deal).filter(Deal.is_archived == False)  # noqa: E712
        if deal_id:
            deals_q = deals_q.filter(Deal.id == deal_id)
        deals_q = deals_q.order_by(Deal.id.desc()).limit(80)
        virtual_types = [
            "estimate_internal",
            "estimate_internal_pdf",
            "estimate_client",
            "estimate_client_pdf",
            "estimate_client_priced",
            "estimate_client_priced_pdf",
            "contract",
            "contract_pdf",
            "technichka",
        ]
        for deal in deals_q.all():
            if doc_type and doc_type not in virtual_types:
                break
            for vt in virtual_types:
                if doc_type and vt != doc_type:
                    continue
                if (deal.id, vt) in seen_keys:
                    continue
                # договор только если есть компания
                if vt.startswith("contract") and not deal.company_id:
                    continue
                items.append({
                    "id": None,
                    "deal_id": deal.id,
                    "deal_title": deal.title,
                    "company_name": deal.company.name if deal.company else None,
                    "doc_type": vt,
                    "doc_label": DOC_TYPE_LABELS.get(vt, vt),
                    "filename": None,
                    "path": None,
                    "created_at": None,
                    "created_by": None,
                    "download_url": _download_url_for_doc(deal.id, vt),
                    "source": "available",
                    "status": "available",
                })

    # Сортировка: сначала generated/stored с датой, потом available
    def _sort_key(it):
        status_rank = 0 if it["status"] in ("generated", "stored") else 1
        return (status_rank, it.get("created_at") or "", -(it.get("deal_id") or 0))

    items.sort(key=_sort_key)
    return {
        "items": items[:800],
        "doc_types": [{"key": k, "label": v} for k, v in DOC_TYPE_LABELS.items()],
        "count": len(items),
    }


def _can_edit_doc_templates(user: User) -> bool:
    if not user:
        return False
    if user.role == "admin":
        return True
    return auth.user_can_access(user, "settings") or auth.user_can_access(user, "documents")


class DocumentTemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    show_logo: Optional[bool] = None
    show_company_block: Optional[bool] = None
    custom_title: Optional[str] = None
    body_notes: Optional[str] = None
    footer_notes: Optional[str] = None
    include_sections: Optional[Dict[str, bool]] = None


@app.get("/api/document-templates")
def api_list_document_templates(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Каталог системных шаблонов документов + список плейсхолдеров."""
    try:
        doc_templates.seed_document_templates(db)
    except Exception:
        db.rollback()
    rows = (
        db.query(DocumentTemplate)
        .order_by(DocumentTemplate.id.asc())
        .all()
    )
    # Стабильный порядок по DEFAULT_TEMPLATES
    order = {s["doc_type"]: i for i, s in enumerate(doc_templates.DEFAULT_TEMPLATES)}
    rows = sorted(rows, key=lambda r: order.get(r.doc_type, 99))
    return {
        "items": [doc_templates.template_to_dict(r) for r in rows],
        "placeholders": doc_templates.PLACEHOLDERS,
        "can_edit": _can_edit_doc_templates(user),
        "warning": (
            "Структура таблицы позиций пока фиксирована кодом; "
            "настраиваются шапка, примечания, футер и блоки."
        ),
    }


@app.get("/api/document-templates/{doc_type}")
def api_get_document_template(
    doc_type: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        doc_templates.seed_document_templates(db)
    except Exception:
        db.rollback()
    row = doc_templates.get_template_row(db, doc_type)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Шаблон не найден"})
    return doc_templates.template_to_dict(row)


@app.put("/api/document-templates/{doc_type}")
def api_update_document_template(
    doc_type: str,
    payload: DocumentTemplateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not _can_edit_doc_templates(user):
        return JSONResponse(status_code=403, content={"error": "Нет прав на изменение шаблонов"})
    try:
        doc_templates.seed_document_templates(db)
    except Exception:
        db.rollback()
    row = doc_templates.get_template_row(db, doc_type)
    if not row:
        return JSONResponse(status_code=404, content={"error": "Шаблон не найден"})

    data = payload.dict(exclude_unset=True)
    for key in (
        "name", "description", "is_active", "show_logo", "show_company_block",
        "custom_title", "body_notes", "footer_notes",
    ):
        if key in data:
            setattr(row, key, data[key])
    if "include_sections" in data and isinstance(data["include_sections"], dict):
        current = dict(row.include_sections or {})
        for sk, sv in data["include_sections"].items():
            if sk in ("items_table", "totals", "signature", "company_contacts"):
                current[sk] = bool(sv)
        row.include_sections = current
    row.updated_by = user.username if user else None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    audit.write_audit(
        db,
        user_id=user.id if user else None,
        entity_type="document_template",
        entity_id=row.id,
        action="update",
        diff={"doc_type": row.doc_type, **{k: data[k] for k in data}},
        ip=audit.request_ip(request),
    )
    db.commit()
    return doc_templates.template_to_dict(row)


class DocumentTemplatePreviewBody(BaseModel):
    custom_title: Optional[str] = None
    body_notes: Optional[str] = None
    footer_notes: Optional[str] = None
    show_logo: Optional[bool] = None
    show_company_block: Optional[bool] = None
    include_sections: Optional[Dict[str, bool]] = None
    deal_id: Optional[int] = None
    context_overrides: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None


def _template_preview_overrides(payload: Optional[DocumentTemplatePreviewBody]) -> Dict[str, Any]:
    """Только настройки шаблона (title/notes/флаги) — без deal_id / context / items."""
    if not payload:
        return {}
    data = payload.dict(exclude_unset=True)
    for k in ("deal_id", "context_overrides", "items"):
        data.pop(k, None)
    return data


def _doc_type_to_estimate_mode(doc_type: str) -> Optional[str]:
    base = (doc_type or "").replace("_pdf", "")
    return {
        "estimate_internal": "internal",
        "estimate_client": "client",
        "estimate_client_priced": "client_priced",
    }.get(base)


def _user_can_access_doc_type(user: User, doc_type: str) -> bool:
    """hide_prices: только техничка и клиентская смета без цен."""
    if user.role == "admin" or not _user_hide_prices(user):
        return True
    base = (doc_type or "").replace("_pdf", "")
    return base in ("estimate_client", "technichka")


def _build_deal_document_context(
    db: Session,
    deal_id: int,
    doc_type: str,
    user: Optional[User] = None,
) -> Tuple[Optional[Tuple[Dict[str, Any], str, Deal]], Optional[JSONResponse]]:
    """Контекст документа по сделке. (context, base, deal) или ошибка."""
    base = (doc_type or "").replace("_pdf", "")
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return None, JSONResponse(status_code=404, content={"error": "Сделка не найдена"})
    if user is not None and not _user_can_access_doc_type(user, base):
        return None, JSONResponse(
            status_code=403,
            content={"error": "Нет доступа к этому типу документа (скрыты цены)"},
        )

    mode = _doc_type_to_estimate_mode(base)
    if mode:
        context = _build_estimate_context(d, mode, db=db)
        return (context, base, d), None
    if base == "technichka":
        context = doc_templates.apply_template_to_context(
            _build_technichka_context(d, db=db), db, "technichka"
        )
        return (context, base, d), None
    if base == "contract":
        comp = d.company
        if not comp:
            return None, JSONResponse(
                status_code=400, content={"error": "Для договора выберите клиента"}
            )
        result = _calc_deal(d, exclude_subrental=True)
        header = _estimate_header_fields(d)
        letterhead = _get_company_letterhead(db)
        logo_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static", "img", "introshow_logo.png",
        )
        context = doc_templates.apply_template_to_context({
            "contract_number": f"CRM-{d.id}",
            "contract_date": datetime.today().strftime("%d.%m.%Y"),
            "number": f"CRM-{d.id}",
            "date": datetime.today().strftime("%d.%m.%Y"),
            **header,
            "company_name": comp.name,
            "director_name": comp.director_name,
            "iin_bin": comp.bin,
            "iban": comp.requisites,
            "event_name": d.title,
            "event_date": d.event_date,
            "event_address": d.event_address,
            "our_company_name": letterhead.get("company_name") or "Intro Show",
            "our_company_phone": letterhead.get("company_phone") or "",
            "our_company_email": letterhead.get("company_email") or "",
            "our_company_address": letterhead.get("company_address") or "",
            "our_company_bin": letterhead.get("company_bin") or "",
            "logo_path": logo_path if os.path.isfile(logo_path) else None,
            "manager_phone": letterhead.get("company_phone") or "",
            "items": result["items"],
            "equipment_total": result["equipment_total"],
            "fixed_total": result["fixed_total"],
            "grand_total": result["grand_total"],
            "discount_percentage": d.discount_percentage or 0,
            "tax_percentage": result.get("tax_percentage", FIXED_TAX_PERCENTAGE),
            "tax_amount": result.get("tax_amount", 0),
        }, db, "contract")
        return (context, base, d), None
    return None, JSONResponse(status_code=404, content={"error": "Неизвестный тип документа"})


def _apply_preview_payload(
    context: Dict[str, Any],
    db: Session,
    base: str,
    payload: Optional[DocumentTemplatePreviewBody],
) -> Dict[str, Any]:
    """Шаблонные overrides + context_overrides/items → готовый context."""
    tpl_overrides = _template_preview_overrides(payload)
    ctx = doc_templates.apply_template_to_context(context, db, base, overrides=tpl_overrides)
    if payload:
        data = payload.dict(exclude_unset=True)
        ctx = doc_templates.apply_preview_data_overrides(
            ctx,
            context_overrides=data.get("context_overrides"),
            items=data.get("items"),
        )
        # повторно применить notes/title если пришли вместе с data overrides
        if any(k in data for k in ("custom_title", "body_notes", "footer_notes")):
            ctx = doc_templates.apply_template_to_context(
                ctx, db, base, overrides=_template_preview_overrides(payload)
            )
    return ctx


def _build_sample_template_context(
    db: Session,
    doc_type: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Tuple[Dict[str, Any], str]], Optional[JSONResponse]]:
    """Готовит context + base doc_type для sample/preview. (context, base) или (None, error_response)."""
    base = (doc_type or "").replace("_pdf", "")
    try:
        doc_templates.seed_document_templates(db)
    except Exception:
        db.rollback()
    row = doc_templates.get_template_row(db, base)
    if not row:
        return None, JSONResponse(status_code=404, content={"error": "Шаблон не найден"})

    context = doc_templates.apply_template_to_context(
        doc_templates.sample_context(base), db, base, overrides=overrides
    )
    logo_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static", "img", "introshow_logo.png",
    )
    if os.path.isfile(logo_path) and context.get("tpl_show_logo", True):
        context["logo_path"] = logo_path
    return (context, base), None


def _resolve_preview_context(
    db: Session,
    doc_type: str,
    payload: Optional[DocumentTemplatePreviewBody] = None,
    user: Optional[User] = None,
) -> Tuple[Optional[Tuple[Dict[str, Any], str]], Optional[JSONResponse]]:
    """Sample или deal-контекст + сессионные правки превью."""
    base = (doc_type or "").replace("_pdf", "")
    deal_id = payload.deal_id if payload else None
    if deal_id:
        built, err = _build_deal_document_context(db, deal_id, base, user=user)
        if err:
            return None, err
        context, base, _deal = built
        context = _apply_preview_payload(context, db, base, payload)
        return (context, base), None

    tpl_overrides = _template_preview_overrides(payload)
    result, err = _build_sample_template_context(db, base, overrides=tpl_overrides)
    if err:
        return None, err
    context, base = result
    if payload:
        data = payload.dict(exclude_unset=True)
        context = doc_templates.apply_preview_data_overrides(
            context,
            context_overrides=data.get("context_overrides"),
            items=data.get("items"),
        )
        if any(k in data for k in ("custom_title", "body_notes", "footer_notes")):
            context = doc_templates.apply_template_to_context(
                context, db, base, overrides=tpl_overrides
            )
    return (context, base), None


def _render_sample_document_file(context: Dict[str, Any], base: str, fmt: str) -> str:
    """Генерирует sample docx/pdf во временный файл, возвращает путь."""
    return _render_preview_document_file(context, base, fmt, db=None)


def _render_preview_document_file(
    context: Dict[str, Any],
    base: str,
    fmt: str,
    db: Session = None,
) -> str:
    """Генерирует docx/pdf превью (sample или сделка) во временный файл."""
    from document_generator import (
        generate_estimate_docx,
        generate_estimate_pdf,
        generate_technichka_docx,
        generate_technichka_pdf,
        generate_contract_pdf,
        generate_contract,
    )

    fmt = (fmt or "docx").lower().strip()
    if fmt not in ("docx", "pdf"):
        fmt = "docx"
    suffix = ".pdf" if fmt == "pdf" else ".docx"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    # Синхронизация notes для Word-шаблона договора
    context = dict(context)
    context["body_notes"] = context.get("tpl_body_notes") or context.get("body_notes") or ""
    context["footer_notes"] = context.get("tpl_footer_notes") or context.get("footer_notes") or ""
    context["custom_title"] = context.get("tpl_custom_title") or context.get("custom_title") or ""

    if base == "technichka":
        if fmt == "pdf":
            generate_technichka_pdf(context, temp_path)
        else:
            generate_technichka_docx(context, temp_path)
    elif base == "contract":
        if fmt == "pdf":
            generate_contract_pdf(context, temp_path)
        else:
            template_path = CONTRACT_TEMPLATE_PATH
            if template_path and os.path.isfile(template_path):
                try:
                    generate_contract(context, template_path, temp_path)
                except Exception:
                    generate_estimate_docx(context, temp_path, mode="client_priced")
            else:
                generate_estimate_docx(context, temp_path, mode="client_priced")
    elif base == "estimate_client":
        if fmt == "pdf":
            generate_estimate_pdf(context, temp_path, mode="client")
        else:
            generate_estimate_docx(context, temp_path, mode="client")
    elif base == "estimate_client_priced":
        if fmt == "pdf":
            generate_estimate_pdf(context, temp_path, mode="client_priced")
        else:
            generate_estimate_docx(context, temp_path, mode="client_priced")
    else:
        if fmt == "pdf":
            generate_estimate_pdf(context, temp_path, mode="internal")
        else:
            generate_estimate_docx(context, temp_path, mode="internal")
    return temp_path


def _file_response_with_cleanup(
    background_tasks: BackgroundTasks,
    temp_path: str,
    media: str,
    fname: str,
    inline: bool = False,
):
    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    return FileResponse(
        temp_path,
        media_type=media,
        filename=fname,
        content_disposition_type="inline" if inline else "attachment",
    )


@app.get("/api/document-templates/{doc_type}/sample")
def api_sample_document_template(
    doc_type: str,
    background_tasks: BackgroundTasks,
    format: str = "docx",
    inline: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Образец документа по шаблону (демо-данные)."""
    result, err = _build_sample_template_context(db, doc_type)
    if err:
        return err
    context, base = result
    fmt = (format or "docx").lower().strip()
    if fmt not in ("docx", "pdf"):
        fmt = "docx"
    temp_path = _render_sample_document_file(context, base, fmt)
    suffix = ".pdf" if fmt == "pdf" else ".docx"
    fname = f"Sample_{base}{suffix}"
    media = (
        "application/pdf"
        if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return _file_response_with_cleanup(
        background_tasks, temp_path, media, fname, inline=bool(inline) and fmt == "pdf"
    )


@app.get("/api/document-templates/{doc_type}/preview.pdf")
def api_preview_document_template_pdf(
    doc_type: str,
    background_tasks: BackgroundTasks,
    inline: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """PDF-превью шаблона для встраивания в CRM (Content-Disposition: inline)."""
    result, err = _build_sample_template_context(db, doc_type)
    if err:
        return err
    context, base = result
    temp_path = _render_sample_document_file(context, base, "pdf")
    return _file_response_with_cleanup(
        background_tasks,
        temp_path,
        "application/pdf",
        f"Preview_{base}.pdf",
        inline=bool(inline),
    )


@app.get("/api/document-templates/{doc_type}/preview.html", response_class=HTMLResponse)
def api_preview_document_template_html(
    doc_type: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTML-превью шаблона (структурированное) для in-app просмотра."""
    result, err = _build_sample_template_context(db, doc_type)
    if err:
        return err
    context, base = result
    return HTMLResponse(doc_templates.build_html_preview(context, base))


@app.post("/api/document-templates/{doc_type}/preview.html", response_class=HTMLResponse)
def api_preview_document_template_html_post(
    doc_type: str,
    payload: DocumentTemplatePreviewBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """HTML-превью с сессионными правками (шапка, позиции, notes)."""
    result, err = _resolve_preview_context(db, doc_type, payload=payload, user=user)
    if err:
        return err
    context, base = result
    return HTMLResponse(doc_templates.build_html_preview(context, base))


@app.post("/api/document-templates/{doc_type}/preview.pdf")
def api_preview_document_template_pdf_post(
    doc_type: str,
    payload: DocumentTemplatePreviewBody,
    background_tasks: BackgroundTasks,
    inline: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """PDF-превью с сессионными правками."""
    result, err = _resolve_preview_context(db, doc_type, payload=payload, user=user)
    if err:
        return err
    context, base = result
    temp_path = _render_preview_document_file(context, base, "pdf", db=db)
    deal_suffix = f"_CRM-{payload.deal_id}" if payload and payload.deal_id else ""
    return _file_response_with_cleanup(
        background_tasks,
        temp_path,
        "application/pdf",
        f"Preview_{base}{deal_suffix}.pdf",
        inline=bool(inline),
    )


@app.post("/api/document-templates/{doc_type}/preview.docx")
def api_preview_document_template_docx_post(
    doc_type: str,
    payload: DocumentTemplatePreviewBody,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Скачать Word с сессионными правками превью."""
    result, err = _resolve_preview_context(db, doc_type, payload=payload, user=user)
    if err:
        return err
    context, base = result
    temp_path = _render_preview_document_file(context, base, "docx", db=db)
    deal_suffix = f"_CRM-{payload.deal_id}" if payload and payload.deal_id else ""
    return _file_response_with_cleanup(
        background_tasks,
        temp_path,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{'Deal' if payload and payload.deal_id else 'Sample'}_{base}{deal_suffix}.docx",
        inline=False,
    )


@app.get("/api/document-templates/{doc_type}/preview-meta")
def api_preview_document_template_meta(
    doc_type: str,
    deal_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Метаданные для формы превью/редактирования (сырые notes + ссылки)."""
    payload = DocumentTemplatePreviewBody(deal_id=deal_id) if deal_id else None
    result, err = _resolve_preview_context(db, doc_type, payload=payload, user=user)
    if err:
        return err
    context, base = result
    row = doc_templates.get_template_row(db, base)
    items_out = []
    for it in context.get("items") or []:
        items_out.append({
            "name": it.get("name") or "",
            "quantity": it.get("quantity"),
            "days": it.get("days"),
            "price": it.get("price"),
            "line_total": it.get("line_total_discounted", it.get("line_total_base")),
        })
    return {
        "doc_type": base,
        "name": (row.name if row else base) + (f" · CRM-{deal_id}" if deal_id else ""),
        "deal_id": deal_id,
        "custom_title": context.get("tpl_custom_title_raw") or context.get("tpl_custom_title") or "",
        "body_notes": context.get("tpl_body_notes_raw") or context.get("tpl_body_notes") or "",
        "footer_notes": context.get("tpl_footer_notes_raw") or context.get("tpl_footer_notes") or "",
        "context": {
            "event_name": context.get("event_name") or context.get("project_name") or "",
            "company_name": context.get("company_name") or "",
            "manager_name": context.get("manager_name") or "",
            "city": context.get("city") or "",
            "event_address": context.get("event_address") or "",
            "event_date": context.get("event_date") or "",
            "departure_date": context.get("departure_date") or "",
            "return_date": context.get("return_date") or "",
            "shifts_label": context.get("shifts_label") or str(context.get("shifts") or ""),
            "grand_total": context.get("grand_total"),
        },
        "items": items_out,
        "preview_html_url": f"/api/document-templates/{base}/preview.html",
        "preview_pdf_url": f"/api/document-templates/{base}/preview.pdf?inline=1",
        "sample_docx_url": f"/api/document-templates/{base}/sample?format=docx",
        "sample_pdf_url": f"/api/document-templates/{base}/sample?format=pdf",
    }


@app.get("/api/deals/{deal_id}/document-registry")
def api_deal_document_registry(
    deal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Реестр документов сделки: доступные шаблоны + статус генерации."""
    if not auth.user_can_access(user, "documents"):
        return JSONResponse(status_code=403, content={"error": "Нет доступа к документам"})
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Сделка не найдена"})

    existing = (
        db.query(DealDocument)
        .filter(DealDocument.deal_id == deal_id, DealDocument.deleted_at.is_(None))
        .order_by(DealDocument.created_at.desc())
        .all()
    )
    by_type = {}
    for row in existing:
        key = (row.doc_type or "").replace("_pdf", "")
        if key not in by_type:
            by_type[key] = row

    catalog = [
        ("estimate_internal", "Для нас (внутренняя)", True),
        ("estimate_client_priced", "Клиенту с ценами", True),
        ("estimate_client", "Клиенту без цен", True),
        ("contract", "Договор", bool(d.company_id)),
        ("technichka", "Техничка", True),
    ]
    items = []
    for doc_type, label, available in catalog:
        if not _user_can_access_doc_type(user, doc_type):
            continue
        row = by_type.get(doc_type)
        items.append({
            "doc_type": doc_type,
            "label": label,
            "available": available,
            "reason": None if available else "Нужен выбранный клиент",
            "generated": bool(row),
            "last_filename": row.filename if row else None,
            "last_created_at": row.created_at.isoformat() if row and row.created_at else None,
            "preview_meta_url": f"/api/document-templates/{doc_type}/preview-meta?deal_id={deal_id}",
        })
    return {
        "deal_id": deal_id,
        "deal_title": d.title or f"CRM-{deal_id}",
        "full_registry_url": f"/documents?deal_id={deal_id}",
        "items": items,
    }


@app.get("/tracking/{deal_id}", response_class=HTMLResponse)
def read_tracking(request: Request, deal_id: int, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        return HTMLResponse("Заказ не найден", status_code=404)
    return templates.TemplateResponse("tracking.html", {"request": request, "deal": deal})

# -----------------
# API ROUTES
# -----------------

# -- Cities (multi-city foundation) --
class CityCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    is_active: Optional[bool] = True
    timezone: Optional[str] = "Asia/Almaty"
    sort_order: Optional[int] = 100


class CityUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    is_active: Optional[bool] = None
    timezone: Optional[str] = None
    sort_order: Optional[int] = None


class CitySwitchIn(BaseModel):
    city_id: int


def _slugify_city(name: str) -> str:
    raw = (name or "").strip().lower()
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        " ": "-", "_": "-",
    })
    s = raw.translate(table)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "city"


@app.get("/api/cities")
def api_list_cities(
    request: Request,
    all: Optional[bool] = False,
    scope: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """scope=deal|all_active — все активные города (карточка сделки / смета).
    all=1 — admin: включая неактивные (настройки).
    иначе — города, доступные пользователю для переключателя."""
    include_inactive = bool(all) and user.role == "admin"
    if include_inactive:
        cities = db.query(City).order_by(City.sort_order, City.id).all()
    elif scope in ("deal", "all_active"):
        cities = db.query(City).filter(City.is_active == True).order_by(City.sort_order, City.id).all()  # noqa: E712
    else:
        cities = _cities_for_user(db, user, include_inactive=False)
    current = _resolve_active_city(db, user, request)
    switch_cities = _cities_for_user(db, user, include_inactive=False)
    return {
        "cities": [_city_to_dict(c) for c in cities],
        "current_city_id": current.id if current else None,
        "current_city_name": current.name if current else "Алматы",
        "can_switch": len(switch_cities) > 1 or user.role in ("admin", "manager"),
    }


@app.post("/api/cities")
def api_create_city(
    payload: CityCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор"})
    name = (payload.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название города"})
    slug = (payload.slug or "").strip().lower() or _slugify_city(name)
    if db.query(City).filter(City.slug == slug).first():
        return JSONResponse(status_code=400, content={"error": "Город с таким slug уже есть"})
    city = City(
        name=name,
        slug=slug,
        is_active=True if payload.is_active is None else bool(payload.is_active),
        timezone=(payload.timezone or "Asia/Almaty").strip() or "Asia/Almaty",
        sort_order=int(payload.sort_order if payload.sort_order is not None else 100),
    )
    db.add(city)
    db.commit()
    db.refresh(city)
    audit.write_audit(
        db, user_id=user.id, entity_type="city", entity_id=city.id,
        action="create", diff=_city_to_dict(city), ip=audit.request_ip(request), commit=True,
    )
    return _city_to_dict(city)


@app.put("/api/cities/{city_id}")
def api_update_city(
    city_id: int,
    payload: CityUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Только администратор"})
    city = db.query(City).filter(City.id == city_id).first()
    if not city:
        return JSONResponse(status_code=404, content={"error": "Город не найден"})
    data = payload.dict(exclude_unset=True)
    if "name" in data:
        data["name"] = (data["name"] or "").strip()
        if not data["name"]:
            return JSONResponse(status_code=400, content={"error": "Пустое название"})
    if "slug" in data and data["slug"]:
        data["slug"] = str(data["slug"]).strip().lower()
        clash = db.query(City).filter(City.slug == data["slug"], City.id != city_id).first()
        if clash:
            return JSONResponse(status_code=400, content={"error": "Slug занят"})
    changed = {}
    for k, v in data.items():
        old = getattr(city, k, None)
        if old != v:
            changed[k] = {"from": old, "to": v}
        setattr(city, k, v)
    if changed:
        audit.write_audit(
            db, user_id=user.id, entity_type="city", entity_id=city.id,
            action="update", diff=changed, ip=audit.request_ip(request),
        )
    db.commit()
    db.refresh(city)
    return _city_to_dict(city)


@app.post("/api/cities/switch")
def api_switch_city(
    payload: CitySwitchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    allowed = _cities_for_user(db, user, include_inactive=False)
    if user.role == "admin":
        allowed = db.query(City).filter(City.is_active == True).order_by(City.sort_order, City.id).all()  # noqa: E712
    city = next((c for c in allowed if c.id == payload.city_id), None)
    if not city:
        return JSONResponse(status_code=403, content={"error": "Нет доступа к этому городу"})
    # Для admin — запоминаем как настройку организации
    if user.role == "admin":
        row = db.query(AppSetting).filter(AppSetting.key == "active_city_id").first()
        if row:
            row.value = str(city.id)
        else:
            db.add(AppSetting(key="active_city_id", value=str(city.id)))
        db.commit()
    resp = JSONResponse(content={
        "status": "ok",
        "current_city_id": city.id,
        "current_city_name": city.name,
    })
    resp.set_cookie(
        ACTIVE_CITY_COOKIE,
        str(city.id),
        max_age=60 * 60 * 24 * 180,
        httponly=False,
        samesite="lax",
        secure=bool(IS_VERCEL) or request.url.scheme == "https",
    )
    return resp


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
def get_equipment(
    warehouse_type: Optional[str] = None,
    condition: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Equipment)
    if warehouse_type in ("own", "subrental"):
        q = q.filter(Equipment.warehouse_type == warehouse_type)
    if condition in EQUIPMENT_CONDITIONS:
        q = q.filter(Equipment.condition == condition)
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
            "condition": _normalize_equipment_condition(getattr(eq, "condition", None)),
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


class CompanyLetterheadUpdate(BaseModel):
    company_name: Optional[str] = None
    company_phone: Optional[str] = None
    company_email: Optional[str] = None
    company_address: Optional[str] = None
    company_bin: Optional[str] = None


@app.get("/api/settings/company")
def get_company_letterhead(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Реквизиты компании для шапки сметы (DOCX/PDF)."""
    return _get_company_letterhead(db)


@app.put("/api/settings/company")
def update_company_letterhead(
    payload: CompanyLetterheadUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Нужны права администратора или менеджера"})
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        if key not in ("company_name", "company_phone", "company_email", "company_address", "company_bin"):
            continue
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = (value or "").strip()
        else:
            db.add(AppSetting(key=key, value=(value or "").strip()))
    db.commit()
    return _get_company_letterhead(db)

@app.post("/api/equipment")
def create_equipment(item: EquipmentCreate, db: Session = Depends(get_db)):
    data = item.dict()
    wtype = (data.get("warehouse_type") or "own").strip().lower()
    data["warehouse_type"] = "subrental" if wtype == "subrental" else "own"
    data["cost_price"] = float(data.get("cost_price") or 0)
    data["supplier"] = (data.get("supplier") or None) or None
    data["condition"] = _normalize_equipment_condition(data.get("condition"))
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
        condition=_normalize_equipment_condition(getattr(src, "condition", None)),
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
        data["condition"] = _normalize_equipment_condition(data.get("condition"))
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

    raw_name = (file.filename or "").strip()
    ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else ""
    if ext not in ("jpg", "jpeg", "png", "webp", "gif", "heic", "heif"):
        ctype = (file.content_type or "").lower()
        if "png" in ctype:
            ext = "png"
        elif "webp" in ctype:
            ext = "webp"
        elif "gif" in ctype:
            ext = "gif"
        else:
            ext = "jpg"
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
    return _not_deleted(db.query(Company), Company).all()

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
    
    deals = (
        _not_deleted(db.query(Deal), Deal)
        .filter(Deal.company_id == company_id)
        .order_by(Deal.id.desc())
        .all()
    )
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
def delete_company(
    request: Request,
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db_comp = _not_deleted(db.query(Company), Company).filter(Company.id == company_id).first()
    if not db_comp:
        return JSONResponse(status_code=404, content={"error": "Company not found"})

    active_deals = (
        _not_deleted(db.query(Deal), Deal)
        .filter(Deal.company_id == company_id)
        .count()
    )
    if active_deals:
        return JSONResponse(
            status_code=400,
            content={"error": "Cannot delete company with active deals"},
        )

    _soft_delete_entity(db_comp, user)
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="company",
        entity_id=company_id,
        action="soft_delete",
        diff={"name": db_comp.name},
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success", "trashed": True}

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


# -- WhatsApp Web bridge + WAHA proxies --
def _waha_url() -> str:
    return os.getenv("WAHA_URL", "http://127.0.0.1:3000").rstrip("/")


def _wa_bridge_url() -> str:
    return os.getenv("WA_BRIDGE_URL", "").rstrip("/")


def _wa_bridge_headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.getenv("WA_WEB_API_KEY", "")
    if key:
        h["X-API-Key"] = key
    return h


def _wa_web_key_ok(request: Request) -> bool:
    """Проверка X-API-Key / Bearer для webhook от bridge. Пустой ключ — только если явно разрешено."""
    expected = os.getenv("WA_WEB_API_KEY", "")
    if not expected:
        # На проде ключ обязателен; без ключа принимаем только если WA_WEB_ALLOW_OPEN=1 (локальная отладка)
        return os.getenv("WA_WEB_ALLOW_OPEN", "") == "1"
    key = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    return key == expected


def _bridge_get(path: str, timeout: float = 5):
    base = _wa_bridge_url()
    if not base:
        return None
    return requests.get(f"{base}{path}", headers=_wa_bridge_headers(), timeout=timeout)


def _bridge_post(path: str, json_body: dict = None, timeout: float = 10):
    base = _wa_bridge_url()
    if not base:
        return None
    return requests.post(
        f"{base}{path}",
        headers=_wa_bridge_headers(),
        json=json_body or {},
        timeout=timeout,
    )


@app.get("/api/wa-web/status")
def wa_web_status(user: User = Depends(get_current_user)):
    """Статус WhatsApp Web bridge (VPS). Для UI «Настройки»."""
    bridge = _wa_bridge_url()
    if not bridge:
        return {
            "status": "offline",
            "label": "Мост не настроен",
            "hint": "Задайте WA_BRIDGE_URL (VPS) и WA_WEB_API_KEY в .env CRM. См. docs/whatsapp-web.md",
            "bridge_url": None,
        }
    try:
        r = _bridge_get("/status", timeout=4)
        if r is None:
            return {"status": "offline", "label": "Мост не настроен", "bridge_url": None}
        if r.status_code == 401:
            return {"status": "offline", "label": "Неверный API-ключ", "bridge_url": bridge}
        data = r.json() if r.ok else {}
        raw = (data.get("status") or "disconnected").lower()
        label_map = {
            "connected": "Подключено",
            "wait_qr": "Ожидание QR",
            "initializing": "Запуск…",
            "disconnected": "Не подключено",
            "auth_failure": "Ошибка авторизации",
        }
        return {
            "status": raw,
            "label": label_map.get(raw, raw),
            "phone": data.get("phone"),
            "has_qr": data.get("has_qr"),
            "last_error": data.get("last_error"),
            "bridge_url": bridge,
        }
    except Exception as e:
        return {
            "status": "offline",
            "label": "Мост недоступен",
            "error": str(e),
            "bridge_url": bridge,
            "hint": "Проверьте, что docker compose wa-bridge запущен на VPS и порт открыт для CRM.",
        }


@app.post("/api/wa-web/connect")
def wa_web_connect(user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not _wa_bridge_url():
        return JSONResponse(
            status_code=400,
            content={"error": "WA_BRIDGE_URL не задан. Запустите bridge на VPS (docs/whatsapp-web.md)."},
        )
    try:
        r = _bridge_post("/connect", timeout=8)
        if r is None:
            return JSONResponse(status_code=400, content={"error": "Мост не настроен"})
        if not r.ok:
            return JSONResponse(status_code=502, content={"error": r.text[:300]})
        return r.json()
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.post("/api/wa-web/logout")
def wa_web_logout(user: User = Depends(get_current_user)):
    if user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Access denied"})
    if not _wa_bridge_url():
        return JSONResponse(status_code=400, content={"error": "WA_BRIDGE_URL не задан"})
    try:
        r = _bridge_post("/logout", timeout=15)
        if r is None:
            return JSONResponse(status_code=400, content={"error": "Мост не настроен"})
        return r.json() if r.ok else JSONResponse(status_code=502, content={"error": r.text[:300]})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/wa-web/qr")
def wa_web_qr(user: User = Depends(get_current_user)):
    """Проксирует PNG QR с bridge для сканирования WhatsApp → Привязанные устройства."""
    if not _wa_bridge_url():
        return JSONResponse(status_code=400, content={"error": "WA_BRIDGE_URL не задан"})
    try:
        r = _bridge_get("/qr", timeout=5)
        if r is None:
            return JSONResponse(status_code=400, content={"error": "Мост не настроен"})
        if r.status_code == 404:
            return JSONResponse(status_code=404, content={"error": "QR ещё не готов или уже отсканирован"})
        if not r.ok:
            return JSONResponse(status_code=502, content={"error": r.text[:300]})
        return Response(content=r.content, media_type="image/png", headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@app.get("/api/wa/status")
def wa_status():
    """Статус WAHA (альтернатива). Основной путь — /api/wa-web/status."""
    try:
        r = requests.get(f"{_waha_url()}/api/sessions/default", timeout=2)
        if r.status_code == 200:
            return r.json()
        return {"status": "NOT_FOUND"}
    except Exception:
        return {"status": "OFFLINE", "error": "WAHA is not running"}


@app.post("/api/wa/start")
def wa_start():
    try:
        r = requests.post(f"{_waha_url()}/api/sessions/start", json={"name": "default"}, timeout=5)
        return {"status": "started"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/wa/qr")
def wa_qr():
    try:
        r = requests.get(f"{_waha_url()}/api/sessions/default/auth/qr?format=raw", timeout=2)
        if r.status_code == 200:
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
        clean_phone = "".join(filter(str.isdigit, msg.phone))
        if clean_phone.startswith("8"):
            clean_phone = "7" + clean_phone[1:]
        chat_id = f"{clean_phone}@c.us"
        # Предпочитаем bridge
        if _wa_bridge_url():
            try:
                r = _bridge_post("/send", {"chat_id": chat_id, "text": msg.text}, timeout=15)
                if r is not None and r.ok:
                    return r.json()
            except Exception:
                pass
        payload = {
            "session": "default",
            "chatId": chat_id,
            "text": msg.text,
        }
        r = requests.post(f"{_waha_url()}/api/sendText", json=payload, timeout=5)
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
    name: Optional[str] = None
    kind: Optional[str] = None
    target_pipeline_id: Optional[int] = None


class StageUpdate(BaseModel):
    name: Optional[str] = None
    order_index: Optional[int] = None
    is_active_rent: Optional[bool] = None
    is_won: Optional[bool] = None
    is_lost: Optional[bool] = None
    creates_deal: Optional[bool] = None


class StagesReorder(BaseModel):
    stage_ids: List[int]


class RoutingRuleIn(BaseModel):
    source: str
    pipeline_id: int
    assignee_id: Optional[int] = None
    is_active: Optional[bool] = True


class RoutingRulesBulk(BaseModel):
    rules: List[RoutingRuleIn]


@app.get("/api/pipelines")
def get_pipelines(db: Session = Depends(get_db)):
    pipelines = db.query(Pipeline).order_by(Pipeline.id).all()
    return [_serialize_pipeline(p, include_stages=True) for p in pipelines]


@app.post("/api/pipelines")
def create_pipeline(pl: PipelineCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    kind = (pl.kind or "deal").strip().lower()
    if kind not in ("lead", "deal"):
        kind = "deal"
    name = (pl.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название воронки"})

    target_id = pl.target_pipeline_id
    if kind == "lead" and not target_id:
        deal_pipe = db.query(Pipeline).filter(Pipeline.kind == "deal").order_by(Pipeline.id).first()
        target_id = deal_pipe.id if deal_pipe else None

    db_pl = Pipeline(name=name, kind=kind, target_pipeline_id=target_id if kind == "lead" else None)
    db.add(db_pl)
    db.commit()
    db.refresh(db_pl)

    if kind == "lead":
        defaults = [
            ("Новый лид", False, False, False, False),
            ("В работе", False, False, False, False),
            ("Квалифицирован", False, False, False, False),
            ("Успешно", True, False, True, False),
            ("Отказ", False, True, False, False),
        ]
    else:
        defaults = [
            ("Первичный контакт", False, False, False, False),
            ("Согласование сметы", False, False, False, False),
            ("Договор и счет", False, False, False, False),
            ("Предоплата внесена", False, False, False, True),
            ("Монтаж / Мероприятие", False, False, False, True),
            ("Успешно реализовано", True, False, False, False),
            ("Сделка проиграна", False, True, False, False),
        ]
    for i, (nm, won, lost, creates, rent) in enumerate(defaults):
        db.add(Stage(
            pipeline_id=db_pl.id,
            name=nm,
            order_index=i + 1,
            is_won=won,
            is_lost=lost,
            creates_deal=creates,
            is_active_rent=rent,
        ))
    db.commit()
    db.refresh(db_pl)
    return _serialize_pipeline(db_pl, include_stages=True)


@app.put("/api/pipelines/{pipeline_id}")
def rename_pipeline(pipeline_id: int, pl: PipelineRename, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    pipe = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipe:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if pl.name is not None:
        name = pl.name.strip()
        if not name:
            return JSONResponse(status_code=400, content={"error": "Пустое название"})
        pipe.name = name
    if pl.kind is not None:
        kind = pl.kind.strip().lower()
        if kind in ("lead", "deal"):
            pipe.kind = kind
            if kind != "lead":
                pipe.target_pipeline_id = None
    if pl.target_pipeline_id is not None:
        if pl.target_pipeline_id == 0:
            pipe.target_pipeline_id = None
        elif pl.target_pipeline_id != pipe.id:
            pipe.target_pipeline_id = pl.target_pipeline_id
    db.commit()
    return _serialize_pipeline(pipe, include_stages=True)


@app.delete("/api/pipelines/{pipeline_id}")
def delete_pipeline(pipeline_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pl:
        return JSONResponse(status_code=404, content={"error": "Pipeline not found"})
    if db.query(Deal).filter(Deal.pipeline_id == pipeline_id).first():
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить воронку с активными сделками"})
    if db.query(Pipeline).count() <= 1:
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить единственную воронку"})
    # Снять ссылки маршрутизации / target
    db.query(PipelineRoutingRule).filter(PipelineRoutingRule.pipeline_id == pipeline_id).delete()
    for other in db.query(Pipeline).filter(Pipeline.target_pipeline_id == pipeline_id).all():
        other.target_pipeline_id = None
    db.delete(pl)
    db.commit()
    return {"status": "ok"}


@app.get("/api/pipelines/{pipeline_id}/stages")
def get_stages(pipeline_id: int, db: Session = Depends(get_db)):
    stages = db.query(Stage).filter(Stage.pipeline_id == pipeline_id).order_by(Stage.order_index, Stage.id).all()
    return [_serialize_stage(s) for s in stages]


@app.post("/api/pipelines/{pipeline_id}/stages")
def create_stage(pipeline_id: int, stage: StageCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    pipe = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipe:
        return JSONResponse(status_code=404, content={"error": "Воронка не найдена"})
    name = (stage.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название стадии"})
    max_order = db.query(Stage).filter(Stage.pipeline_id == pipeline_id).count()
    order_index = stage.order_index if stage.order_index is not None else (max_order + 1)
    st = Stage(
        pipeline_id=pipeline_id,
        name=name,
        order_index=order_index,
        is_active_rent=bool(stage.is_active_rent),
        is_won=bool(stage.is_won),
        is_lost=bool(stage.is_lost),
        creates_deal=bool(stage.creates_deal),
    )
    db.add(st)
    db.commit()
    db.refresh(st)
    return _serialize_stage(st)


@app.put("/api/pipelines/{pipeline_id}/stages/reorder")
def reorder_stages(pipeline_id: int, body: StagesReorder, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    stages = {s.id: s for s in db.query(Stage).filter(Stage.pipeline_id == pipeline_id).all()}
    for i, sid in enumerate(body.stage_ids):
        st = stages.get(sid)
        if st:
            st.order_index = i + 1
    db.commit()
    return get_stages(pipeline_id, db)


@app.put("/api/stages/{stage_id}")
def update_stage(stage_id: int, stage_update: StageUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return JSONResponse(status_code=404, content={"error": "Stage not found"})
    if stage_update.name is not None:
        name = stage_update.name.strip()
        if not name:
            return JSONResponse(status_code=400, content={"error": "Пустое название"})
        st.name = name
    if stage_update.order_index is not None:
        st.order_index = stage_update.order_index
    if stage_update.is_active_rent is not None:
        st.is_active_rent = stage_update.is_active_rent
    if stage_update.is_won is not None:
        st.is_won = stage_update.is_won
    if stage_update.is_lost is not None:
        st.is_lost = stage_update.is_lost
    if stage_update.creates_deal is not None:
        st.creates_deal = stage_update.creates_deal
    db.commit()
    return _serialize_stage(st)


@app.delete("/api/stages/{stage_id}")
def delete_stage(stage_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    st = db.query(Stage).filter(Stage.id == stage_id).first()
    if not st:
        return JSONResponse(status_code=404, content={"error": "Stage not found"})
    if db.query(Deal).filter(Deal.stage == stage_id).first():
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить стадию с активными сделками"})
    pipe_id = st.pipeline_id
    if db.query(Stage).filter(Stage.pipeline_id == pipe_id).count() <= 1:
        return JSONResponse(status_code=400, content={"error": "Нельзя удалить единственную стадию"})
    db.delete(st)
    db.commit()
    return {"status": "ok"}


@app.get("/api/pipeline-routing")
def get_pipeline_routing(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rules = db.query(PipelineRoutingRule).order_by(PipelineRoutingRule.source).all()
    by_source = {r.source: r for r in rules}
    result = []
    for src, label in LEAD_SOURCES.items():
        r = by_source.get(src)
        result.append({
            "source": src,
            "label": label,
            "pipeline_id": r.pipeline_id if r else None,
            "assignee_id": r.assignee_id if r else None,
            "is_active": bool(r.is_active) if r else False,
            "id": r.id if r else None,
        })
    return result


@app.put("/api/pipeline-routing")
def save_pipeline_routing(
    body: RoutingRulesBulk,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    denied = _require_pipeline_admin(user)
    if denied:
        return denied
    snapshot = []
    for item in body.rules:
        src = (item.source or "").strip().lower()
        if src not in LEAD_SOURCES:
            continue
        pipe = db.query(Pipeline).filter(Pipeline.id == item.pipeline_id).first()
        if not pipe:
            continue
        rule = db.query(PipelineRoutingRule).filter(PipelineRoutingRule.source == src).first()
        if not rule:
            rule = PipelineRoutingRule(source=src)
            db.add(rule)
        rule.pipeline_id = item.pipeline_id
        rule.assignee_id = item.assignee_id
        rule.is_active = True if item.is_active is None else bool(item.is_active)
        snapshot.append({
            "source": src,
            "pipeline_id": item.pipeline_id,
            "assignee_id": item.assignee_id,
            "is_active": rule.is_active,
        })
    audit.write_audit(
        db, user_id=user.id, entity_type="pipeline_routing", entity_id=None,
        action="routing_change", diff={"rules": snapshot},
        ip=audit.request_ip(request),
    )
    db.commit()
    return get_pipeline_routing(db, user)

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
        all_deals = _not_deleted(db.query(Deal), Deal).filter(Deal.stage.in_(active_stage_ids)).all()
    else:
        all_deals = _not_deleted(db.query(Deal), Deal).all()
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
        "equipment_total": result.get("own_equipment_total", result.get("equipment_total", 0)),
        "subrental_total": result.get("subrental_total", 0),
        "subrental_base": result.get("subrental_base", 0),
        "fixed_total": result.get("fixed_total", 0),
        "discount_amount": result.get("discount_amount", 0),
        "after_discount": result.get("after_discount", 0),
        "tax_percentage": result.get("tax_percentage", 0),
        "tax_amount": result.get("tax_amount", 0),
        "grand_total": result.get("grand_total", 0),
        "cost_total": result.get("cost_total", 0),
        "margin": result.get("margin", 0),
    }


OPS_STATUSES = [
    ("none", "—"),
    ("packed", "Собрали"),
    ("departed", "Уехали"),
    ("on_site", "На площадке"),
    ("returned", "Вернули"),
    ("closed", "Закрыли"),
]
OPS_STATUS_LABELS = {k: v for k, v in OPS_STATUSES}

SUBRENTAL_STATUSES = [
    ("reserved", "В резерве"),
    ("issued", "Выдано"),
    ("returned", "Вернули"),
]
SUBRENTAL_STATUS_LABELS = {k: v for k, v in SUBRENTAL_STATUSES}


def _fmt_dt(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)[:16]


def _serialize_deal_item_subrental(di: DealItem) -> dict:
    status = getattr(di, "subrental_status", None) or None
    return {
        "subrental_status": status,
        "subrental_status_label": SUBRENTAL_STATUS_LABELS.get(status or "", "") if status else "",
        "issued_at": _fmt_dt(getattr(di, "issued_at", None)),
        "issued_by_id": getattr(di, "issued_by_id", None),
        "issued_by_name": _user_display_name(getattr(di, "issued_by", None)),
        "returned_at": _fmt_dt(getattr(di, "returned_at", None)),
        "returned_by_id": getattr(di, "returned_by_id", None),
        "returned_by_name": _user_display_name(getattr(di, "returned_by", None)),
        "subrental_note": getattr(di, "subrental_note", None) or "",
    }


def _apply_subrental_defaults(di: DealItem, eq: Optional[Equipment], preserved: Optional[dict] = None) -> None:
    """Для позиций субаренды: reserved если пусто; при сохранении сметы сохраняем прошлый статус."""
    wtype = (getattr(eq, "warehouse_type", None) or "own") if eq else "own"
    if wtype != "subrental":
        di.subrental_status = None
        di.issued_at = None
        di.issued_by_id = None
        di.returned_at = None
        di.returned_by_id = None
        di.subrental_note = None
        return
    if preserved and preserved.get("subrental_status"):
        di.subrental_status = preserved["subrental_status"]
        di.issued_at = preserved.get("issued_at")
        di.issued_by_id = preserved.get("issued_by_id")
        di.returned_at = preserved.get("returned_at")
        di.returned_by_id = preserved.get("returned_by_id")
        di.subrental_note = preserved.get("subrental_note")
    elif not getattr(di, "subrental_status", None):
        di.subrental_status = "reserved"


def _preserve_subrental_by_equipment(old_items: list) -> dict:
    """equipment_id → снимок статуса субаренды (при replace-all сметы)."""
    out = {}
    for oi in old_items or []:
        st = getattr(oi, "subrental_status", None)
        if not st:
            continue
        # если дубли — оставляем более «продвинутый» статус
        rank = {"reserved": 1, "issued": 2, "returned": 3}
        prev = out.get(oi.equipment_id)
        if prev and rank.get(prev.get("subrental_status"), 0) >= rank.get(st, 0):
            continue
        out[oi.equipment_id] = {
            "subrental_status": st,
            "issued_at": getattr(oi, "issued_at", None),
            "issued_by_id": getattr(oi, "issued_by_id", None),
            "returned_at": getattr(oi, "returned_at", None),
            "returned_by_id": getattr(oi, "returned_by_id", None),
            "subrental_note": getattr(oi, "subrental_note", None),
        }
    return out


def _money_picture(deal: Deal, totals: Optional[dict] = None) -> dict:
    """Единая денежная картина сделки: выручка → затраты → маржа."""
    if totals is None:
        totals = _estimate_totals_payload(_calc_deal(deal))
    payroll = _payroll_summary(deal)
    revenue = float(totals.get("grand_total") or 0)
    cost_sub = float(totals.get("cost_total") or 0)
    payroll_gross = float(payroll.get("total_gross") or 0)
    expenses = sum(float(e.amount or 0) for e in (deal.expenses or []))
    advances = sum(float(a.amount or 0) for a in (deal.advances or []))
    sales_fix = float(getattr(deal, "sales_fix_kzt", None) or 0)
    project_fix = float(getattr(deal, "project_fix_kzt", None) or 0)
    margin_target = float(getattr(deal, "margin_target_pct", None) or 10)
    # Маржа: выручка − субаренда − ФОТ − расходы − фиксы менеджеров
    margin = revenue - cost_sub - payroll_gross - expenses - sales_fix - project_fix
    margin_pct = (margin / revenue * 100.0) if revenue else 0.0
    return {
        "revenue": revenue,
        "subrental_cost": cost_sub,
        "subrental_client": float(totals.get("subrental_total") or 0),
        "payroll": payroll_gross,
        "payroll_net": float(payroll.get("total_net") or 0),
        "expenses": expenses,
        "advances": advances,
        "sales_fix_kzt": sales_fix,
        "project_fix_kzt": project_fix,
        "margin_target_pct": margin_target,
        "margin_pct": round(margin_pct, 1),
        "margin": margin,
        "estimate_margin": float(totals.get("margin") or 0),
    }


def _notify_user(
    db: Session,
    user_id: Optional[int],
    *,
    kind: str,
    title: str,
    body: str = "",
    link: str = None,
    deal_id: int = None,
    task_id: int = None,
    skip_user_id: int = None,
):
    if not user_id or user_id == skip_user_id:
        return
    db.add(AppNotification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body or None,
        link=link,
        deal_id=deal_id,
        task_id=task_id,
        is_read=False,
    ))


def _default_assignee_id(db: Session) -> Optional[int]:
    u = db.query(User).filter(User.role.in_(["admin", "manager"])).order_by(User.id).first()
    return u.id if u else None


def _pipeline_kind(pipe: Optional[Pipeline]) -> str:
    if not pipe:
        return "deal"
    kind = (getattr(pipe, "kind", None) or "deal").strip().lower()
    return kind if kind in ("lead", "deal") else "deal"


def _stage_is_won(st: Optional[Stage]) -> bool:
    if not st:
        return False
    if getattr(st, "is_won", False) or getattr(st, "creates_deal", False):
        return True
    return "успешн" in (st.name or "").lower()


def _stage_is_lost(st: Optional[Stage]) -> bool:
    if not st:
        return False
    if getattr(st, "is_lost", False):
        return True
    return "проигра" in (st.name or "").lower() or (st.name or "").strip().lower() == "отказ"


def _stage_creates_deal(st: Optional[Stage], pipe: Optional[Pipeline] = None) -> bool:
    if not st:
        return False
    if getattr(st, "creates_deal", False):
        return True
    pipe = pipe or getattr(st, "pipeline", None)
    return _pipeline_kind(pipe) == "lead" and _stage_is_won(st)


def _serialize_stage(s: Stage) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "order_index": s.order_index,
        "is_active_rent": bool(s.is_active_rent),
        "is_won": bool(getattr(s, "is_won", False)),
        "is_lost": bool(getattr(s, "is_lost", False)),
        "creates_deal": bool(getattr(s, "creates_deal", False)),
        "pipeline_id": s.pipeline_id,
    }


def _serialize_pipeline(p: Pipeline, include_stages: bool = False) -> dict:
    data = {
        "id": p.id,
        "name": p.name,
        "kind": _pipeline_kind(p),
        "target_pipeline_id": getattr(p, "target_pipeline_id", None),
        "stages_count": len(p.stages or []),
    }
    if include_stages:
        stages = sorted(p.stages or [], key=lambda s: (s.order_index or 0, s.id))
        data["stages"] = [_serialize_stage(s) for s in stages]
    return data


def _resolve_pipeline_for_source(db: Session, source: Optional[str]) -> tuple:
    """Возвращает (pipeline, assignee_id) по правилам маршрутизации."""
    src = (source or "manual").strip().lower()
    rule = (
        db.query(PipelineRoutingRule)
        .filter(PipelineRoutingRule.source == src, PipelineRoutingRule.is_active == True)  # noqa: E712
        .first()
    )
    if rule:
        pipe = db.query(Pipeline).filter(Pipeline.id == rule.pipeline_id).first()
        if pipe:
            return pipe, rule.assignee_id

    lead_pipe = (
        db.query(Pipeline)
        .filter(Pipeline.kind == "lead")
        .order_by(Pipeline.id)
        .first()
    )
    if lead_pipe:
        return lead_pipe, None
    return db.query(Pipeline).order_by(Pipeline.id).first(), None


def _first_stage(db: Session, pipeline_id: Optional[int]) -> Optional[Stage]:
    if not pipeline_id:
        return None
    return (
        db.query(Stage)
        .filter(Stage.pipeline_id == pipeline_id)
        .order_by(Stage.order_index, Stage.id)
        .first()
    )


def _convert_lead_to_deal(
    db: Session,
    lead: Deal,
    lead_pipeline: Pipeline,
    convert_to_pipeline_id: Optional[int] = None,
) -> Optional[Deal]:
    """Создаёт сделку в целевой deal-воронке (Аренда/Продажа) из успешного лида. Идемпотентно."""
    existing = db.query(Deal).filter(Deal.prev_deal_id == lead.id).first()
    if existing:
        return existing

    target = None
    if convert_to_pipeline_id:
        target = (
            db.query(Pipeline)
            .filter(Pipeline.id == convert_to_pipeline_id, Pipeline.kind == "deal")
            .first()
        )
    if not target:
        target_id = getattr(lead_pipeline, "target_pipeline_id", None)
        target = db.query(Pipeline).filter(Pipeline.id == target_id).first() if target_id else None
    if not target:
        # Предпочитаем «Аренда», иначе первая deal-воронка
        target = (
            db.query(Pipeline)
            .filter(Pipeline.kind == "deal", Pipeline.name == "Аренда")
            .first()
        ) or (
            db.query(Pipeline)
            .filter(Pipeline.kind == "deal")
            .order_by(Pipeline.id)
            .first()
        )
    if not target or target.id == lead.pipeline_id:
        return None

    first = _first_stage(db, target.id)
    title = lead.title or f"Сделка из лида №{lead.id}"
    if title.lower().startswith("заявка"):
        title = title.replace("Заявка:", "Сделка:", 1).replace("заявка:", "Сделка:", 1)

    sales_mgr = getattr(lead, "sales_manager_id", None) or lead.assignee_id or _default_assignee_id(db)
    new_deal = Deal(
        title=title[:200],
        company_id=lead.company_id,
        contact_id=lead.contact_id,
        assignee_id=lead.assignee_id or sales_mgr,
        sales_manager_id=sales_mgr,
        project_manager_id=getattr(lead, "project_manager_id", None),
        pipeline_id=target.id,
        stage=first.id if first else 1,
        setup_date=lead.setup_date,
        event_date=lead.event_date or "",
        event_address=lead.event_address,
        city=lead.city,
        city_id=getattr(lead, "city_id", None) or (_default_city(db).id if _default_city(db) else None),
        shifts=lead.shifts or 1.0,
        discount_percentage=lead.discount_percentage or 0.0,
        tax_percentage=FIXED_TAX_PERCENTAGE,
        final_sum=lead.final_sum or 0.0,
        comment=lead.comment,
        chat_channel=lead.chat_channel,
        chat_id=lead.chat_id,
        prev_deal_id=lead.id,
        source=lead.source or lead.chat_channel or "manual",
        qualification=_normalize_qualification(getattr(lead, "qualification", None)),
        is_qualified=True,
        is_archived=False,
        sales_fix_kzt=float(getattr(lead, "sales_fix_kzt", None) or 0),
        project_fix_kzt=float(getattr(lead, "project_fix_kzt", None) or 0),
        margin_target_pct=float(getattr(lead, "margin_target_pct", None) or 10),
    )
    db.add(new_deal)
    db.flush()

    # Копируем позиции сметы, если были на лиде
    for it in list(lead.items or []):
        db.add(DealItem(
            deal_id=new_deal.id,
            equipment_id=it.equipment_id,
            quantity=it.quantity,
            days=it.days,
            price=it.price,
        ))

    lead.is_qualified = True
    db.add(DealHistory(
        deal_id=lead.id,
        action_text=f"Лид конвертирован в сделку №{new_deal.id} («{target.name}»)",
    ))
    db.add(DealHistory(
        deal_id=new_deal.id,
        action_text=f"Сделка создана из лида №{lead.id}",
    ))
    return new_deal


def _require_pipeline_admin(user: User):
    if not user or user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Нужны права администратора или менеджера"})
    return None


def _user_crm_own_only(user: User) -> bool:
    if not user or user.role == "admin":
        return False
    perms = user.permissions or []
    return "crm_own_only" in perms


def _user_hide_prices(user: User) -> bool:
    if not user or user.role == "admin":
        return False
    return "hide_prices" in (user.permissions or [])


def _user_hide_margin(user: User) -> bool:
    if _user_hide_prices(user):
        return True
    return auth.user_has_flag(user, "hide_margin")


def _user_hide_payroll(user: User) -> bool:
    if _user_hide_prices(user):
        return True
    return auth.user_has_flag(user, "hide_payroll")


def _user_hide_subrental_cost(user: User) -> bool:
    if _user_hide_prices(user):
        return True
    return auth.user_has_flag(user, "hide_subrental_cost")


def _user_is_sales(user: User) -> bool:
    if not user:
        return False
    if user.role == "admin":
        return True
    return auth.user_has_flag(user, "role_sales") or not auth.user_has_flag(user, "role_project")


def _user_is_project(user: User) -> bool:
    if not user:
        return False
    if user.role == "admin":
        return True
    return auth.user_has_flag(user, "role_project") or not auth.user_has_flag(user, "role_sales")


def _normalize_qualification(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip().lower()
    aliases = {
        "аренда": "rental",
        "прокат": "rental",
        "rental": "rental",
        "продажа": "sale",
        "продажи": "sale",
        "sale": "sale",
        "sales": "sale",
        "спам": "spam",
        "спам-отказ": "spam",
        "отказ": "spam",
        "spam": "spam",
    }
    norm = aliases.get(raw, raw)
    return norm if norm in QUALIFICATION_VALUES else None


def _pipeline_for_qualification(db: Session, qualification: Optional[str]) -> Optional[Pipeline]:
    q = _normalize_qualification(qualification)
    if q == "sale":
        return (
            db.query(Pipeline)
            .filter(Pipeline.kind == "deal", Pipeline.name.in_(["Продажа", "Продажи"]))
            .order_by(Pipeline.id)
            .first()
        )
    if q == "rental":
        return (
            db.query(Pipeline)
            .filter(Pipeline.kind == "deal", Pipeline.name.in_(["Аренда", "Прокат"]))
            .order_by(Pipeline.id)
            .first()
        )
    return None


def _lost_stage_for_pipeline(db: Session, pipeline_id: Optional[int]) -> Optional[Stage]:
    if not pipeline_id:
        return None
    stages = (
        db.query(Stage)
        .filter(Stage.pipeline_id == pipeline_id)
        .order_by(Stage.order_index, Stage.id)
        .all()
    )
    for st in stages:
        if _stage_is_lost(st):
            return st
    return None


def _get_company_letterhead(db: Session) -> dict:
    keys = ("company_name", "company_phone", "company_email", "company_address", "company_bin")
    rows = {r.key: (r.value or "") for r in db.query(AppSetting).filter(AppSetting.key.in_(keys)).all()}
    return {k: rows.get(k, "") for k in keys}


def _user_assigned_to_deal(db: Session, user: User, deal: Deal) -> bool:
    if not user or not deal:
        return False
    if user.role == "admin":
        return True
    if deal.assignee_id == user.id:
        return True
    if getattr(deal, "sales_manager_id", None) == user.id:
        return True
    if getattr(deal, "project_manager_id", None) == user.id:
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
        pm = getattr(deal, "project_manager", None)
        if pm:
            manager_name = pm.full_name or pm.username or ""
        elif deal.assignee:
            manager_name = deal.assignee.full_name or deal.assignee.username or ""
    except Exception:
        manager_name = ""

    sales_name = ""
    try:
        sm = getattr(deal, "sales_manager", None)
        if sm:
            sales_name = sm.full_name or sm.username or ""
        elif deal.assignee:
            sales_name = deal.assignee.full_name or deal.assignee.username or ""
    except Exception:
        sales_name = ""

    shifts = _deal_shifts(deal)
    shifts_label = str(int(shifts)) if shifts == int(shifts) else str(shifts)

    return {
        "company_name": deal.company.name if deal.company else "",
        "event_name": deal.title or "",
        "project_name": deal.title or "",
        "contact_name": contact_name,
        "manager_name": manager_name,
        "sales_manager_name": sales_name,
        "project_manager_name": manager_name,
        "city": (getattr(deal, "city", None) or "") or "",
        "event_address": deal.event_address or "",
        "departure_date": depart,
        "return_date": ret,
        "rent_period": rent_period,
        "shifts": shifts,
        "shifts_label": shifts_label,
    }


def _build_technichka_context(deal: Deal, assignee_name: str = "", db: Session = None) -> dict:
    """Контекст технички: только оборудование (без FIXED_CATEGORIES логистики/персонала)."""
    result = _calc_deal(deal)
    header = _estimate_header_fields(deal)
    items = [
        i for i in (result.get("items") or [])
        if not _is_fixed_category(i.get("category"))
    ]
    letterhead = {}
    if db is not None:
        letterhead = _get_company_letterhead(db)
    else:
        try:
            with Session(engine) as s:
                letterhead = _get_company_letterhead(s)
        except Exception:
            letterhead = {}
    logo_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static", "img", "introshow_logo.png",
    )
    ctx = {
        "number": f"TECH-{deal.id}",
        "date": datetime.today().strftime("%d.%m.%Y"),
        **header,
        "assignee_name": assignee_name or "",
        "items": items,
        "our_company_name": letterhead.get("company_name") or "Intro Show",
        "our_company_phone": letterhead.get("company_phone") or "",
        "our_company_email": letterhead.get("company_email") or "",
        "our_company_address": letterhead.get("company_address") or "",
        "our_company_bin": letterhead.get("company_bin") or "",
        "logo_path": logo_path if os.path.isfile(logo_path) else None,
        "manager_phone": letterhead.get("company_phone") or "",
    }
    return doc_templates.apply_template_to_context(ctx, db, "technichka")


DOC_TYPE_LABELS = {
    "estimate_internal": "Смета внутренняя (Word)",
    "estimate_internal_pdf": "Смета внутренняя (PDF)",
    "estimate_client": "Смета клиенту без цен (Word)",
    "estimate_client_pdf": "Смета клиенту без цен (PDF)",
    "estimate_client_priced": "Смета клиенту с ценами (Word)",
    "estimate_client_priced_pdf": "Смета клиенту с ценами (PDF)",
    "contract": "Договор (Word)",
    "contract_pdf": "Договор (PDF)",
    "technichka": "Техничка (Word)",
    "technichka_pdf": "Техничка (PDF)",
}


def _estimate_doc_type(mode_norm: str, as_pdf: bool = False) -> str:
    base = {
        "internal": "estimate_internal",
        "client": "estimate_client",
        "client_priced": "estimate_client_priced",
    }.get(mode_norm, "estimate_internal")
    return f"{base}_pdf" if as_pdf else base


def _register_deal_document(
    db: Session,
    deal_id: int,
    doc_type: str,
    filename: str = None,
    path: str = None,
    created_by: str = None,
) -> DealDocument:
    """Записать факт генерации/наличия документа (обновляет последнюю запись того же типа)."""
    row = (
        db.query(DealDocument)
        .filter(DealDocument.deal_id == deal_id, DealDocument.doc_type == doc_type)
        .order_by(DealDocument.id.desc())
        .first()
    )
    if row:
        row.filename = filename or row.filename
        row.path = path or row.path
        row.created_at = datetime.utcnow()
        if created_by:
            row.created_by = created_by
    else:
        row = DealDocument(
            deal_id=deal_id,
            doc_type=doc_type,
            filename=filename,
            path=path,
            created_by=created_by,
        )
        db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
    return row


def _download_url_for_doc(deal_id: int, doc_type: str) -> str:
    if doc_type == "contract":
        return f"/api/deals/{deal_id}/contract"
    if doc_type == "contract_pdf":
        return f"/api/deals/{deal_id}/contract.pdf"
    if doc_type in ("technichka", "technichka_pdf"):
        return f"/api/deals/{deal_id}/technichka"
    mode_map = {
        "estimate_internal": ("internal", False),
        "estimate_internal_pdf": ("internal", True),
        "estimate_client": ("client", False),
        "estimate_client_pdf": ("client", True),
        "estimate_client_priced": ("client_priced", False),
        "estimate_client_priced_pdf": ("client_priced", True),
    }
    mode, as_pdf = mode_map.get(doc_type, ("internal", False))
    if as_pdf:
        return f"/api/deals/{deal_id}/estimate.pdf?mode={mode}"
    return f"/api/deals/{deal_id}/estimate?mode={mode}"


def _scan_uploads_technichka(db: Session) -> list:
    """Найти technichka_deal{ID}_* в uploads и вернуть записи для реестра."""
    rows = []
    try:
        names = os.listdir(UPLOADS_DIR)
    except OSError:
        return rows
    deal_ids = {d.id: d for d in db.query(Deal).all()}
    for fname in names:
        m = re.match(r"technichka_deal(\d+)_", fname, re.I)
        if not m:
            continue
        deal_id = int(m.group(1))
        deal = deal_ids.get(deal_id)
        if not deal:
            continue
        is_pdf = fname.lower().endswith(".pdf")
        doc_type = "technichka_pdf" if is_pdf else "technichka"
        abs_path = os.path.join(UPLOADS_DIR, fname)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(abs_path))
        except OSError:
            mtime = None
        rows.append({
            "id": None,
            "deal_id": deal_id,
            "deal_title": deal.title,
            "company_name": deal.company.name if deal.company else None,
            "doc_type": doc_type,
            "doc_label": DOC_TYPE_LABELS.get(doc_type, doc_type),
            "filename": fname,
            "path": f"/uploads/{fname}",
            "created_at": mtime.isoformat() if mtime else None,
            "created_by": None,
            "download_url": f"/uploads/{fname}",
            "source": "uploads",
            "status": "stored",
        })
    return rows


def _save_technichka_file(deal: Deal, assignee_name: str = "", db: Session = None, created_by: str = None) -> tuple:
    """Генерирует PDF технички, сохраняет в uploads. Returns (url, filename, abs_path)."""
    from document_generator import generate_technichka_pdf
    import uuid
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    fname = f"technichka_deal{deal.id}_{uuid.uuid4().hex[:8]}.pdf"
    abs_path = os.path.join(UPLOADS_DIR, fname)
    generate_technichka_pdf(_build_technichka_context(deal, assignee_name), abs_path)
    url = f"/uploads/{fname}"
    if db is not None:
        _register_deal_document(
            db, deal.id, "technichka_pdf",
            filename=fname, path=url, created_by=created_by or assignee_name,
        )
    return url, fname, abs_path


def assign_staff_to_deal(
    db: Session,
    deal: Deal,
    emp: User,
    created_by: str,
    role_name: Optional[str] = None,
    note: Optional[str] = None,
    creator_user_id: Optional[int] = None,
) -> DealStaffAssignment:
    """Назначить сотрудника: техничка PDF → задача → комментарий в чате → напоминание за 1 день."""
    existing = db.query(DealStaffAssignment).filter(
        DealStaffAssignment.deal_id == deal.id,
        DealStaffAssignment.user_id == emp.id,
    ).first()
    if existing:
        return existing

    emp_name = emp.full_name or emp.username
    url, fname, _ = _save_technichka_file(
        deal, emp_name, db=db, created_by=created_by,
    )
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
        "Техничка в чате задачи",
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
    # Готовый PDF — в чат задачи кликабельной ссылкой (не путь в описании)
    db.add(TaskComment(
        task_id=task.id,
        user_id=creator_user_id,
        text=f"📎 Техничка (PDF)\n{url}",
    ))
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
    qual = _normalize_qualification(getattr(d, "qualification", None))
    city_name = ""
    if getattr(d, "workspace_city", None):
        city_name = d.workspace_city.name or ""
    elif getattr(d, "city", None):
        city_name = d.city or ""
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
        "sales_manager_id": getattr(d, "sales_manager_id", None),
        "project_manager_id": getattr(d, "project_manager_id", None),
        "contact_id": d.contact_id,
        "source": d.source or d.chat_channel or "manual",
        "loss_reason": d.loss_reason,
        "qualification": qual,
        "qualification_label": QUALIFICATION_LABELS.get(qual or "", ""),
        "is_qualified": bool(d.is_qualified) or qual in ("rental", "sale"),
        "is_archived": bool(d.is_archived),
        "city_id": getattr(d, "city_id", None),
        "city_name": city_name,
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
    city_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = _not_deleted(db.query(Deal), Deal)
    if not include_archived:
        query = query.filter((Deal.is_archived == False) | (Deal.is_archived.is_(None)))  # noqa: E712
    if pipeline_id:
        query = query.filter(Deal.pipeline_id == pipeline_id)
    if _user_crm_own_only(user):
        query = query.filter(or_(
            Deal.assignee_id == user.id,
            Deal.sales_manager_id == user.id,
            Deal.project_manager_id == user.id,
        ))
    elif assignee == "me":
        query = query.filter(or_(
            Deal.assignee_id == user.id,
            Deal.sales_manager_id == user.id,
            Deal.project_manager_id == user.id,
        ))
    elif assignee and assignee.isdigit():
        query = query.filter(Deal.assignee_id == int(assignee))
    if source:
        query = query.filter((Deal.source == source) | ((Deal.source.is_(None)) & (Deal.chat_channel == source)))
    if rent_from:
        query = query.filter(Deal.setup_date >= rent_from)
    if rent_to:
        query = query.filter(Deal.event_date <= rent_to)

    active = _resolve_active_city(db, user, request)
    filter_city = city_id if city_id is not None else (active.id if active else None)
    # city_id=0 → все города (только admin/manager)
    if city_id == 0:
        if user.role in ("admin", "manager"):
            filter_city = None
        else:
            filter_city = active.id if active else None
    query = _apply_deal_city_filter(query, filter_city)

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
def create_deal(deal: DealCreate, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    qual = _normalize_qualification(deal.qualification)
    sales_mgr = deal.sales_manager_id or assignee_id
    project_mgr = deal.project_manager_id

    active_city = _resolve_active_city(db, user, request)
    deal_city_id = deal.city_id or (active_city.id if active_city else None)
    if deal_city_id and not db.query(City).filter(City.id == deal_city_id).first():
        deal_city_id = active_city.id if active_city else None
    city_text = (deal.city or "").strip() or None
    if deal_city_id and not city_text:
        c_row = db.query(City).filter(City.id == deal_city_id).first()
        city_text = c_row.name if c_row else None

    db_deal = Deal(
        title=deal.title,
        company_id=deal.company_id,
        contact_id=contact_id,
        assignee_id=assignee_id,
        sales_manager_id=sales_mgr,
        project_manager_id=project_mgr,
        pipeline_id=deal.pipeline_id,
        setup_date=deal.setup_date,
        event_date=deal.event_date,
        event_address=deal.event_address,
        city=city_text,
        city_id=deal_city_id,
        shifts=float(deal.shifts) if deal.shifts is not None else 1.0,
        discount_percentage=deal.discount_percentage,
        tax_percentage=FIXED_TAX_PERCENTAGE,
        stage=stage_id,
        source=deal.source or "manual",
        qualification=qual,
        is_qualified=bool(deal.is_qualified) or qual in ("rental", "sale"),
        sales_fix_kzt=float(deal.sales_fix_kzt or 0),
        project_fix_kzt=float(deal.project_fix_kzt or 0),
        margin_target_pct=float(deal.margin_target_pct if deal.margin_target_pct is not None else 10),
    )
    db.add(db_deal)
    db.commit()
    db.refresh(db_deal)
    
    history_entry = DealHistory(deal_id=db_deal.id, action_text="Сделка создана")
    db.add(history_entry)
    audit.write_audit(
        db, user_id=user.id, entity_type="deal", entity_id=db_deal.id,
        action="create", diff={"title": db_deal.title, "pipeline_id": db_deal.pipeline_id},
        ip=audit.request_ip(request),
    )
    db.commit()

    items_warning = None
    if deal.items_json:
        try:
            items_list = json.loads(deal.items_json)
            for it in items_list:
                eq_id = it['id']
                eq = db.query(Equipment).filter(Equipment.id == eq_id).first()
                db_item = DealItem(
                    deal_id=db_deal.id, equipment_id=eq_id,
                    quantity=it['qty'], days=it['days'],
                    price=it.get('price'),
                )
                _apply_subrental_defaults(db_item, eq)
                db.add(db_item)
            db.commit()
            _recalc_deal_sum(db, db_deal)
        except Exception as exc:
            db.rollback()
            items_warning = f"Сделка создана, но позиции не сохранены: {exc}"
            import traceback
            traceback.print_exc()

    result = {"id": db_deal.id}
    if items_warning:
        result["warning"] = items_warning
    return result

@app.put("/api/deals/{deal_id}/stage")
def update_deal_stage(
    deal_id: int,
    stage_update: DealStageUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db_deal = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not db_deal:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    if stage_update.qualification is not None:
        q_in = _normalize_qualification(stage_update.qualification)
        if stage_update.qualification and not q_in:
            return JSONResponse(
                status_code=400,
                content={"error": "Квалификация: аренда / продажа / спам-отказ"},
            )
        db_deal.qualification = q_in
        db_deal.is_qualified = q_in in ("rental", "sale")

    new_stage_obj = db.query(Stage).filter(Stage.id == stage_update.stage).first()
    pipe = db.query(Pipeline).filter(Pipeline.id == db_deal.pipeline_id).first()
    qual = _normalize_qualification(getattr(db_deal, "qualification", None))

    # Спам/отказ: при попытке «успеха» лида — уводим в lost без создания сделки
    spam_redirected = False
    if (
        new_stage_obj
        and pipe
        and _pipeline_kind(pipe) == "lead"
        and _stage_creates_deal(new_stage_obj, pipe)
        and qual == "spam"
    ):
        lost = _lost_stage_for_pipeline(db, pipe.id)
        if not lost:
            return JSONResponse(
                status_code=400,
                content={"error": "Нет стадии отказа в воронке лидов — добавьте «Отказ»"},
            )
        new_stage_obj = lost
        stage_update.stage = lost.id
        if not (stage_update.loss_reason or db_deal.loss_reason or "").strip():
            stage_update.loss_reason = "спам-отказ"
        spam_redirected = True

    if new_stage_obj and _stage_is_lost(new_stage_obj):
        reason = (stage_update.loss_reason or db_deal.loss_reason or "").strip()
        if qual == "spam" and not reason:
            reason = "спам-отказ"
        if not reason:
            return JSONResponse(status_code=400, content={"error": "Укажите причину отказа"})
        db_deal.loss_reason = reason

    # Конвертация лида требует квалификации аренда/продажа
    if (
        new_stage_obj
        and pipe
        and _pipeline_kind(pipe) == "lead"
        and _stage_creates_deal(new_stage_obj, pipe)
        and not spam_redirected
    ):
        if not qual:
            return JSONResponse(
                status_code=400,
                content={"error": "Укажите квалификацию лида: аренда / продажа / спам-отказ"},
            )
        if qual not in ("rental", "sale"):
            return JSONResponse(
                status_code=400,
                content={"error": "Для создания сделки выберите аренда или продажа"},
            )

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
    if db_deal.loss_reason and new_stage_obj and _stage_is_lost(new_stage_obj):
        hist += f" (причина: {db_deal.loss_reason})"
    if spam_redirected:
        hist += " [спам → отказ без сделки]"
    db.add(DealHistory(deal_id=deal_id, action_text=hist))
    audit.write_audit(
        db, user_id=user.id if user else None, entity_type="deal", entity_id=deal_id,
        action="stage_change",
        diff={"from": old_name or old_stage, "to": new_name, "stage_id": stage_update.stage},
        ip=audit.request_ip(request),
    )

    converted_deal_id = None
    converted_pipeline_name = None
    if (
        new_stage_obj
        and pipe
        and _pipeline_kind(pipe) == "lead"
        and _stage_creates_deal(new_stage_obj, pipe)
        and not spam_redirected
    ):
        convert_pid = stage_update.convert_to_pipeline_id
        if not convert_pid:
            suggested = _pipeline_for_qualification(db, qual)
            if suggested:
                convert_pid = suggested.id
        new_deal = _convert_lead_to_deal(
            db, db_deal, pipe,
            convert_to_pipeline_id=convert_pid,
        )
        if new_deal:
            converted_deal_id = new_deal.id
            tp = db.query(Pipeline).filter(Pipeline.id == new_deal.pipeline_id).first()
            converted_pipeline_name = tp.name if tp else None
            audit.write_audit(
                db, user_id=user.id if user else None, entity_type="deal", entity_id=new_deal.id,
                action="create",
                diff={"from_lead": deal_id, "pipeline": converted_pipeline_name, "qualification": qual},
                ip=audit.request_ip(request),
            )

    # При «Успешно» в deal-воронке — зарплатная ведомость
    if new_stage_obj and _stage_is_won(new_stage_obj) and _pipeline_kind(pipe) == "deal":
        if not db_deal.payroll_lines:
            n = generate_payroll_for_deal(db, db_deal, replace=True)
            if n:
                db.add(DealHistory(
                    deal_id=deal_id,
                    action_text=f"Сформирована зарплатная ведомость: {n} строк(и) из сметы",
                ))

    db.commit()

    if new_stage_obj and ("Монтаж" in (new_stage_obj.name or "") or "доставлен" in (new_stage_obj.name or "").lower()):
        company = db_deal.company
        if company:
            msg = f"Здравствуйте, {company.director_name or company.name}! Ваш заказ '{db_deal.title}' перешел в статус: {new_stage_obj.name}. Оборудование доставлено/монтируется."
            if company.phone:
                notifications.send_wa_message(company.phone, msg)
            if company.telegram_chat_id:
                notifications.send_tg_message(company.telegram_chat_id, msg)
            for sub in getattr(db_deal, "push_subscriptions", []) or []:
                notifications.send_web_push({
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                }, msg)

    resp = {"status": "success", "stage": db_deal.stage}
    if spam_redirected:
        resp["spam_rejected"] = True
        resp["message"] = "Лид отмечен как спам-отказ — сделка не создана"
    if converted_deal_id:
        resp["converted_deal_id"] = converted_deal_id
        resp["converted_pipeline"] = converted_pipeline_name
        resp["message"] = (
            f"Создана сделка №{converted_deal_id}"
            + (f" в воронке «{converted_pipeline_name}»" if converted_pipeline_name else "")
        )
    return resp

@app.get("/api/deals/{deal_id}")
def get_deal_detail(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
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
        row = {
            "id": i.id,
            "equipment_id": i.equipment_id,
            "quantity": i.quantity,
            "days": i.days,
            "name": eq.name if eq else "Unknown",
            "price": price,
            "stock_price": 0 if hide else (eq.price if eq else 0),
            "category_type": "fixed" if eq and _is_fixed_category(eq.category) else "equipment",
            "warehouse_type": (getattr(eq, "warehouse_type", None) or "own") if eq else "own",
            "cost_price": 0 if (hide or _user_hide_subrental_cost(user)) else float(getattr(eq, "cost_price", 0) or 0) if eq else 0,
            "supplier": getattr(eq, "supplier", None) if eq else None,
            "category": eq.category if eq else "",
        }
        row.update(_serialize_deal_item_subrental(i))
        items.append(row)

    totals = None if hide else _estimate_totals_payload(_calc_deal(d))
    hide_margin = _user_hide_margin(user)
    hide_payroll = _user_hide_payroll(user)
    hide_sub_cost = _user_hide_subrental_cost(user)

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
    sales_manager_name = None
    if getattr(d, "sales_manager_id", None) and getattr(d, "sales_manager", None):
        sales_manager_name = d.sales_manager.full_name or d.sales_manager.username
    project_manager_name = None
    if getattr(d, "project_manager_id", None) and getattr(d, "project_manager", None):
        project_manager_name = d.project_manager.full_name or d.project_manager.username
    qual = _normalize_qualification(getattr(d, "qualification", None))

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
        "city_id": getattr(d, "city_id", None),
        "city_name": (
            d.workspace_city.name if getattr(d, "workspace_city", None) else (getattr(d, "city", None) or "")
        ),
        "shifts": float(getattr(d, "shifts", None) or 1),
        "discount_percentage": 0 if hide else d.discount_percentage,
        "tax_percentage": 0 if hide else _deal_tax(d),
        "final_sum": 0 if hide else d.final_sum,
        "totals": totals,
        "hide_prices": hide,
        "hide_margin": hide_margin,
        "hide_payroll": hide_payroll,
        "hide_subrental_cost": hide_sub_cost,
        "role_sales": _user_is_sales(user),
        "role_project": _user_is_project(user),
        "comment": d.comment,
        "created_at": d.created_at.strftime("%d.%m.%Y") if d.created_at else "",
        "chat_channel": d.chat_channel,
        "chat_id": d.chat_id,
        "contact": contact,
        "contact_id": d.contact_id,
        "assignee_id": d.assignee_id,
        "assignee_name": assignee_name,
        "sales_manager_id": getattr(d, "sales_manager_id", None),
        "sales_manager_name": sales_manager_name,
        "project_manager_id": getattr(d, "project_manager_id", None),
        "project_manager_name": project_manager_name,
        "source": d.source or d.chat_channel or "manual",
        "loss_reason": d.loss_reason,
        "qualification": qual,
        "qualification_label": QUALIFICATION_LABELS.get(qual or "", ""),
        "is_qualified": bool(d.is_qualified) or qual in ("rental", "sale"),
        "sales_fix_kzt": float(getattr(d, "sales_fix_kzt", None) or 0),
        "project_fix_kzt": float(getattr(d, "project_fix_kzt", None) or 0),
        "margin_target_pct": float(getattr(d, "margin_target_pct", None) or 10),
        "is_archived": bool(d.is_archived),
        "ops_status": getattr(d, "ops_status", None) or "none",
        "ops_status_label": OPS_STATUS_LABELS.get(getattr(d, "ops_status", None) or "none", "—"),
        "prev_deal": prev_deal,
        "items": items,
        "history": history,
        "activities": activities,
        "invoices": [] if hide else invoices,
        "advances": [] if hide else advances,
        "expenses": [] if hide else expenses,
        "advances_total": 0 if hide else sum(a["amount"] for a in advances),
        "expenses_total": 0 if hide else sum(e["amount"] for e in expenses),
        "payroll_lines": [] if hide_payroll else [_serialize_payroll_line(p) for p in sorted(d.payroll_lines, key=lambda x: x.id)],
        "payroll_summary": None if hide_payroll else _payroll_summary(d),
        "money_picture": None if hide_margin else _money_picture(d, totals),
        "tasks": [{
            "id": t.id, "title": t.title, "status": t.status,
            "due_date": t.due_date or "", "priority": t.priority or "normal",
            "assignee": t.assignee or "",
        } for t in sorted(d.tasks or [], key=lambda x: x.id, reverse=True)[:20]],
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
def update_deal_items(
    deal_id: int,
    update: DealItemsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    # Менеджер проекта без role_sales не меняет смету
    if auth.user_has_flag(user, "role_project") and not auth.user_has_flag(user, "role_sales") and user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Смету меняет менеджер продаж"})

    old_items = db.query(DealItem).filter(DealItem.deal_id == deal_id).all()
    preserved = _preserve_subrental_by_equipment(old_items)

    # Remove old items
    db.query(DealItem).filter(DealItem.deal_id == deal_id).delete()

    # Add new items (субаренда → reserved, статус выдачи сохраняем по equipment_id)
    eq_ids = [i["equipment_id"] for i in (update.items or []) if i.get("equipment_id")]
    eq_map = {
        e.id: e for e in db.query(Equipment).filter(Equipment.id.in_(eq_ids)).all()
    } if eq_ids else {}
    for i in update.items:
        eq_id = i["equipment_id"]
        di = DealItem(
            deal_id=deal_id,
            equipment_id=eq_id,
            quantity=i["quantity"],
            days=i["days"],
            price=i.get("price"),
        )
        _apply_subrental_defaults(di, eq_map.get(eq_id), preserved.get(eq_id))
        db.add(di)

    d.discount_percentage = update.discount_percentage
    d.tax_percentage = FIXED_TAX_PERCENTAGE
    audit.write_audit(
        db, user_id=user.id, entity_type="deal", entity_id=deal_id,
        action="items_save",
        diff={"items_count": len(update.items or []), "discount": update.discount_percentage},
        ip=audit.request_ip(request),
    )
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
    city_id: Optional[int] = None
    shifts: Optional[float] = None
    comment: Optional[str] = None
    company_id: Optional[int] = None
    contact_id: Optional[int] = None
    assignee_id: Optional[int] = None
    sales_manager_id: Optional[int] = None
    project_manager_id: Optional[int] = None
    source: Optional[str] = None
    loss_reason: Optional[str] = None
    qualification: Optional[str] = None
    is_qualified: Optional[bool] = None
    is_archived: Optional[bool] = None
    pipeline_id: Optional[int] = None
    sales_fix_kzt: Optional[float] = None
    project_fix_kzt: Optional[float] = None
    margin_target_pct: Optional[float] = None

@app.put("/api/deals/{deal_id}")
def update_deal(
    deal_id: int,
    update: DealUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if _user_crm_own_only(user) and not _user_assigned_to_deal(db, user, d):
        return JSONResponse(status_code=403, content={"error": "Нет доступа"})

    data = update.dict(exclude_unset=True)
    # Фронт всегда шлёт assignee_id в форме сделки — блокируем только реальную смену
    for mgr_field in ("assignee_id", "sales_manager_id", "project_manager_id"):
        if mgr_field in data and user.role not in ("admin", "manager"):
            if data.get(mgr_field) != getattr(d, mgr_field, None):
                return JSONResponse(status_code=403, content={"error": "Нет права менять ответственного"})
            data.pop(mgr_field, None)

    if "qualification" in data:
        q_raw = data.get("qualification")
        if q_raw in ("", None):
            data["qualification"] = None
        else:
            q_norm = _normalize_qualification(q_raw)
            if not q_norm:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Квалификация: аренда / продажа / спам-отказ"},
                )
            data["qualification"] = q_norm
            data["is_qualified"] = q_norm in ("rental", "sale")

    # Автоподстановка основного контакта при смене компании
    if "company_id" in data and data["company_id"] and "contact_id" not in data:
        primary = db.query(Contact).filter(
            Contact.company_id == data["company_id"], Contact.is_primary == True  # noqa: E712
        ).first()
        if primary:
            data["contact_id"] = primary.id

    changed = {}
    for field, value in data.items():
        old = getattr(d, field, None)
        if old != value:
            changed[field] = {"from": old, "to": value}
        setattr(d, field, value)

    if "city_id" in data:
        _sync_deal_city_text(db, d)
        if "city" not in changed and d.city:
            # city text обновлён из справочника
            pass

    hist_bits = []
    if "qualification" in changed:
        q_to = changed["qualification"]["to"]
        hist_bits.append(f"Квалификация: {QUALIFICATION_LABELS.get(q_to or '', q_to or '—')}")
    if "sales_manager_id" in changed:
        sm = db.query(User).filter(User.id == data.get("sales_manager_id")).first() if data.get("sales_manager_id") else None
        hist_bits.append(f"Менеджер продаж: {_user_display_name(sm) or '—'}")
    if "project_manager_id" in changed:
        pm = db.query(User).filter(User.id == data.get("project_manager_id")).first() if data.get("project_manager_id") else None
        hist_bits.append(f"Менеджер проекта: {_user_display_name(pm) or '—'}")
    for bit in hist_bits:
        db.add(DealHistory(deal_id=deal_id, action_text=bit))

    if changed:
        audit.write_audit(
            db, user_id=user.id, entity_type="deal", entity_id=deal_id,
            action="update", diff=changed, ip=audit.request_ip(request),
        )
    db.commit()
    return {"status": "success"}


@app.post("/api/deals/{deal_id}/archive")
def archive_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    d.is_archived = True
    db.add(DealHistory(deal_id=deal_id, action_text="Сделка архивирована"))
    db.commit()
    return {"status": "success"}


@app.post("/api/deals/{deal_id}/unarchive")
def unarchive_deal(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    d.is_archived = False
    db.add(DealHistory(deal_id=deal_id, action_text="Сделка восстановлена из архива"))
    db.commit()
    return {"status": "success"}


@app.delete("/api/deals/{deal_id}")
def soft_delete_deal(
    request: Request,
    deal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Переместить сделку/смету в корзину (soft-delete)."""
    d = _not_deleted(db.query(Deal), Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    _soft_delete_entity(d, user)
    # Связанные активные задачи тоже в корзину
    for t in _not_deleted(db.query(Task), Task).filter(Task.deal_id == deal_id).all():
        _soft_delete_entity(t, user)
    db.add(DealHistory(deal_id=deal_id, action_text="Сделка перемещена в корзину"))
    audit.write_audit(
        db,
        user_id=user.id,
        entity_type="deal",
        entity_id=deal_id,
        action="soft_delete",
        diff={"title": d.title},
        ip=audit.request_ip(request),
    )
    db.commit()
    return {"status": "success", "trashed": True}


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
        creator_user_id=user.id,
    )
    _notify_user(
        db, emp.id,
        kind="staff_assign",
        title=f"Вас назначили на проект «{deal.title}»",
        body=(body.role_name or "Участник команды") + (f" — {body.note}" if body.note else ""),
        link=f"/crm?deal={deal.id}",
        deal_id=deal.id,
        task_id=row.task_id,
        skip_user_id=user.id,
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "status": "success",
        "attachment_url": next((a.url for a in deal.attachments if a.id == row.attachment_id), None),
        "task_id": row.task_id,
    }


def _find_departure_task_id(db: Session, deal_id: int, user_id: int) -> Optional[int]:
    """Fallback: задача «Выезд» по сделке + исполнителю (если task_id на назначении пуст)."""
    row = (
        db.query(Task.id)
        .join(TaskAssignee, TaskAssignee.task_id == Task.id)
        .filter(
            Task.deal_id == deal_id,
            TaskAssignee.user_id == user_id,
            Task.title.like("Выезд:%"),
        )
        .order_by(Task.id.desc())
        .first()
    )
    return row[0] if row else None


@app.delete("/api/deals/{deal_id}/staff/{assignment_id}")
def api_unassign_staff(deal_id: int, assignment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.query(DealStaffAssignment).filter(
        DealStaffAssignment.id == assignment_id,
        DealStaffAssignment.deal_id == deal_id,
    ).first()
    if not row:
        return JSONResponse(status_code=404, content={"error": "Назначение не найдено"})
    name = (row.user.full_name or row.user.username) if row.user else "—"
    emp_id = row.user_id
    task_id = row.task_id or _find_departure_task_id(db, deal_id, emp_id)

    # Отменить авто-напоминание «завтра выезд» для этого сотрудника
    if emp_id:
        reminders = db.query(Activity).filter(
            Activity.deal_id == deal_id,
            Activity.type == "reminder",
            Activity.assignee_id == emp_id,
            Activity.status == "planned",
        ).all()
        for act in reminders:
            title = act.title or ""
            if "выезд" in title.lower() or (name != "—" and name in title):
                db.delete(act)

    row.task_id = None
    db.delete(row)
    _soft_delete_task(db, task_id, user)
    db.add(DealHistory(deal_id=deal_id, action_text=f"Снят с проекта: {name}"))
    db.commit()
    return {"status": "success", "deleted_task_id": task_id}


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
    generate_technichka_docx(_build_technichka_context(d, who, db=db), temp_path)
    _register_deal_document(
        db, d.id, "technichka",
        filename=f"Technichka_{d.id}.docx", created_by=who,
    )

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
def update_payroll_line(
    line_id: int,
    body: PayrollLineUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if _user_hide_payroll(user):
        return JSONResponse(status_code=403, content={"error": "Нет доступа к ведомости"})
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
    audit.write_audit(
        db, user_id=user.id, entity_type="payroll", entity_id=line.deal_id,
        action="payroll_update",
        diff={"line_id": line_id, "fields": list(data.keys()), "gross": line.gross},
        ip=audit.request_ip(request),
    )
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
        if _stage_is_won(s) or _stage_is_lost(s)
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
    label = CHANNEL_LABELS.get(channel, channel)
    phone = ""
    if channel == "whatsapp":
        raw = chat_id.split("@")[0]
        phone = f"+{raw}" if raw.isdigit() else ""
    if not company:
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
    elif phone and not (company.phone or "").strip():
        company.phone = phone
        db.commit()

    # Контакт: имя/телефон из чата (квалификацию менеджер ставит вручную)
    contact = None
    if company.id:
        contact = (
            db.query(Contact)
            .filter(Contact.company_id == company.id, Contact.is_primary == True)  # noqa: E712
            .first()
        )
        if not contact and phone:
            contact = db.query(Contact).filter(
                Contact.company_id == company.id, Contact.phone == phone
            ).first()
        if not contact:
            contact = Contact(
                name=sender_name or company.name or f"Контакт {label}",
                phone=phone or company.phone or None,
                company_id=company.id,
                is_primary=True,
            )
            db.add(contact)
            db.commit()
            db.refresh(contact)
        else:
            if sender_name and not (contact.name or "").strip():
                contact.name = sender_name
            if phone and not (contact.phone or "").strip():
                contact.phone = phone
            db.commit()

    pipeline, route_assignee = _resolve_pipeline_for_source(db, channel)
    first_stage = _first_stage(db, pipeline.id) if pipeline else None

    prev_deal = linked[0] if linked else None
    assignee_id = route_assignee or _default_assignee_id(db)
    default_city = _default_city(db)
    deal = Deal(
        title=f"Заявка из {label} — {sender_name or chat_id}",
        company_id=company.id,
        contact_id=contact.id if contact else None,
        pipeline_id=pipeline.id if pipeline else 1,
        stage=first_stage.id if first_stage else 1,
        event_date="",
        city=default_city.name if default_city else "Алматы",
        city_id=default_city.id if default_city else None,
        chat_channel=channel,
        chat_id=chat_id,
        prev_deal_id=prev_deal.id if prev_deal else None,
        source=channel,
        assignee_id=assignee_id,
        sales_manager_id=assignee_id,
        qualification=None,  # менеджер квалифицирует вручную
    )
    db.add(deal)
    db.commit()
    db.refresh(deal)

    db.add(DealHistory(deal_id=deal.id, action_text=f"Сделка создана автоматически: входящее сообщение в {label}"))
    if contact:
        db.add(DealHistory(
            deal_id=deal.id,
            action_text=f"Контакт из чата: {contact.name or '—'} {contact.phone or ''}".strip(),
        ))
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


# -- WhatsApp Web bridge (whatsapp-web.js на VPS) --
@app.post("/api/webhooks/whatsapp-web")
def wa_web_webhook(request: Request, event: dict, db: Session = Depends(get_db)):
    """Входящие от self-hosted WhatsApp Web bridge → Inbox + автосделка.

    Payload: {event: "message"|"status", chat_id, text, sender_name, ...}
    Auth: заголовок X-API-Key = WA_WEB_API_KEY.
    """
    if not _wa_web_key_ok(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    ev = (event.get("event") or "message").lower()
    if ev == "status":
        # статусные пинги — только лог, без Inbox
        print("wa-web status:", event.get("status"), event.get("phone") or "")
        return {"status": "ok"}

    if ev != "message":
        return {"status": "ignored"}

    chat_id = str(event.get("chat_id") or event.get("chatId") or "").strip()
    text_msg = str(event.get("text") or event.get("body") or "").strip()
    if not chat_id or not text_msg:
        return {"status": "ignored"}
    sender = str(event.get("sender_name") or event.get("sender") or chat_id.split("@")[0])
    try:
        ensure_deal_for_chat(db, "whatsapp", chat_id, sender_name=sender)
    except Exception as e:
        print("ensure_deal_for_chat (wa-web) error:", e)
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


COMPANY_CHAT_TITLE = "Чат компании"


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


def _sync_company_chat_members(db: Session, chat: InternalChat) -> None:
    """Все активные сотрудники — участники чата компании."""
    for u in db.query(User).all():
        _ensure_chat_member(db, chat.id, u.id)
    if chat.title != COMPANY_CHAT_TITLE:
        chat.title = COMPANY_CHAT_TITLE


def _ensure_company_chat(db: Session, user: User) -> InternalChat:
    """Один общий чат компании; при открытии синхронизируем всех сотрудников."""
    chat = (
        db.query(InternalChat)
        .filter(InternalChat.chat_type == "company")
        .order_by(InternalChat.id.asc())
        .first()
    )
    if not chat:
        chat = InternalChat(
            chat_type="company",
            title=COMPANY_CHAT_TITLE,
            created_by_id=user.id,
            updated_at=datetime.utcnow(),
        )
        db.add(chat)
        db.flush()
    _sync_company_chat_members(db, chat)
    db.commit()
    db.refresh(chat)
    return chat


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
    elif chat.chat_type == "company":
        title = COMPANY_CHAT_TITLE

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
    # Гарантируем «Чат компании» и членство всех сотрудников при каждом открытии списка
    _ensure_company_chat(db, user)
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
    # Компанейский чат — всегда сверху
    chats.sort(key=lambda c: (0 if c.chat_type == "company" else 1, -(c.updated_at.timestamp() if c.updated_at else 0)))
    return [_chat_to_dict(db, c, user.id) for c in chats]


@app.post("/api/internal-chats/company")
def open_company_chat(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Открыть/создать общий чат компании со всеми сотрудниками."""
    chat = _ensure_company_chat(db, user)
    return _chat_to_dict(db, chat, user.id)


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


@app.delete("/api/internal-chats/messages/{message_id}")
def delete_internal_message(
    message_id: int,
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
    if msg.sender_id != user.id and user.role != "admin":
        return JSONResponse(status_code=403, content={"error": "Удалить может автор или администратор"})
    db.delete(msg)
    db.commit()
    return {"status": "ok", "id": message_id}


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
    header = _estimate_header_fields(d)
    letterhead = _get_company_letterhead(db)

    context = doc_templates.apply_template_to_context({
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        **header,
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
        "discount_percentage": d.discount_percentage,
        "our_company_name": letterhead.get("company_name") or "Intro Show",
        "our_company_phone": letterhead.get("company_phone") or "",
        "our_company_email": letterhead.get("company_email") or "",
        "our_company_address": letterhead.get("company_address") or "",
        "our_company_bin": letterhead.get("company_bin") or "",
        # доступны в Word-шаблоне как tpl_body_notes / tpl_footer_notes / tpl_custom_title
        "body_notes": "",
        "footer_notes": "",
    }, db, "contract")
    context["body_notes"] = context.get("tpl_body_notes") or ""
    context["footer_notes"] = context.get("tpl_footer_notes") or ""
    context["custom_title"] = context.get("tpl_custom_title") or ""

    template_path = CONTRACT_TEMPLATE_PATH
    fd, temp_path = tempfile.mkstemp(suffix=".docx")
    os.close(fd)

    generate_contract(context, template_path, temp_path)
    _register_deal_document(
        db, d.id, "contract",
        filename=f"Contract_{d.id}_{comp.name}.docx",
    )

    def cleanup_file(path: str):
        try: os.remove(path)
        except: pass
            
    background_tasks.add_task(cleanup_file, temp_path)
    
    return FileResponse(
        temp_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"Contract_{d.id}_{comp.name}.docx"
    )

def _normalize_estimate_mode(mode: str) -> str:
    """
    Режимы сметы для сотрудников:
      - internal — внутренняя (субаренда отдельно, маржа)
      - client — клиенту без цен за ед. (только суммы строк / итог)
      - client_priced — клиенту с ценами за ед. (без маржи / себестоимости)
    hide_prices у пользователя — флаг доступа в CRM, не путать с режимом документа.
    """
    mode_norm = (mode or "internal").strip().lower()
    aliases = {
        "client_no_prices": "client",
        "client_without_prices": "client",
        "без_цен": "client",
        "client_with_prices": "client_priced",
        "с_ценами": "client_priced",
        "priced": "client_priced",
    }
    mode_norm = aliases.get(mode_norm, mode_norm)
    if mode_norm not in ("internal", "client", "client_priced"):
        mode_norm = "internal"
    return mode_norm


@app.get("/api/deals/{deal_id}/estimate")
def download_deal_estimate(
    deal_id: int,
    background_tasks: BackgroundTasks,
    mode: str = "internal",
    db: Session = Depends(get_db),
):
    """Скачивание сметы .docx: mode=internal|client|client_priced."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mode_norm = _normalize_estimate_mode(mode)

    # Клиентская: субаренда как обычные позиции (без отдельного блока); internal — полный вид
    context = _build_estimate_context(d, mode_norm, db=db)

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
    if mode_norm == "client":
        fname = f"Smeta_client_bez_cen_CRM-{d.id}.docx"
    elif mode_norm == "client_priced":
        fname = f"Smeta_client_s_cenami_CRM-{d.id}.docx"
    else:
        fname = f"Smeta_vnutr_CRM-{d.id}.docx"
    _register_deal_document(db, d.id, _estimate_doc_type(mode_norm, False), filename=fname)
    return FileResponse(
        temp_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=fname,
    )


def _build_estimate_context(d: Deal, mode_norm: str, db: Session = None) -> dict:
    # Не исключаем субаренду: в client document_generator кладёт её в основную таблицу
    # (без блока «Субаренда» / себестоимости). hide_subrental_section управляет только вёрсткой.
    result = _calc_deal(d, exclude_subrental=False)
    header = _estimate_header_fields(d)
    is_client = mode_norm in ("client", "client_priced")
    letterhead = {}
    if db is not None:
        letterhead = _get_company_letterhead(db)
    else:
        try:
            with Session(engine) as s:
                letterhead = _get_company_letterhead(s)
        except Exception:
            letterhead = {}
    logo_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static", "img", "introshow_logo.png",
    )
    ctx = {
        "number": f"CRM-{d.id}",
        "date": datetime.today().strftime("%d.%m.%Y"),
        **header,
        "letterhead": letterhead,
        "our_company_name": letterhead.get("company_name") or "Intro Show",
        "our_company_phone": letterhead.get("company_phone") or "",
        "our_company_email": letterhead.get("company_email") or "",
        "our_company_address": letterhead.get("company_address") or "",
        "our_company_bin": letterhead.get("company_bin") or "",
        "logo_path": logo_path if os.path.isfile(logo_path) else None,
        "manager_phone": letterhead.get("company_phone") or "",
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
        "hide_subrental_section": is_client,
    }
    doc_type = {
        "internal": "estimate_internal",
        "client": "estimate_client",
        "client_priced": "estimate_client_priced",
    }.get(mode_norm, "estimate_internal")
    return doc_templates.apply_template_to_context(ctx, db, doc_type)


@app.get("/api/deals/{deal_id}/estimate.pdf")
def download_deal_estimate_pdf(
    deal_id: int,
    background_tasks: BackgroundTasks,
    mode: str = "internal",
    db: Session = Depends(get_db),
):
    """Скачивание сметы .pdf: mode=internal|client|client_priced."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})

    mode_norm = _normalize_estimate_mode(mode)

    from document_generator import generate_estimate_pdf
    context = _build_estimate_context(d, mode_norm, db=db)
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    generate_estimate_pdf(context, temp_path, mode=mode_norm)

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except OSError:
            pass

    background_tasks.add_task(cleanup_file, temp_path)
    if mode_norm == "client":
        fname = f"Smeta_client_bez_cen_CRM-{d.id}.pdf"
    elif mode_norm == "client_priced":
        fname = f"Smeta_client_s_cenami_CRM-{d.id}.pdf"
    else:
        fname = f"Smeta_vnutr_CRM-{d.id}.pdf"
    _register_deal_document(db, d.id, _estimate_doc_type(mode_norm, True), filename=fname)
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
    header = _estimate_header_fields(d)
    letterhead = _get_company_letterhead(db)
    logo_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "static", "img", "introshow_logo.png",
    )
    context = doc_templates.apply_template_to_context({
        "contract_number": f"CRM-{d.id}",
        "contract_date": datetime.today().strftime("%d.%m.%Y"),
        **header,
        "company_name": comp.name,
        "director_name": comp.director_name,
        "iin_bin": comp.bin,
        "iban": comp.requisites,
        "event_name": d.title,
        "event_date": d.event_date,
        "event_address": d.event_address,
        "our_company_name": letterhead.get("company_name") or "Intro Show",
        "our_company_phone": letterhead.get("company_phone") or "",
        "our_company_email": letterhead.get("company_email") or "",
        "our_company_address": letterhead.get("company_address") or "",
        "our_company_bin": letterhead.get("company_bin") or "",
        "logo_path": logo_path if os.path.isfile(logo_path) else None,
        "manager_phone": letterhead.get("company_phone") or "",
        "items": result["items"],
        "equipment_total": result["equipment_total"],
        "fixed_total": result["fixed_total"],
        "grand_total": result["grand_total"],
        "discount_percentage": d.discount_percentage or 0,
        "tax_percentage": result.get("tax_percentage", FIXED_TAX_PERCENTAGE),
        "tax_amount": result.get("tax_amount", 0),
    }, db, "contract")

    from document_generator import generate_contract_pdf
    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    generate_contract_pdf(context, temp_path)
    _register_deal_document(
        db, d.id, "contract_pdf",
        filename=f"Contract_{d.id}_{comp.name}.pdf",
    )

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


# -----------------
# v2: Today / Notifications / Ops / Templates / Client pack
# -----------------

class OpsStatusUpdate(BaseModel):
    ops_status: str


@app.put("/api/deals/{deal_id}/ops-status")
def update_deal_ops_status(
    deal_id: int, body: OpsStatusUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    status = (body.ops_status or "none").strip()
    if status not in OPS_STATUS_LABELS:
        return JSONResponse(status_code=400, content={"error": "Неизвестный статус отгрузки"})
    old = getattr(d, "ops_status", None) or "none"
    d.ops_status = status
    if old != status:
        db.add(DealHistory(
            deal_id=deal_id,
            action_text=f"Отгрузка: {OPS_STATUS_LABELS.get(old, old)} → {OPS_STATUS_LABELS[status]}",
        ))
    db.commit()
    return {"status": "success", "ops_status": status, "ops_status_label": OPS_STATUS_LABELS[status]}


class SubrentalStatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


@app.put("/api/deal-items/{item_id}/subrental-status")
def update_deal_item_subrental_status(
    item_id: int,
    body: SubrentalStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Смена статуса субаренды: reserved / issued / returned."""
    di = db.query(DealItem).filter(DealItem.id == item_id).first()
    if not di:
        return JSONResponse(status_code=404, content={"error": "Позиция не найдена"})
    eq = di.equipment
    wtype = (getattr(eq, "warehouse_type", None) or "own") if eq else "own"
    if wtype != "subrental":
        return JSONResponse(status_code=400, content={"error": "Статус субаренды только для позиций внешнего склада"})
    status = (body.status or "").strip().lower()
    if status not in SUBRENTAL_STATUS_LABELS:
        return JSONResponse(status_code=400, content={"error": "Статус: reserved / issued / returned"})

    old = getattr(di, "subrental_status", None) or "reserved"
    now = datetime.utcnow()
    di.subrental_status = status
    if body.note is not None:
        note = (body.note or "").strip()
        di.subrental_note = note[:500] if note else None

    if status == "reserved":
        di.issued_at = None
        di.issued_by_id = None
        di.returned_at = None
        di.returned_by_id = None
    elif status == "issued":
        di.issued_at = getattr(di, "issued_at", None) or now
        di.issued_by_id = user.id
        di.returned_at = None
        di.returned_by_id = None
    elif status == "returned":
        if not getattr(di, "issued_at", None):
            di.issued_at = now
            di.issued_by_id = di.issued_by_id or user.id
        di.returned_at = now
        di.returned_by_id = user.id

    eq_name = eq.name if eq else f"#{di.equipment_id}"
    who = _user_display_name(user) or "—"
    if old != status:
        db.add(DealHistory(
            deal_id=di.deal_id,
            action_text=(
                f"Субаренда «{eq_name}»: "
                f"{SUBRENTAL_STATUS_LABELS.get(old, old)} → {SUBRENTAL_STATUS_LABELS[status]} "
                f"({who})"
            ),
        ))
    audit.write_audit(
        db, user_id=user.id, entity_type="deal", entity_id=di.deal_id,
        action="subrental_status",
        diff={
            "deal_item_id": di.id,
            "equipment_id": di.equipment_id,
            "equipment_name": eq_name,
            "from": old,
            "to": status,
            "note": di.subrental_note or "",
        },
        ip=audit.request_ip(request),
    )
    db.commit()
    db.refresh(di)
    payload = {
        "status": "success",
        "id": di.id,
        "deal_id": di.deal_id,
        "equipment_id": di.equipment_id,
        "name": eq_name,
        "quantity": di.quantity,
        "supplier": getattr(eq, "supplier", None) if eq else None,
    }
    payload.update(_serialize_deal_item_subrental(di))
    return payload


@app.get("/api/deals/{deal_id}/client-pack")
def deal_client_pack(deal_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Ссылки клиентского пакета: смета + договор (PDF). Без ЭЦП."""
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    if _user_hide_prices(user):
        return JSONResponse(status_code=403, content={"error": "Нет доступа к суммам"})
    base = f"/api/deals/{deal_id}"
    estimate_pdf = f"{base}/estimate.pdf?mode=client"
    estimate_priced_pdf = f"{base}/estimate.pdf?mode=client_priced"
    contract_pdf = f"{base}/contract.pdf"
    return {
        "deal_id": deal_id,
        "title": d.title,
        "company_name": d.company.name if d.company else "",
        "estimate_pdf": estimate_pdf,
        "estimate_pdf_priced": estimate_priced_pdf,
        "estimate_docx": f"{base}/estimate?mode=client",
        "estimate_docx_priced": f"{base}/estimate?mode=client_priced",
        "contract_pdf": contract_pdf,
        "contract_docx": f"{base}/contract",
        "modes_hint": "client = без цен за ед.; client_priced = с ценами (для сотрудников / клиента)",
        "message": (
            f"Добрый день! По проекту «{d.title}» направляем смету и договор.\n"
            f"Смета без цен (PDF): {estimate_pdf}\n"
            f"Смета с ценами (PDF): {estimate_priced_pdf}\n"
            f"Договор (PDF): {contract_pdf}"
        ),
    }


@app.get("/api/today")
def api_today(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Сводка «Сегодня»: назначения/техничка, задачи, непрочитанные чаты."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    uname = user.full_name or user.username
    active_city = _resolve_active_city(db, user, request)

    staff_rows = (
        db.query(DealStaffAssignment)
        .filter(DealStaffAssignment.user_id == user.id)
        .order_by(DealStaffAssignment.id.desc())
        .limit(80)
        .all()
    )
    assignments = []
    for s in staff_rows:
        deal = s.deal
        if not deal or deal.is_archived:
            continue
        if active_city and getattr(deal, "city_id", None) not in (None, active_city.id):
            continue
        date_key = deal.setup_date or deal.event_date or ""
        day = "today" if date_key == today else ("tomorrow" if date_key == tomorrow else None)
        if day is None and date_key not in (today, tomorrow):
            # всё равно покажем ближайшие 7 дней и без даты (свежие назначения)
            if date_key:
                try:
                    dd = datetime.strptime(date_key[:10], "%Y-%m-%d")
                    if dd.date() < datetime.utcnow().date() or dd.date() > (datetime.utcnow() + timedelta(days=7)).date():
                        continue
                except ValueError:
                    pass
            elif s.created_at and (datetime.utcnow() - s.created_at).days > 14:
                continue
            day = "soon"
        assignments.append({
            "assignment_id": s.id,
            "deal_id": deal.id,
            "deal_title": deal.title,
            "role_name": s.role_name or "",
            "setup_date": deal.setup_date or "",
            "event_date": deal.event_date or "",
            "address": deal.event_address or "",
            "ops_status": getattr(deal, "ops_status", None) or "none",
            "ops_status_label": OPS_STATUS_LABELS.get(getattr(deal, "ops_status", None) or "none", "—"),
            "technichka_url": f"/api/deals/{deal.id}/technichka",
            "task_id": s.task_id,
            "day": day or "soon",
        })

    my_tasks = []
    for t in db.query(Task).filter(Task.status.in_(["open", "in_progress"])).order_by(Task.id.desc()).limit(120).all():
        is_mine = False
        if t.assignee and uname and t.assignee == uname:
            is_mine = True
        for a in (t.assignees or []):
            if a.user_id == user.id or (a.name and a.name == uname):
                is_mine = True
                break
        if not is_mine:
            continue
        due = (t.due_date or "")[:10]
        my_tasks.append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "due_date": t.due_date or "",
            "priority": t.priority or "normal",
            "deal_id": t.deal_id,
            "due_bucket": "today" if due == today else ("tomorrow" if due == tomorrow else ("overdue" if due and due < today else "later")),
        })
        if len(my_tasks) >= 40:
            break

    unread_chats = []
    memberships = db.query(InternalChatMember).filter(InternalChatMember.user_id == user.id).all()
    for m in memberships:
        chat = m.chat
        if not chat:
            continue
        last = (
            db.query(InternalMessage)
            .filter(InternalMessage.chat_id == chat.id)
            .order_by(InternalMessage.id.desc())
            .first()
        )
        if not last:
            continue
        unread = max(0, (last.id or 0) - (m.last_read_message_id or 0))
        if unread <= 0:
            continue
        unread_chats.append({
            "chat_id": chat.id,
            "title": chat.title or ("Сделка #" + str(chat.deal_id) if chat.deal_id else "Чат"),
            "chat_type": chat.chat_type,
            "deal_id": chat.deal_id,
            "unread": unread,
            "last_text": (last.text or "")[:120],
        })
    unread_chats.sort(key=lambda x: -x["unread"])

    # Субаренда сегодня / завтра: сделки с датой монтажа/ивента и позициями subrental
    sub_deals = (
        _not_deleted(db.query(Deal), Deal)
        .filter(
            Deal.is_archived == False,  # noqa: E712
            ((Deal.setup_date.in_([today, tomorrow])) | (Deal.event_date.in_([today, tomorrow]))),
        )
        .order_by(Deal.setup_date.asc(), Deal.id.desc())
        .limit(80)
        .all()
    )
    subrentals_today = []
    for deal in sub_deals:
        sub_items = []
        for di in (deal.items or []):
            eq = di.equipment
            if not eq or (getattr(eq, "warehouse_type", None) or "own") != "subrental":
                continue
            st = getattr(di, "subrental_status", None) or "reserved"
            sub_items.append({
                "id": di.id,
                "name": eq.name,
                "quantity": di.quantity,
                "supplier": getattr(eq, "supplier", None) or "",
                "subrental_status": st,
                "subrental_status_label": SUBRENTAL_STATUS_LABELS.get(st, st),
                "issued_by_name": _user_display_name(getattr(di, "issued_by", None)),
                "issued_at": _fmt_dt(getattr(di, "issued_at", None)),
                "returned_by_name": _user_display_name(getattr(di, "returned_by", None)),
                "returned_at": _fmt_dt(getattr(di, "returned_at", None)),
            })
        if not sub_items:
            continue
        if deal.setup_date == today or deal.event_date == today:
            day = "today"
        elif deal.setup_date == tomorrow or deal.event_date == tomorrow:
            day = "tomorrow"
        else:
            day = "soon"
        assignee_name = ""
        if deal.assignee:
            assignee_name = _user_display_name(deal.assignee)
        elif getattr(deal, "project_manager", None):
            assignee_name = _user_display_name(deal.project_manager)
        reserved_n = sum(1 for x in sub_items if x["subrental_status"] == "reserved")
        issued_n = sum(1 for x in sub_items if x["subrental_status"] == "issued")
        returned_n = sum(1 for x in sub_items if x["subrental_status"] == "returned")
        subrentals_today.append({
            "deal_id": deal.id,
            "deal_title": deal.title,
            "setup_date": deal.setup_date or "",
            "event_date": deal.event_date or "",
            "day": day,
            "assignee_name": assignee_name or "—",
            "ops_status": getattr(deal, "ops_status", None) or "none",
            "ops_status_label": OPS_STATUS_LABELS.get(getattr(deal, "ops_status", None) or "none", "—"),
            "items": sub_items,
            "items_summary": ", ".join(f"{x['name']} ×{x['quantity']}" for x in sub_items[:6])
                + ("…" if len(sub_items) > 6 else ""),
            "suppliers": ", ".join(sorted({x["supplier"] for x in sub_items if x["supplier"]})) or "—",
            "counts": {"reserved": reserved_n, "issued": issued_n, "returned": returned_n, "total": len(sub_items)},
            "crm_url": f"/crm?deal={deal.id}",
        })

    return {
        "today": today,
        "tomorrow": tomorrow,
        "assignments": assignments,
        "tasks": my_tasks,
        "unread_chats": unread_chats[:30],
        "subrentals": subrentals_today,
        "counts": {
            "assignments": len(assignments),
            "tasks": len(my_tasks),
            "unread_chats": len(unread_chats),
            "subrentals": len(subrentals_today),
        },
    }


# Роботы напоминаний: троттлинг между опросами колокольчика
_ROBOTS_LAST_RUN = 0.0
_ROBOTS_LOCK = __import__("threading").Lock()


def _notify_once(
    db: Session,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    link: str = None,
    deal_id: int = None,
    task_id: int = None,
    dedupe_hours: int = 20,
) -> bool:
    """Создать уведомление, если такого же kind+entity не было недавно."""
    if not user_id:
        return False
    since = datetime.utcnow() - timedelta(hours=dedupe_hours)
    q = db.query(AppNotification).filter(
        AppNotification.user_id == user_id,
        AppNotification.kind == kind,
        AppNotification.created_at >= since,
    )
    if deal_id:
        q = q.filter(AppNotification.deal_id == deal_id)
    if task_id:
        q = q.filter(AppNotification.task_id == task_id)
    if q.first():
        return False
    db.add(AppNotification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        link=link,
        deal_id=deal_id,
        task_id=task_id,
    ))
    return True


def _run_reminder_robots(db: Session) -> dict:
    """
    P1-роботы (без visual builder):
      - сделка без движения >24ч → напоминание ответственному
      - просроченные задачи → эскалация постановщику / исполнителю
    Вызывается из /api/notifications с троттлингом ~5 мин.
    """
    global _ROBOTS_LAST_RUN
    now_ts = datetime.utcnow().timestamp()
    with _ROBOTS_LOCK:
        if now_ts - _ROBOTS_LAST_RUN < 300:
            return {"skipped": True}
        _ROBOTS_LAST_RUN = now_ts

    created = {"stale_deals": 0, "overdue_tasks": 0}
    cutoff = datetime.utcnow() - timedelta(hours=24)
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    try:
        active_deals = (
            _not_deleted(db.query(Deal), Deal)
            .filter(Deal.is_archived == False)  # noqa: E712
            .order_by(Deal.id.desc())
            .limit(200)
            .all()
        )
        for d in active_deals:
            st = db.query(Stage).filter(Stage.id == d.stage).first()
            if st and (_stage_is_won(st) or _stage_is_lost(st)):
                continue
            last = d.created_at or cutoff
            hist = (
                db.query(DealHistory)
                .filter(DealHistory.deal_id == d.id)
                .order_by(DealHistory.created_at.desc())
                .first()
            )
            if hist and hist.created_at and hist.created_at > last:
                last = hist.created_at
            if last and last > cutoff:
                continue
            uid = d.assignee_id
            if not uid:
                continue
            if _notify_once(
                db,
                user_id=uid,
                kind="stale_deal",
                title="Сделка без движения >24ч",
                body=f"Не забудьте про сделку «{d.title}» — нет активности больше суток.",
                link=f"/crm?deal={d.id}",
                deal_id=d.id,
            ):
                created["stale_deals"] += 1

        overdue_tasks = (
            db.query(Task)
            .filter(
                Task.status.in_(["open", "in_progress"]),
                Task.due_date.isnot(None),
                Task.due_date < today_str,
            )
            .order_by(Task.id.desc())
            .limit(80)
            .all()
        )
        for t in overdue_tasks:
            recipients = set()
            if t.creator_id:
                recipients.add(t.creator_id)
            for a in (t.assignees or []):
                if a.user_id:
                    recipients.add(a.user_id)
            for uid in recipients:
                if _notify_once(
                    db,
                    user_id=uid,
                    kind="overdue_task",
                    title="Просроченная задача",
                    body=f"«{t.title}» — срок {t.due_date}",
                    link=f"/tasks?task={t.id}",
                    task_id=t.id,
                    deal_id=t.deal_id,
                ):
                    created["overdue_tasks"] += 1

        if created["stale_deals"] or created["overdue_tasks"]:
            db.commit()
    except Exception:
        db.rollback()
        return {"error": "robots_failed"}

    return created


@app.get("/api/notifications")
def list_notifications(
    unread_only: bool = False,
    limit: int = 40,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Лёгкие роботы-напоминания на опросе колокольчика (троттлинг внутри)
    try:
        _run_reminder_robots(db)
    except Exception:
        pass

    q = db.query(AppNotification).filter(AppNotification.user_id == user.id)
    if unread_only:
        q = q.filter(AppNotification.is_read == False)  # noqa: E712
    rows = q.order_by(AppNotification.id.desc()).limit(max(1, min(limit, 100))).all()
    unread_count = (
        db.query(AppNotification)
        .filter(AppNotification.user_id == user.id, AppNotification.is_read == False)  # noqa: E712
        .count()
    )
    return {
        "unread_count": unread_count,
        "items": [{
            "id": n.id,
            "kind": n.kind,
            "title": n.title,
            "body": n.body or "",
            "link": n.link or "",
            "deal_id": n.deal_id,
            "task_id": n.task_id,
            "is_read": bool(n.is_read),
            "created_at": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "",
        } for n in rows],
    }


@app.post("/api/notifications/read")
def mark_notifications_read(
    body: dict = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    body = body or {}
    ids = body.get("ids")
    q = db.query(AppNotification).filter(
        AppNotification.user_id == user.id,
        AppNotification.is_read == False,  # noqa: E712
    )
    if ids:
        q = q.filter(AppNotification.id.in_(list(ids)))
    updated = 0
    for n in q.all():
        n.is_read = True
        updated += 1
    db.commit()
    return {"status": "success", "updated": updated}


@app.get("/api/estimate-templates")
def list_estimate_templates(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(EstimateTemplate).order_by(EstimateTemplate.id.desc()).all()
    return [{
        "id": t.id, "name": t.name, "description": t.description or "",
        "items": t.items_json or [], "created_by": t.created_by or "",
        "created_at": t.created_at.strftime("%Y-%m-%d") if t.created_at else "",
    } for t in rows]


class EstimateTemplateIn(BaseModel):
    name: str
    description: Optional[str] = None
    items: List[dict] = []


@app.post("/api/estimate-templates")
def create_estimate_template(
    body: EstimateTemplateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    name = (body.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название"})
    t = EstimateTemplate(
        name=name,
        description=(body.description or "").strip() or None,
        items_json=body.items or [],
        created_by=user.full_name or user.username,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "status": "success"}


@app.post("/api/estimate-templates/{template_id}/apply/{deal_id}")
def apply_estimate_template(
    template_id: int, deal_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    t = db.query(EstimateTemplate).filter(EstimateTemplate.id == template_id).first()
    d = db.query(Deal).filter(Deal.id == deal_id).first()
    if not t or not d:
        return JSONResponse(status_code=404, content={"error": "Не найдено"})
    added = 0
    for it in (t.items_json or []):
        eq_id = it.get("equipment_id")
        if not eq_id:
            continue
        eq = db.query(Equipment).filter(Equipment.id == eq_id).first()
        if not eq:
            continue
        db.add(DealItem(
            deal_id=d.id,
            equipment_id=eq.id,
            quantity=int(it.get("quantity") or 1),
            days=int(it.get("days") or 1),
            price=it.get("price"),
        ))
        added += 1
    db.add(DealHistory(deal_id=d.id, action_text=f"Применён шаблон сметы «{t.name}» (+{added})"))
    db.commit()
    db.refresh(d)
    _recalc_deal_sum(db, d)
    return {"status": "success", "added": added, "final_sum": d.final_sum}


@app.get("/api/checklist-templates")
def list_checklist_templates(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(ChecklistTemplate).order_by(ChecklistTemplate.id.desc()).all()
    return [{
        "id": t.id, "name": t.name, "items": t.items_json or [],
        "created_by": t.created_by or "",
    } for t in rows]


class ChecklistTemplateIn(BaseModel):
    name: str
    items: List[str] = []


@app.post("/api/checklist-templates")
def create_checklist_template(
    body: ChecklistTemplateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    name = (body.name or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "Укажите название"})
    items = [x.strip() for x in (body.items or []) if (x or "").strip()]
    t = ChecklistTemplate(
        name=name,
        items_json=items,
        created_by=user.full_name or user.username,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "status": "success"}


@app.post("/api/checklist-templates/{template_id}/apply/{task_id}")
def apply_checklist_template(
    template_id: int, task_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    t = db.query(ChecklistTemplate).filter(ChecklistTemplate.id == template_id).first()
    task = db.query(Task).filter(Task.id == task_id).first()
    if not t or not task:
        return JSONResponse(status_code=404, content={"error": "Не найдено"})
    base = db.query(TaskChecklistItem).filter(TaskChecklistItem.task_id == task_id).count()
    added = 0
    for i, text_item in enumerate(t.items_json or []):
        text_item = (text_item or "").strip()
        if not text_item:
            continue
        db.add(TaskChecklistItem(
            task_id=task_id, text=text_item, is_done=False, sort_order=base + i,
        ))
        added += 1
    db.commit()
    return {"status": "success", "added": added}


@app.get("/api/search")
def api_search(q: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Лёгкий глобальный поиск: сделки, компании, задачи."""
    query = (q or "").strip()
    if len(query) < 2:
        return {"deals": [], "companies": [], "tasks": []}
    like = f"%{query}%"
    deals = (
        _not_deleted(db.query(Deal), Deal)
        .filter(Deal.title.ilike(like), Deal.is_archived == False)  # noqa: E712
        .limit(10)
        .all()
    )
    companies = db.query(Company).filter(Company.name.ilike(like)).limit(10).all()
    tasks = db.query(Task).filter(Task.title.ilike(like)).limit(10).all()
    return {
        "deals": [{"id": d.id, "title": d.title, "url": f"/crm?deal={d.id}"} for d in deals],
        "companies": [{"id": c.id, "title": c.name, "url": f"/companies?id={c.id}"} for c in companies],
        "tasks": [{"id": t.id, "title": t.title, "url": f"/tasks?open={t.id}"} for t in tasks],
    }


@app.get("/api/audit-logs")
def api_audit_logs(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Журнал аудита (только admin / manager)."""
    if not user or user.role not in ("admin", "manager"):
        return JSONResponse(status_code=403, content={"error": "Доступ только для администратора или менеджера"})
    q = db.query(AuditLog).order_by(AuditLog.id.desc())
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type.strip().lower())
    if entity_id is not None:
        q = q.filter(AuditLog.entity_id == entity_id)
    rows = q.limit(max(1, min(int(limit or 100), 500))).all()
    user_ids = {r.user_id for r in rows if r.user_id}
    names = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(list(user_ids))).all():
            names[u.id] = u.full_name or u.username
    return {
        "entity_labels": audit.ENTITY_LABELS,
        "action_labels": audit.ACTION_LABELS,
        "items": [{
            "id": r.id,
            "user_id": r.user_id,
            "user_name": names.get(r.user_id) or ("система" if not r.user_id else f"#{r.user_id}"),
            "entity_type": r.entity_type,
            "entity_label": audit.ENTITY_LABELS.get(r.entity_type, r.entity_type),
            "entity_id": r.entity_id,
            "action": r.action,
            "action_label": audit.ACTION_LABELS.get(r.action, r.action),
            "diff": r.diff,
            "ip": r.ip or "",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        } for r in rows],
    }
