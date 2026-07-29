from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, JSON, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime

import os
import shutil
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "rental_app.db")


def get_database_url() -> str:
    configured_url = os.environ.get("DATABASE_URL")
    if configured_url:
        return configured_url

    if os.environ.get("VERCEL"):
        db_path = os.path.join(tempfile.gettempdir(), "rental_app.db")
        if not os.path.exists(db_path) and os.path.exists(DEFAULT_DB_PATH):
            shutil.copyfile(DEFAULT_DB_PATH, db_path)
        return f"sqlite:///{db_path}"

    return f"sqlite:///{DEFAULT_DB_PATH}"


DATABASE_URL = get_database_url()


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user") # admin / manager / user
    full_name = Column(String, nullable=True)
    # Список разделов, доступных пользователю (JSON-массив ключей).
    # null или пустой список = доступ ко всем разделам (для admin всегда всё).
    permissions = Column(JSON, nullable=True)

class Folder(Base):
    __tablename__ = "folders"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    parent_id = Column(Integer, ForeignKey("folders.id"), nullable=True)

    parent = relationship("Folder", remote_side=[id], backref="subfolders")

class Equipment(Base):
    __tablename__ = "equipment"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    category = Column(String, index=True) # Звук, Свет, Экраны, Сцена, Логистика, Персонал, Расходники
    price = Column(Float, default=0.0) # Цена клиенту за единицу (KZT)
    cost_price = Column(Float, default=0.0)  # себестоимость (для субаренды — цена у поставщика)
    stock_quantity = Column(Integer, default=0) # Общее количество на складе
    status = Column(String, default="Доступно") # Доступно / В ремонте
    warehouse_type = Column(String, default="own")  # own — свой склад / subrental — субаренда
    supplier = Column(String, nullable=True)  # поставщик субаренды
    folder_id = Column(Integer, ForeignKey("folders.id"), nullable=True)
    description = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    
    # 3D Project & AI enrich fields
    weight = Column(Float, nullable=True) # Вес (кг)
    dimensions = Column(String, nullable=True) # Габариты (ШхВхГ)
    power_w = Column(Float, nullable=True) # Мощность (Вт)
    dispersion = Column(String, nullable=True) # Дисперсия / Угол раскрытия
    
    # Dynamic Custom Fields
    custom_fields = Column(JSON, default=dict)

    folder = relationship("Folder", backref="equipment")

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    bin = Column(String)
    director_name = Column(String)
    phone = Column(String)
    email = Column(String)
    requisites = Column(String) # IBAN etc.
    based_on = Column(String, default="Устава")
    address = Column(String)
    bank_name = Column(String)
    kbe = Column(String)
    bik = Column(String)
    telegram_chat_id = Column(String, nullable=True)
    instagram = Column(String, nullable=True)  # username в Instagram для перехода в Direct

    deals = relationship("Deal", back_populates="company")
    contacts = relationship("Contact", back_populates="company")


class Contact(Base):
    """Контактные лица, привязанные к компаниям (как в Битрикс24)."""
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    position = Column(String, nullable=True)     # должность
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    comment = Column(String, nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    is_primary = Column(Boolean, default=False)  # основной контакт компании
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company", back_populates="contacts")


class Pipeline(Base):
    __tablename__ = "pipelines"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    
    stages = relationship("Stage", back_populates="pipeline", cascade="all, delete-orphan")
    deals = relationship("Deal", back_populates="pipeline")

class Stage(Base):
    __tablename__ = "stages"
    id = Column(Integer, primary_key=True, index=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"))
    name = Column(String)
    order_index = Column(Integer, default=0)
    is_active_rent = Column(Boolean, default=False)
    
    pipeline = relationship("Pipeline", back_populates="stages")
    deals = relationship("Deal", back_populates="stage_obj")

class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=True)
    stage = Column(Integer, ForeignKey("stages.id"), default=1)
    setup_date = Column(String) # Выезд оборудования / монтаж
    event_date = Column(String) # Возврат оборудования / мероприятие
    event_address = Column(String) # Адрес площадки
    city = Column(String, nullable=True)  # Город проекта (шапка сметы)
    shifts = Column(Float, default=1.0)  # Смены / кол-во дней в шапке
    discount_percentage = Column(Float, default=0.0)
    tax_percentage = Column(Float, default=16.0)  # НДС/налог всегда 16%
    final_sum = Column(Float, default=0.0)
    comment = Column(String)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Привязка к чату мессенджера (для автосозданных сделок из входящих сообщений)
    chat_channel = Column(String, nullable=True)   # whatsapp / telegram / instagram
    chat_id = Column(String, nullable=True)
    prev_deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)  # прошлое обращение клиента
    source = Column(String, nullable=True)  # whatsapp / telegram / instagram / manual / referral / site / other
    loss_reason = Column(String, nullable=True)
    is_qualified = Column(Boolean, default=False)
    is_archived = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company", back_populates="deals")
    contact = relationship("Contact", foreign_keys=[contact_id])
    assignee = relationship("User", foreign_keys=[assignee_id])
    pipeline = relationship("Pipeline", back_populates="deals")
    stage_obj = relationship("Stage", back_populates="deals")
    items = relationship("DealItem", back_populates="deal")
    activities = relationship("Activity", back_populates="deal", cascade="all, delete-orphan")

