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
    price = Column(Float, default=0.0) # Цена за единицу (KZT)
    stock_quantity = Column(Integer, default=0) # Общее количество на складе
    status = Column(String, default="Доступно") # Доступно / В ремонте
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
    setup_date = Column(String) # Дата монтажа
    event_date = Column(String) # Дата мероприятия
    event_address = Column(String) # Адрес площадки
    discount_percentage = Column(Float, default=0.0)
    final_sum = Column(Float, default=0.0)
    comment = Column(String)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    # Привязка к чату мессенджера (для автосозданных сделок из входящих сообщений)
    chat_channel = Column(String, nullable=True)   # whatsapp / telegram / instagram
    chat_id = Column(String, nullable=True)
    prev_deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)  # прошлое обращение клиента
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    company = relationship("Company", back_populates="deals")
    contact = relationship("Contact", foreign_keys=[contact_id])
    pipeline = relationship("Pipeline", back_populates="deals")
    stage_obj = relationship("Stage", back_populates="deals")
    items = relationship("DealItem", back_populates="deal")

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
    assignee = Column(String, nullable=True)    # ответственный (username)
    created_by = Column(String, nullable=True)  # постановщик
    due_date = Column(String, nullable=True)    # YYYY-MM-DD или YYYY-MM-DDTHH:MM
    priority = Column(String, default="normal") # low / normal / high
    status = Column(String, default="open")     # open / in_progress / done / deferred
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    deal = relationship("Deal", backref="tasks")


class Invoice(Base):
    """Счета для обмена с 1С."""
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    number = Column(String, unique=True, index=True)
    date = Column(String)
    company_bin = Column(String, index=True)
    company_name = Column(String, nullable=True)
    amount = Column(Float, default=0.0)
    status = Column(String, default="new")  # new / paid / cancelled
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=True)
    external_id = Column(String, nullable=True)  # идентификатор документа в 1С
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
