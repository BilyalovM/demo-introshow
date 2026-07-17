from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, JSON, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime

import os
import shutil

# Vercel filesystem is read-only except for /tmp
if os.environ.get("VERCEL"):
    db_path = "/tmp/rental_app.db"
    if not os.path.exists(db_path):
        # Copy the bundled database to /tmp so it's writable
        original_db = os.path.join(os.path.dirname(__file__), "rental_app.db")
        if os.path.exists(original_db):
            shutil.copy(original_db, db_path)
    DATABASE_URL = f"sqlite:///{db_path}"
else:
    DATABASE_URL = "sqlite:////Users/maximbilyalov/Documents/КОС/rental_app/rental_app.db"


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user") # admin or user

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

    deals = relationship("Deal", back_populates="company")

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

    company = relationship("Company", back_populates="deals")
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

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