class DealItem(Base):
    __tablename__ = "deal_items"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"))
    equipment_id = Column(Integer, ForeignKey("equipment.id"))
    quantity = Column(Integer, default=1)
    days = Column(Integer, default=1)
    price = Column(Float, nullable=True)  # цена в этой смете; null = текущая цена склада

    deal = relationship("Deal", back_populates="items")
    equipment = relationship("Equipment")

class CustomField(Base):
    __tablename__ = "custom_fields"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    field_type = Column(String) # string, number, boolean, date, link, etc.

class DealFieldValue(Base):
    __tablename__ = "deal_field_values"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"))
    field_id = Column(Integer, ForeignKey("custom_fields.id"))
    value = Column(String)

    deal = relationship("Deal", backref="custom_values")
    field = relationship("CustomField")

class DealHistory(Base):
    __tablename__ = "deal_history"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"))
    action_text = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="history")

class Project2D(Base):
    __tablename__ = "projects_2d"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), unique=True)
    layout_data_json = Column(String, default="[]")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="project_2d")

class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"))
    endpoint = Column(String)
    p256dh = Column(String)
    auth = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="push_subscriptions")

class BotSettings(Base):
    """Настройки AI чат-бота (одна строка). График работы, персона, каналы."""
    __tablename__ = "bot_settings"
    id = Column(Integer, primary_key=True, index=True)
    enabled = Column(Boolean, default=True)
    # {"mon": {"on": true, "start": "10:00", "end": "22:00"}, ...}
    schedule = Column(JSON, nullable=True)
    # {"whatsapp": true, "telegram": true, "instagram": true}
    channels = Column(JSON, nullable=True)
    persona = Column(String, nullable=True)          # описание "личности" бота
    off_hours_message = Column(String, nullable=True) # автоответ вне графика
    timezone = Column(String, default="Asia/Almaty")


class KnowledgeItem(Base):
    """База знаний чат-бота — по ней он отвечает клиентам."""
    __tablename__ = "knowledge_items"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    content = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ChatMessage(Base):
    """Единая лента сообщений всех мессенджеров (WhatsApp / Telegram / Instagram)."""
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    channel = Column(String, index=True)   # whatsapp / telegram / instagram
    chat_id = Column(String, index=True)   # идентификатор чата в канале
    sender_name = Column(String, nullable=True)
    direction = Column(String)             # in / out
    text = Column(String)
    is_bot = Column(Boolean, default=False)  # ответ отправлен ботом
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Task(Base):
    """Задачи сотрудников — логика как в Битрикс24."""
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(String, nullable=True)
    assignee = Column(String, nullable=True)    # ответственный (имя / username)
    created_by = Column(String, nullable=True)  # постановщик (имя, для отображения)
    creator_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # постановщик (id)
    due_date = Column(String, nullable=True)    # YYYY-MM-DD или YYYY-MM-DDTHH:MM
    priority = Column(String, default="normal") # low / normal / high
    status = Column(String, default="open")     # open / in_progress / done / deferred
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    tags = Column(String, nullable=True)        # теги через запятую
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="tasks")
    creator = relationship("User", foreign_keys=[creator_id])
    comments = relationship("TaskComment", back_populates="task", cascade="all, delete-orphan",
                            order_by="TaskComment.created_at")


class TaskComment(Base):
    """Комментарии в чате задачи (как «Чат задачи» в Битрикс24)."""
    __tablename__ = "task_comments"
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    text = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    task = relationship("Task", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])


class Activity(Base):
    """Дела по сделке (звонок / встреча / сообщение / напоминание) — как в Битрикс24."""
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False)
    type = Column(String, default="call")  # call / meeting / message / reminder
    title = Column(String)
    due_at = Column(String, nullable=True)  # YYYY-MM-DD or YYYY-MM-DDTHH:MM
    status = Column(String, default="planned")  # planned / done / canceled
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    result = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", back_populates="activities")
    assignee = relationship("User", foreign_keys=[assignee_id])


class Invoice(Base):
    """Счета для обмена с 1С и UI CRM."""
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String, unique=True, index=True)
    date = Column(String)
    company_bin = Column(String, index=True)
    company_name = Column(String, nullable=True)
    amount = Column(Float, default=0.0)
    status = Column(String, default="draft")  # draft / sent / paid / canceled / new / cancelled
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    external_id = Column(String, nullable=True)  # идентификатор документа в 1С
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="invoices")


class CrmNote(Base):
    """Комментарии/заметки в карточке компании или контакта (таймлайн как в Битрикс24)."""
    __tablename__ = "crm_notes"
    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String, index=True)  # company | contact
    entity_id = Column(Integer, index=True)
    text = Column(String)
    author = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class DealAttachment(Base):
    """Файлы и ссылки (Google Docs/Sheets) к событию/сделке в календаре."""
    __tablename__ = "deal_attachments"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    title = Column(String, nullable=True)
    kind = Column(String, default="link")  # link / file
    url = Column(String, nullable=True)   # внешняя ссылка или /uploads/...
    file_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="attachments")


class DealAdvance(Base):
    """Аванс сотруднику по проекту/сделке — вычитается из зарплаты."""
    __tablename__ = "deal_advances"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, default=0.0)
    date = Column(String, nullable=True)  # YYYY-MM-DD
    comment = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="advances")
    user = relationship("User", foreign_keys=[user_id])


class DealExpense(Base):
    """Расход компании внутри проекта (такси, закупка, довоз и т.п.)."""
    __tablename__ = "deal_expenses"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    category = Column(String, default="other")  # taxi / purchase / delivery / other
    amount = Column(Float, default=0.0)
    date = Column(String, nullable=True)  # YYYY-MM-DD
    description = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="expenses")


class DealStaffAssignment(Base):
    """Назначение сотрудника на проект: техничка + задача + напоминание."""
    __tablename__ = "deal_staff_assignments"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role_name = Column(String, nullable=True)
    note = Column(String, nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    attachment_id = Column(Integer, ForeignKey("deal_attachments.id"), nullable=True)
    notified_at = Column(DateTime, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="staff_assignments")
    user = relationship("User", foreign_keys=[user_id])


class DealPayrollLine(Base):
    """Строка зарплатной ведомости по сделке (из позиций «Персонал» сметы)."""
    __tablename__ = "deal_payroll_lines"
    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    equipment_id = Column(Integer, ForeignKey("equipment.id"), nullable=True)
    role_name = Column(String)  # название роли из сметы
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # кому платим
    quantity = Column(Integer, default=1)  # кол-во человек / единиц
    days = Column(Integer, default=1)  # смены / дни
    rate = Column(Float, default=0.0)  # ставка за смену
    gross = Column(Float, default=0.0)  # rate * quantity * days
    attendance = Column(String, default="pending")  # pending / present / absent / fine
    fine_amount = Column(Float, default=0.0)
    comment = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="payroll_lines")
    user = relationship("User", foreign_keys=[user_id])
    equipment = relationship("Equipment")


class InternalChat(Base):
    """Внутренний чат сотрудников (DM или тред по сделке) — отдельно от клиентского Inbox."""
    __tablename__ = "internal_chats"
    id = Column(Integer, primary_key=True, index=True)
    chat_type = Column(String, default="dm")  # dm / deal
    title = Column(String, nullable=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True, index=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", foreign_keys=[deal_id])
    members = relationship("InternalChatMember", back_populates="chat", cascade="all, delete-orphan")
    messages = relationship("InternalMessage", back_populates="chat", cascade="all, delete-orphan")


class InternalChatMember(Base):
    """Участник внутреннего чата + курсор прочитанного."""
    __tablename__ = "internal_chat_members"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("internal_chats.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    last_read_message_id = Column(Integer, default=0)
    joined_at = Column(DateTime, default=datetime.datetime.utcnow)

    chat = relationship("InternalChat", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class InternalMessage(Base):
    """Сообщение во внутреннем чате сотрудников."""
    __tablename__ = "internal_messages"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("internal_chats.id"), nullable=False, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(String)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    chat = relationship("InternalChat", back_populates="messages")
    sender = relationship("User", foreign_keys=[sender_id])
    task = relationship("Task", foreign_keys=[task_id])


def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
